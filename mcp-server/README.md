# Wardrowbe MCP Server

Lets an MCP client (Claude, ChatGPT, ...) act as a second interface to a running
wardrowbe instance: add garments with tags the assistant judged itself (wardrowbe's
internal Vision/LLM is skipped with `skip_ai=true`), browse the wardrobe, log wears
and washes, and author outfit suggestions. One server serves the whole family: each
person has their own token, and each token maps to that person's own wardrobe.

See [CLAUDE.md](../CLAUDE.md) for the product vision and
[docs/adr/0001-switch-base-to-wardrowbe.md](../docs/adr/0001-switch-base-to-wardrowbe.md)
for why the project is based on wardrowbe.

## Tools

| Tool | Writes? | What it does |
|---|---|---|
| `get_status` | no | Is wardrowbe up, and is its own AI on (if not, the assistant tags and styles) |
| `whoami` | no | The account this connection acts as |
| `list_items` | no | List/search items: text, type, colors, favorite, needs wash, archived |
| `get_item` | no | One item with all tags and wear counters |
| `get_wardrobe_summary` | no | Analytics: counts, most/least worn, acceptance rate, trends |
| `add_clothing_item` | yes | Add an item from image bytes (base64) with caller-judged tags |
| `add_from_product` | yes | Add an item from a product image URL (backend downloads it, SSRF-checked) + `source_url` |
| `update_item` | yes | Change details/tags; only the fields passed are changed |
| `log_wear` / `log_wash` | yes | Record a wear / a wash |
| `archive_item` / `restore_item` | yes | Hide an item / bring it back |
| `suggest_outfit` | yes | Ask wardrowbe's own AI for an outfit (needs `AI_TEXT_ENABLED`) |
| `create_outfit_suggestion` | yes | Save an outfit the assistant composed itself; works with internal AI off |
| `list_outfits` / `get_outfit` | no | Browse outfits |
| `respond_to_outfit` | yes | Accept / reject / skip an outfit (newest pending one by default) |
| `submit_outfit_feedback` | yes | Ratings, worn or not, comment |
| `list_notifications` | no | Recently sent notifications |
| `test_notification` | yes | Send a real test notification through one channel |

Every tool has a `title` and `readOnlyHint`/`destructiveHint` annotations, which the
Claude and ChatGPT directories both require. Nothing here deletes data.

Two plain-HTTP routes sit beside `/mcp`:

- `GET /health`: open liveness probe.
- `POST /upload`: multipart field `image`, same Bearer token. Creates an untagged
  item and returns its id. A phone photo is megabytes of base64, which an assistant
  cannot sensibly pass as a tool argument, so the bytes travel here and the assistant
  then tags the item with `update_item`.

## Run on the home network (Docker)

From the repository root:

```bash
# 1. In .env: the name other devices use to reach this machine, and the MCP port.
#    LAN_HOST=my-mac.local        (or the machine's LAN IP)
#    MCP_PORT=8090

# 2. One entry per family member.
cp mcp-server/users.example.json mcp-server/users.json
openssl rand -hex 32          # run once per person, paste as that person's "token"

# 3. Start everything, then create the database schema.
export COMPOSE_FILE=docker-compose.yml:docker-compose.lan.yml
docker compose up -d --build
docker compose exec backend alembic upgrade head
```

| What | Where |
|---|---|
| Web UI | `http://<LAN_HOST>:<FRONTEND_PORT>` |
| MCP endpoint | `http://<LAN_HOST>:<MCP_PORT>/mcp` |

The backend API, Postgres and Redis are bound to `127.0.0.1` only.

`users.json` is read at startup: after editing it, run `docker compose restart mcp`.
It holds secrets and is git-ignored.

### Who is who

`users.json` maps a token to an email. The server signs in to the backend as that
email through the backend's dev-mode login (`POST /api/v1/auth/sync` with
`DEBUG=true` and no OIDC). The web UI's dev login derives the account from the email
the same way, so **signing in to the web UI with the email from `users.json` shows
the same wardrobe the assistant writes to.**

### Security model, and its limit

- Every request to `/mcp` and `/upload` needs a known Bearer token; a token only
  ever reaches its own account.
- The backend's dev-mode login trusts any email. Anyone who can open the web UI can
  sign in as any family member. That is fine on a trusted home network and nowhere
  else. **Do not port-forward this setup to the internet.**
- Going public (and listing in the Claude or ChatGPT directory) needs per-user OAuth
  in front of this server and a real identity provider behind the backend. That is
  the next step in the plan, not part of this setup.

## Connect a client

Claude Code:

```bash
claude mcp add --transport http wardrowbe http://<LAN_HOST>:8090/mcp \
  --header "Authorization: Bearer <your token>"
```

Claude Desktop, and any client that only speaks stdio, through `mcp-remote`:

```json
{
  "mcpServers": {
    "wardrowbe": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://<LAN_HOST>:8090/mcp",
               "--allow-http", "--header", "Authorization: Bearer <your token>"]
    }
  }
}
```

claude.ai and ChatGPT in the browser or phone app cannot use a LAN address: their
connectors call the server from the vendor's cloud, which cannot see your network.
Those need the public, OAuth-protected deployment.

## Run over stdio (one local user, no Docker)

```bash
cd mcp-server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
claude mcp add wardrowbe \
  --env WARDROBE_API_BASE=http://127.0.0.1:8000 \
  --env WARDROBE_EMAIL=you@example.com \
  -- "$PWD/.venv/bin/python3" "$PWD/server.py"
```

Over stdio `add_clothing_item` also accepts `image_path`, a file on this machine.
Over HTTP that parameter is refused: it would let a remote caller read the server's
files.

## Configuration

| Variable | Transport | Meaning |
|---|---|---|
| `MCP_TRANSPORT` | both | `stdio` (default) or `http` |
| `WARDROBE_API_BASE` | both | Backend base URL, default `http://127.0.0.1:8000` |
| `WARDROBE_PUBLIC_URL` | both | Origin clients can open; image links in results are rewritten to it |
| `MCP_HOST` / `MCP_PORT` | http | Bind address, default `0.0.0.0:8090` |
| `MCP_USERS_FILE` | http | Path to the users JSON |
| `WARDROBE_EMAIL` | stdio | The account to act as |
| `WARDROBE_DISPLAY_NAME` | stdio | Display name for a newly created account |

## Smoke test

Runs every tool over the real protocol against a running server. It creates real
items, so point it at a throwaway user:

```bash
MCP_URL=http://<LAN_HOST>:8090 MCP_TOKEN=<token> MCP_TOKEN_2=<another user's token> \
  .venv/bin/python smoke_test.py photo1.png photo2.png
```

## Credits

The wear/wash/outfit/analytics/notification tools follow the tool surface of
[saya6k/mcp-wardrowbe](https://github.com/saya6k/mcp-wardrowbe) (MIT), rewritten
against this fork's API. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
