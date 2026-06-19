"""
resolver.py
===========
Best-effort "social post URL -> local thumbnail image file" resolver.

Public surface
--------------
    resolve_one(url) -> ResolveResult        # full ladder + download + cache
    enrich(items, deadline_seconds=60, max_workers=8) -> int

Design goals (per spec):
  * Best-effort, NEVER fatal. Any failure on one URL yields "no image",
    never an exception that escapes to the caller.
  * Time-bounded concurrency. A whole batch finishes inside a wall-clock
    deadline regardless of how many URLs hang.
  * Multi-path downloader (requests -> requests no-verify -> httpx -> urllib).
  * Per-process URL cache (dedupe repeated links).
  * Optional cookie auth via env (no hardcoded secrets).
  * Every heavy dependency (yt-dlp, httpx, playwright, PIL) is import-guarded
    so a missing package degrades that one rung, not the whole module.
"""

from __future__ import annotations

import os
import re
import ssl
import time
import json
import queue
import logging
import tempfile
import threading
import urllib.request
import concurrent.futures
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse, quote

log = logging.getLogger("resolver")

# --------------------------------------------------------------------------- #
# Optional dependency probing -- each rung degrades independently.
# --------------------------------------------------------------------------- #
try:
    import requests
except Exception:                                   # pragma: no cover
    requests = None

try:
    import httpx
except Exception:                                   # pragma: no cover
    httpx = None

try:
    import certifi
except Exception:                                   # pragma: no cover
    certifi = None

try:
    import yt_dlp
except Exception:                                   # pragma: no cover
    yt_dlp = None

try:
    from PIL import Image
except Exception:                                   # pragma: no cover
    Image = None

# parsel is nice but optional; we fall back to regex meta parsing without it.
try:
    from parsel import Selector
except Exception:                                   # pragma: no cover
    Selector = None

# Playwright is the heaviest dep -- only probed lazily inside its rung.

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HTTP_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
    "image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Output / config -----------------------------------------------------------
EXPORT_DIR = os.environ.get(
    "THUMBNAIL_EXPORT_DIR", os.path.join(tempfile.gettempdir(), "thumbnail_export")
)
os.makedirs(EXPORT_DIR, exist_ok=True)

# Optional cookie auth for login-gated content (no hardcoding).
#   THUMBNAIL_COOKIEFILE   -> path to a Netscape cookies.txt
#   THUMBNAIL_COOKIES_FROM -> a browser name for yt-dlp cookiesfrombrowser
COOKIEFILE = os.environ.get("THUMBNAIL_COOKIEFILE") or None
COOKIES_FROM_BROWSER = os.environ.get("THUMBNAIL_COOKIES_FROM") or None

PER_URL_TIMEOUT = float(os.environ.get("THUMBNAIL_PER_URL_TIMEOUT", "20"))


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #
@dataclass
class ResolveResult:
    url: str
    platform: str = "unknown"
    thumbnail_url: Optional[str] = None
    image_path: Optional[str] = None
    method: Optional[str] = None        # which rung succeeded
    ok: bool = False
    reason: str = ""                    # human explanation when not ok

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Platform detection
# --------------------------------------------------------------------------- #
def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if any(h in host for h in ("youtube.com", "youtu.be")):
        return "youtube"
    if "tiktok" in host:
        return "tiktok"
    if "instagram" in host:
        return "instagram"
    if "threads" in host:
        return "threads"
    if any(h in host for h in ("twitter.com", "x.com", "t.co")):
        return "twitter"
    if any(h in host for h in ("facebook.com", "fb.watch", "fb.com")):
        return "facebook"
    return "unknown"


# --------------------------------------------------------------------------- #
# Per-process cache
# --------------------------------------------------------------------------- #
_CACHE: Dict[str, ResolveResult] = {}
_CACHE_LOCK = threading.Lock()


def _cache_get(url: str) -> Optional[ResolveResult]:
    with _CACHE_LOCK:
        return _CACHE.get(url)


def _cache_put(url: str, res: ResolveResult) -> None:
    with _CACHE_LOCK:
        _CACHE[url] = res


# --------------------------------------------------------------------------- #
# Multi-path downloader
# --------------------------------------------------------------------------- #
def _looks_like_image(data: bytes) -> bool:
    return bool(data) and len(data) > 256


