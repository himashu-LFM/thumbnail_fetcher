#!/usr/bin/env python3
"""
extract_to_excel.py
===================
Input : a file of URLs (.csv / .xlsx / .txt) — links only is fine.
Output: an .xlsx where every URL becomes one row with:
          - the post's IMAGE embedded in the first column, and
          - its DETAILS: title, caption, author, likes, comments, views, date.

Any data cell we could NOT fetch is highlighted **light red**, so you can see at
a glance exactly what's missing (e.g. image came but caption/likes were blocked).

How it gets things
------------------
- IMAGE   -> resolver.py's ladder (yt-dlp / Twitter syndication / OpenGraph /
             TikTok-oEmbed / YouTube / Facebook-mobile / Playwright) + its
             multi-path downloader. (sir's engine — unchanged, just used.)
- DETAILS -> yt-dlp metadata (title/description/uploader/like_count/
             comment_count/view_count/upload_date), with OpenGraph and Twitter
             syndication as fallbacks for the text.

This file ADDS capability; it does not modify resolver.py or app.py.

Usage
-----
    python extract_to_excel.py links.csv
    python extract_to_excel.py links.xlsx -o out.xlsx --workers 8
    python extract_to_excel.py links.csv --cookies-from-browser chrome
    python extract_to_excel.py links.csv --limit 20
"""
from __future__ import annotations

import os
import re
import io
import csv
import sys
import argparse
import concurrent.futures

import resolver  # sir's engine, same folder

# Light-red fill for missing cells (Excel-style).
LIGHT_RED = "FFD6D6"

# Visible columns (image is embedded into column A).
COLS = ["image", "url", "platform", "status", "title", "caption", "author",
        "likes", "comments", "views", "date", "image_url", "failure_reason"]
# Cells highlighted light-red when empty (the "details" the user cares about).
HIGHLIGHT_IF_EMPTY = ["title", "caption", "author", "likes", "comments", "views", "date"]

URL_RE = re.compile(r"https?://[^\s,;'\"]+", re.I)


# --------------------------------------------------------------------------- #
# Metadata (text) extraction
# --------------------------------------------------------------------------- #
class _NullLogger:
    def debug(self, m): pass
    def info(self, m): pass
    def warning(self, m): pass
    def error(self, m): pass


def _ytdlp_meta(url):
    """Full metadata from yt-dlp (caption, author, likes, comments, views...)."""
    yt = getattr(resolver, "yt_dlp", None)
    if yt is None:
        return {}
    opts = {"skip_download": True, "quiet": True, "no_warnings": True,
            "noplaylist": True, "socket_timeout": 15, "logger": _NullLogger()}
    if getattr(resolver, "COOKIEFILE", None):
        opts["cookiefile"] = resolver.COOKIEFILE
    if getattr(resolver, "COOKIES_FROM_BROWSER", None):
        opts["cookiesfrombrowser"] = (resolver.COOKIES_FROM_BROWSER,)
    try:
        with yt.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=False)
    except Exception:
        return {}
    if not info:
        return {}
    return {
        "title": info.get("title") or "",
        "caption": info.get("description") or "",
        "author": (info.get("uploader") or info.get("channel")
                   or info.get("uploader_id") or ""),
        "likes": info.get("like_count"),
        "comments": info.get("comment_count"),
        "views": info.get("view_count"),
        "date": info.get("upload_date") or "",
        "image_url": info.get("thumbnail") or "",
    }


def _meta_tag(html, prop):
    x = re.search(rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\']'
                  rf'[^>]+content=["\']([^"\']+)["\']', html, re.I)
    if not x:
        x = re.search(rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+'
                      rf'(?:property|name)=["\']{re.escape(prop)}["\']', html, re.I)
    return x.group(1).strip() if x else ""


