import os
import re
import html
import json
from urllib.parse import urlparse

import requests
import yt_dlp
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

app = FastAPI(title="ClipTool Instagram Parser", version="1.0.0")

SECRET = os.getenv("CLIPTOOL_SECRET", "")
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)

class ResolveRequest(BaseModel):
    url: str

def verify_secret(x_cliptool_key: str | None):
    if SECRET and x_cliptool_key != SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

def normalize_instagram_url(value: str) -> str:
    value = value.strip()
    try:
        p = urlparse(value)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid URL")

    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    allowed = (
        host == "instagram.com"
        or host == "m.instagram.com"
        or host.endswith(".instagram.com")
    )
    if not allowed:
        raise HTTPException(
            status_code=400,
            detail="Only public Instagram links are supported."
        )

    if p.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Invalid URL scheme")

    return value

def shortcode_from_url(url: str):
    m = re.search(r"instagram\.com/(?:reel|reels|p|tv)/([^/?#]+)", url, re.I)
    return m.group(1) if m else None

def pick_formats(info: dict):
    items = []

    direct = info.get("url")
    if direct and direct.startswith("http"):
        items.append({
            "label": "Original",
            "quality": "original",
            "url": direct,
            "width": info.get("width"),
            "height": info.get("height"),
        })

    for f in info.get("formats") or []:
        u = f.get("url")
        if not u or not u.startswith("http"):
            continue

        # Prefer single-file video+audio formats.
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        if not vcodec or vcodec == "none":
            continue
        if acodec == "none":
            continue

        width = f.get("width")
        height = f.get("height")
        note = f.get("format_note") or f.get("format_id") or "Video"
        dims = f"{width}×{height}" if width and height else ""
        label = f"{note} · {dims}" if dims else note

        items.append({
            "label": label,
            "quality": dims or note,
            "url": u,
            "width": width,
            "height": height,
        })

    # de-duplicate URLs
    seen = set()
    out = []
    for item in items:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        out.append(item)

    # Highest resolution first
    out.sort(
        key=lambda x: ((x.get("height") or 0), (x.get("width") or 0)),
        reverse=True
    )
    return out

def resolve_with_ytdlp(url: str):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
        "http_headers": {
            "User-Agent": UA,
            "Referer": "https://www.instagram.com/",
        },
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise RuntimeError("Instagram post contains no downloadable video.")
        info = entries[0]

    variants = pick_formats(info)

    return {
        "title": info.get("title") or info.get("description") or "Instagram Video",
        "thumbnail": info.get("thumbnail") or "",
        "duration": info.get("duration") or 0,
        "variants": variants,
        "parser": "yt-dlp",
    }

def unescape_instagram_url(value: str) -> str:
    value = html.unescape(value)
    value = value.replace(r"\/", "/")
    value = value.replace(r"\u0026", "&")
    try:
        # Decode escaped unicode sequences only when present.
        if "\\u" in value:
            value = bytes(value, "utf-8").decode("unicode_escape")
    except Exception:
        pass
    return value

def resolve_with_embed(url: str):
    code = shortcode_from_url(url)
    if not code:
        raise RuntimeError("Could not identify Instagram shortcode.")

    embed_url = f"https://www.instagram.com/p/{code}/embed/captioned/"
    r = requests.get(
        embed_url,
        headers={
            "User-Agent": UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.instagram.com/",
        },
        timeout=20,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Instagram embed returned HTTP {r.status_code}.")

    text = r.text
    candidates = []

    patterns = [
        r'<meta[^>]+property=["\']og:video(?::secure_url)?["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video(?::secure_url)?["\']',
        r'"video_url"\s*:\s*"([^"]+)"',
        r'"videoUrl"\s*:\s*"([^"]+)"',
        r'\\"video_url\\"\s*:\s*\\"([^"]+)',
    ]

    for pat in patterns:
        for m in re.finditer(pat, text, re.I):
            u = unescape_instagram_url(m.group(1))
            if u.startswith("http"):
                candidates.append(u)

    # Remove duplicates
    urls = []
    seen = set()
    for u in candidates:
        if u not in seen:
            seen.add(u)
            urls.append(u)

    if not urls:
        raise RuntimeError(
            "Instagram did not expose a public video URL. "
            "The post may require login, be private, or Instagram may have changed its page."
        )

    thumb = ""
    tm = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
        text,
        re.I,
    )
    if tm:
        thumb = html.unescape(tm.group(1))

    return {
        "title": "Instagram Video",
        "thumbnail": thumb,
        "duration": 0,
        "variants": [
            {
                "label": "Original",
                "quality": "original",
                "url": u,
                "width": None,
                "height": None,
            }
            for u in urls
        ],
        "parser": "embed-fallback",
    }

@app.get("/")
def root():
    return {"ok": True, "service": "ClipTool Instagram Parser"}

@app.get("/health")
def health():
    return {"ok": True, "protected": bool(SECRET)}

@app.post("/resolve")
def resolve(req: ResolveRequest, x_cliptool_key: str | None = Header(default=None)):
    verify_secret(x_cliptool_key)
    url = normalize_instagram_url(req.url)

    errors = []

    try:
        result = resolve_with_ytdlp(url)
        if result["variants"]:
            return {"ok": True, **result}
    except Exception as e:
        errors.append(f"yt-dlp: {e}")

    try:
        result = resolve_with_embed(url)
        if result["variants"]:
            return {"ok": True, **result}
    except Exception as e:
        errors.append(f"embed: {e}")

    raise HTTPException(
        status_code=422,
        detail=(
            "Could not resolve this public Instagram video. "
            "Instagram sometimes requires login or blocks datacenter requests. "
            + " | ".join(errors[-2:])
        ),
    )
