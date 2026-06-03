"""CLI entry point: grocery-buddy <command>"""
from __future__ import annotations

import asyncio
import logging

import click


@click.group()
@click.option("--log-level", default="INFO", help="Log level (DEBUG, INFO, WARNING, ERROR)")
def main(log_level: str) -> None:
    """Grocery Buddy — 24/7 autonomous grocery agent."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


@main.command()
@click.option("--user-id", required=True, help="User UUID to onboard")
def onboard(user_id: str) -> None:
    """Run the conversational onboarding intake for a user."""
    from grocery_buddy.agents.onboarding import run_onboarding
    asyncio.run(run_onboarding(user_id))


@main.command()
def worker() -> None:
    """Start the Temporal worker (runs until SIGINT)."""
    from grocery_buddy.workflows.worker import run_worker
    asyncio.run(run_worker())


@main.command()
@click.option("--user-id", required=True, help="User UUID to run for")
def run(user_id: str) -> None:
    """Manually trigger one grocery run workflow."""
    asyncio.run(_run_workflow(user_id))


async def _run_workflow(user_id: str) -> None:
    from temporalio.client import Client
    from grocery_buddy.config import settings
    from grocery_buddy.models import GroceryRunInput
    from grocery_buddy.workflows.grocery_run import GroceryRunWorkflow
    import uuid

    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
    handle = await client.start_workflow(
        GroceryRunWorkflow.run,
        GroceryRunInput(user_id=user_id),
        id=f"grocery-run-{user_id}-{uuid.uuid4().hex[:8]}",
        task_queue=settings.temporal_task_queue,
    )
    click.echo(f"Started workflow: {handle.id}")
    result = await handle.result()
    click.echo(f"Result: {result}")


@main.command()
@click.option("--port", default=8080, help="Port for the webhook server")
def webhook(port: int) -> None:
    """Start the approval webhook server (receives ntfy Approve/Reject taps)."""
    import uvicorn
    from grocery_buddy.webhook import app
    uvicorn.run(app, host="0.0.0.0", port=port)


@main.command()
@click.option("--user-id", required=True, help="User UUID")
@click.option("--cron", default="0 8 * * *", help="Cron schedule (UTC)")
@click.option("--timezone", "tz", default="America/New_York")
def schedule(user_id: str, cron: str, tz: str) -> None:
    """Create (or update) a Temporal Schedule for daily grocery runs."""
    asyncio.run(_create_schedule(user_id, cron, tz))


async def _create_schedule(user_id: str, cron: str, tz: str) -> None:
    from temporalio.client import Client, Schedule, ScheduleActionStartWorkflow, ScheduleSpec
    from temporalio.client import ScheduleCalendarSpec
    from grocery_buddy.config import settings
    from grocery_buddy.models import GroceryRunInput
    from grocery_buddy.workflows.grocery_run import GroceryRunWorkflow

    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)

    schedule_id = f"grocery-daily-{user_id}"
    try:
        handle = client.get_schedule_handle(schedule_id)
        await handle.delete()
        click.echo(f"Replaced existing schedule {schedule_id}")
    except Exception:
        pass

    await client.create_schedule(
        schedule_id,
        Schedule(
            action=ScheduleActionStartWorkflow(
                GroceryRunWorkflow.run,
                GroceryRunInput(user_id=user_id),
                id=f"grocery-run-{user_id}-scheduled",
                task_queue=settings.temporal_task_queue,
            ),
            spec=ScheduleSpec(cron_expressions=[cron]),
        ),
    )
    click.echo(f"Schedule created: {schedule_id} ({cron} UTC, display timezone: {tz})")

    # Persist to DB
    from grocery_buddy.db import get_pool
    import uuid as uuid_mod
    pool = await get_pool()
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
        uuid_mod.UUID(user_id), cron, tz, schedule_id,
    )


@main.command()
def mcp() -> None:
    """Start the MCP server (for local Claude Code tool use)."""
    from grocery_buddy.mcp_server import main as mcp_main
    mcp_main()


@main.command()
@click.option("--user-id", required=True, help="User UUID to evaluate")
def evals(user_id: str) -> None:
    """Run prediction accuracy evals and emit scores to Langfuse."""
    asyncio.run(_run_evals(user_id))


async def _run_evals(user_id: str) -> None:
    from grocery_buddy.evals import run_evals
    results = await run_evals(user_id)
    click.echo("\n── Eval results ──────────────────────────────")
    for key, val in results.items():
        click.echo(f"  {key}: {val}")
    click.echo()