def _og_meta(url):
    """OpenGraph/meta text for normal web pages (and as a generic fallback)."""
    if resolver.requests is None:
        return {}
    html = None
    for verify in (True, False):
        try:
            r = resolver.requests.get(url, headers=resolver.HTTP_HEADERS,
                                      timeout=15, allow_redirects=True, verify=verify)
            if r.ok and r.text:
                html = r.text
                break
        except Exception:
            pass
    if not html:
        return {}
    return {
        "title": _meta_tag(html, "og:title") or "",
        "caption": _meta_tag(html, "og:description") or _meta_tag(html, "description") or "",
        "author": _meta_tag(html, "og:site_name") or "",
        "image_url": _meta_tag(html, "og:image") or "",
    }


def _twitter_meta(url):
    """Tweet text/author/counts via the public syndication endpoint."""
    tid = resolver._twitter_status_id(url)
    if not tid or resolver.requests is None:
        return {}
    ep = (f"https://cdn.syndication.twimg.com/tweet-result?id={tid}"
          f"&lang=en&token={resolver._twitter_syndication_token(tid)}")
    try:
        r = resolver.requests.get(ep, headers=resolver.HTTP_HEADERS, timeout=12)
        if not (r.ok and "json" in r.headers.get("content-type", "").lower()):
            return {}
        d = r.json()
    except Exception:
        return {}
    if not d:
        return {}
    u = d.get("user") or {}
    text = d.get("text") or ""
    return {
        "title": (text[:90] + ("…" if len(text) > 90 else "")),
        "caption": text,
        "author": u.get("name") or u.get("screen_name") or "",
        "likes": d.get("favorite_count"),
        "comments": d.get("conversation_count"),
        "date": d.get("created_at") or "",
    }


def _tiktok_meta(url):
    """TikTok oEmbed gives the caption (its 'title') + creator without login —
    the same endpoint that yields the thumbnail. (Likes/comments are NOT in
    oEmbed; those still need yt-dlp + cookies.)"""
    if resolver.requests is None:
        return {}
    from urllib.parse import quote
    ep = "https://www.tiktok.com/oembed?url=" + quote(url, safe="")
    try:
        r = resolver.requests.get(ep, headers=resolver.HTTP_HEADERS, timeout=12,
                                  allow_redirects=True)
        if not (r.ok and "json" in r.headers.get("content-type", "").lower()):
            return {}
        d = r.json()
    except Exception:
        return {}
    if not d:
        return {}
    title = (d.get("title") or "").strip()
    return {
        "title": (title[:90] + ("…" if len(title) > 90 else "")),
        "caption": title,
        "author": (d.get("author_name") or "").strip(),
        "image_url": d.get("thumbnail_url") or "",
    }


def fetch_details(url, platform):
    """Best-available text metadata. yt-dlp first, then a platform-specific
    fallback (Twitter syndication / TikTok oEmbed) or generic OpenGraph."""
    meta = _ytdlp_meta(url)
    if not (meta.get("caption") or meta.get("title")):
        if platform == "twitter":
            fb = _twitter_meta(url)
        elif platform == "tiktok":
            fb = _tiktok_meta(url)
        else:
            fb = _og_meta(url)
        for k, v in (fb or {}).items():
            if not meta.get(k):
                meta[k] = v
    return meta or {}


