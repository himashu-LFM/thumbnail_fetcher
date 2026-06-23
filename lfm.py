"""
lfm.py
======
Ingest a ListenFirst Media (LFM) platform export and turn each row into a
"post card" that mirrors the platform's own Content view.

Why this exists
---------------
The URLs we were scraping are themselves *exports of the LFM platform*, which is
the source of truth. The export already carries the post's thumbnail URL plus all
the metrics shown on the platform card -- so instead of fighting external
networks (blocks, geo, login walls), we read the thumbnail + metrics straight
from the export. External scraping (resolver.py) is kept only as a fallback for
any row missing a thumbnail URL.

Public surface
--------------
    parse_export(file_bytes, filename) -> (cards: list[dict], mapping: dict)
    build_cards(file_bytes, filename, deadline=60, workers=8) -> (cards, mapping, summary)

The column mapping is auto-detected with fuzzy header matching and returned so
the caller (and the user) can see exactly which export column fed which field.
"""

from __future__ import annotations

import io
import csv
import re
import os
import logging
import concurrent.futures
from typing import Optional, List, Dict, Any, Tuple

import resolver

log = logging.getLogger("lfm")


# --------------------------------------------------------------------------- #
# Header matching
# --------------------------------------------------------------------------- #
def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


# field -> ordered candidate header fragments (most specific first).
# Matched by exact-equality first, then "header contains candidate".
FIELD_CANDIDATES: Dict[str, List[str]] = {
    "thumbnail_url": ["thumbnailurl", "thumbnail", "imageurl", "postimageurl",
                      "postimage", "mediaurl", "mediathumbnail", "picture",
                      "imagelink", "thumb", "image"],
    "post_url": ["posturl", "permalink", "postlink", "contenturl", "posturlfull",
                 "url", "link"],
    "platform": ["channeltype", "socialnetwork", "channel", "platform", "network",
                 "source"],
    # author/nickname cover listening exports (YouScan); brand/account cover LFM.
    "brand": ["author", "nickname", "displayname", "username", "brandname",
              "accountname", "profilename", "pagename", "brand", "account",
              "profile", "property", "page"],
    # badge 1 (media kind): "Content Types" (YouScan) or "Post Type" (LFM).
    "post_type": ["contenttypes", "contenttype", "mediatype", "posttype", "type"],
    # badge 2 (format): "Format" (LFM) or "Source specific format" (YouScan).
    "post_format": ["postformat", "format", "subtype", "posttypedetail",
                    "sourcespecificformat"],
    # primary caption = the post body. Title/description are a fallback
    # (caption_alt) because in listening exports tweets put the body in "Text"
    # while videos put it in "Title".
    "caption": ["postcaption", "postmessage", "posttext", "caption", "message",
                "text", "content", "body"],
    "caption_alt": ["title", "headline", "description"],
    "date": ["publisheddate", "publishdate", "postdate", "publishtime",
             "posttime", "createdtime", "datetime", "publishedtime",
             "published", "date", "timestamp"],
}

# Metric label -> (ordered candidates, is_percent). Order = display order on card.
METRIC_FIELDS: List[Tuple[str, List[str], bool]] = [
    ("Engagements",         ["engagements", "engagement", "totalengagements"], False),
    ("Reactions",           ["reactions", "reaction"], False),
    ("Likes",               ["likes", "love"], False),
    ("Comments",            ["comments", "comment"], False),
    ("Shares",              ["shares", "share", "reshares", "reposts", "repost"], False),
    ("Views",               ["views"], False),
    ("Video Views",         ["videoviews"], False),
    ("Response Rate",       ["responserate"], True),
    ("Video Response Rate", ["videoresponserate"], True),
]


def _match_columns(headers: List[str]) -> Dict[str, int]:
    """Map each target field to a column index. Specific fields claim first so
    e.g. 'thumbnail url' is taken before generic 'url' can grab it."""
    norm = [_norm(h) for h in headers]
    used: set = set()
    mapping: Dict[str, int] = {}

    def claim(field: str, candidates: List[str]):
        # 1) exact equality
        for cand in candidates:
            for i, h in enumerate(norm):
                if i in used:
                    continue
                if h == cand:
                    mapping[field] = i
                    used.add(i)
                    return
        # 2) header contains candidate (longest candidate wins via order)
        for cand in candidates:
            for i, h in enumerate(norm):
                if i in used:
                    continue
                if cand in h:
                    mapping[field] = i
                    used.add(i)
                    return

    for field, cands in FIELD_CANDIDATES.items():
        claim(field, cands)
    for label, cands, _pct in METRIC_FIELDS:
        claim("metric::" + label, cands)
    return mapping


