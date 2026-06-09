"""Network-level helpers for the Amazon automation — interception over scraping.

Two tools live here, both aimed at the same failure mode that DOM scraping has:
Amazon renames a CSS class and a rendered-card walk silently returns nothing.

  1. ``block_heavy_resources`` — a ``page.route`` filter that aborts images, media,
     fonts and a short list of pure ad/telemetry beacons. This directly addresses
     the documented "Amazon pages never go idle" problem (the search/orders pages
     keep fetching ads + telemetry forever), making loads faster and more
     deterministic for the read paths. Applied ONLY to read paths (search, order
     scrape) — never to the sign-in or add-to-cart flows, where a blocked captcha
     image or suppressed telemetry could break auth or look bot-like on the money
     path.

  2. ``confirm_add_to_cart`` — wraps the add-to-cart click in ``expect_response`` and
     treats Amazon's own cart-mutation network call (which returns JSON) as the
     success signal. That response fires whether or not the visual confirmation
     overlay's id changed, so it is far more stable than waiting on one of seven
     overlay CSS ids.

  3. ``JsonResponseCollector`` — opportunistic capture of JSON XHR bodies matching
     URL substrings. Amazon's search/orders pages are server-rendered HTML today
     (no JSON to mine), so this is a hedge + diagnostic: if/when those pages move to
     client-rendered JSON, the capture path is already here, and meanwhile it records
     whether any JSON endpoint fired (a health signal) without us depending on it.

All helpers are best-effort and never raise into the caller.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from playwright.async_api import Page, Response, Route

logger = logging.getLogger(__name__)

# Resource types that carry no data we read and only slow the page down / keep it
# from ever going idle. Stylesheets are intentionally kept — visibility checks
# (``is_visible``) depend on layout.
_BLOCK_RESOURCE_TYPES = frozenset({"image", "media", "font"})

# Pure ad / telemetry beacon hosts + paths. Kept deliberately short and limited to
# clearly-non-functional traffic: we do NOT block fraud/anti-bot endpoints
# (fwcim, etc.), since suppressing those is exactly what makes a session look like a
# bot. Matched as case-insensitive substrings of the request URL.
_BLOCK_URL_SUBSTRINGS = (
    "amazon-adsystem.com",   # ad serving
    "/aax2/",                # ad exchange
    "fls-na.amazon.com",     # pixel/beacon telemetry
    "/1/batch/1/OE/",        # CSM client-side-metrics beacons
    "/1/action-impressions/",
)


async def block_heavy_resources(page: Page) -> None:
    """Route ``page`` so images/media/fonts and ad/telemetry beacons are aborted.

    Best-effort: if registering the route fails the page still works, just slower.
    Apply to read-only pages (search, order scrape) only.
    """
    async def _handler(route: Route) -> None:
        try:
            req = route.request
            url = (req.url or "").lower()
            if req.resource_type in _BLOCK_RESOURCE_TYPES or any(
                s in url for s in _BLOCK_URL_SUBSTRINGS
            ):
                await route.abort()
                return
            await route.continue_()
        except Exception:
            # Never let a routing decision wedge the page — fall through to the net.
            try:
                await route.continue_()
            except Exception:
                pass

    try:
        await page.route("**/*", _handler)
    except Exception as exc:
        logger.debug("Resource blocking not applied (%s)", exc)


# Substrings that identify Amazon's add-to-cart / cart-mutation network calls. The
# exact endpoint differs across the classic, smart-wagon ("huc") and side-sheet
# flows, so we match any of them. A 2xx response to one of these after the click is
# a reliable "the item went in the cart" signal independent of the visual overlay.
_ATC_RESPONSE_MARKERS = (
    "add-to-cart",
    "/gp/add-to-cart",
    "huc/v2",
    "smart-wagon",
    "/hz/cart",
    "/cart/ajax",
    "/cart/add",
)


def _looks_like_cart_mutation(response: Response) -> bool:
    try:
        if response.status >= 400:
            return False
        url = (response.url or "").lower()
        return any(m in url for m in _ATC_RESPONSE_MARKERS)
    except Exception:
        return False


async def confirm_add_to_cart(
    page: Page,
    do_click: Callable[[], Awaitable[None]],
    *,
    timeout_ms: int = 8_000,
) -> bool | None:
    """Click add-to-cart and treat Amazon's cart-mutation response as confirmation.

    Runs ``do_click`` inside an ``expect_response`` window watching for a 2xx call to
    one of Amazon's cart endpoints. Returns:
      * ``True``  — a cart-mutation response was observed (item added).
      * ``None``  — inconclusive: either no matching response arrived within the
                    timeout, or the click itself failed. The caller should fall back
                    to the DOM signals (overlay / cart-count); if the add truly failed
                    those will also come up empty and the caller reports failure. We
                    never raise — a single item shouldn't sink a whole checkout.
    """
    try:
        async with page.expect_response(_looks_like_cart_mutation, timeout=timeout_ms):
            await do_click()
        return True
    except Exception as exc:
        # If the click itself failed, surface it; otherwise this is just "no cart
        # response seen in time" → inconclusive, let the caller fall back.
        if "expect_response" not in str(exc) and "Timeout" not in str(exc):
            logger.debug("confirm_add_to_cart: click path raised (%s)", exc)
        return None


class JsonResponseCollector:
    """Opportunistically capture JSON XHR bodies whose URL matches given substrings.

    Attach BEFORE navigating. Reads bodies on a background task so the sync event
    handler never blocks; call ``await drain()`` after the page settles to let those
    reads finish before inspecting ``payloads()``. Entirely best-effort — a body that
    can't be read (already consumed, navigated away) is simply dropped.
    """

    def __init__(self, url_substrings: tuple[str, ...]):
        self._subs = tuple(s.lower() for s in url_substrings)
        self._tasks: list[asyncio.Future] = []
        self._records: list[dict[str, Any]] = []

    def attach(self, page: Page) -> None:
        page.on("response", self._on_response)

    def _on_response(self, response: Response) -> None:
        try:
            url = (response.url or "").lower()
            if not any(s in url for s in self._subs):
                return
            ctype = ((response.headers or {}).get("content-type") or "").lower()
            if "json" not in ctype:
                self._records.append({"url": response.url, "status": response.status, "json": None})
                return
            self._tasks.append(asyncio.ensure_future(self._read(response)))
        except Exception:
            pass

    async def _read(self, response: Response) -> None:
        record: dict[str, Any] = {"url": response.url, "status": response.status, "json": None}
        try:
            record["json"] = await response.json()
        except Exception:
            pass
        self._records.append(record)

    async def drain(self, timeout: float = 2.0) -> None:
        if not self._tasks:
            return
        try:
            await asyncio.wait(self._tasks, timeout=timeout)
        except Exception:
            pass

    @property
    def saw_any(self) -> bool:
        """True if any matching response (JSON or not) was seen — a health signal."""
        return bool(self._records)

    def payloads(self) -> list[Any]:
        """Parsed JSON bodies that were successfully read."""
        return [r["json"] for r in self._records if r.get("json") is not None]