# --------------------------------------------------------------------------- #
# Per-URL: image (resolver) + details (metadata)
# --------------------------------------------------------------------------- #
def extract_one(url):
    url = (url or "").strip()
    row = {c: "" for c in COLS}
    row["image_file"] = ""          # internal: path for embedding
    row["url"] = url
    if not re.match(r"^https?://", url, re.I):
        row["status"] = "failed"
        row["failure_reason"] = "Not a valid URL (must start with http:// or https://)"
        return row

    platform = resolver.detect_platform(url)
    row["platform"] = platform

    # 1) image via the resolver ladder (also gives a thumbnail_url)
    img_reason = ""
    try:
        res = resolver.resolve_one(url)
        if res:
            row["image_file"] = res.image_path or ""
            row["image_url"] = res.thumbnail_url or ""
            if not res.ok:
                img_reason = res.reason or ""
    except Exception as e:
        img_reason = f"image error: {type(e).__name__}"

    # 2) details via metadata
    try:
        meta = fetch_details(url, platform)
    except Exception:
        meta = {}
    row["title"] = (meta.get("title") or "").strip()
    row["caption"] = (meta.get("caption") or "").strip()
    row["author"] = (meta.get("author") or "").strip()
    row["likes"] = "" if meta.get("likes") in (None, "") else str(meta.get("likes"))
    row["comments"] = "" if meta.get("comments") in (None, "") else str(meta.get("comments"))
    row["views"] = "" if meta.get("views") in (None, "") else str(meta.get("views"))
    row["date"] = (meta.get("date") or "").strip()
    if not row["image_url"]:
        row["image_url"] = (meta.get("image_url") or "").strip()

    got_image = bool(row["image_file"])
    got_text = any(row[c] for c in ["title", "caption", "author", "likes", "comments"])
    if got_image or got_text:
        row["status"] = "ok"
        # honest note when image came but text didn't (or vice versa)
        if got_image and not got_text:
            row["failure_reason"] = "image OK, but post details were blocked/unavailable"
        elif got_text and not got_image:
            row["failure_reason"] = "details OK, but image could not be fetched"
    else:
        row["status"] = "failed"
        row["failure_reason"] = img_reason or "no image or details found"
    return row


# --------------------------------------------------------------------------- #
# Input reading (links-only files are fine)
# --------------------------------------------------------------------------- #
def read_urls(path):
    name = path.lower()
    urls = []
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        for ws in wb.worksheets:
            for r in ws.iter_rows(values_only=True):
                for cell in r:
                    if cell and isinstance(cell, str):
                        urls += URL_RE.findall(cell)
    else:  # csv / tsv / txt
        raw = open(path, "rb").read().replace(b"\x00", b"").decode("utf-8-sig", errors="ignore")
        urls += URL_RE.findall(raw)
    # dedupe, preserve order
    seen, out = set(), []
    for u in urls:
        u = u.strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


# --------------------------------------------------------------------------- #
# Excel output (embedded images + light-red for missing cells)
# --------------------------------------------------------------------------- #
def write_xlsx(rows, path):
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill, Font, Alignment
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.utils import get_column_letter
    from PIL import Image as PILImage

    wb = Workbook()
    ws = wb.active
    ws.title = "extracted"
    ws.append(COLS)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F2937")

    widths = {"image": 16, "url": 42, "platform": 11, "status": 9, "title": 34,
              "caption": 48, "author": 20, "likes": 10, "comments": 10,
              "views": 10, "date": 14, "image_url": 38, "failure_reason": 40}
    for i, col in enumerate(COLS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(col, 14)

    red = PatternFill("solid", fgColor=LIGHT_RED)
    out_dir = os.path.dirname(os.path.abspath(path))
    thumbs = os.path.join(out_dir, "_xlsx_thumbs")
    os.makedirs(thumbs, exist_ok=True)
    embedded = 0

    for i, row in enumerate(rows, start=2):
        for j, col in enumerate(COLS, start=1):
            if col == "image":
                continue  # embedded below
            val = row.get(col, "")
            cell = ws.cell(row=i, column=j, value=val)
            cell.alignment = Alignment(vertical="top", wrap_text=col in ("caption", "title", "failure_reason"))
            if col in HIGHLIGHT_IF_EMPTY and not val:
                cell.fill = red   # missing detail -> light red

        # embed image in column A; if none, mark the image cell light red
        local = row.get("image_file", "")
        if local and os.path.exists(local):
            try:
                with PILImage.open(local) as im:
                    im = im.convert("RGB")
                    im.thumbnail((110, 110))
                    tp = os.path.join(thumbs, f"r{i}.png")
                    im.save(tp, "PNG")
                    w, h = im.size
                xi = XLImage(tp)
                xi.width, xi.height = w, h
                ws.add_image(xi, f"A{i}")
                ws.row_dimensions[i].height = max(h * 0.78, 24)
                embedded += 1
            except Exception:
                ws.cell(row=i, column=1).fill = red
        else:
            ws.cell(row=i, column=1).fill = red

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLS))}{len(rows) + 1}"
    wb.save(path)
    return embedded


