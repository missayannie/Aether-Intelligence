"""Universalis market-board price client.

Universalis needs item IDs, so we resolve item names via XIVAPI search first,
then query current listings. Both are free, keyless public APIs.

Uses XIVAPI v2 (v2.xivapi.com) for name->id: the old xivapi.com (v1) search
cluster is down ("No alive nodes found"), which silently broke price lookups.
v2 returns the item's `row_id`, which IS the numeric item id Universalis expects.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from config import USER_AGENT
from sources import cache

UNIVERSALIS = "https://universalis.app/api/v2"
XIVAPI_V2 = "https://v2.xivapi.com/api"


@dataclass
class PriceResult:
    item_name: str
    item_id: int
    world_or_dc: str
    avg_price: float
    min_price: int
    listings: list[dict]  # [{price_per_unit, quantity, world_name}]
    source: str = "Universalis"


class UniversalisClient:
    def __init__(self, timeout: float = 15.0):
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def resolve_item(self, name: str) -> tuple[int, str] | None:
        """Name -> (item_id, canonical_name) via XIVAPI v2 search.

        Cached for a month: an item's id never changes. Note the PRICE lookup below
        is deliberately NOT cached — live listings are the whole point of the tool.
        """
        import json as _json

        query = name.replace('"', "").strip()

        def _load() -> str:
            r = self._client.get(
                f"{XIVAPI_V2}/search",
                params={
                    "sheets": "Item",
                    "query": f'Name~"{query}"',
                    "fields": "Name",
                    "limit": 5,
                },
            )
            r.raise_for_status()
            return r.text

        try:
            results = _json.loads(
                cache.fetch_text("itemid", f"name:{query.lower()}", cache.TTL_ITEM_ID, _load)
            ).get("results", [])
        except (ValueError, TypeError):
            return None
        if not results:
            return None
        # Prefer an exact (case-insensitive) name match; else the top hit.
        best = next(
            (x for x in results
             if (x.get("fields", {}).get("Name", "")).lower() == name.lower()),
            results[0],
        )
        return best["row_id"], best.get("fields", {}).get("Name", name)

    def get_price(self, name: str, world_or_dc: str = "Aether", listings: int = 5) -> PriceResult | None:
        """Current cheapest listings for an item on a world or data center."""
        resolved = self.resolve_item(name)
        if not resolved:
            return None
        item_id, canonical = resolved

        r = self._client.get(
            f"{UNIVERSALIS}/{world_or_dc}/{item_id}",
            params={"listings": listings, "entries": 0},
        )
        # Universalis 404s for items with no market data — untradable gear, currencies,
        # etc. That's a normal "no price" answer, not an error, so don't raise.
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        raw = data.get("listings", [])[:listings]
        return PriceResult(
            item_name=canonical,
            item_id=item_id,
            world_or_dc=world_or_dc,
            avg_price=data.get("currentAveragePrice", 0.0),
            min_price=data.get("minPrice", 0),
            listings=[
                {
                    "price_per_unit": lst.get("pricePerUnit"),
                    "quantity": lst.get("quantity"),
                    "world_name": lst.get("worldName", world_or_dc),
                }
                for lst in raw
            ],
        )
