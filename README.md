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

One command, nothing to clone:

```
bash -c "$(curl -fsSL https://raw.githubusercontent.com/wearevolt/tie-substack/main/install.command)"
```

It asks for your publication URL (Enter accepts the default). To skip the
prompt entirely — handy for onboarding a team:

```
TIE_SUBSTACK_PUB=https://yourpub.substack.com bash -c "$(curl -fsSL https://raw.githubusercontent.com/wearevolt/tie-substack/main/install.command)"
```

Or work from a clone (uses the adjacent `server.py`, so local edits install
as-is; also works by double-clicking `install.command` in Finder):

```
git clone https://github.com/wearevolt/tie-substack
cd tie-substack && ./install.command
```

Either way the installer creates a venv in `~/.tie-substack/`, installs
`python-substack` + `pycookiecheat`, writes the publication into
`~/.tie-substack/config.json` (0600), and registers the server in Claude
Desktop's `claude_desktop_config.json`. Re-running it updates the server and
keeps your settings, including a saved cookie.

Then: **quit Claude fully (Cmd-Q) and reopen**, ask it to run
`substack_status`, then `refresh_cookie`.

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

`publication_url` must be the canonical **`https://<name>.substack.com`** URL:
`python-substack` resolves the publication with a regex that contains a literal
`https://`, so a bare host or a custom domain matches nothing. The server
normalizes a missing scheme and rejects non-Substack domains with an explicit
error (rather than the `'NoneType' object is not subscriptable` this used to
produce), and `substack_status` reports session validity and publication access
as separate checks.

## Tests

`python3 test_server.py` — offline regression checks (URL normalization, subdomain
resolution, error attribution, `/drafts` payload unwrapping, draft summaries).
Needs no cookie and makes no network calls.

## Caveats

- Unofficial API — a Substack change can break any tool here; nothing is
  destructive by default and `publish_draft` is the only immediate-publish path.
- The cookie belongs to a **user**, not a publication: use a session of the
  account that should own the posts (for TIE: the publication owner's).
- Substack Notes are out of scope for now (native Notes scheduling exists in
  the Substack UI since April 2026).
