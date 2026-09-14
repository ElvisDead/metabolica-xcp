from __future__ import annotations

import asyncio
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Iterable
from urllib.parse import quote

import httpx


API_BASE = os.getenv(
    "COUNTERPARTY_API_BASE",
    "https://api.counterparty.io:4000/v2",
).rstrip("/")

CACHE_SECONDS = max(
    5,
    int(os.getenv("CACHE_SECONDS", "60"))
)


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


_cache: dict[str, tuple[float, dict[str, Any]]] = {}


# ---------------------------------------------------------
# BASIC HELPERS
# ---------------------------------------------------------

def _first(
    row: dict[str, Any],
    *keys: str,
    default: Any = None,
) -> Any:

    for key in keys:
        if key in row and row[key] is not None:
            return row[key]

    return default


def _int(
    value: Any,
    default: int = 0,
) -> int:

    try:
        return int(value)

    except (TypeError, ValueError):
        return default


def _str(
    value: Any,
) -> str | None:

    if value is None:
        return None

    return str(value)


def _valid_status(
    row: dict[str, Any],
) -> bool:

    status = str(
        row.get("status", "valid")
    ).lower()

    return status not in {
        "invalid",
        "expired",
        "cancelled",
        "canceled",
        "dropped",
        "failed",
    }


def _flatten_row(
    row: dict[str, Any],
) -> dict[str, Any]:
    """
    Counterparty API normally returns direct rows.

    Some event-style results may instead look like:

        {
            "event": "...",
            "params": {...},
            "block_index": ...
        }

    This makes both formats usable.
    """

    params = row.get("params")

    if not isinstance(params, dict):
        return row

    merged = dict(params)

    for key in (
        "tx_hash",
        "block_index",
        "block_time",
        "event_index",
        "event",
    ):

        if (
            key not in merged
            and row.get(key) is not None
        ):
            merged[key] = row[key]

    return merged


# ---------------------------------------------------------
# API FETCHING
# ---------------------------------------------------------

async def _fetch_all(
    client: httpx.AsyncClient,
    path: str,
) -> list[dict[str, Any]]:
    """
    Read all pages from one Counterparty API route.
    """

    items: list[dict[str, Any]] = []

    cursor: Any = None

    # Safety limit:
    # 25 × 1000 records is vastly more than we need.
    for _ in range(25):

        params: dict[str, Any] = {
            "limit": 1000
        }

        if cursor is not None:
            params["cursor"] = cursor

        response = await client.get(
            f"{API_BASE}{path}",
            params=params,
        )

        if response.status_code == 404:
            return []

        response.raise_for_status()

        payload = response.json()

        result = payload.get(
            "result",
            [],
        )

        if isinstance(result, list):

            for item in result:

                if isinstance(item, dict):
                    items.append(
                        _flatten_row(item)
                    )

        elif isinstance(result, dict):

            items.append(
                _flatten_row(result)
            )

        cursor = payload.get(
            "next_cursor"
        )

        if cursor in (
            None,
            "",
            False,
        ):
            break

    return items


# ---------------------------------------------------------
# SUBASSET RESOLUTION
# ---------------------------------------------------------

async def _resolve_asset(
    client: httpx.AsyncClient,
    asset_name: str,
) -> tuple[str, str | None]:
    """
    Resolve:

        METABOLICA.0001

    into:

        A8133710759795923228

    while preserving asset_longname.

    Returns:

        (
            numeric_asset,
            asset_longname
        )
    """

    encoded = quote(
        asset_name,
        safe="",
    )

    response = await client.get(
        f"{API_BASE}/assets/{encoded}"
    )

    if response.status_code == 404:

        return (
            asset_name,
            None,
        )

    response.raise_for_status()

    payload = response.json()

    info = payload.get(
        "result",
        {},
    )

    if not isinstance(info, dict):

        return (
            asset_name,
            None,
        )

    numeric_asset = (
        _str(info.get("asset"))
        or asset_name
    )

    asset_longname = _str(
        info.get("asset_longname")
    )

    return (
        numeric_asset,
        asset_longname,
    )


# ---------------------------------------------------------
# RESULT MERGING
# ---------------------------------------------------------

