from __future__ import annotations

import hashlib
import io
import json
import math
import os
import random
from html import escape
from typing import Any

from PIL import Image, ImageDraw

RULESET_VERSION = os.getenv("RULESET_VERSION", "metabolika-pixel-v0.2.0")

# Тёмный фон
BG = (16, 18, 26, 255)

# Тусклое "дородовое" состояние, если событий нет
PREBIRTH = (105, 111, 122, 40)

# Цвета событий: RGBA
COLORS: dict[str, tuple[int, int, int, int]] = {
    "BIRTH": (95, 220, 255, 70),
    "FAIRMINT": (90, 220, 255, 65),
    "ISSUANCE": (90, 180, 255, 55),
    "TRANSFER": (245, 245, 245, 52),
    "BTC_SALE": (255, 200, 55, 62),
    "XCP_SALE": (90, 235, 125, 62),
    "BURN": (235, 65, 75, 72),
}

# Базовый размер облака по типу события
BASE_RADIUS: dict[str, float] = {
    "BIRTH": 34.0,
    "FAIRMINT": 32.0,
    "ISSUANCE": 28.0,
    "TRANSFER": 22.0,
    "BTC_SALE": 26.0,
    "XCP_SALE": 26.0,
    "BURN": 36.0,
}

# Базовое число пикселей-частиц на событие
BASE_PARTICLES: dict[str, int] = {
    "BIRTH": 2400,
    "FAIRMINT": 2200,
    "ISSUANCE": 1600,
    "TRANSFER": 1100,
    "BTC_SALE": 1500,
    "XCP_SALE": 1500,
    "BURN": 1800,
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
        f"{event.get('kind', '')}|"
        f"{event.get('tx_hash', '')}|"
        f"{event.get('raw_id', '')}|"
        f"{event.get('block_index', '')}|"
        f"{index}"
    )


def _area_ratio(width: int, height: int) -> float:
    return (width * height) / float(560 * 400)


def _clamp(v: float, lo: int, hi: int) -> int:
    return int(max(lo, min(hi, v)))


def _particle_count_for(kind: str, width: int, height: int) -> int:
    base = BASE_PARTICLES.get(kind, 1200)
    count = int(math.ceil(base * _area_ratio(width, height)))
    return max(200, count)


def _radius_for(kind: str, width: int, height: int) -> float:
    base = BASE_RADIUS.get(kind, 24.0)
    scale = math.sqrt(_area_ratio(width, height))
    return max(8.0, base * scale)


def _color_for_kind(kind: str) -> tuple[int, int, int, int]:
    return COLORS.get(kind, (180, 180, 255, 20))


