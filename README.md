# tie-substack

A local MCP server that lets Claude (Cowork / Desktop / Code) **create Substack
drafts, pin their URL slug, and schedule publication** — the missing "Substack
leg" of the [tie-social](https://github.com/wearevolt/tie-social) campaign
pipeline. With the slug pinned at draft time, the public URL
(`https://<publication>/p/<slug>`) is known **before** the piece is live, so
social posts scheduled in Zernio can carry the real link instead of a guessed
one.

Substack has no official write API. This server wraps the community
[`python-substack`](https://github.com/ma2za/python-substack) library
(Substack's internal endpoints) — functional for years, but an unofficial
integration: endpoints may change, and scheduling more than 3 months out is
not supported by Substack.

## How auth works (and why Claude never sees the cookie)

Auth is your browser's Substack session cookie (`substack.sid`). It lives only
in `~/.tie-substack/config.json` (file mode 0600) on your machine:

- **`refresh_cookie`** pulls it straight from your local Chrome/Chromium/Brave/
  Firefox profile via [pycookiecheat](https://github.com/n8henrie/pycookiecheat)
  (macOS asks for Keychain access) and writes it into the config. Tool output
  lists cookie *names* only — the value is never displayed, returned to the
  model, or logged.
- Alternatively, paste the value into the config file yourself (DevTools →
  Application → Cookies → `substack.sid`). Never paste it into a chat.

The cookie is a full-access credential for the Substack account that owns the
session — treat the config file like a password. Logging out of Substack in
that browser (or changing the password) invalidates it; `substack_status`
detects a dead cookie and points at `refresh_cookie`.

## Install (macOS)

```
git clone https://github.com/wearevolt/tie-substack
cd tie-substack && ./install.command
```

The installer creates a venv in `~/.tie-substack/`, installs
`python-substack` + `pycookiecheat`, asks for your publication URL, and
registers the server in Claude Desktop's `claude_desktop_config.json`.
Then: restart Claude, ask it to run `substack_status`, then `refresh_cookie`.

## Tools

| Tool | What it does |
|---|---|
| `substack_status` | Cookie configured/valid? Logged-in user, publication, deps. |
| `refresh_cookie` | Pull session cookies from the local browser into the config (names-only output). |
| `create_draft` | Draft from Markdown with **slug pinned** → `draft_id`, `slug`, `post_url`, `editor_url`. |
| `set_slug` | Change an existing draft's slug. |
| `schedule_draft` | Schedule publication at an exact ISO instant (offset required — naive timestamps rejected). |
| `unschedule_draft` | Cancel a scheduled publication. |
| `get_draft` / `list_drafts` | Inspect drafts (title, slug, post_url, scheduled_for). |
| `publish_draft` | Publish **now** (optional email) — explicit user request only. |
| `delete_draft` | Delete a draft. |

## The intended flow (tie-social)

1. At the campaign's strategy checkpoint the slug is decided.
2. `create_draft` (title + slug + placeholder or full Markdown body) →
   `post_url` is now real.
3. Human pastes/polishes the final text in the Substack editor (`editor_url`).
4. `schedule_draft` for publish date D at the agreed time.
5. Social posts scheduled in Zernio use `post_url` — no guessed links.

## Env overrides

| Variable | Meaning |
|---|---|
| `TIE_SUBSTACK_CONFIG` | Config file path (default `~/.tie-substack/config.json`). |
| `SUBSTACK_PUBLICATION_URL` | Publication URL (wins over config). |
| `SUBSTACK_SESSION_TOKEN` | `substack.sid` value (wins over config; for CI-style setups). |

## Caveats

- Unofficial API — a Substack change can break any tool here; nothing is
  destructive by default and `publish_draft` is the only immediate-publish path.
- The cookie belongs to a **user**, not a publication: use a session of the
  account that should own the posts (for TIE: the publication owner's).
- Substack Notes are out of scope for now (native Notes scheduling exists in
  the Substack UI since April 2026).
