"""Schedule management shared between the CLI and the Telegram chat handler.

Wraps Temporal schedule creation/update and DB persistence, plus a
human-readable next-run description using croniter.
"""
from __future__ import annotations

import logging
import uuid as uuid_mod
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from croniter import croniter

from grocery_buddy.config import settings

logger = logging.getLogger(__name__)

# Default timezone assumed when the user doesn't specify one.
DEFAULT_TZ = "America/New_York"


# ── Next-run helpers ──────────────────────────────────────────────────────────


def next_run_utc(cron: str) -> datetime:
    """Return the next UTC fire time for a cron expression."""
    now = datetime.now(UTC)
    it = croniter(cron, now)
    return it.get_next(datetime).replace(tzinfo=UTC)


def describe_next_run(cron: str, tz_name: str = DEFAULT_TZ) -> str:
    """Return a friendly string like 'tomorrow at 8:00 AM ET' for the next fire."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(DEFAULT_TZ)

    nxt = next_run_utc(cron).astimezone(tz)
    now_local = datetime.now(tz)

    # Day label
    delta_days = (nxt.date() - now_local.date()).days
    if delta_days == 0:
        day = "today"
    elif delta_days == 1:
        day = "tomorrow"
    elif delta_days < 7:
        day = nxt.strftime("%A")          # e.g. "Wednesday"
    else:
        day = nxt.strftime("%b %-d")      # e.g. "Jun 15"

    time_str = nxt.strftime("%-I:%M %p").lower()   # e.g. "8:00 am"
    tz_abbr = nxt.strftime("%Z")                    # e.g. "ET", "PDT"
    return f"{day} at {time_str} {tz_abbr}"


def describe_cadence(cron: str) -> str:
    """Convert a cron expression to a plain-English cadence label.

    Covers the most common cases; falls back to the raw cron string.
    """
    parts = cron.strip().split()
    if len(parts) != 5:
        return cron

    minute, hour, dom, month, dow = parts

    # Every N minutes  */5 * * * *
    if minute.startswith("*/") and hour == "*" and dom == "*" and month == "*" and dow == "*":
        n = minute[2:]
        return f"every {n} minutes"

    # Every N hours  0 */N * * *
    if minute == "0" and hour.startswith("*/") and dom == "*" and month == "*" and dow == "*":
        n = hour[2:]
        return f"every {n} hours"

    # Daily at fixed time  MM HH * * *
    if dom == "*" and month == "*" and dow == "*":
        try:
            h, m = int(hour), int(minute)
            dt = datetime(2000, 1, 1, h, m)
            return f"daily at {dt.strftime('%-I:%M %p').lower()}"
        except ValueError:
            pass

    return cron


# ── Upsert helpers ────────────────────────────────────────────────────────────


async def upsert_schedule(
    pool,
    user_id: str,
    cron: str,
    tz_name: str = DEFAULT_TZ,
) -> None:
    """Create or replace the user's Temporal schedule and persist it to the DB."""
    from temporalio.client import Client, Schedule, ScheduleActionStartWorkflow, ScheduleSpec

    from grocery_buddy.models import GroceryRunInput
    from grocery_buddy.workflows.grocery_run import GroceryRunWorkflow

    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
    schedule_id = f"grocery-daily-{user_id}"

    try:
        handle = client.get_schedule_handle(schedule_id)
        await handle.delete()
    except Exception:
        pass  # didn't exist yet

    await client.create_schedule(
        schedule_id,
        Schedule(
            action=ScheduleActionStartWorkflow(
                GroceryRunWorkflow.run,
                GroceryRunInput(user_id=user_id, trigger="schedule"),
                id=f"grocery-run-{user_id}-scheduled",
                task_queue=settings.temporal_task_queue,
            ),
            spec=ScheduleSpec(cron_expressions=[cron]),
        ),
    )
    logger.info("Schedule %s set to %r (tz=%s)", schedule_id, cron, tz_name)

    await pool.execute(
        """
        INSERT INTO schedules (user_id, cadence, timezone, enabled, temporal_schedule_id)
        VALUES ($1, $2, $3, TRUE, $4)
        ON CONFLICT (user_id) DO UPDATE
          SET cadence = EXCLUDED.cadence,
              timezone = EXCLUDED.timezone,
              temporal_schedule_id = EXCLUDED.temporal_schedule_id,
              enabled = TRUE,
              updated_at = NOW()
        """,
        uuid_mod.UUID(user_id), cron, tz_name, schedule_id,
    )


async def get_schedule(pool, user_id: str) -> dict | None:
    """Return the user's current schedule row, or None if not set."""
    row = await pool.fetchrow(
        "SELECT cadence, timezone, enabled FROM schedules WHERE user_id = $1",
        uuid_mod.UUID(user_id),
    )
    return dict(row) if row else None
