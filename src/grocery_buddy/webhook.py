"""FastAPI webhook server — receives approve/reject taps from ntfy and signals Temporal.

Run with: uv run uvicorn grocery_buddy.webhook:app --port 8080
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from temporalio.client import Client

from grocery_buddy.config import settings

logger = logging.getLogger(__name__)
app = FastAPI(title="grocery-buddy webhook")

_temporal_client: Client | None = None


async def _get_client() -> Client:
    global _temporal_client
    if _temporal_client is None:
        _temporal_client = await Client.connect(
            settings.temporal_host,
            namespace=settings.temporal_namespace,
        )
    return _temporal_client


@app.post("/approve/{workflow_id}")
async def approve(workflow_id: str) -> dict:
    try:
        client = await _get_client()
        handle = client.get_workflow_handle(workflow_id)
        await handle.signal("approve")
        logger.info("Approved workflow %s", workflow_id)
        return {"status": "approved", "workflow_id": workflow_id}
    except Exception as exc:
        logger.error("Failed to approve %s: %s", workflow_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/reject/{workflow_id}")
async def reject(workflow_id: str) -> dict:
    try:
        client = await _get_client()
        handle = client.get_workflow_handle(workflow_id)
        await handle.signal("reject")
        logger.info("Rejected workflow %s", workflow_id)
        return {"status": "rejected", "workflow_id": workflow_id}
    except Exception as exc:
        logger.error("Failed to reject %s: %s", workflow_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
