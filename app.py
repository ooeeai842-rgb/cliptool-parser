import os
import re
import html
import base64
import tempfile
import time
from urllib.parse import urlparse

import requests
import yt_dlp
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

app = FastAPI(title="ClipTool Instagram Parser", version="3.0.0")

SECRET = os.getenv("CLIPTOOL_SECRET", "")
COOKIE_B64 = os.getenv("INSTAGRAM_COOKIES_B64", "").strip()
COOKIE_RAW = os.getenv("INSTAGRAM_COOKIE", "").strip()

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)

class ResolveRequest(BaseModel):
    url: str

def verify_secret(value):
    if SECRET and value != SECRET:
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

    if not (
        host == "instagram.com"
        or host == "m.instagram.com"
        or host.endswith(".instagram.com")
    ):
        raise HTTPException(status_code=400, detail="Only Instagram URLs are supported.")

    if p.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Invalid URL scheme")

    return value

def shortcode_from_url(url: str):
    m = re.search(r"instagram\.com/(?:reel|reels|p|tv)/([^/?#]+)", url, re.I)
    return m.group(1) if m else None

def make_cookie_file():
    """
    Supports either:
      INSTAGRAM_COOKIES_B64 = base64 of a Netscape cookies.txt file
    OR
      INSTAGRAM_COOKIE = raw Cookie header, e.g. sessionid=...; csrftoken=...; ds_user_id=...
    Returns a temp filename or None.
    """
    if not COOKIE_B64 and not COOKIE_RAW:
        return None

    f = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, suffix=".txt")

    if COOKIE_B64:
        try:
            raw = base64.b64decode(COOKIE_B64).decode("utf-8")
            if not raw.lstrip().startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
                f.close()
                os.unlink(f.name)
                raise RuntimeError("INSTAGRAM_COOKIES_B64 is not a Netscape cookies.txt file.")
            f.write(raw)
            f.flush()
            f.close()
            return f.name
        except Exception:
            try:
                f.close()
                os.unlink(f.name)
            except Exception:
                pass
            raise

    # Convert a raw Cookie header to Netscape format.
    # This keeps setup simple for a personal private backend.
    expiry = int(time.time()) + 60 * 60 * 24 * 30
    f.write("# Netscape HTTP Cookie File\n")
    for piece in COOKIE_RAW.split(";"):
        piece = piece.strip()
        if not piece or "=" not in piece:
            continue
        name, value = piece.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        f.write(f".instagram.com\tTRUE\t/\tTRUE\t{expiry}\t{name}\t{value}\n")

    f.flush()
    f.close()
    return f.name

def pick_formats(info: dict):
    items = []

    for f in info.get("formats") or []:
        u = f.get("url")
        if not u or not str(u).startswith("http"):
            continue
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        if not vcodec or vcodec == "none":
            continue
        if acodec == "none":
            continue

        w = f.get("width")
        h = f.get("height")
        dims = f"{w}×{h}" if w and h else ""
        note = f.get("format_note") or f.get("format_id") or "Video"
        items.append({
            "label": f"{note} · {dims}" if dims else note,
            "quality": dims or note,
            "url": u,
            "width": w,
            "height": h,
        })

    direct = info.get("url")
    if direct and str(direct).startswith("http"):
        items.append({
            "label": "Original",
            "quality": "original",
            "url": direct,
            "width": info.get("width"),
            "height": info.get("height"),
        })

    seen = set()
    out = []
    for x in items:
        if x["url"] not in seen:
            seen.add(x["url"])
            out.append(x)

    out.sort(key=lambda x: ((x.get("height") or 0), (x.get("width") or 0)), reverse=True)
    return out

def resolve_with_ytdlp(url: str):
    cookie_file = None
    try:
        cookie_file = make_cookie_file()
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
        if cookie_file:
            opts["cookiefile"] = cookie_file

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            if not entries:
                raise RuntimeError("No video found.")
            info = entries[0]

        variants = pick_formats(info)

        return {
            "title": info.get("title") or info.get("description") or "Instagram Video",
            "thumbnail": info.get("thumbnail") or "",
            "duration": info.get("duration") or 0,
            "variants": variants,
            "parser": "yt-dlp+cookies" if cookie_file else "yt-dlp",
        }
    finally:
        if cookie_file:
            try:
                os.unlink(cookie_file)
            except Exception:
                pass

def unescape_url(value: str) -> str:
    value = html.unescape(value).replace(r"\/", "/").replace(r"\u0026", "&")
    return value

def resolve_with_embed(url: str):
    code = shortcode_from_url(url)
    if not code:
        raise RuntimeError("Could not identify shortcode.")

    embed_url = f"https://www.instagram.com/p/{code}/embed/captioned/"
    headers = {
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.instagram.com/",
    }
    if COOKIE_RAW:
        headers["Cookie"] = COOKIE_RAW

    r = requests.get(embed_url, headers=headers, timeout=20)

    if r.status_code != 200:
        raise RuntimeError(f"Instagram embed returned HTTP {r.status_code}.")

    text = r.text
    candidates = []
    patterns = [
        r'<meta[^>]+property=["\']og:video(?::secure_url)?["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video(?::secure_url)?["\']',
        r'"video_url"\s*:\s*"([^"]+)"',
        r'"videoUrl"\s*:\s*"([^"]+)"',
    ]

    for pat in patterns:
        for m in re.finditer(pat, text, re.I):
            u = unescape_url(m.group(1))
            if u.startswith("http"):
                candidates.append(u)

    urls = []
    seen = set()
    for u in candidates:
        if u not in seen:
            seen.add(u)
            urls.append(u)

    if not urls:
        raise RuntimeError("Instagram did not expose a video URL.")

    return {
        "title": "Instagram Video",
        "thumbnail": "",
        "duration": 0,
        "variants": [
            {"label": "Original", "quality": "original", "url": u}
            for u in urls
        ],
        "parser": "embed-fallback",
    }

@app.get("/")
def root():
    return {
        "ok": True,
        "service": "ClipTool Instagram Parser",
        "version": "3.0.0",
        "instagram_auth": bool(COOKIE_B64 or COOKIE_RAW),
    }

@app.get("/health")
def health():
    return {
        "ok": True,
        "protected": bool(SECRET),
        "instagram_auth": bool(COOKIE_B64 or COOKIE_RAW),
        "version": "3.0.0",
    }

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

    auth_hint = (
        " Instagram authentication is configured but Instagram still rejected the request."
        if COOKIE_B64 or COOKIE_RAW
        else " Add INSTAGRAM_COOKIES_B64 or INSTAGRAM_COOKIE in Render to authenticate."
    )

    raise HTTPException(
        status_code=422,
        detail="Could not resolve this Instagram video." + auth_hint + " | " + " | ".join(errors[-2:]),
    )