def _find_header_row(rows: List[List[Any]]) -> int:
    """LFM exports sometimes carry preamble rows. Pick the early row that
    recognises the most known columns."""
    best_i, best_score = 0, -1
    for i, row in enumerate(rows[:12]):
        if not row:
            continue
        m = _match_columns([str(c) for c in row])
        score = len(m)
        if score > best_score:
            best_score, best_i = score, i
    return best_i


# --------------------------------------------------------------------------- #
# Reading the file
# --------------------------------------------------------------------------- #
def _read_rows(file_bytes: bytes, filename: str) -> List[List[Any]]:
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
        ws = wb.active
        return [list(r) for r in ws.iter_rows(values_only=True)]
    # csv / tsv / txt
    text = file_bytes.decode("utf-8", errors="ignore")
    delim = "\t" if (name.endswith(".tsv") or text.count("\t") > text.count(",")) else ","
    return [row for row in csv.reader(io.StringIO(text), delimiter=delim)]


def _fmt_number(v: Any) -> Optional[str]:
    if v is None or v == "":
        return None
    s = str(v).strip()
    if s.lower() in ("n/a", "na", "-", "none"):
        return None
    # strip existing commas / %, keep number
    raw = s.replace(",", "").replace("%", "").strip()
    try:
        f = float(raw)
    except ValueError:
        return s  # non-numeric, show as-is
    if f == int(f):
        return f"{int(f):,}"
    return f"{f:,.2f}"


def _fmt_percent(v: Any) -> Optional[str]:
    n = _fmt_number(v)
    if n is None:
        return None
    return n if n.endswith("%") else f"{n}%"


def _platform_from(value: Any, post_url: str) -> str:
    v = _norm(value)
    table = {
        "facebook": "facebook", "fb": "facebook",
        "instagram": "instagram", "ig": "instagram",
        "twitter": "twitter", "x": "twitter",
        "tiktok": "tiktok",
        "youtube": "youtube", "yt": "youtube",
        "threads": "threads",
    }
    for key, plat in table.items():
        if key in v:
            return plat
    # fall back to URL detection
    return resolver.detect_platform(post_url or "")


