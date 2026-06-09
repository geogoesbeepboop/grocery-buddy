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
    import uuid

    from temporalio.client import Client

    from grocery_buddy.config import settings
    from grocery_buddy.models import GroceryRunInput
    from grocery_buddy.workflows.grocery_run import GroceryRunWorkflow

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
@click.option("--user-id", required=True, help="User UUID making the request")
@click.argument("message", nargs=-1, required=True)
def ask(user_id: str, message: tuple[str, ...]) -> None:
    """Talk to the agent: 'grocery-buddy ask --user-id <uuid> I need eggs early'.

    Parses the request; if it's a purchase, kicks off an approval-gated QuickBuy
    workflow (you'll get an ntfy push to approve before anything is bought).
    """
    asyncio.run(_ask(user_id, " ".join(message)))


async def _ask(user_id: str, message: str) -> None:
    import uuid

    from temporalio.client import Client

    from grocery_buddy.agents.assistant import parse_request
    from grocery_buddy.config import settings
    from grocery_buddy.models import QuickBuyInput, QuickBuyItem
    from grocery_buddy.workflows.quick_buy import QuickBuyWorkflow

    intent = await parse_request(message)

    if intent["action"] == "update_inventory":
        from grocery_buddy.db import get_pool
        from grocery_buddy.tools.inventory import set_actual_quantity

        pool = await get_pool()
        for it in intent.get("items", []):
            row = await set_actual_quantity(
                pool, user_id, it["product"], float(it["qty"]), it.get("unit")
            )
            click.echo(f"Updated {row['product']} → {float(row['qty']):g} {row.get('unit', '')}".rstrip())
        return

    if intent["action"] == "start_grocery_run":
        from grocery_buddy.models import GroceryRunInput
        from grocery_buddy.workflows.grocery_run import GroceryRunWorkflow

        click.echo("Checking what you're running low on and pricing it out…")
        client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
        handle = await client.start_workflow(
            GroceryRunWorkflow.run,
            GroceryRunInput(user_id=user_id, trigger="manual"),
            id=f"grocery-run-{user_id}-{uuid.uuid4().hex[:8]}",
            task_queue=settings.temporal_task_queue,
        )
        click.echo(f"Started workflow: {handle.id}")
        return

    if intent["action"] != "quick_buy":
        click.echo(intent.get("reply", "Got it!"))
        return

    items = [QuickBuyItem(product=i["product"], qty=i["qty"], unit=i["unit"]) for i in intent["items"]]
    summary = ", ".join(f"{i.qty:g} {i.product}" for i in items)
    click.echo(f"Got it — requesting: {summary}")

    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
    handle = await client.start_workflow(
        QuickBuyWorkflow.run,
        QuickBuyInput(user_id=user_id, items=items, reason=intent.get("reason", "")),
        id=f"quick-buy-{user_id}-{uuid.uuid4().hex[:8]}",
        task_queue=settings.temporal_task_queue,
    )
    click.echo(f"Started workflow: {handle.id}")
    click.echo("You'll get a push notification to approve before anything is purchased.")


@main.command()
@click.option("--port", default=8080, help="Port for the webhook server")
def webhook(port: int) -> None:
    """Start the webhook server (receives Telegram messages and button callbacks)."""
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
    from temporalio.client import (
        Client,
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleSpec,
    )

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
    import uuid as uuid_mod

    from grocery_buddy.db import get_pool
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


@main.command(name="scraper-health")
def scraper_health() -> None:
    """Probe Amazon extraction (price + ASIN) and alert on regression."""
    asyncio.run(_scraper_health())


async def _scraper_health() -> None:
    from grocery_buddy.monitoring import check_scraper_health
    res = await check_scraper_health()
    click.echo(f"\nScraper health: {res['status'].upper()}")
    for c in res["checked"]:
        mark = "✅" if c["ok"] else "❌"
        click.echo(f"  {mark} {c['query']}: {c['candidates']} candidates")
    if res["detail"]:
        click.echo(f"  detail: {res['detail']}")


@main.command()
@click.option("--user-id", required=True, help="User UUID")
def gate(user_id: str) -> None:
    """Show the money-live readiness gate (every condition must pass to go live)."""
    asyncio.run(_gate(user_id))


async def _gate(user_id: str) -> None:
    from grocery_buddy.gating import money_live_ready
    res = await money_live_ready(user_id)
    click.echo(f"\nmoney_live_ready: {res['ready']}")
    for name, cond in res["conditions"].items():
        mark = "✅" if cond["pass"] else "❌"
        click.echo(f"  {mark} {name}: {cond['detail']}")