def _size_for_particle(kind: str, rng: random.Random, canvas_min: int) -> int:
    if kind in {"BIRTH", "FAIRMINT"}:
        return rng.randint(1, max(2, canvas_min // 120))
    if kind == "BURN":
        return rng.randint(1, max(3, canvas_min // 96))
    return rng.randint(1, max(2, canvas_min // 140))


def _event_center(event: dict[str, Any], index: int, width: int, height: int) -> tuple[float, float]:
    """
    Один event -> один центр облака.
    Координаты детерминированы историей события.
    """
    rng = _rng(_event_seed(event, index))

    margin_x = max(20, int(width * 0.08))
    margin_y = max(20, int(height * 0.08))

    min_x = margin_x
    max_x = max(min_x + 1, width - margin_x)
    min_y = margin_y
    max_y = max(min_y + 1, height - margin_y)

    cx = rng.uniform(min_x, max_x)
    cy = rng.uniform(min_y, max_y)
    return cx, cy


def _prebirth_layer(state: dict[str, Any], width: int, height: int) -> list[tuple[int, int, int, tuple[int, int, int, int]]]:
    """
    Если у актива вообще нет событий — слабое рассеянное облако.
    """
    rng = _rng(f"prebirth|{state.get('asset', '')}|{state.get('asset_id', '')}")
    layer: list[tuple[int, int, int, tuple[int, int, int, int]]] = []

    count = max(400, int(1400 * _area_ratio(width, height)))
    canvas_min = min(width, height)

    cx = rng.uniform(width * 0.30, width * 0.70)
    cy = rng.uniform(height * 0.30, height * 0.70)
    radius = max(10.0, canvas_min * 0.10)

    for _ in range(count):
        dx = rng.gauss(0.0, radius * 0.70)
        dy = rng.gauss(0.0, radius * 0.70)
        x = _clamp(cx + dx, 0, width - 1)
        y = _clamp(cy + dy, 0, height - 1)
        size = rng.randint(1, max(1, canvas_min // 150))

        dist = math.sqrt(dx * dx + dy * dy)
        falloff = max(0.12, 1.0 - (dist / (radius * 2.3)))
        alpha = int(PREBIRTH[3] * falloff * rng.uniform(0.55, 1.0))
        rgba = (PREBIRTH[0], PREBIRTH[1], PREBIRTH[2], max(6, alpha))
        layer.append((x, y, size, rgba))

    return layer


def _event_cloud_layer(
    event: dict[str, Any],
    index: int,
    width: int,
    height: int,
) -> list[tuple[int, int, int, tuple[int, int, int, int]]]:
    """
    Одно событие -> одно облако.
    Внутри облака много пикселей, но центр только один.
    """
    kind = str(event.get("kind", "TRANSFER"))
    rgba = _color_for_kind(kind)
    rng = _rng(_event_seed(event, index))

    cx, cy = _event_center(event, index, width, height)
    radius = _radius_for(kind, width, height)
    count = _particle_count_for(kind, width, height)
    canvas_min = min(width, height)

    layer: list[tuple[int, int, int, tuple[int, int, int, int]]] = []

    # Чтобы облака были чуть "живее", даём каждому детерминированную
    # эллиптичность и поворот, но центр остаётся один.
    stretch_x = rng.uniform(0.85, 1.25)
    stretch_y = rng.uniform(0.85, 1.25)
    angle = rng.uniform(0.0, math.pi)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)

    for _ in range(count):
        # Основная масса частиц вокруг одного центра
        local_x = rng.gauss(0.0, radius * 0.55) * stretch_x
        local_y = rng.gauss(0.0, radius * 0.55) * stretch_y

        # Небольшая дополнительная неровность
        local_x += rng.gauss(0.0, radius * 0.10)
        local_y += rng.gauss(0.0, radius * 0.10)

        # Поворот эллипса
        dx = local_x * cos_a - local_y * sin_a
        dy = local_x * sin_a + local_y * cos_a

        x = _clamp(cx + dx, 0, width - 1)
        y = _clamp(cy + dy, 0, height - 1)

        dist = math.sqrt(dx * dx + dy * dy)
        falloff = max(0.15, 1.0 - (dist / (radius * 2.2)))
        alpha = int(rgba[3] * falloff * rng.uniform(0.55, 1.0))
        alpha = max(6, min(255, alpha))

        size = _size_for_particle(kind, rng, canvas_min)
        layer.append((x, y, size, (rgba[0], rgba[1], rgba[2], alpha)))

    # Ядро — немного более ярких точек
    spark_count = max(20, count // 90)
    for _ in range(spark_count):
        dx = rng.gauss(0.0, radius * 0.22)
        dy = rng.gauss(0.0, radius * 0.22)
        x = _clamp(cx + dx, 0, width - 1)
        y = _clamp(cy + dy, 0, height - 1)
        size = 1
        alpha = min(255, int(rgba[3] * rng.uniform(2.0, 3.2)))
        layer.append((x, y, size, (rgba[0], rgba[1], rgba[2], alpha)))

    return layer


def pixel_layers(
    state: dict[str, Any],
    width: int,
    height: int,
) -> list[list[tuple[int, int, int, tuple[int, int, int, int]]]]:
    """
    Возвращает список слоёв.
    Каждый слой соответствует одному событию.
    """
    events = _events(state)
    layers: list[list[tuple[int, int, int, tuple[int, int, int, int]]]] = []

    born = bool(state.get("born"))
    if not born and not events:
        layers.append(_prebirth_layer(state, width, height))
        return layers

    for idx, event in enumerate(events):
        layers.append(_event_cloud_layer(event, idx, width, height))

    return layers


def render_png(state: dict[str, Any], size: int = 48) -> bytes:
    return render_png_rect(state, size, size)


def render_png_rect(state: dict[str, Any], width: int = 560, height: int = 400) -> bytes:
    """
    PNG-рендер с supersampling:
    - маленькие иконки выглядят мягче
    - карточка 560x400 получается чище
    """
    scale = 8 if max(width, height) <= 64 else 4
    canvas_w = width * scale
    canvas_h = height * scale

    base = Image.new("RGBA", (canvas_w, canvas_h), BG)
    layers = pixel_layers(state, canvas_w, canvas_h)

    for layer in layers:
        overlay = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay, "RGBA")

        for x, y, size, rgba in layer:
            x2 = min(canvas_w - 1, x + size - 1)
            y2 = min(canvas_h - 1, y + size - 1)
            draw.rectangle((x, y, x2, y2), fill=rgba)

        base = Image.alpha_composite(base, overlay)

    final = base.resize((width, height), Image.Resampling.LANCZOS).convert("RGBA")
    out = io.BytesIO()
    final.save(out, format="PNG", optimize=True)
    return out.getvalue()


def render_svg(state: dict[str, Any], width: int = 1000, height: int = 714) -> str:
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">',
        f'<rect width="{width}" height="{height}" fill="rgb({BG[0]},{BG[1]},{BG[2]})"/>',
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
    parts.append(f"<title>{asset} — Metabolika Living XCP Pixel Protocol {escape(RULESET_VERSION)}</title>")
    parts.append("</svg>")
    return "".join(parts)


def state_fingerprint(state: dict[str, Any]) -> str:
    payload = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
