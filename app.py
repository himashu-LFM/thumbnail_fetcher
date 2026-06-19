"""
app.py -- simple test UI for the thumbnail resolver.

Input methods on the home page:
  * paste URLs directly (one per line)
  * upload a .txt file (one URL per line)
  * upload a .xlsx / .csv file (every cell that looks like a URL is collected)

Results page shows, per URL: detected platform, the thumbnail, the method that
won, and a Download button. A summary footer reports resolved/total and why
each miss missed (code gap vs. network/auth/region limitation).

Run:  python app.py     ->  http://127.0.0.1:5000
"""

from __future__ import annotations

import os
import re
import io
import sys
import logging
import asyncio
from flask import (
    Flask, request, render_template, send_file, abort, url_for, jsonify
)

# --- terminal logging: show the resolver working in real time -------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
# keep Flask's own request logging quieter so resolver logs stand out
logging.getLogger("werkzeug").setLevel(logging.WARNING)

import resolver
import lfm

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB uploads

URL_RE = re.compile(r"https?://[^\s,;'\"]+", re.I)


# --------------------------------------------------------------------------- #
# URL extraction from the three input methods
# --------------------------------------------------------------------------- #
def _urls_from_text(text: str):
    return URL_RE.findall(text or "")


def _urls_from_txt(file_storage):
    raw = file_storage.read().decode("utf-8", errors="ignore")
    return _urls_from_text(raw)


def _urls_from_spreadsheet(file_storage, filename):
    name = filename.lower()
    data = file_storage.read()
    urls = []
    if name.endswith(".csv"):
        text = data.decode("utf-8", errors="ignore")
        urls = _urls_from_text(text)
    elif name.endswith((".xlsx", ".xlsm")):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    for cell in row:
                        if cell and isinstance(cell, str):
                            urls.extend(_urls_from_text(cell))
        except Exception:
            pass
    return urls


def _dedupe(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _collect_urls(req) -> list:
    urls = []
    urls += _urls_from_text(req.form.get("urls", ""))
    f = req.files.get("file")
    if f and f.filename:
        name = f.filename.lower()
        if name.endswith(".txt"):
            urls += _urls_from_txt(f)
        elif name.endswith((".csv", ".xlsx", ".xlsm")):
            urls += _urls_from_spreadsheet(f, name)
        else:
            # unknown extension: try as plain text
            try:
                urls += _urls_from_txt(f)
            except Exception:
                pass
    return _dedupe([u.strip() for u in urls if u and u.strip()])


# --------------------------------------------------------------------------- #
# Blocking build, run off the event loop
# --------------------------------------------------------------------------- #
def _build(items, deadline, workers):
    resolver.enrich(items, deadline_seconds=deadline, max_workers=workers)
    return items


async def _build_async(items, deadline, workers):
    return await asyncio.to_thread(_build, items, deadline, workers)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/resolve", methods=["POST"])
def resolve():
    urls = _collect_urls(request)
    if not urls:
        return render_template("index.html",
                               error="No URLs found. Paste links or upload a "
                                     ".txt / .csv / .xlsx file.")
    try:
        deadline = float(request.form.get("deadline", "60"))
    except ValueError:
        deadline = 60.0

    items = [{"url": u} for u in urls]
    # Run the blocking ladder off any event loop, time-bounded inside enrich().
    asyncio.run(_build_async(items, deadline, 8))

    # attach a stable id + served-image url for the template
    rows = []
    for i, it in enumerate(items):
        r = it.get("result") or {}
        rows.append({
            "idx": i,
            "url": it["url"],
            "platform": r.get("platform", "unknown"),
            "ok": r.get("ok", False),
            "method": r.get("method"),
            "reason": r.get("reason", ""),
            "thumbnail_url": r.get("thumbnail_url"),
            "image_path": it.get("imagePath"),
            "img_src": url_for("image", token=os.path.basename(it["imagePath"]))
                       if it.get("imagePath") else None,
        })

    total = len(rows)
    ok = sum(1 for x in rows if x["ok"])
    summary = resolver.summarize(items)
    return render_template("results.html", rows=rows, total=total, ok=ok,
                           summary=summary)


@app.route("/lfm", methods=["POST"])
def lfm_export():
    """Primary path: ingest an LFM platform export and render the exact
    platform cards, using the export's own thumbnail URLs (source of truth)."""
    f = request.files.get("file")
    if not f or not f.filename:
        return render_template("index.html",
                               error="Please choose an LFM export (.xlsx or .csv).")
    try:
        deadline = float(request.form.get("deadline", "60"))
    except ValueError:
        deadline = 60.0

    data = f.read()
    try:
        cards, mapping, summary = asyncio.run(
            asyncio.to_thread(lfm.build_cards, data, f.filename, deadline, 8)
        )
    except Exception as e:
        return render_template("index.html",
                               error=f"Could not read that export: {e}")
    if not cards:
        return render_template(
            "index.html",
            error="No post rows detected in that file. Is it an LFM Content "
                  "export? Detected mapping: " + str(mapping.get("columns")))
    return render_template("lfm_results.html", cards=cards, summary=summary,
                           mapping=mapping)


@app.route("/demo")
def demo():
    """Render the bundled sample LFM export as cards (for previewing the UI)."""
    p = os.path.join(os.path.dirname(__file__), "samples", "lfm_export_sample.csv")
    with open(p, "rb") as fh:
        data = fh.read()
    cards, mapping, summary = lfm.build_cards(data, "lfm_export_sample.csv", 60, 8)
    return render_template("lfm_results.html", cards=cards, summary=summary,
                           mapping=mapping)


@app.route("/image/<token>")
def image(token):
    """Serve a resolved thumbnail from the export dir (inline)."""
    # token is just a basename; constrain it to the export dir.
    safe = os.path.basename(token)
    path = os.path.join(resolver.EXPORT_DIR, safe)
    if not os.path.exists(path):
        abort(404)
    ext = os.path.splitext(safe)[1].lower()
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".webp": "image/webp", ".gif": "image/gif"}.get(ext)
    return send_file(path, mimetype=mime)


@app.route("/download/<token>")
def download(token):
    safe = os.path.basename(token)
    path = os.path.join(resolver.EXPORT_DIR, safe)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=safe)


@app.route("/api/resolve", methods=["POST"])
def api_resolve():
    """JSON API: {"urls": [...]} -> resolved results."""
    payload = request.get_json(silent=True) or {}
    urls = _dedupe(payload.get("urls", []))
    items = [{"url": u} for u in urls]
    asyncio.run(_build_async(items, float(payload.get("deadline", 60)), 8))
    return jsonify({
        "resolved": sum(1 for it in items if (it.get("result") or {}).get("ok")),
        "total": len(items),
        "items": [it.get("result") for it in items],
    })


if __name__ == "__main__":
    print(f"Export dir: {resolver.EXPORT_DIR}")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