def _ext_for(url: str, data: bytes) -> str:
    # 1) trust the URL suffix
    path = urlparse(url).path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    # 2) fall back to magic bytes
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ".jpg"


def _download_bytes(url: str) -> Optional[bytes]:
    """Try every transport until one yields real image bytes."""
    # Path 1: requests with browser UA (verified TLS)
    if requests is not None:
        try:
            r = requests.get(url, headers=HTTP_HEADERS, timeout=15, allow_redirects=True)
            if r.ok and _looks_like_image(r.content):
                log.info("  download ✓ path1 requests (%d bytes)", len(r.content))
                return r.content
        except Exception as e:
            log.debug("download p1 failed: %s", e)

        # Path 2: requests with TLS verification off (corporate MITM / SSL trust)
        try:
            import warnings
            from urllib3.exceptions import InsecureRequestWarning
            warnings.simplefilter("ignore", InsecureRequestWarning)
            r = requests.get(
                url, headers=HTTP_HEADERS, timeout=15, allow_redirects=True, verify=False
            )
            if r.ok and _looks_like_image(r.content):
                log.info("  download ✓ path2 requests no-verify (%d bytes)", len(r.content))
                return r.content
        except Exception as e:
            log.debug("download p2 failed: %s", e)

    # Path 3: httpx with certifi bundle, both trust_env settings
    if httpx is not None:
        for trust_env in (True, False):
            try:
                verify = certifi.where() if certifi is not None else True
                with httpx.Client(
                    headers=HTTP_HEADERS,
                    timeout=15,
                    follow_redirects=True,
                    verify=verify,
                    trust_env=trust_env,
                ) as c:
                    r = c.get(url)
                    if r.status_code < 400 and _looks_like_image(r.content):
                        log.info("  download ✓ path3 httpx trust_env=%s (%d bytes)",
                                 trust_env, len(r.content))
                        return r.content
            except Exception as e:
                log.debug("download p3 (trust_env=%s) failed: %s", trust_env, e)

    # Path 4: urllib as last resort (unverified context)
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers=HTTP_HEADERS)
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            data = resp.read()
            if _looks_like_image(data):
                log.info("  download ✓ path4 urllib (%d bytes)", len(data))
                return data
    except Exception as e:
        log.debug("download p4 failed: %s", e)

    log.warning("  download ✗ all 4 paths failed for %s", _short(url, 90))
    return None


def _ytimg_alternates(thumb_url: str) -> List[str]:
    """Not every YouTube video has maxresdefault/sddefault -- hqdefault always
    exists. Return progressively-safer i.ytimg fallbacks for a ytimg URL."""
    m = re.search(r"/vi(?:_webp)?/([A-Za-z0-9_-]{11})/", thumb_url)
    if not m:
        return []
    vid = m.group(1)
    return [f"https://i.ytimg.com/vi/{vid}/{q}.jpg"
            for q in ("maxresdefault", "sddefault", "hqdefault", "mqdefault")]


def _save_image(url: str, thumb_url: str) -> Optional[str]:
    candidates = [thumb_url] + _ytimg_alternates(thumb_url)
    data = None
    for cand in candidates:
        data = _download_bytes(cand)
        if data:
            thumb_url = cand
            break
    if not data:
        return None
    ext = _ext_for(thumb_url, data)
    # filename derived from a stable hash of the post URL (dedupe-friendly)
    safe = re.sub(r"[^a-zA-Z0-9]", "_", url)[-60:]
    fname = f"{abs(hash(url)) & 0xFFFFFFFF:08x}_{safe}{ext}"
    path = os.path.join(EXPORT_DIR, fname)
    try:
        with open(path, "wb") as f:
            f.write(data)
        return path
    except Exception as e:
        log.debug("save failed: %s", e)
        return None


# --------------------------------------------------------------------------- #
# Rung 1: yt-dlp metadata only
# --------------------------------------------------------------------------- #
def _via_ytdlp(url: str) -> Optional[str]:
    if yt_dlp is None:
        return None

    class _NullLogger:        # swallow yt-dlp's stderr chatter
        def debug(self, m): pass
        def info(self, m): pass
        def warning(self, m): pass
        def error(self, m): pass

    opts = {
        "skip_download": True,
        "extract_flat": False,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 12,
        "logger": _NullLogger(),
    }
    if COOKIEFILE:
        opts["cookiefile"] = COOKIEFILE
    if COOKIES_FROM_BROWSER:
        opts["cookiesfrombrowser"] = (COOKIES_FROM_BROWSER,)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        log.debug("yt-dlp failed: %s", e)
        return None
    if not info:
        return None
    thumbs = info.get("thumbnails") or []
    if thumbs:
        # highest-res is conventionally last
        last = thumbs[-1]
        if isinstance(last, dict) and last.get("url"):
            return last["url"]
    return info.get("thumbnail")


