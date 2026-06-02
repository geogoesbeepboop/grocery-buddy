"""Shared data models (Pydantic) used across activities, agents, and tools."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import UUID


# ── DB row mirrors ────────────────────────────────────────────────────────────


@dataclass
class InventoryItem:
    id: UUID
    user_id: UUID
    product: str
    qty: float
    unit: str
    par_level: float
    updated_at: datetime


@dataclass
class ConsumptionProfile:
    id: UUID
    user_id: UUID
    product: str
    declared_rate: float  # units per day
    unit: str
    household_factor: float
    notes: str | None
    updated_at: datetime


@dataclass
class ConsumptionEvent:
    id: UUID
    user_id: UUID
    product: str
    delta: float
    source: str
    ts: datetime


@dataclass
class Cart:
    id: UUID
    user_id: UUID
    status: str
    total_usd: Decimal | None
    retailer: str
    workflow_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass
class CartItem:
    id: UUID
    cart_id: UUID
    product: str
    qty: float
    unit: str
    price_usd: Decimal | None
    price_source: str | None
    asin: str | None
    kroger_sku: str | None
    notes: str | None


@dataclass
class UserPreferences:
    user_id: UUID
    default_store: str
    auto_purchase_cap_usd: float
    lead_time_days: float
    buffer_days: float


# ── Temporal workflow I/O (must be JSON-serializable) ─────────────────────────


@dataclass
class GroceryRunInput:
    user_id: str


@dataclass
class PricedItem:
    product: str
    qty: float
    unit: str
    price_usd: float
    price_source: str
    asin: str | None = None
    kroger_sku: str | None = None
    notes: str | None = None


@dataclass
class DraftCart:
    cart_id: str
    total_usd: float
    item_count: int
    retailer: str


@dataclass
class GroceryRunResult:
    status: str  # no_items_needed | purchased | approved | rejected | expired | failed
    cart_id: str | None = None
    message: str | None = None


@dataclass
class LookupInput:
    user_id: str
    items: list[LowItem] = field(default_factory=list)


@dataclass
class LowItem:
    product: str
    qty: float
    unit: str
    days_remaining: float
    par_level: float


@dataclass
class BuildCartInput:
    user_id: str
    priced_items: list[PricedItem] = field(default_factory=list)
    workflow_id: str = ""


@dataclass
class NotificationInput:
    cart_id: str
    total_usd: float
    item_count: int
    workflow_id: str


@dataclass
class PurchaseInput:
    cart_id: str
    user_id: str
    idempotency_key: str


@dataclass
class UpdateCartInput:
    cart_id: str
    status: str
