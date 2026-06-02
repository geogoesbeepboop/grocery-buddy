"""ntfy.sh push notifications with inline Approve / Reject action buttons."""
from __future__ import annotations

import httpx

from grocery_buddy.config import settings


async def send_approval_push(
    cart_id: str,
    total_usd: float,
    item_count: int,
    workflow_id: str,
) -> None:
    """Send a push notification asking the user to approve or reject the cart."""
    approve_url = f"{settings.webhook_base_url}/approve/{workflow_id}"
    reject_url = f"{settings.webhook_base_url}/reject/{workflow_id}"

    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"{settings.ntfy_url}/{settings.ntfy_topic}",
            json={
                "topic": settings.ntfy_topic,
                "title": f"Grocery cart ready — ${total_usd:.2f} ({item_count} items)",
                "message": f"Cart {cart_id[:8]} needs your approval. Tap to approve or reject.",
                "priority": 4,
                "tags": ["shopping_cart"],
                "actions": [
                    {
                        "action": "http",
                        "label": "✅ Approve",
                        "url": approve_url,
                        "method": "POST",
                        "clear": True,
                    },
                    {
                        "action": "http",
                        "label": "❌ Reject",
                        "url": reject_url,
                        "method": "POST",
                        "clear": True,
                    },
                ],
            },
        )


async def send_purchase_confirmation(
    cart_id: str,
    total_usd: float,
    order_ref: str | None,
) -> None:
    """Notify the user that a purchase was completed."""
    msg = f"Order placed — ${total_usd:.2f}"
    if order_ref:
        msg += f" (ref: {order_ref})"

    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            f"{settings.ntfy_url}/{settings.ntfy_topic}",
            json={
                "topic": settings.ntfy_topic,
                "title": "Grocery order placed ✅",
                "message": msg,
                "priority": 3,
                "tags": ["white_check_mark"],
            },
        )


async def send_error_notification(message: str) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            f"{settings.ntfy_url}/{settings.ntfy_topic}",
            json={
                "topic": settings.ntfy_topic,
                "title": "Grocery agent error ⚠️",
                "message": message,
                "priority": 4,
                "tags": ["warning"],
            },
        )
