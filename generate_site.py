from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from history import load_state
from renderer import render_png, render_png_rect, render_svg, state_fingerprint

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
ASSETS_FILE = ROOT / "assets.json"
RULESET_VERSION = os.getenv("RULESET_VERSION", "metabolika-pixel-v0.1")
CACHE_SECONDS = int(os.getenv("CACHE_SECONDS", "60"))
API_BASE = os.getenv("COUNTERPARTY_API_BASE", "https://api.counterparty.io:4000/v2").rstrip("/")
SITE_BASE_URL = os.getenv("SITE_BASE_URL", "").rstrip("/")


def derive_site_base_url() -> str:
    if SITE_BASE_URL:
        return SITE_BASE_URL
    repo = os.getenv("GITHUB_REPOSITORY", "")
    if repo and "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner}.github.io/{name}"
    return "https://example.github.io/metabolica-xcp"


def ensure_dirs() -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")


def load_assets() -> list[dict[str, Any]]:
    data = json.loads(ASSETS_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("assets.json must contain a JSON list")
    return data


def metadata_payload(asset: str, slug: str, title: str, base_url: str) -> dict[str, Any]:
    return {
        "asset": asset,
        "description": (
            f"{title}. Dynamic image generated deterministically from Counterparty on-chain history. "
            f"Ruleset: {RULESET_VERSION}."
        ),
        "image": f"{base_url}/{slug}.png",
        "website": f"{base_url}/{slug}/",
    }


def write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, payload: dict[str, Any] | list[Any]) -> None:
    write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def asset_page(base_url: str, slug: str, state: dict[str, Any], title: str) -> str:
    counts = state.get("counts", {}) if isinstance(state.get("counts"), dict) else {}
    born_text = "born" if state.get("born") else "pre-birth"
    warning = ""
    if state.get("api_error"):
        warning = f'<p class="warn">API issue: {state.get("api_error")}</p>'
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
body {{ font-family: system-ui, sans-serif; background:#14161c; color:#eef0f4; max-width:920px; margin:40px auto; padding:0 18px; }}
a {{ color:#4acde2; }}
img {{ width:min(560px,100%); height:auto; border-radius:16px; background:#14161c; box-shadow:0 8px 32px rgba(0,0,0,.28); }}
.small {{ width:48px; height:48px; image-rendering:auto; border-radius:8px; }}
.muted {{ color:#9aa1ad; }}
.warn {{ color:#ffd48a; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:18px 0; }}
.card {{ background:rgba(255,255,255,0.04); border-radius:12px; padding:12px; }}
code {{ background:rgba(255,255,255,0.06); padding:2px 6px; border-radius:6px; }}
</style>
</head>
<body>
<h1>{title}</h1>
<p class="muted">Asset <code>{state.get("asset")}</code> · {born_text} · ruleset <code>{RULESET_VERSION}</code></p>
<p><img src="{base_url}/{slug}-card.png" width="560" height="400" alt="Dynamic card"><br>
<img class="small" src="{base_url}/{slug}.png" width="48" height="48" alt="Wallet icon"></p>
{warning}
<div class="grid">
  <div class="card"><b>Events</b><br>{state.get("event_count", 0)}</div>
  <div class="card"><b>Transfers</b><br>{counts.get("TRANSFER", 0)}</div>
  <div class="card"><b>BTC sales</b><br>{counts.get("BTC_SALE", 0)}</div>
  <div class="card"><b>XCP sales</b><br>{counts.get("XCP_SALE", 0)}</div>
  <div class="card"><b>Burns</b><br>{counts.get("BURN", 0)}</div>
  <div class="card"><b>Addresses</b><br>{state.get("observed_addresses", 0)}</div>
</div>
<p>
<a href="{base_url}/{slug}.json">metadata JSON</a> ·
<a href="{base_url}/{slug}.png">48×48 PNG</a> ·
<a href="{base_url}/{slug}-card.png">560×400 PNG</a> ·
<a href="{base_url}/{slug}.svg">SVG</a> ·
<a href="{base_url}/{slug}/state.json">state JSON</a> ·
<a href="{base_url}/">collection</a>
</p>
<p class="muted">Каждое событие образует собственный полупрозрачный пиксельный слой. Если в истории ничего не изменилось, изображение тоже не меняется.</p>
</body>
</html>
"""


def index_page(base_url: str, items: list[dict[str, Any]]) -> str:
    cards = []
    for item in items:
        cards.append(
            f"""
<article class="card">
  <a href="{base_url}/{item['slug']}/"><img src="{base_url}/{item['slug']}-card.png" width="280" height="200" alt="{item['title']}"></a>
  <h2><a href="{base_url}/{item['slug']}/">{item['title']}</a></h2>
  <p class="muted"><code>{item['asset']}</code></p>
  <p>Events: <b>{item['event_count']}</b> · Transfers: <b>{item['counts'].get('TRANSFER',0)}</b> · BTC: <b>{item['counts'].get('BTC_SALE',0)}</b> · XCP: <b>{item['counts'].get('XCP_SALE',0)}</b></p>
</article>
"""
        )
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Metabolica Living XCP</title>
<style>
body {{ font-family: system-ui, sans-serif; background:#14161c; color:#eef0f4; max-width:1200px; margin:40px auto; padding:0 18px; }}
a {{ color:#4acde2; text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:18px; }}
.card {{ background:rgba(255,255,255,0.04); border-radius:16px; padding:14px; }}
.card img {{ width:100%; height:auto; display:block; border-radius:12px; }}
.muted {{ color:#9aa1ad; }}
code {{ background:rgba(255,255,255,0.06); padding:2px 6px; border-radius:6px; }}
</style>
</head>
<body>
<h1>Metabolica Living XCP</h1>
<p class="muted">Статический GitHub Pages-сайт, который GitHub Actions пересобирает из Counterparty history. Обновлено: {updated}. Ruleset: <code>{RULESET_VERSION}</code></p>
<div class="grid">{''.join(cards)}</div>
</body>
</html>
"""


async def build() -> None:
    ensure_dirs()
    base_url = derive_site_base_url()
    assets = load_assets()
    index_items: list[dict[str, Any]] = []

    for item in assets:
        asset = str(item["asset"])
        slug = str(item["slug"])
        title = str(item.get("title") or asset)

        state = await load_state(asset, force=True, cache_seconds=CACHE_SECONDS, api_base=API_BASE)
        state["ruleset"] = RULESET_VERSION
        state["fingerprint"] = state_fingerprint(state)
        state["updated_at"] = datetime.now(timezone.utc).isoformat()

        metadata = metadata_payload(asset, slug, title, base_url)
        write_json(DOCS / f"{slug}.json", metadata)
        write_bytes(DOCS / f"{slug}.png", render_png(state, 48))
        write_bytes(DOCS / f"{slug}-card.png", render_png_rect(state, 560, 400))
        write_text(DOCS / f"{slug}.svg", render_svg(state, 1000, 714))
        write_json(DOCS / slug / "state.json", state)
        write_text(DOCS / slug / "index.html", asset_page(base_url, slug, state, title))

        index_items.append({
            "asset": asset,
            "slug": slug,
            "title": title,
            "event_count": state.get("event_count", 0),
            "counts": state.get("counts", {}),
            "fingerprint": state.get("fingerprint"),
        })

    write_json(DOCS / "collection.json", index_items)
    write_text(DOCS / "index.html", index_page(base_url, index_items))


if __name__ == "__main__":
    asyncio.run(build())
