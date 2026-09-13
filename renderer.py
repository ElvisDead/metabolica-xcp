from __future__ import annotations

import hashlib
import io
import json
import math
import random
from html import escape
from typing import Any

from PIL import Image, ImageDraw

BG = (20, 22, 28, 255)
PREBIRTH = (105, 111, 122, 42)

COLORS = {
    "BIRTH": (55, 137, 255, 64),
    "FAIRMINT": (74, 205, 226, 64),
    "ISSUANCE": (110, 210, 255, 52),
    "TRANSFER": (238, 240, 244, 34),
    "BTC_SALE": (246, 183, 60, 44),
    "XCP_SALE": (38, 194, 129, 44),
    "BURN": (28, 8, 10, 70),
}

BASE_COUNTS = {
    "BIRTH": 650,
    "FAIRMINT": 650,
    "ISSUANCE": 420,
    "TRANSFER": 220,
    "BTC_SALE": 300,
    "XCP_SALE": 260,
    "BURN": 380,
}


def _seed(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:16], 16)


def _rng(text: str) -> random.Random:
    return random.Random(_seed(text))


def _events(state: dict[str, Any]) -> list[dict[str, Any]]:
    value = state.get("events", [])
    return value if isinstance(value, list) else []


def _event_seed(event: dict[str, Any], index: int) -> str:
    return (
        f"{event.get('kind','')}|{event.get('tx_hash','')}|{event.get('raw_id','')}|"
        f"{event.get('block_index','')}|{index}"
    )


def _area_ratio(width: int, height: int) -> float:
    return (width * height) / float(560 * 400)


def _count_for(kind: str, width: int, height: int) -> int:
    base = BASE_COUNTS.get(kind, 180)
    count = int(math.ceil(base * _area_ratio(width, height)))
    return max(25, count)


def _clamp(v: float, lo: int, hi: int) -> int:
    return int(max(lo, min(hi, v)))


def pixel_layers(state: dict[str, Any], width: int, height: int) -> list[list[tuple[int, int, int, tuple[int, int, int, int]]]]:
    """Each event becomes one translucent layer made of pixel clusters."""
    born = bool(state.get("born"))
    events = _events(state)
    layers: list[list[tuple[int, int, int, tuple[int, int, int, int]]]] = []

    if not born and not events:
        rng = _rng(f"prebirth|{state.get('asset','')}")
        layer: list[tuple[int, int, int, tuple[int, int, int, int]]] = []
        count = max(40, int(220 * _area_ratio(width, height)))
        cluster_count = 4
        centers = [(rng.randrange(width), rng.randrange(height)) for _ in range(cluster_count)]
        spread = max(8.0, min(width, height) * 0.08)
        for _ in range(count):
            cx, cy = centers[rng.randrange(cluster_count)]
            x = _clamp(rng.gauss(cx, spread), 0, width - 1)
            y = _clamp(rng.gauss(cy, spread), 0, height - 1)
            size = rng.randint(1, max(1, min(width, height) // 140))
            alpha = rng.randint(18, 52)
            layer.append((x, y, size, (PREBIRTH[0], PREBIRTH[1], PREBIRTH[2], alpha)))
        layers.append(layer)
        return layers

    for idx, event in enumerate(events):
        kind = str(event.get("kind", "TRANSFER"))
        rgba = COLORS.get(kind, COLORS["TRANSFER"])
        count = _count_for(kind, width, height)
        rng = _rng(_event_seed(event, idx))
        cluster_count = rng.randint(3, 8)
        centers = [(rng.randrange(width), rng.randrange(height)) for _ in range(cluster_count)]
        base_spread = min(width, height) * (0.03 + 0.006 * min(idx, 12))
        spread = max(8.0, base_spread)
        layer: list[tuple[int, int, int, tuple[int, int, int, int]]] = []
        for _ in range(count):
            cx, cy = centers[rng.randrange(cluster_count)]
            x = _clamp(rng.gauss(cx, spread), 0, width - 1)
            y = _clamp(rng.gauss(cy, spread), 0, height - 1)
            if kind in {"BIRTH", "FAIRMINT"}:
                size = rng.randint(1, max(2, min(width, height) // 110))
            elif kind == "BURN":
                size = rng.randint(1, max(3, min(width, height) // 95))
            else:
                size = rng.randint(1, max(2, min(width, height) // 130))
            alpha_jitter = rng.randint(-12, 12)
            alpha = max(8, min(255, rgba[3] + alpha_jitter))
            layer.append((x, y, size, (rgba[0], rgba[1], rgba[2], alpha)))
        layers.append(layer)
    return layers


def render_png(state: dict[str, Any], size: int = 48) -> bytes:
    return render_png_rect(state, size, size)


def render_png_rect(state: dict[str, Any], width: int = 560, height: int = 400) -> bytes:
    scale = 8 if max(width, height) <= 64 else 4
    canvas_w = width * scale
    canvas_h = height * scale
    base = Image.new("RGBA", (canvas_w, canvas_h), BG)
    layers = pixel_layers(state, canvas_w, canvas_h)
    for layer in layers:
        overlay = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay, "RGBA")
        for x, y, size, rgba in layer:
            draw.rectangle((x, y, x + size - 1, y + size - 1), fill=rgba)
        base = Image.alpha_composite(base, overlay)
    final = base.resize((width, height), Image.Resampling.LANCZOS).convert("RGBA")
    out = io.BytesIO()
    final.save(out, format="PNG", optimize=True)
    return out.getvalue()


def render_svg(state: dict[str, Any], width: int = 1000, height: int = 714) -> str:
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">',
        f'<rect width="{width}" height="{height}" fill="#14161c"/>',
    ]
    layers = pixel_layers(state, width, height)
    for layer in layers:
        parts.append("<g>")
        for x, y, size, rgba in layer:
            r, g, b, a = rgba
            alpha = a / 255.0
            parts.append(
                f'<rect x="{x}" y="{y}" width="{size}" height="{size}" '
                f'fill="rgb({r},{g},{b})" fill-opacity="{alpha:.4f}"/>'
            )
        parts.append("</g>")
    asset = escape(str(state.get("asset", "")))
    parts.append(f"<title>{asset} — Metabolika Living XCP Pixel Protocol v0.1</title>")
    parts.append("</svg>")
    return "".join(parts)


def state_fingerprint(state: dict[str, Any]) -> str:
    payload = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