# --------------------------------------------------------------------------- #
# Parse export -> cards
# --------------------------------------------------------------------------- #
def parse_export(file_bytes: bytes, filename: str,
                 max_rows: Optional[int] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    rows = _read_rows(file_bytes, filename)
    if not rows:
        return [], {"error": "empty file"}

    hidx = _find_header_row(rows)
    headers = [str(c) if c is not None else "" for c in rows[hidx]]
    colmap = _match_columns(headers)

    # human-readable mapping report
    mapping_report = {
        field: (headers[idx] if idx is not None else None)
        for field, idx in colmap.items()
    }

    def cell(row, field):
        idx = colmap.get(field)
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    cards: List[Dict[str, Any]] = []
    for rank, row in enumerate(rows[hidx + 1:], start=1):
        if not row or all((c is None or str(c).strip() == "") for c in row):
            continue
        post_url = (str(cell(row, "post_url") or "")).strip()
        thumb = (str(cell(row, "thumbnail_url") or "")).strip()
        # skip rows that are clearly not posts (no url and no thumbnail)
        if not post_url and not thumb:
            continue

        platform = _platform_from(cell(row, "platform"), post_url)

        post_type = (str(cell(row, "post_type") or "")).strip()
        ptl = post_type.lower()
        has_media_kind = any(k in ptl for k in ("video", "image", "photo",
                                                "gallery", "carousel", "album"))
        is_text_only = bool(ptl) and not has_media_kind

        metrics = []
        for label, _cands, is_pct in METRIC_FIELDS:
            raw = cell(row, "metric::" + label)
            val = _fmt_percent(raw) if is_pct else _fmt_number(raw)
            if val is not None:
                metrics.append({"label": label, "value": val,
                                "highlight": label == "Engagements"})

        cards.append({
            "rank": rank,
            "brand": (str(cell(row, "brand") or "")).strip() or "—",
            "platform": platform,
            "date": (str(cell(row, "date") or "")).strip(),
            "post_url": post_url,
            "thumbnail_url": thumb or None,
            "post_type": post_type,
            "post_format": (str(cell(row, "post_format") or "")).strip(),
            "is_text_only": is_text_only,
            "caption": ((str(cell(row, "caption") or "")).strip()
                        or (str(cell(row, "caption_alt") or "")).strip()),
            "metrics": metrics,
            "image_src": None,      # filled by build_cards after download
            "resolved_via": None,
            "status": "pending",    # export | scraped | no_media | failed | timeout
            "reason": "",
        })
        if max_rows and len(cards) >= max_rows:
            break

    return cards, {"header_row": hidx + 1, "columns": mapping_report,
                   "unmapped_headers": [h for i, h in enumerate(headers)
                                        if i not in colmap.values() and h]}


# --------------------------------------------------------------------------- #
# Download thumbnails (export URL first, scraper as fallback)
# --------------------------------------------------------------------------- #
def _ok(card, path, via, status):
    card["image_path"] = path
    card["image_src"] = os.path.basename(path)
    card["resolved_via"] = via
    card["status"] = status
    return card


def _fetch_thumb(card: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a single card's thumbnail. Best case: the export's Image URL.
    Worst case: no/empty/dead export URL -> scrape the post URL via the full
    resolver ladder. Always records a status + honest reason; never raises."""
    try:
        # 1) best case -- the export's own thumbnail URL (source of truth)
        if card.get("thumbnail_url"):
            path = resolver._save_image(card["post_url"] or card["thumbnail_url"],
                                        card["thumbnail_url"])
            if path:
                return _ok(card, path, "export", "export")
            log.info("export thumb URL dead, scraping post URL instead: %s",
                     str(card["thumbnail_url"])[:80])

        # 2) text-only posts genuinely have no image -- don't waste a scrape
        if card.get("is_text_only"):
            card["status"] = "no_media"
            card["resolved_via"] = "text post — no media"
            return card

        # 3) worst case -- scrape the external post URL via the full ladder
        if card.get("post_url"):
            res = resolver.resolve_one(card["post_url"])
            if res.ok and res.image_path:
                return _ok(card, res.image_path,
                           "scraped:" + (res.method or "scrape"), "scraped")
            # honest failure reason from the resolver (region/auth/code gap)
            card["status"] = "failed"
            card["resolved_via"] = "scrape failed"
            card["reason"] = res.reason or "no thumbnail found by any method"
            return card

        # no thumbnail URL and no post URL -> nothing we can do
        card["status"] = "failed"
        card["resolved_via"] = "no image"
        card["reason"] = "row has neither an image URL nor a post URL"
        return card
    except Exception as e:                              # belt-and-braces
        card["status"] = "failed"
        card["resolved_via"] = "error"
        card["reason"] = f"unexpected error: {e!r}"
        return card


def build_cards(file_bytes: bytes, filename: str,
                deadline: float = 60, workers: int = 8,
                max_rows: Optional[int] = None):
    cards, mapping = parse_export(file_bytes, filename, max_rows=max_rows)
    has_thumb_col = bool(mapping.get("columns", {}).get("thumbnail_url"))
    rows_with_thumb = sum(1 for c in cards if c.get("thumbnail_url"))
    mode = ("export thumbnails" if rows_with_thumb else "SCRAPE-ONLY (worst case)")
    log.info("LFM export: %d card(s), header row %s, mode=%s, thumb-col=%s",
             len(cards), mapping.get("header_row"), mode,
             mapping["columns"].get("thumbnail_url") or "NONE")
    if not has_thumb_col:
        log.warning("No Image/Thumbnail URL column detected -- every media post "
                    "will be scraped from its post URL (slower, may be blocked).")

    # Worst-case guard: scraping is slow, so give it a per-card soft cap and let
    # the batch deadline cancel the long tail rather than hang.
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_thumb, c): c for c in cards}
        done, not_done = concurrent.futures.wait(futs, timeout=deadline)
        for f in not_done:
            f.cancel()
            c = futs[f]
            if c.get("status") in (None, "pending"):
                c["status"] = "timeout"
                c["resolved_via"] = "timed out"
                c["reason"] = f"exceeded the {deadline:.0f}s batch deadline"

    # honest breakdown
    n = len(cards)
    by = {"export": 0, "scraped": 0, "no_media": 0, "failed": 0, "timeout": 0}
    for c in cards:
        by[c.get("status", "failed")] = by.get(c.get("status", "failed"), 0) + 1
    got = by["export"] + by["scraped"]
    parts = [f"{n} posts ({mode})", f"{got} thumbnails resolved"]
    if by["export"]:   parts.append(f"{by['export']} from export")
    if by["scraped"]:  parts.append(f"{by['scraped']} scraped (fallback)")
    if by["no_media"]: parts.append(f"{by['no_media']} text/no-media")
    if by["failed"]:   parts.append(f"{by['failed']} failed")
    if by["timeout"]:  parts.append(f"{by['timeout']} timed out")
    summary = " · ".join(parts) + "."
    log.info("BATCH summary: %s", summary)
    return cards, mapping, summary