# --------------------------------------------------------------------------- #
# Excel from LFM "cards" (used by the /lfm page's Export button)
# --------------------------------------------------------------------------- #
LFM_COLS = ["image", "url", "platform", "status", "title", "caption", "author",
            "likes", "comments", "shares", "views", "engagements", "date",
            "post_type", "format", "image_url", "failed_url", "failure_reason"]
LFM_HIGHLIGHT = ["title", "caption", "author", "likes", "comments", "shares", "views", "engagements"]


def _num_or_blank(v):
    return "" if v in (None, "") else v


def enrich_cards(cards, workers=8, deadline=180):
    """For cards that have NO caption, scrape the post URL for details
    (yt-dlp / OpenGraph / Twitter syndication) so even image-only rows get
    caption / author / likes / comments. Bounded by a wall-clock deadline."""
    todo = [c for c in cards if not c.get("caption") and c.get("post_url")]
    if not todo:
        return

    def work(c):
        try:
            c["_meta"] = fetch_details(c["post_url"], c.get("platform") or "") or {}
        except Exception:
            c["_meta"] = {}
        m = c["_meta"]
        if not c.get("caption") and m.get("caption"):
            c["caption"] = m["caption"]
        if not c.get("title") and m.get("title"):
            c["title"] = m["title"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, c) for c in todo]
        concurrent.futures.wait(futs, timeout=deadline)