# --------------------------------------------------------------------------- #
# Rung 2: OpenGraph / meta-tag scrape
# --------------------------------------------------------------------------- #
_META_PROPS = ("og:image", "og:image:url", "twitter:image", "twitter:image:src")


def _extract_meta_image(html: str) -> Optional[str]:
    if Selector is not None:
        try:
            sel = Selector(text=html)
            for prop in _META_PROPS:
                for attr in ("property", "name"):
                    val = sel.xpath(
                        f'//meta[@{attr}="{prop}"]/@content'
                    ).get()
                    if val:
                        return val.strip()
        except Exception:
            pass
    # regex fallback (order-preserving over the property list)
    for prop in _META_PROPS:
        m = re.search(
            rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]+'
            rf'content=["\']([^"\']+)["\']',
            html,
            re.I,
        )
        if not m:
            m = re.search(
                rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+'
                rf'(?:property|name)=["\']{re.escape(prop)}["\']',
                html,
                re.I,
            )
        if m:
            return m.group(1).strip()
    return None


def _via_opengraph(url: str) -> Optional[str]:
    html = None
    if requests is not None:
        for verify in (True, False):
            try:
                r = requests.get(
                    url, headers=HTTP_HEADERS, timeout=15,
                    allow_redirects=True, verify=verify,
                )
                if r.ok and r.text:
                    html = r.text
                    break
            except Exception as e:
                log.debug("og GET (verify=%s) failed: %s", verify, e)
    if html is None and httpx is not None:
        try:
            verify = certifi.where() if certifi is not None else True
            with httpx.Client(headers=HTTP_HEADERS, timeout=15,
                              follow_redirects=True, verify=verify) as c:
                r = c.get(url)
                if r.status_code < 400:
                    html = r.text
        except Exception as e:
            log.debug("og httpx failed: %s", e)
    if not html:
        return None
    return _extract_meta_image(html)


# --------------------------------------------------------------------------- #
# Rung 3: platform-specific public endpoints
# --------------------------------------------------------------------------- #
def _via_tiktok_oembed(url: str) -> Optional[str]:
    """TikTok oEmbed. Only parse if content-type is JSON -- geo-restricted
    networks return a 302 -> HTML regional page instead."""
    endpoint = "https://www.tiktok.com/oembed?url=" + quote(url, safe="")
    if requests is not None:
        try:
            r = requests.get(endpoint, headers=HTTP_HEADERS, timeout=12,
                             allow_redirects=True)
            ctype = r.headers.get("content-type", "")
            if r.ok and "json" in ctype.lower():
                data = r.json()
                return data.get("thumbnail_url")
        except Exception as e:
            log.debug("tiktok oembed (requests) failed: %s", e)
    if httpx is not None:
        try:
            verify = certifi.where() if certifi is not None else True
            with httpx.Client(headers=HTTP_HEADERS, timeout=12,
                              follow_redirects=True, verify=verify) as c:
                r = c.get(endpoint)
                ctype = r.headers.get("content-type", "")
                if r.status_code < 400 and "json" in ctype.lower():
                    return r.json().get("thumbnail_url")
        except Exception as e:
            log.debug("tiktok oembed (httpx) failed: %s", e)
    return None


def _twitter_status_id(url: str) -> Optional[str]:
    m = re.search(r"/status(?:es)?/(\d+)", url)
    return m.group(1) if m else None


def _twitter_syndication_token(tid: str) -> str:
    """Reproduce the token X's embedded-tweet widget derives from the id."""
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    x = (int(tid) / 1e15) * 3.141592653589793
    intp = int(x)
    # base-36 of the integer part
    s = ""
    n = intp
    if n == 0:
        s = "0"
    while n:
        s = digits[n % 36] + s
        n //= 36
    frac = x - intp
    if frac > 0:
        s += "."
        for _ in range(20):
            frac *= 36
            d = int(frac)
            s += digits[d]
            frac -= d
    return re.sub(r"(0+|\.)", "", s)


