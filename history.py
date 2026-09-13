from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, asdict
from typing import Any
from urllib.parse import quote

import httpx

API_BASE_DEFAULT = "https://api.counterparty.io:4000/v2"
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


@dataclass(frozen=True)
class Event:
    kind: str
    block_index: int
    tx_hash: str
    source: str | None = None
    destination: str | None = None
    amount: str | None = None
    status: str | None = None
    raw_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _first(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _valid_status(row: dict[str, Any]) -> bool:
    status = str(row.get("status", "valid")).lower()
    return status not in {"invalid", "expired", "cancelled", "canceled", "dropped", "failed"}


async def _fetch_all(client: httpx.AsyncClient, api_base: str, path: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: Any = None
    for _ in range(25):
        params: dict[str, Any] = {"limit": 1000}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get(f"{api_base}{path}", params=params)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result", [])
        if isinstance(result, list):
            items.extend(x for x in result if isinstance(x, dict))
        elif isinstance(result, dict):
            items.append(result)
        cursor = payload.get("next_cursor")
        if cursor in (None, "", False):
            break
    return items


def _normalize(asset: str, data: dict[str, list[dict[str, Any]]]) -> list[Event]:
    events: list[Event] = []

    issuances = sorted(
        (r for r in data.get("issuances", []) if _valid_status(r)),
        key=lambda r: (_int(r.get("block_index")), _int(r.get("tx_index"))),
    )
    born = False
    for row in issuances:
        quantity = _int(_first(row, "quantity", default=0))
        qnorm = _first(row, "quantity_normalized", default="0")
        positive = quantity > 0
        if not positive:
            try:
                positive = float(str(qnorm)) > 0
            except ValueError:
                positive = False
        if not positive:
            continue
        kind = "BIRTH" if not born else "ISSUANCE"
        born = True
        events.append(Event(
            kind=kind,
            block_index=_int(row.get("block_index")),
            tx_hash=_str(_first(row, "tx_hash", "hash", default="")) or "",
            source=_str(_first(row, "source", "issuer")),
            amount=_str(_first(row, "quantity_normalized", "quantity")),
            status=_str(row.get("status")),
        ))

    for row in data.get("fairmints", []):
        if not _valid_status(row):
            continue
        events.append(Event(
            kind="FAIRMINT",
            block_index=_int(row.get("block_index")),
            tx_hash=_str(_first(row, "tx_hash", "hash", default="")) or "",
            source=_str(row.get("source")),
            amount=_str(_first(row, "earn_quantity_normalized", "earn_quantity", "quantity")),
            status=_str(row.get("status")),
        ))

    for row in data.get("sends", []):
        if not _valid_status(row):
            continue
        events.append(Event(
            kind="TRANSFER",
            block_index=_int(row.get("block_index")),
            tx_hash=_str(_first(row, "tx_hash", "hash", default="")) or "",
            source=_str(row.get("source")),
            destination=_str(row.get("destination")),
            amount=_str(_first(row, "quantity_normalized", "quantity")),
            status=_str(row.get("status")),
        ))

    for row in data.get("dispenses", []):
        if not _valid_status(row):
            continue
        row_asset = _str(row.get("asset"))
        if row_asset and row_asset != asset:
            continue
        events.append(Event(
            kind="BTC_SALE",
            block_index=_int(row.get("block_index")),
            tx_hash=_str(_first(row, "tx_hash", "hash", default="")) or "",
            source=_str(row.get("source")),
            destination=_str(row.get("destination")),
            amount=_str(_first(row, "btc_amount_normalized", "btc_amount")),
            status=_str(row.get("status")),
        ))

    for row in data.get("matches", []):
        if not _valid_status(row):
            continue
        forward = _str(_first(row, "forward_asset", "tx0_asset", "give_asset"))
        backward = _str(_first(row, "backward_asset", "tx1_asset", "get_asset"))
        pair = {forward, backward}
        if asset not in pair or "XCP" not in pair:
            continue
        raw_id = _str(_first(row, "id", "match_id"))
        tx_hash = _str(_first(row, "tx_hash", "tx0_hash", "hash", "id", default="")) or ""
        amount = None
        if forward == "XCP":
            amount = _str(_first(row, "forward_quantity_normalized", "forward_quantity"))
        elif backward == "XCP":
            amount = _str(_first(row, "backward_quantity_normalized", "backward_quantity"))
        events.append(Event(
            kind="XCP_SALE",
            block_index=_int(row.get("block_index")),
            tx_hash=tx_hash,
            source=_str(_first(row, "tx0_address", "source")),
            destination=_str(_first(row, "tx1_address", "destination")),
            amount=amount,
            status=_str(row.get("status")),
            raw_id=raw_id,
        ))

    for row in data.get("destructions", []):
        if not _valid_status(row):
            continue
        row_asset = _str(row.get("asset"))
        if row_asset and row_asset != asset:
            continue
        events.append(Event(
            kind="BURN",
            block_index=_int(row.get("block_index")),
            tx_hash=_str(_first(row, "tx_hash", "hash", default="")) or "",
            source=_str(row.get("source")),
            amount=_str(_first(row, "quantity_normalized", "quantity")),
            status=_str(row.get("status")),
        ))

    events.sort(key=lambda e: (e.block_index, e.tx_hash, e.raw_id or "", e.kind))

    seen: set[tuple[Any, ...]] = set()
    unique: list[Event] = []
    for e in events:
        key = (e.kind, e.block_index, e.tx_hash, e.source, e.destination, e.amount, e.raw_id)
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique


def _state(asset: str, events: list[Event], api_error: str | None = None) -> dict[str, Any]:
    counts: dict[str, int] = {}
    addresses: set[str] = set()
    for e in events:
        counts[e.kind] = counts.get(e.kind, 0) + 1
        if e.source:
            addresses.add(e.source)
        if e.destination:
            addresses.add(e.destination)
    born = any(e.kind in {"BIRTH", "FAIRMINT"} for e in events)
    burned = any(e.kind == "BURN" for e in events)
    return {
        "asset": asset,
        "born": born,
        "burned": burned,
        "event_count": len(events),
        "counts": counts,
        "observed_addresses": len(addresses),
        "last_block": max((e.block_index for e in events), default=None),
        "api_error": api_error,
        "events": [e.to_dict() for e in events],
    }


async def load_state(asset: str, *, force: bool = False, cache_seconds: int = 60, api_base: str = API_BASE_DEFAULT) -> dict[str, Any]:
    now = time.monotonic()
    cached = _CACHE.get(asset)
    if cached and not force and now - cached[0] < cache_seconds:
        return cached[1]

    encoded = quote(asset, safe="")
    paths = {
        "issuances": f"/assets/{encoded}/issuances",
        "fairmints": f"/assets/{encoded}/fairmints",
        "sends": f"/assets/{encoded}/sends",
        "dispenses": f"/assets/{encoded}/dispenses",
        "matches": f"/assets/{encoded}/matches",
        "destructions": f"/assets/{encoded}/destructions",
    }

    try:
        timeout = httpx.Timeout(8.0, connect=4.0)
        async with httpx.AsyncClient(timeout=timeout, headers={"accept": "application/json"}) as client:
            results = await asyncio.gather(*(_fetch_all(client, api_base, path) for path in paths.values()), return_exceptions=True)
        data: dict[str, list[dict[str, Any]]] = {}
        errors: list[str] = []
        for key, result in zip(paths.keys(), results):
            if isinstance(result, Exception):
                data[key] = []
                errors.append(f"{key}: {type(result).__name__}")
            else:
                data[key] = result
        events = _normalize(asset, data)
        state = _state(asset, events, api_error="; ".join(errors) if errors else None)
    except Exception as exc:
        state = _state(asset, [], api_error=f"{type(exc).__name__}: {exc}")

    _CACHE[asset] = (now, state)
    return state
