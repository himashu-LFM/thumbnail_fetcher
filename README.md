# Social Thumbnail Fetcher

Best-effort `social post URL → local thumbnail image` resolver, with a simple
Flask test UI. Supports YouTube, Instagram, TikTok, Twitter/X, Threads, Facebook.

## Quick start

```bash
pip install -r requirements.txt
python app.py
# open http://127.0.0.1:5000
```

Optional last-resort headless browser (for Threads / X / some Facebook):

```bash
pip install playwright
python -m playwright install chromium
```

## How it works — resolution ladder (stops at first success)

1. **yt-dlp** metadata-only (`skip_download=True`). Best for YouTube + most video.
2. **OpenGraph / meta scrape** (`og:image`, `og:image:url`, `twitter:image`,
   `twitter:image:src`). Rescues Instagram single-image / photo posts.
3. **Platform endpoints**: TikTok `oembed` (JSON-only guard for geo redirects);
   deterministic `i.ytimg.com` URL for YouTube.
4. **Headless browser** (Playwright/Chromium) for login/JS-gated pages.

Then a **multi-path downloader** (requests → requests `verify=False` → httpx+certifi
both `trust_env` → urllib) saves the bytes, with extension from URL suffix or magic
bytes. Everything is best-effort — one bad URL never crashes the batch, and the whole
batch is bounded by a wall-clock deadline (default 60s) via a `ThreadPoolExecutor`.

## Module API (`resolver.py`)

```python
resolver.resolve_one(url) -> ResolveResult     # full ladder + download + cache
resolver.enrich(items, deadline_seconds=60, max_workers=8) -> int
resolver.summarize(items) -> str
```

`enrich` sets `item["imagePath"]` and `item["result"]` in place. CLI:

```bash
python resolver.py "https://youtu.be/..." "https://www.instagram.com/p/..."
```

## Configuration (env vars — no hardcoded secrets)

| Var | Purpose |
|-----|---------|
| `THUMBNAIL_EXPORT_DIR` | where images are written (default: temp dir) |
| `THUMBNAIL_COOKIEFILE` | Netscape `cookies.txt` for login-gated posts |
| `THUMBNAIL_COOKIES_FROM` | browser name for yt-dlp `cookiesfrombrowser` |
| `THUMBNAIL_PER_URL_TIMEOUT` | per-URL soft timeout (s) |

## Platform reality (honest limits)

- **YouTube** → yt-dlp, reliable.
- **Instagram** → public single posts via og:image (reliable); private posts need
  cookies; full carousels need the headless DOM scrape (not enabled by default).
- **TikTok** → oEmbed endpoint. **Geo-restricted**: from a blocked region every
  method 302-redirects to a regional info page and returns no image. This is a
  network/region limit, not a code bug — verify from an unrestricted (e.g. US) host.
- **Twitter/X, Threads, Facebook** → need the Playwright rung and often cookies.

The results page footer reports `resolved/total` and, for each miss, *why* it missed
(code gap vs. network/auth/region).
