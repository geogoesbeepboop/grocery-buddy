"""Deterministic tests for the intent-parsing CODE path (model mocked).

These assert the tool_use → action-dict mapping in assistant.py without any network
call: we patch grocery_buddy.llm.create_message to return canned content blocks. This
is the cheap, every-commit safety net; prompt *quality* is covered by evals/ instead.
"""
import types

from grocery_buddy import llm
from grocery_buddy.agents import assistant


def _tool_use(name, inp):
    return types.SimpleNamespace(type="tool_use", name=name, input=inp)


def _text(t):
    return types.SimpleNamespace(type="text", text=t)


def _patch(monkeypatch, blocks):
    async def fake_create(**kwargs):
        return types.SimpleNamespace(content=blocks)

    monkeypatch.setattr(llm, "create_message", fake_create)


# ── parse_request (no pending cart) ───────────────────────────────────────────


async def test_request_purchase_maps_to_quick_buy(monkeypatch):
    _patch(monkeypatch, [_tool_use("request_purchase", {"items": [{"product": "milk"}], "reason": "out"})])
    res = await assistant.parse_request("we need milk")
    assert res["action"] == "quick_buy"
    assert res["items"][0]["product"] == "milk"
    assert res["items"][0]["qty"] == 1.0


async def test_restock_maps_to_grocery_run(monkeypatch):
    _patch(monkeypatch, [_tool_use("restock_low_items", {})])
    res = await assistant.parse_request("buy what I'm low on")
    assert res["action"] == "start_grocery_run"


async def test_update_pantry_maps_to_update_inventory(monkeypatch):
    _patch(monkeypatch, [_tool_use("update_pantry_quantity", {"items": [{"product": "eggs", "qty": 12}]})])
    res = await assistant.parse_request("we still have a dozen eggs")
    assert res["action"] == "update_inventory"
    assert res["items"][0]["qty"] == 12.0


async def test_not_arrived_maps_to_report_not_arrived(monkeypatch):
    _patch(monkeypatch, [_tool_use("report_not_arrived", {"items": [{"product": "milk"}]})])
    res = await assistant.parse_request("the milk never came")
    assert res["action"] == "report_not_arrived"
    assert res["items"] == ["milk"]


async def test_no_tool_falls_back_to_chat(monkeypatch):
    _patch(monkeypatch, [_text("Happy to help!")])
    res = await assistant.parse_request("thanks!")
    assert res["action"] == "chat"
    assert "Happy" in res["reply"]


# ── parse_briefing_reply (cart pending) ───────────────────────────────────────

_CART = [{"product": "milk", "qty": 1, "unit": "gallon", "price_usd": 3.99}]


async def test_approve_cart(monkeypatch):
    _patch(monkeypatch, [_tool_use("approve_cart", {})])
    res = await assistant.parse_briefing_reply("yes looks good", _CART)
    assert res["action"] == "approve"


async def test_buy_items_makes_new_cart(monkeypatch):
    _patch(monkeypatch, [_tool_use("buy_items", {"items": [{"product": "eggs"}]})])
    res = await assistant.parse_briefing_reply("just get the eggs", _CART)
    assert res["action"] == "buy_items"
    assert res["items"][0]["product"] == "eggs"


async def test_reject_cart(monkeypatch):
    _patch(monkeypatch, [_tool_use("reject_cart", {})])
    res = await assistant.parse_briefing_reply("no thanks", _CART)
    assert res["action"] == "reject"
