# tie-substack

A local MCP server that lets Claude (Cowork / Desktop / Code) **create Substack drafts, pin
their URL slug, and schedule publication** for **several client publications from one
server**: the "Substack leg" of the [tie-social](https://github.com/wearevolt/tie-social)
campaign pipeline. With the slug pinned at draft time, the public URL
(`https://<publication>/p/<slug>`) is known **before** the piece is live, so social posts
scheduled in Zernio carry the real link instead of a guessed one.

Substack has no official write API. This server wraps the community
[`python-substack`](https://github.com/ma2za/python-substack) library (Substack's internal
endpoints): functional for years, but an unofficial integration; endpoints may change, and
scheduling more than 3 months out is not supported by Substack.

## The client contract (0.5.0)

- **Every Substack-facing tool takes `client`**: the brand slug the tie-social plugin uses
  (`brands/<slug>/brand.json` → `substack.client`). There is no "current client"; a missing
  or unknown client is refused with the configured list.
- **Every publication-scoped tool also takes `expected_publication`** (the brand's
  `publication.domain`). Before the handler runs, the server compares the canonical host of
  that value, the registry's publication for the client, and the publication the live
  session actually reaches. Any mismatch refuses the call and names all three. Every
  response echoes `client`, `publication`, `act_as` and `logged_in_as`.
- **Identity binding.** A client is usable only when its session reaches its publication
  AND matches the pinned `act_as` identity; reads follow the same rule. An unbound client
  is refused until `bind_client` runs.
- **Three tool scopes, three facades.** `registry` tools are local only and get no API;
  `probe` tools get a facade that can only look at identity and access; `publication` tools
  get the full client, and only after the check above. A test fails when a tool lacks a
  scope or crosses it.
- **Sessions switch per call.** Each client's session is validated and cached briefly; an
  expired one is re-read once from the client's bound cookie source, then the call fails
  with the fix named. Interleaved calls for different clients in one chat are safe.

## How auth works (and why Claude never sees a cookie)

Auth is a browser's Substack session cookie (`substack.sid`), one per client. Sessions live
only in `~/.tie-substack/sessions/<client>.json` (file mode 0600); the registry
`~/.tie-substack/clients.json` (0600) holds no secrets, only the publication, the bound
cookie source and the pinned identity.

- `bind_client` reads the cookie straight from a local Chrome-family profile or a dedicated
  browser via [pycookiecheat](https://github.com/n8henrie/pycookiecheat) (macOS asks for
  Keychain access), validates it against the client's publication, stores it and pins the
  login as `act_as`. Tool output lists cookie *names* only; the value is never displayed,
  returned to the model, or logged.
- `refresh_session` re-reads the bound source after a logout or expiry; it refuses when the
  source is now another login (nothing stored).

The cookie is a full-access credential for the Substack account that owns the session:
treat the session files like passwords.

## Install (macOS)

```
bash -c "$(curl -fsSL https://raw.githubusercontent.com/wearevolt/tie-substack/main/install.command)"
```

Or from a clone (`./install.command`, also by double-click). The installer creates a venv in
`~/.tie-substack/`, installs `python-substack` + `pycookiecheat`, **migrates any 0.4 configs**
(below), verifies every client live, and registers **one** server entry (`tie-substack`) in
Claude Desktop's `claude_desktop_config.json`. Then quit Claude fully (Cmd-Q), reopen, and
ask it to run `clients_status`.

Adding a client needs no reinstall: in a chat, `add_client(client, publication_url)`, then
`bind_client(client, …)`.

## Tools

| Scope | Tool | What it does |
|---|---|---|
| registry | `list_clients` | Configured clients: publication, cookie source, pinned identity, session stored?, bound?. Local only. |
| registry | `add_client` | Register a client under its brand slug with its `https://<name>.substack.com` URL. |
| registry | `list_browser_profiles` | Chrome-family profiles (directory, display name, email) from the plaintext Local State, plus the dedicated browsers clients are bound to. No cookies, no Keychain prompt. |
| registry | `open_client_browser` | OPTIONAL. Launch a client's dedicated browser (its bound `user_data_dir`) for a one-time login. |
| probe | `clients_status` | Per client: bound?, session valid / expired / missing, identity matches?, publication reachable?, `logged_in_as`. The readiness signal (`--check-clients` in the installer). |
| probe | `bind_client` | Bind a cookie source (`profile`, `user_data_dir` or `cookie_file`; with none, scan the browser's standard profiles and accept exactly ONE login that reaches the publication) and pin `act_as`. Switching to a different identity needs `confirm_switch`. |
| probe | `refresh_session` | Re-read the bound source, validate against publication and pin, store. |
| publication | `get_publication_settings` | Paid subscriptions?, sections, existing tags, unavailable values. Call before offering choices. |
| publication | `create_draft` | Draft from Markdown with the **slug pinned** + explicit settings → `draft_id`, `slug`, `post_url`, `editor_url`, settings **as stored**. |
| publication | `update_post_settings` / `apply_tags` / `set_slug` | Fix settings, attach tags (new ones only with `allow_new`), change the slug. |
| publication | `schedule_draft` / `unschedule_draft` | Schedule at an exact ISO instant (offset required; refuses without `confirm_send_email` when the post emails); cancel. |
| publication | `get_draft` / `list_drafts` | Inspect drafts (`list_drafts` is a narrower projection; its notes say what is absent). |
| publication | `publish_draft` / `delete_draft` | Publish **now** (explicit user request only; `confirm_send_email` when emailing); delete. |

## Settings, defaults, and the confirmation gate

Substack's Publish dialog has more switches than a title and a slug, and **every one of them
gets a value whether or not you choose it**, so the tools make them explicit:

| Setting | Default | Notes |
|---|---|---|
| `audience` | `everyone` | `only_free` / `only_paid` / `founding` need paid subscriptions; rejected with a reason otherwise. |
| `comment_permissions` | `everyone` | `none` **is** "comments disabled". Always sent explicitly (the library otherwise copies `audience` into it). |
| `send_email` | `true` | Emails every subscriber on publish. **Cannot be unsent.** |
| `send_free_preview` | `false` | Only meaningful for a paid audience. |
| `section_id` | none | Validated against the publication's real sections. |
| `seo_title` / `seo_description` | fall back to title / subtitle | |
| `share_automatically` (publish only) | `false` | Posts publicly elsewhere; never enabled implicitly. |
| tags | none | Publication-level objects; applying an unknown one **creates it permanently**. |

**The irreversible bit is gated.** `schedule_draft` and `publish_draft` **refuse** when the
post would email subscribers unless `confirm_send_email: true` is passed. Intended flow: read
capabilities → propose settings → show the user a summary → get a yes → schedule.
`unschedule_draft` is the escape hatch while a schedule is still pending.

**Results report server state, not the request.** After every mutation the tools re-read the
draft and report what Substack stored; a requested value that differs is surfaced as
`settings_drift` / `tags_not_attached`. The schedule lives in `postSchedules` (single-draft
payload only); attached tags come from `GET post/<id>/tag`.

## The intended flow (tie-social, Step 0 pins the client)

1. `clients_status` → the client is ready (else `refresh_session` / `bind_client`).
2. `get_publication_settings(client, expected_publication)` → what's actually available.
3. `create_draft(client, expected_publication, title, slug, settings, body)` → `post_url` is real.
4. Human polishes the text in the Substack editor (`editor_url`).
5. Show settings + publish time; on an explicit yes,
   `schedule_draft(client, expected_publication, draft_id, datetime_iso, confirm_send_email=true)`.
6. Social posts scheduled in Zernio use `post_url`; no guessed links.

## Migration from 0.4 (one server per client) — what the installer does

1. **Selects the 0.4 configs by reference, not by glob**: the file each `tie-substack*` entry
   in `claude_desktop_config.json` points at (`TIE_SUBSTACK_CONFIG`), or `~/.tie-substack/config.json`
   for the default entry. The publication comes from `SUBSTACK_PUBLICATION_URL` in the entry's
   env (the 0.4 server accepted that alone) or `publication_url` in the file; reserved 0.5 paths
   (`clients.json`, `sessions/`, `backup-*/`, `migration.json`, `legacy/`) never join. The slug
   comes from the entry name (`tie-substack-<client>`); the default entry maps to
   `TIE_SUBSTACK_DEFAULT_SLUG` (default `tie`). An entry whose publication cannot be determined
   is reported as *not imported* and left untouched.
2. **Backs up**: `~/.tie-substack/backup-original/` (written once, on the first truly
   pre-migration run, with a `MANIFEST.json` of sha256 hashes: the desktop config, the
   selected configs, the 0.4 `server.py`), a per-run `backup-<timestamp>/`, and the 0.4
   server kept at `~/.tie-substack/legacy/server.py`.
3. **Imports idempotently** into `clients.json` + `sessions/`, keeping saved cookies and
   `act_as` pins; `~/.tie-substack/migration.json` journals source path, hash, slug and result.
   An unchanged source is skipped on re-run; a changed one re-imports that client unless its
   registry record was rebound since (then the registry is kept and the conflict reported).
4. **Verifies** with `server.py check-clients` (live): session valid, publication resolves,
   identity matches.
5. **Switches per verified client**: the single `tie-substack` entry is added; a 0.4 entry is
   removed only when its client was imported and verified ready. Every other 0.4 entry (a
   client that is not ready, or an entry that could not be imported) keeps working on
   `legacy/server.py` (the old default entry as `tie-substack-legacy`), the new entry sits
   beside them, and the installer names the fix (usually `bind_client`). Re-running repeats
   steps 1–5; already-imported sources are skipped.
6. **Rollback**: `./install.command --rollback` restores `backup-original/` (hashes verified
   first; a corrupted snapshot is refused); `--rollback=last` restores the latest per-run
   backup. Both leave `clients.json`, `sessions/` and the journal in place, so a later retry
   resumes from them.

## Env

| Variable | Meaning |
|---|---|
| `TIE_SUBSTACK_HOME` | Registry home (default `~/.tie-substack`); the tests point it at a temp dir. |
| `TIE_SUBSTACK_CLIENT` + `SUBSTACK_SESSION_TOKEN` | An explicit `substack.sid` for exactly that client (CI-style setups); ignored for every other client. |

`publication_url` must be the canonical **`https://<name>.substack.com`** form:
`python-substack` resolves the publication with a regex that contains a literal
`https://`, so a custom domain matches nothing. `add_client` normalizes a missing scheme and
rejects non-Substack domains.

## Tests

`python3 test_server.py`: offline regression checks (URL normalization, draft summaries,
settings validation, tags, the registry, every `expected_publication` mismatch on a read, a
write and a cleanup tool, identity and unbound refusals, the scope drift guard, interleaved
clients, one-shot auto-refresh, `bind_client` scan/selection/switch, `refresh_session`,
`clients_status`, and the migration: selection by reference, idempotent import, journal
conflicts, write-once backup, switch with and without failures, rollback with hash check).
Needs no cookie and makes no network calls.

## Caveats

- Unofficial API: a Substack change can break any tool here; nothing is destructive by
  default and `publish_draft` is the only immediate-publish path.
- A session belongs to a **user**, not a publication: bind the account that should own the
  posts (for a client publication: its owner's or the agency seat's with team access).
- Substack Notes are out of scope for now (Phase 2 of the productization plan adds them on
  this session layer).