def _is_avatar(u: Optional[str]) -> bool:
    """X/Twitter og:image is the account avatar for media-less tweets."""
    return bool(u) and "profile_images" in u


def _pick_twitter_media(node: dict) -> Optional[str]:
    """Highest-value still image from a syndication tweet node."""
    if not isinstance(node, dict):
        return None
    best = None
    for m in (node.get("mediaDetails") or []):
        u = m.get("media_url_https")
        if u:
            best = u  # last/highest wins; videos expose an amplify thumb here too
    if best:
        return best
    for p in (node.get("photos") or []):
        if p.get("url"):
            return p["url"]
    return None


def _via_twitter_syndication(url: str) -> Optional[str]:
    """Public embed JSON API: real media for photo tweets, with a fallback to
    the quoted tweet's media. No login required for public posts."""
    tid = _twitter_status_id(url)
    if not tid:
        return None
    endpoint = (
        f"https://cdn.syndication.twimg.com/tweet-result?id={tid}"
        f"&lang=en&token={_twitter_syndication_token(tid)}"
    )
    data = None
    if requests is not None:
        try:
            r = requests.get(endpoint, headers=HTTP_HEADERS, timeout=12)
            if r.ok and "json" in r.headers.get("content-type", "").lower():
                data = r.json()
        except Exception as e:
            log.debug("twitter syndication (requests) failed: %s", e)
    if data is None and httpx is not None:
        try:
            verify = certifi.where() if certifi is not None else True
            with httpx.Client(headers=HTTP_HEADERS, timeout=12,
                              follow_redirects=True, verify=verify) as c:
                r = c.get(endpoint)
                if r.status_code < 400 and "json" in r.headers.get("content-type", "").lower():
                    data = r.json()
        except Exception as e:
            log.debug("twitter syndication (httpx) failed: %s", e)
    if not data:
        return None
    # own media first, then the quoted tweet's media
    return _pick_twitter_media(data) or _pick_twitter_media(data.get("quoted_tweet") or {})


MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)


def _via_facebook_mobile(url: str) -> Optional[str]:
    """Facebook's desktop /photo viewer is a login-gated JS shell with no
    og:image. The m.facebook.com mobile page, fetched with a mobile UA, still
    exposes og:image for PUBLIC photos/posts. (Private content still needs
    cookies.)"""
    mob = re.sub(r"^https?://(www\.|web\.)?facebook\.com",
                 "https://m.facebook.com", url, flags=re.I)
    headers = {**HTTP_HEADERS, "User-Agent": MOBILE_UA}
    html = None
    if requests is not None:
        for verify in (True, False):
            try:
                r = requests.get(mob, headers=headers, timeout=15,
                                 allow_redirects=True, verify=verify)
                if r.ok and r.text:
                    html = r.text
                    break
            except Exception as e:
                log.debug("fb mobile GET (verify=%s) failed: %s", verify, e)
    if not html and httpx is not None:
        try:
            verify = certifi.where() if certifi is not None else True
            with httpx.Client(headers=headers, timeout=15,
                              follow_redirects=True, verify=verify) as c:
                r = c.get(mob)
                if r.status_code < 400:
                    html = r.text
        except Exception as e:
            log.debug("fb mobile httpx failed: %s", e)
    if not html:
        return None
    img = _extract_meta_image(html)
    if img:
        return img.replace("&amp;", "&")
    return None


def _via_youtube_fallback(url: str) -> Optional[str]:
    """Deterministic i.ytimg.com URL from the video id -- no API, no scrape."""
    vid = None
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})", url)
    if m:
        vid = m.group(1)
    if not vid:
        return None
    return f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg"