def write_cards_xlsx(cards, path):
    """LFM cards -> .xlsx: image embedded in column A + full data columns
    (details from the export, or scraped _meta for rows missing them). Missing
    detail cells are light-red; failed rows carry their URL + reason last."""
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill, Font, Alignment
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.utils import get_column_letter
    from PIL import Image as PILImage

    # 1) compute every row's values first
    rowvals = []
    for card in cards:
        m = {x["label"]: x["value"] for x in (card.get("metrics") or [])}
        meta = card.get("_meta") or {}
        status = card.get("status", "")
        is_fail = status in ("failed", "timeout")
        rowvals.append({
            "url": card.get("post_url", ""),
            "platform": card.get("platform", ""),
            "status": status,
            "title": card.get("title", "") or meta.get("title", ""),
            "caption": card.get("caption", "") or meta.get("caption", ""),
            "author": card.get("brand", "") or meta.get("author", ""),
            "likes": _num_or_blank(m.get("Likes") or meta.get("likes")),
            "comments": _num_or_blank(m.get("Comments") or meta.get("comments")),
            "shares": _num_or_blank(m.get("Shares")),
            "views": _num_or_blank(m.get("Views") or m.get("Video Views") or meta.get("views")),
            "engagements": _num_or_blank(m.get("Engagements")),
            "date": card.get("date", "") or meta.get("date", ""),
            "post_type": card.get("post_type", ""),
            "format": card.get("post_format", ""),
            "image_url": card.get("thumbnail_url", "") or meta.get("image_url", ""),
            "failed_url": card.get("post_url", "") if is_fail else "",
            "failure_reason": card.get("reason", "") if is_fail else "",
            "_image_path": card.get("image_path", ""),
        })

    # 2) keep core columns always; include an optional column only if it has data
    ALWAYS = {"image", "url", "platform", "status", "caption", "author",
              "failed_url", "failure_reason"}
    active = [c for c in LFM_COLS
              if c in ALWAYS or any(rv.get(c) not in ("", None) for rv in rowvals)]

    # 3) build the sheet using the active columns
    wb = Workbook()
    ws = wb.active
    ws.title = "content"
    ws.append(active)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F2937")
    widths = {"image": 16, "url": 40, "platform": 11, "status": 10, "title": 32,
              "caption": 50, "author": 20, "date": 18, "post_type": 12,
              "format": 14, "image_url": 38, "failed_url": 40, "failure_reason": 40}
    for i, col in enumerate(active, start=1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(col, 11)
    img_col = active.index("image") + 1 if "image" in active else None

    red = PatternFill("solid", fgColor=LIGHT_RED)
    out_dir = os.path.dirname(os.path.abspath(path))
    thumbs = os.path.join(out_dir, "_xlsx_thumbs")
    os.makedirs(thumbs, exist_ok=True)
    embedded = 0
    for i, rv in enumerate(rowvals, start=2):
        for j, col in enumerate(active, start=1):
            if col == "image":
                continue
            v = rv.get(col, "")
            cell = ws.cell(row=i, column=j, value=v)
            cell.alignment = Alignment(vertical="top",
                                       wrap_text=col in ("caption", "title", "failure_reason"))
            if col in LFM_HIGHLIGHT and (v == "" or v is None):
                cell.fill = red
        local = rv.get("_image_path", "")
        if img_col and local and os.path.exists(local):
            try:
                with PILImage.open(local) as im:
                    im = im.convert("RGB")
                    im.thumbnail((110, 110))
                    tp = os.path.join(thumbs, f"l{i}.png")
                    im.save(tp, "PNG")
                    w, h = im.size
                xi = XLImage(tp)
                xi.width, xi.height = w, h
                ws.add_image(xi, f"{get_column_letter(img_col)}{i}")
                ws.row_dimensions[i].height = max(h * 0.78, 24)
                embedded += 1
            except Exception:
                ws.cell(row=i, column=img_col).fill = red
        elif img_col:
            ws.cell(row=i, column=img_col).fill = red
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(active))}{len(rowvals) + 1}"
    wb.save(path)
    return embedded


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="URLs -> Excel with embedded image + details (missing cells light-red).")
    ap.add_argument("input", help="Input file of URLs (.csv / .xlsx / .txt).")
    ap.add_argument("-o", "--output", help="Output .xlsx (default: <input>_extracted.xlsx).")
    ap.add_argument("--workers", type=int, default=8, help="Concurrent workers (default 8).")
    ap.add_argument("--limit", type=int, default=0, help="Process at most N URLs (0 = all).")
    ap.add_argument("--cookiefile", help="Netscape cookies.txt for login-gated posts.")
    ap.add_argument("--cookies-from-browser", dest="cookies_from_browser",
                    help="Browser name (chrome/edge/firefox) to read cookies from.")
    args = ap.parse_args()

    if not os.path.exists(args.input):
        sys.exit(f"Input not found: {args.input}")

    out = args.output or (os.path.splitext(args.input)[0] + "_extracted.xlsx")
    images_dir = os.path.splitext(out)[0] + "_images"
    os.makedirs(images_dir, exist_ok=True)

    # point the resolver's downloader at our images folder + cookie config
    resolver.EXPORT_DIR = os.path.abspath(images_dir)
    if args.cookiefile:
        resolver.COOKIEFILE = args.cookiefile
    if args.cookies_from_browser:
        resolver.COOKIES_FROM_BROWSER = args.cookies_from_browser

    urls = read_urls(args.input)
    if args.limit > 0:
        urls = urls[:args.limit]
    if not urls:
        sys.exit("No URLs found in the input file.")

    print(f"Input:   {args.input}")
    print(f"URLs:    {len(urls)}")
    print(f"Output:  {out}")
    print(f"Images:  {images_dir}")
    print("-" * 60)

    rows = [None] * len(urls)
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(extract_one, u): i for i, u in enumerate(urls)}
        for fut in concurrent.futures.as_completed(futs):
            i = futs[fut]
            try:
                rows[i] = fut.result()
            except Exception as e:
                rows[i] = {c: "" for c in COLS}
                rows[i].update(url=urls[i], status="failed",
                               failure_reason=f"worker error: {type(e).__name__}",
                               image_file="")
            done += 1
            if done % 10 == 0 or done == len(urls):
                ok = sum(1 for r in rows if r and r.get("status") == "ok")
                print(f"  {done}/{len(urls)}  ok={ok}")

    n = write_xlsx(rows, out)
    ok = sum(1 for r in rows if r and r.get("status") == "ok")
    print("-" * 60)
    print(f"Done. ok={ok}, failed={len(rows) - ok}, images embedded={n}")
    print(f"Excel: {out}")


if __name__ == "__main__":
    main()
