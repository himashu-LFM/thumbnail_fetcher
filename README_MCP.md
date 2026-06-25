# Data Fetcher — MCP Connector (for teammates)

This makes the hosted data-fetcher available **inside Claude as a tool**. A
teammate just asks Claude "fetch data for these URLs" and Claude calls the tool —
no browser upload, no scraping/VPN on their machine (the EC2 server does the
work).

There are **two options** — pick one:

---

## OPTION A — Plain API (simplest, no MCP)
No install at all. In **Claude Code** (or any place Claude can make web calls),
just tell Claude to call the API:

> "POST these URLs to http://54.198.245.58:5000/api/extract and show me the data"
> (add header `X-API-Key: <key>` if a key is set)

- JSON:  `POST /api/extract`  body `{"urls":[...]}`
- Excel: `POST /api/extract?format=xlsx`  → downloads an .xlsx (image embedded)

Test from a terminal:
```
curl -X POST http://54.198.245.58:5000/api/extract -H "Content-Type: application/json" -d "{\"urls\":[\"https://www.youtube.com/watch?v=dQw4w9WgXcQ\"]}"
```

---

## OPTION B — MCP connector (a built-in tool in Claude)

### Per machine, one time
1. Install deps:
   ```
   pip install mcp requests
   ```
2. Copy `mcp_server.py` onto the machine (anywhere).
3. Add it to your Claude config:

**Claude Desktop** — edit `claude_desktop_config.json`
(Windows: `%APPDATA%\Claude\claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "post-data-fetcher": {
      "command": "python",
      "args": ["C:\\path\\to\\mcp_server.py"],
      "env": {
        "THUMBNAIL_API_BASE": "http://54.198.245.58:5000",
        "THUMBNAIL_API_KEY": "yourkey-if-set"
      }
    }
  }
}
```

**Claude Code** — one command:
```
claude mcp add post-data-fetcher --env THUMBNAIL_API_BASE=http://54.198.245.58:5000 -- python C:\path\to\mcp_server.py
```

4. Restart Claude Desktop / reload Claude Code.

### Use it
In any chat:
> "Use fetch_post_data to get the data for these URLs: <paste urls>"

Claude calls the tool → the EC2 server fetches → you get the data (and
`image_file_url` links to the images). Any of your skills can use the same tool.

### Check it's working
> "Run fetcher_health"  → should say `OK (200) — http://54.198.245.58:5000`

---

## Notes
- The MCP server here is a **thin client** — it only calls the hosted API, so the
  machine needs just `mcp` + `requests` (no VPN, no scraping libs).
- **Claude Code / Claude Desktop**: this local (stdio) connector works directly.
- **Cowork**: whether you can add a custom MCP connector depends on your Cowork
  setup. If allowed, use the same `mcp_server.py`; if not, use Option A from a
  place that can reach the API.
- **Remote MCP (advanced):** instead of per-machine, host it once on the EC2:
  `python mcp_server.py --sse` (serves on `0.0.0.0:8000`), and teammates connect
  to that MCP URL. (Needs the server reachable + appropriate auth.)

## Prerequisite (server side, by the owner)
The EC2 app must be running the updated code with the `/api/extract` endpoint:
`git pull` on the server, then restart (`nohup python app.py &`).
