"""Shared pytest fixtures."""
from __future__ import annotations

import pytest


@pytest.fixture
def sample_inventory():
    from grocery_buddy.predictor import InventoryItem
    return [
        InventoryItem(product="Eggs", qty=3.0, unit="count", par_level=12.0),
        InventoryItem(product="Whole milk", qty=0.25, unit="gallon", par_level=0.5),
        InventoryItem(product="Oats", qty=2.0, unit="lbs", par_level=1.0),
        InventoryItem(product="Coffee beans", qty=0.1, unit="lbs", par_level=0.5),
    ]


@pytest.fixture
def sample_profiles():
    from grocery_buddy.predictor import ConsumptionProfile
    return [
        ConsumptionProfile(product="Eggs", declared_rate=1.0, unit="count"),   # 1/day
        ConsumptionProfile(product="Whole milk", declared_rate=0.14, unit="gallon"),  # ~1/week
        ConsumptionProfile(product="Oats", declared_rate=0.1, unit="lbs"),
        ConsumptionProfile(product="Coffee beans", declared_rate=0.05, unit="lbs"),
    ]