# --------------------------------------------------------------------------- #
# Rung 4: headless browser (Playwright) -- last resort
# --------------------------------------------------------------------------- #
def _via_playwright(url: str) -> Optional[str]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx_kwargs = {"user_agent": BROWSER_UA}
            context = browser.new_context(**ctx_kwargs)
            if COOKIEFILE and os.path.exists(COOKIEFILE):
                try:
                    context.add_cookies(_parse_netscape_cookies(COOKIEFILE))
                except Exception:
                    pass
            page = context.new_page()
            page.goto(url, timeout=20000, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            content = page.content()
            browser.close()
            return _extract_meta_image(content)
    except Exception as e:
        log.debug("playwright failed: %s", e)
        return None


def _parse_netscape_cookies(path: str) -> List[Dict[str, Any]]:
    cookies = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.strip().split("\t")
            if len(parts) >= 7:
                cookies.append({
                    "domain": parts[0], "path": parts[2],
                    "secure": parts[3] == "TRUE",
                    "name": parts[5], "value": parts[6],
                })
    return cookies


# --------------------------------------------------------------------------- #
# The ladder
# --------------------------------------------------------------------------- #
def _short(url: str, n: int = 60) -> str:
    return url if len(url) <= n else url[: n - 1] + "…"


def _resolve_thumbnail_url(url: str, platform: str):
    """Return (thumbnail_url, method, reason). reason is set only on failure."""
    tried = []
    tag = f"[{platform}] {_short(url)}"

    def _try(name, fn):
        log.info("%s  -> trying %s", tag, name)
        t = fn()
        if t:
            log.info("%s  ✓ %s hit: %s", tag, name, _short(t, 90))
        else:
            log.info("%s  · %s: nothing", tag, name)
        tried.append(name)
        return t

    # Rung 1: yt-dlp (best for YouTube + most video platforms)
    t = _try("yt-dlp", lambda: _via_ytdlp(url))
    if t:
        return t, "yt-dlp", ""

    # Rung 1b: Twitter/X syndication API -- handles photo tweets (yt-dlp only
    # does videos) and falls back to a quoted tweet's media. Public, no login.
    if platform == "twitter":
        t = _try("twitter-syndication", lambda: _via_twitter_syndication(url))
        if t:
            return t, "twitter-syndication", ""

    # Rung 2: OpenGraph (rescues IG single image, many photo posts).
    # For Twitter, og:image is just the account avatar -- reject it.
    og = _try("opengraph", lambda: _via_opengraph(url))
    if og and platform == "twitter" and _is_avatar(og):
        log.info("%s  · opengraph returned the account avatar -- rejected", tag)
        og = None
    if og:
        return og, "opengraph", ""

    # Rung 3: platform-specific public endpoints
    if platform == "tiktok":
        t = _try("tiktok-oembed", lambda: _via_tiktok_oembed(url))
        if t:
            return t, "tiktok-oembed", ""
    if platform == "youtube":
        t = _try("youtube-ytimg", lambda: _via_youtube_fallback(url))
        if t:
            return t, "youtube-ytimg", ""
    if platform == "facebook":
        t = _try("facebook-mobile", lambda: _via_facebook_mobile(url))
        if t:
            return t, "facebook-mobile", ""

    # Rung 4: headless browser (login/JS-gated: Threads, X, some Facebook)
    t = _try("playwright", lambda: _via_playwright(url))
    if t:
        return t, "playwright", ""

    reason = _diagnose(platform, tried)
    return None, None, reason


def _diagnose(platform: str, tried: List[str]) -> str:
    base = f"all rungs failed ({', '.join(tried)})"
    hint = {
        "tiktok": "TikTok geo/IP restriction is the usual cause -- verify from an "
                  "unrestricted (e.g. US) host; this is network/region, not a code bug.",
        "instagram": "likely a private post or login wall -- supply cookies via "
                     "THUMBNAIL_COOKIEFILE.",
        "twitter": "X login/JS gate -- needs the headless browser rung "
                   "(pip install playwright) and often cookies.",
        "threads": "Threads is JS/login gated -- needs Playwright and often cookies.",
        "facebook": "Facebook commonly requires login -- needs Playwright + cookies.",
        "youtube": "unusual for YouTube -- check the URL is a valid video.",
        "unknown": "unrecognised platform / not a public post URL.",
    }.get(platform, "")
    return f"{base}. {hint}".strip()


# --------------------------------------------------------------------------- #
# Public: resolve_one
# --------------------------------------------------------------------------- #
def resolve_one(url: str) -> ResolveResult:
    """Full ladder + download + cache. Never raises."""
    url = (url or "").strip()
    if not url:
        return ResolveResult(url=url, ok=False, reason="empty url")

    cached = _cache_get(url)
    if cached is not None:
        log.info("[cache] hit for %s (%s)", _short(url),
                 "OK" if cached.ok else "miss")
        return cached

    platform = detect_platform(url)
    log.info("RESOLVE start [%s] %s", platform, _short(url))
    res = ResolveResult(url=url, platform=platform)
    try:
        thumb_url, method, reason = _resolve_thumbnail_url(url, platform)
        res.thumbnail_url = thumb_url
        res.method = method
        if thumb_url:
            log.info("[%s] downloading thumbnail via multi-path…", platform)
            path = _save_image(url, thumb_url)
            if path:
                res.image_path = path
                res.ok = True
                log.info("RESOLVE  OK  [%s] via %s -> %s",
                         platform, method, os.path.basename(path))
            else:
                res.ok = False
                res.reason = "found thumbnail URL but every download path failed"
                log.warning("RESOLVE MISS [%s] %s", platform, res.reason)
        else:
            res.ok = False
            res.reason = reason or "no thumbnail found"
            log.warning("RESOLVE MISS [%s] %s", platform, _short(res.reason, 120))
    except Exception as e:                          # absolute safety net
        res.ok = False
        res.reason = f"unexpected error: {e!r}"
        log.exception("resolve_one crashed for %s", url)

    _cache_put(url, res)
    return res


# --------------------------------------------------------------------------- #
# Public: enrich (time-bounded concurrency)
# --------------------------------------------------------------------------- #
def enrich(items: List[Dict[str, Any]], deadline_seconds: float = 60,
           max_workers: int = 8) -> int:
    """
    For each item (a dict with at least item["url"]), resolve its thumbnail and
    set item["imagePath"], item["result"] in place. Bounded by a wall-clock
    deadline; whatever hasn't finished is cancelled and marked timed-out.

    Returns the count of successfully resolved thumbnails.
    """
    urls = [it.get("url") for it in items]
    resolved = 0
    log.info("=" * 64)
    log.info("BATCH start: %d url(s), %d workers, %.0fs deadline",
             len(urls), max_workers, deadline_seconds)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_map = {ex.submit(resolve_one, u): i
                      for i, u in enumerate(urls) if u}
        done, not_done = concurrent.futures.wait(
            future_map, timeout=deadline_seconds
        )
        for fut in not_done:
            fut.cancel()
        for fut, idx in future_map.items():
            if fut in done:
                try:
                    res = fut.result()
                except Exception as e:
                    res = ResolveResult(url=urls[idx], ok=False,
                                        reason=f"future error: {e!r}")
            else:
                res = ResolveResult(
                    url=urls[idx],
                    platform=detect_platform(urls[idx] or ""),
                    ok=False,
                    reason=f"timed out (exceeded {deadline_seconds:.0f}s batch deadline)",
                )
            items[idx]["result"] = res.to_dict()
            items[idx]["imagePath"] = res.image_path
            if res.ok:
                resolved += 1

    if not_done:
        log.warning("BATCH: %d url(s) hit the %.0fs deadline and were cancelled",
                    len(not_done), deadline_seconds)
    log.info("BATCH done: %d/%d resolved", resolved, len(urls))
    log.info("=" * 64)
    return resolved


# --------------------------------------------------------------------------- #
# Convenience summary (used by the CLI / UI footer)
# --------------------------------------------------------------------------- #
def summarize(items: List[Dict[str, Any]]) -> str:
    total = len(items)
    ok = sum(1 for it in items if (it.get("result") or {}).get("ok"))
    misses = []
    for it in items:
        r = it.get("result") or {}
        if not r.get("ok"):
            misses.append(f"  - [{r.get('platform','?')}] {it.get('url')}: {r.get('reason')}")
    out = [f"Resolved {ok}/{total} thumbnails."]
    if misses:
        out.append("Missed:")
        out.extend(misses)
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# CLI for quick testing:  python resolver.py url1 url2 ...
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    test_urls = sys.argv[1:]
    if not test_urls:
        print("usage: python resolver.py <url> [<url> ...]")
        raise SystemExit(0)
    items = [{"url": u} for u in test_urls]
    n = enrich(items, deadline_seconds=60, max_workers=8)
    for it in items:
        r = it["result"]
        print(f"[{r['platform']:9}] {'OK ' if r['ok'] else 'MISS'} "
              f"{r.get('method') or '-':14} {it['url']}")
        if r["ok"]:
            print(f"            -> {it['imagePath']}")
        else:
            print(f"            !! {r['reason']}")
    print("\n" + summarize(items))
