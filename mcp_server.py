"""
mcp_server.py
=============
An MCP server that exposes the hosted data-fetcher as a Claude TOOL.

It is a THIN CLIENT: it just calls the hosted API (`/api/extract`) on the EC2
server, which does the actual fetching (internet + cookies live there). So a
teammate's machine needs NO VPN/scraping setup — only this small file.

Tool exposed to Claude:
    fetch_post_data(urls, deadline=120) -> list of dicts, one per URL:
        { url, platform, status, title, caption, author, likes, comments,
          views, date, image_url, image_file_url, failure_reason }

------------------------------------------------------------------------------
SETUP (per machine, one time)
------------------------------------------------------------------------------
1. Install deps:           pip install mcp requests
2. Set where the API is:   env THUMBNAIL_API_BASE = http://54.198.245.58:5000
   (and THUMBNAIL_API_KEY if the API requires a key)
3. Add to Claude Desktop / Claude Code MCP config (see README_MCP.md).

RUN MODES
- Local (stdio) — what Claude Desktop/Code launches automatically:
      python mcp_server.py
- Remote (SSE/HTTP) — host it once on the EC2 server, teammates connect by URL:
      python mcp_server.py --sse        # serves on 0.0.0.0:8000
"""
import os
import sys
import requests

from mcp.server.fastmcp import FastMCP

# Where the hosted fetcher API lives (the EC2 server).
API_BASE = os.environ.get("THUMBNAIL_API_BASE", "http://54.198.245.58:5000").rstrip("/")
API_KEY = os.environ.get("THUMBNAIL_API_KEY") or ""

mcp = FastMCP("post-data-fetcher")


@mcp.tool()
def fetch_post_data(urls: list[str], deadline: int = 120) -> list[dict]:
    """Fetch post data for social/web URLs (Instagram, TikTok, YouTube,
    Twitter/X, Facebook, blogs, etc.). For each URL returns its image link,
    caption, title, author, likes, comments, views and date.

    Args:
        urls: list of post URLs to fetch.
        deadline: max seconds to wait for the whole batch.

    Returns:
        A list of dicts (one per URL) with keys: url, platform, status, title,
        caption, author, likes, comments, views, date, image_url,
        image_file_url (a stable link to the downloaded image), failure_reason.
    """
    if not urls:
        return []
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    try:
        r = requests.post(
            f"{API_BASE}/api/extract",
            json={"urls": urls, "deadline": deadline},
            headers=headers,
            timeout=deadline + 30,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return [{"url": u, "status": "failed",
                 "failure_reason": f"could not reach fetcher API: {type(e).__name__}: {e}"}
                for u in urls]
    return data.get("items", [])


@mcp.tool()
def fetcher_health() -> str:
    """Quick check that the hosted fetcher API is reachable."""
    try:
        r = requests.get(API_BASE + "/", timeout=15)
        return f"OK ({r.status_code}) — {API_BASE}"
    except Exception as e:
        return f"NOT reachable: {type(e).__name__}: {e}"


if __name__ == "__main__":
    if "--sse" in sys.argv:
        # Remote transport: host this on the EC2 server; teammates connect by URL.
        mcp.settings.host = os.environ.get("MCP_HOST", "0.0.0.0")
        mcp.settings.port = int(os.environ.get("MCP_PORT", "8000"))
        mcp.run(transport="sse")
    else:
        # Local transport: Claude Desktop / Claude Code launches this per machine.
        mcp.run()