def _merge_rows(
    *collections: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Merge results obtained through numeric asset
    and longname routes, removing duplicate rows.
    """

    result: list[dict[str, Any]] = []

    seen: set[
        tuple[Any, ...]
    ] = set()

    for collection in collections:

        for row in collection:

            key = (
                _str(
                    _first(
                        row,
                        "tx_hash",
                        "hash",
                    )
                ),

                _int(
                    row.get(
                        "block_index"
                    )
                ),

                _str(
                    _first(
                        row,
                        "id",
                        "match_id",
                        "fairminter_tx_hash",
                    )
                ),

                _str(
                    row.get("source")
                ),

                _str(
                    row.get("asset")
                ),

                _str(
                    _first(
                        row,
                        "earn_quantity",
                        "quantity",
                        "btc_amount",
                    )
                ),
            )

            if key in seen:
                continue

            seen.add(key)

            result.append(row)

    return result


# ---------------------------------------------------------
# ASSET MATCHING
# ---------------------------------------------------------

def _matches_asset(
    value: Any,
    aliases: set[str],
) -> bool:

    if value is None:
        return False

    return str(value) in aliases


# ---------------------------------------------------------
# NORMALIZATION
# ---------------------------------------------------------

def _normalize(
    display_asset: str,
    numeric_asset: str,
    data: dict[
        str,
        list[dict[str, Any]]
    ],
) -> list[Event]:

    aliases = {
        display_asset,
        numeric_asset,
    }

    events: list[Event] = []


    # =====================================================
    # 1. FAIRMINTS
    # =====================================================
    #
    # IMPORTANT:
    #
    # Counterparty Fairmint can also produce an
    # ASSET_ISSUANCE record.
    #
    # Therefore we collect Fairmint tx hashes FIRST,
    # then remove the corresponding issuance duplicates.
    #

    fairmint_tx_hashes: set[str] = set()

    for row in data.get(
        "fairmints",
        [],
    ):

        if not _valid_status(row):
            continue

        row_asset = _str(
            row.get("asset")
        )

        if (
            row_asset
            and row_asset not in aliases
        ):
            continue

        tx_hash = (
            _str(
                _first(
                    row,
                    "tx_hash",
                    "hash",
                    default="",
                )
            )
            or ""
        )

        if tx_hash:
            fairmint_tx_hashes.add(
                tx_hash
            )

        amount = _str(
            _first(
                row,

                "earn_quantity_normalized",
                "earn_quantity",

                "quantity_normalized",
                "quantity",
            )
        )

        events.append(
            Event(
                kind="FAIRMINT",

                block_index=_int(
                    row.get(
                        "block_index"
                    )
                ),

                tx_hash=tx_hash,

                source=_str(
                    row.get("source")
                ),

                amount=amount,

                status=_str(
                    row.get("status")
                ),

                raw_id=_str(
                    _first(
                        row,
                        "id",
                        "event_index",
                    )
                ),
            )
        )


    # =====================================================
    # 2. ORDINARY ISSUANCES
    # =====================================================

    issuances = sorted(

        (
            row
            for row in data.get(
                "issuances",
                [],
            )
            if _valid_status(row)
        ),

        key=lambda row: (
            _int(
                row.get(
                    "block_index"
                )
            ),

            _int(
                row.get(
                    "tx_index"
                )
            ),
        ),
    )

    ordinary_positive_issuance_seen = False

    for row in issuances:

        tx_hash = (
            _str(
                _first(
                    row,
                    "tx_hash",
                    "hash",
                    default="",
                )
            )
            or ""
        )


        # -----------------------------------------
        # CRITICAL FIX
        # -----------------------------------------
        #
        # If this issuance came from a Fairmint,
        # ignore it.
        #
        # Otherwise one mint becomes:
        #
        # FAIRMINT
        # +
        # ISSUANCE
        #
        # which is wrong.
        #

        if (
            tx_hash
            and tx_hash
            in fairmint_tx_hashes
        ):
            continue


        quantity = _int(
            _first(
                row,
                "quantity",
                default=0,
            )
        )

        qnorm = str(
            row.get(
                "quantity_normalized",
                "0",
            )
        )

        positive = quantity > 0

        if not positive:

            try:

                positive = (
                    float(qnorm) > 0
                )

            except (
                TypeError,
                ValueError,
            ):

                positive = False


        # -----------------------------------------
        # Ignore registration-only issuance.
        #
        # Our original METABOLICA.0001 issuance
        # had supply = 0.
        #
        # It should not create a pixel layer.
        # -----------------------------------------

        if not positive:
            continue


        if (
            not ordinary_positive_issuance_seen
            and not fairmint_tx_hashes
        ):

            kind = "BIRTH"

        else:

            kind = "ISSUANCE"


        ordinary_positive_issuance_seen = True


        events.append(
            Event(
                kind=kind,

                block_index=_int(
                    row.get(
                        "block_index"
                    )
                ),

                tx_hash=tx_hash,

                source=_str(
                    _first(
                        row,
                        "source",
                        "issuer",
                    )
                ),

                amount=_str(
                    _first(
                        row,

                        "quantity_normalized",
                        "quantity",
                    )
                ),

                status=_str(
                    row.get(
                        "status"
                    )
                ),
            )
        )


    # =====================================================
    # 3. NORMAL SEND / TRANSFER
    # =====================================================

    for row in data.get(
        "sends",
        [],
    ):

        if not _valid_status(row):
            continue

        row_asset = _str(
            row.get("asset")
        )

        if (
            row_asset
            and row_asset not in aliases
        ):
            continue


        events.append(
            Event(
                kind="TRANSFER",

                block_index=_int(
                    row.get(
                        "block_index"
                    )
                ),

                tx_hash=(
                    _str(
                        _first(
                            row,
                            "tx_hash",
                            "hash",
                            default="",
                        )
                    )
                    or ""
                ),

                source=_str(
                    row.get(
                        "source"
                    )
                ),

                destination=_str(
                    row.get(
                        "destination"
                    )
                ),

                amount=_str(
                    _first(
                        row,

                        "quantity_normalized",
                        "quantity",
                    )
                ),

                status=_str(
                    row.get(
                        "status"
                    )
                ),
            )
        )


    # =====================================================
    # 4. BTC DISPENSER SALES
    # =====================================================

    for row in data.get(
        "dispenses",
        [],
    ):

        if not _valid_status(row):
            continue

        row_asset = _str(
            row.get("asset")
        )

        if (
            row_asset
            and row_asset not in aliases
        ):
            continue


        events.append(
            Event(
                kind="BTC_SALE",

                block_index=_int(
                    row.get(
                        "block_index"
                    )
                ),

                tx_hash=(
                    _str(
                        _first(
                            row,
                            "tx_hash",
                            "hash",
                            default="",
                        )
                    )
                    or ""
                ),

                source=_str(
                    row.get(
                        "source"
                    )
                ),

                destination=_str(
                    row.get(
                        "destination"
                    )
                ),

                amount=_str(
                    _first(
                        row,

                        "btc_amount_normalized",
                        "btc_amount",
                    )
                ),

                status=_str(
                    row.get(
                        "status"
                    )
                ),

                raw_id=_str(
                    _first(
                        row,

                        "dispense_index",
                        "event_index",
                    )
                ),
            )
        )


    # =====================================================
    # 5. XCP DEX SALES
    # =====================================================

    for row in data.get(
        "matches",
        [],
    ):

        if not _valid_status(row):
            continue


        forward = _str(
            _first(
                row,

                "forward_asset",
                "tx0_asset",
                "give_asset",
            )
        )


        backward = _str(
            _first(
                row,

                "backward_asset",
                "tx1_asset",
                "get_asset",
            )
        )


        has_our_asset = (
            forward in aliases
            or backward in aliases
        )


        has_xcp = (
            forward == "XCP"
            or backward == "XCP"
        )


        if not (
            has_our_asset
            and has_xcp
        ):
            continue


        raw_id = _str(
            _first(
                row,

                "id",
                "match_id",
            )
        )


        tx_hash = (
            _str(
                _first(
                    row,

                    "tx_hash",
                    "id",
                    "tx0_hash",
                    "tx1_hash",

                    default="",
                )
            )
            or ""
        )


        amount = None


        if forward == "XCP":

            amount = _str(
                _first(
                    row,

                    "forward_quantity_normalized",
                    "forward_quantity",
                )
            )


        elif backward == "XCP":

            amount = _str(
                _first(
                    row,

                    "backward_quantity_normalized",
                    "backward_quantity",
                )
            )


        events.append(
            Event(
                kind="XCP_SALE",

                block_index=_int(
                    row.get(
                        "block_index"
                    )
                ),

                tx_hash=tx_hash,

                source=_str(
                    _first(
                        row,

                        "tx0_address",
                        "source",
                    )
                ),

                destination=_str(
                    _first(
                        row,

                        "tx1_address",
                        "destination",
                    )
                ),

                amount=amount,

                status=_str(
                    row.get(
                        "status"
                    )
                ),

                raw_id=raw_id,
            )
        )


    # =====================================================
    # 6. BURN / DESTRUCTION
    # =====================================================

    for row in data.get(
        "destructions",
        [],
    ):

        if not _valid_status(row):
            continue


        row_asset = _str(
            row.get("asset")
        )


        if (
            row_asset
            and row_asset not in aliases
        ):
            continue


        events.append(
            Event(
                kind="BURN",

                block_index=_int(
                    row.get(
                        "block_index"
                    )
                ),

                tx_hash=(
                    _str(
                        _first(
                            row,

                            "tx_hash",
                            "hash",

                            default="",
                        )
                    )
                    or ""
                ),

                source=_str(
                    row.get(
                        "source"
                    )
                ),

                amount=_str(
                    _first(
                        row,

                        "quantity_normalized",
                        "quantity",
                    )
                ),

                status=_str(
                    row.get(
                        "status"
                    )
                ),
            )
        )


    # =====================================================
    # DETERMINISTIC EVENT ORDER
    # =====================================================

    kind_order = {

        "FAIRMINT": 0,

        "BIRTH": 1,

        "ISSUANCE": 2,

        "TRANSFER": 3,

        "BTC_SALE": 4,

        "XCP_SALE": 5,

        "BURN": 6,
    }


    events.sort(
        key=lambda event: (

            event.block_index,

            event.tx_hash,

            kind_order.get(
                event.kind,
                99,
            ),

            event.raw_id or "",
        )
    )


    # =====================================================
    # EXACT DEDUPLICATION
    # =====================================================

    seen: set[
        tuple[Any, ...]
    ] = set()

    unique: list[Event] = []


    for event in events:

        key = (

            event.kind,

            event.block_index,

            event.tx_hash,

            event.source,

            event.destination,

            event.amount,

            event.raw_id,
        )


        if key in seen:
            continue


        seen.add(key)

        unique.append(event)


    return unique


# ---------------------------------------------------------
# STATE BUILDING
# ---------------------------------------------------------

def _state(
    display_asset: str,
    numeric_asset: str,
    events: list[Event],
    api_error: str | None = None,
) -> dict[str, Any]:

    counts: dict[str, int] = {}

    addresses: set[str] = set()


    for event in events:

        counts[event.kind] = (
            counts.get(
                event.kind,
                0,
            )
            + 1
        )


        if event.source:
            addresses.add(
                event.source
            )


        if event.destination:
            addresses.add(
                event.destination
            )


    born = any(
        event.kind
        in {
            "BIRTH",
            "FAIRMINT",
            "ISSUANCE",
        }
        for event in events
    )


    burned = any(
        event.kind == "BURN"
        for event in events
    )


    return {

        "asset":
            display_asset,

        "asset_id":
            numeric_asset,

        "born":
            born,

        "burned":
            burned,

        "event_count":
            len(events),

        "counts":
            counts,

        "observed_addresses":
            len(addresses),

        "last_block":
            max(
                (
                    event.block_index
                    for event in events
                ),
                default=None,
            ),

        "api_error":
            api_error,

        "events":
            [
                event.to_dict()
                for event in events
            ],
    }


# ---------------------------------------------------------
# PUBLIC ENTRY POINT
# ---------------------------------------------------------

async def load_state(
    asset: str,
    force: bool = False,
) -> dict[str, Any]:

    now = time.monotonic()


    cached = _cache.get(
        asset
    )


    if (
        cached
        and not force
        and (
            now - cached[0]
            < CACHE_SECONDS
        )
    ):

        return cached[1]


    timeout = httpx.Timeout(
        8.0,
        connect=4.0,
    )


    headers = {
        "accept":
            "application/json"
    }


    numeric_asset = asset

    longname: str | None = None

    errors: list[str] = []


    try:

        async with httpx.AsyncClient(
            timeout=timeout,
            headers=headers,
        ) as client:


            # =============================================
            # Resolve:
            #
            # METABOLICA.0001
            #
            # ->
            #
            # A8133710759795923228
            # =============================================

            try:

                (
                    numeric_asset,
                    longname,
                ) = await _resolve_asset(
                    client,
                    asset,
                )


            except Exception as exc:

                errors.append(
                    "asset-resolve: "
                    f"{type(exc).__name__}: "
                    f"{exc}"
                )

                numeric_asset = asset


            encoded_numeric = quote(
                numeric_asset,
                safe="",
            )


            encoded_display = quote(
                asset,
                safe="",
            )


            # =============================================
            # PRIMARY:
            #
            # query internal asset ID
            #
            # A8133710759795923228
            # =============================================

            primary_paths = {

                "issuances":
                    f"/assets/{encoded_numeric}/issuances",

                "fairmints":
                    f"/assets/{encoded_numeric}/fairmints",

                "sends":
                    f"/assets/{encoded_numeric}/sends",

                "dispenses":
                    f"/assets/{encoded_numeric}/dispenses",

                "matches":
                    f"/assets/{encoded_numeric}/matches",

                "destructions":
                    f"/assets/{encoded_numeric}/destructions",
            }


            primary_results = (
                await asyncio.gather(

                    *(
                        _fetch_all(
                            client,
                            path,
                        )
                        for path
                        in primary_paths.values()
                    ),

                    return_exceptions=True,
                )
            )


            data: dict[
                str,
                list[dict[str, Any]]
            ] = {}


            for (
                key,
                result,
            ) in zip(
                primary_paths.keys(),
                primary_results,
            ):


                if isinstance(
                    result,
                    Exception,
                ):

                    data[key] = []

                    errors.append(
                        f"{key}: "
                        f"{type(result).__name__}: "
                        f"{result}"
                    )


                else:

                    data[key] = result


            # =============================================
            # FALLBACK:
            #
            # Also query human longname.
            #
            # Some Counterparty API deployments/routes
            # may resolve the longname differently.
            #
            # We merge both safely.
            # =============================================

            if numeric_asset != asset:


                fallback_paths = {

                    "issuances":
                        f"/assets/{encoded_display}/issuances",

                    "fairmints":
                        f"/assets/{encoded_display}/fairmints",

                    "sends":
                        f"/assets/{encoded_display}/sends",

                    "dispenses":
                        f"/assets/{encoded_display}/dispenses",

                    "matches":
                        f"/assets/{encoded_display}/matches",

                    "destructions":
                        f"/assets/{encoded_display}/destructions",
                }


                fallback_results = (
                    await asyncio.gather(

                        *(
                            _fetch_all(
                                client,
                                path,
                            )
                            for path
                            in fallback_paths.values()
                        ),

                        return_exceptions=True,
                    )
                )


                for (
                    key,
                    result,
                ) in zip(
                    fallback_paths.keys(),
                    fallback_results,
                ):


                    if isinstance(
                        result,
                        Exception,
                    ):

                        errors.append(

                            f"{key}-longname-fallback: "
                            f"{type(result).__name__}: "
                            f"{result}"
                        )

                        continue


                    data[key] = _merge_rows(

                        data.get(
                            key,
                            [],
                        ),

                        result,
                    )


        # =============================================
        # NORMALIZE RAW API DATA
        # =============================================

        events = _normalize(

            display_asset=asset,

            numeric_asset=numeric_asset,

            data=data,
        )


        # =============================================
        # FINAL STATE
        # =============================================

        state = _state(

            display_asset=(
                longname
                or asset
            ),

            numeric_asset=
                numeric_asset,

            events=
                events,

            api_error=(
                "; ".join(errors)
                if errors
                else None
            ),
        )


    except Exception as exc:


        state = _state(

            display_asset=
                asset,

            numeric_asset=
                numeric_asset,

            events=[],

            api_error=(
                f"{type(exc).__name__}: "
                f"{exc}"
            ),
        )


    _cache[asset] = (
        now,
        state,
    )


    return state
