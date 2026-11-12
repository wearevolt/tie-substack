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

Design docs: [`wearevolt/internal-agentic-services-docs`](https://github.com/wearevolt/internal-agentic-services-docs),
`docs/marketing/` (the 0.5 client registry is designed in `20260928_apotapov_productization/`).

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
  with the fix named. A 401/403 in the middle of a call re-reads the source once more
  (even when the stored cookie still passes the profile probe), re-runs read-only tools
  on the new cookie, and names the fix for multi-step writers. Interleaved calls for
  different clients in one chat are safe.

## How auth works (and why Claude never sees a cookie)

Auth is a browser's Substack session cookie (`substack.sid`), one per client. Sessions live
only in `~/.tie-substack/sessions/<client>.json` (file mode 0600); the registry
`~/.tie-substack/clients.json` (0600) holds no secrets, only the publication, the bound
cookie source and the pinned identity.

- `bind_client` reads the client's session, validates it against the client's publication,
  stores it and pins the login as `act_as`. Tool output lists cookie *names* only; the value is
  never displayed, returned to the model, or logged.
- `bind_client dedicated: true` (0.8.0; the default since 0.9.0, on macOS too) gives the client a browser
  profile of its own, `~/.tie-substack/browsers/<client>`, read through Chrome DevTools instead
  of the Cookies database: nothing is decrypted, so it works where pycookiecheat cannot (Chrome
  on Windows). Without a login there yet it opens that window, answers `login_needed` and stores
  nothing; the operator logs in to Substack in that window, closes it and calls it again (0.8.1:
  the login window has no debugging port, since Substack's sign-in sends no email to a browser
  with one; the profile is read once the window is closed). Every later read
  starts the profile headless for about a second, takes the substack.com cookies and closes it,
  so an expired session is re-read without anyone opening a window.
- On macOS an explicit `profile` (a standard Chrome-family profile) or `cookie_file` still binds
  through [pycookiecheat](https://github.com/n8henrie/pycookiecheat) (macOS asks for Keychain
  access). Before 0.9.0, `bind_client` with no source scanned the standard profiles; that scan is
  gone.
- `refresh_session` re-reads the bound source after a logout or expiry; it refuses when the
  source is now another login (nothing stored). When a dedicated window has lost its login
  (0.9.1), it opens that window and answers `login_needed` with the same `tell_user` sentence as
  the first bind, instead of an error.
- One Substack account, one client (0.9.1): `bind_client` refuses an account already pinned to
  another client, since two clients on one account share every draft and Note;
  `confirm_shared_identity` overrides it on the user's explicit ask. Every tool's `client`
  parameter tells the model to ask when more than one client is registered and the request
  names none.

The cookie is a full-access credential for the Substack account that owns the session:
treat the session files like passwords.

## Install (macOS and Windows, 0.9.1)

A Claude Desktop extension, no Python or terminal needed: download
[`tie-substack.mcpb`](https://github.com/wearevolt/tie-substack/releases/latest/download/tie-substack.mcpb)
and add it in Claude: Settings → Desktop app → Extensions (Manage) → Advanced settings →
Install extension. It is a `uv` extension (`build_mcpb.sh`; the `extension` workflow builds it,
starts it on macOS and Windows and attaches it to every `v*` release): Claude Desktop uses the
`uv` it finds or downloads its own, and installs Python 3.12 and the locked dependencies
(`uv.lock`) into the extension's folder, so installing needs network. Then quit Claude fully
(Cmd-Q on macOS; on Windows Quit from the system tray) and reopen it, and in a Cowork chat:
"add client <slug> with https://<name>.substack.com, then bind it". A Chrome window of its own
opens; log in to Substack there as a user of that publication, close the window (on macOS quit
that Chrome with Cmd+Q: closing its window leaves it running and holding the profile) and ask to bind
again. Chrome is required (Chromium and Brave work too). Adding a client needs no reinstall.

**Moving from `install.command` (macOS).** The installer's entry in `claude_desktop_config.json`
would run a second tie-substack beside the extension, so remove it first (backed up; the registry
and sessions stay, the extension reads them):

```
bash -c "$(curl -fsSL https://raw.githubusercontent.com/wearevolt/tie-substack/main/install.command)" _ --to-extension
```

Then quit Claude, reopen it and install the extension. Clients bound to a browser profile keep
working; 0.4 entries the migration left on `legacy/server.py` stay as they are. `--rollback=last`
brings the entry back. `install.command` itself stays one more release (for this and rollback).

Logging in to the dedicated window:
- Substack emails a code (type it in that window) or a sign-in link (copy it into that
  window's address bar; a click opens it in your usual browser and logs that one in instead).
- Ask for the email once and wait a few minutes: repeated requests get "too many login
  emails" for a while.
- Don't log out of Substack elsewhere until the dedicated window is logged in.

Claude on Windows is a Microsoft Store (MSIX) app: its config lives under
`%LOCALAPPDATA%\Claude-Data\claude_<id>\`, and extensions run on Windows itself, not in
Cowork's Linux VM. Sessions and the registry stay in `%USERPROFILE%\.tie-substack`, so
installing a newer extension over the old one keeps every client.

## Tools

| Scope | Tool | What it does |
|---|---|---|
| registry | `list_clients` | Configured clients: publication, cookie source, pinned identity, session stored?, bound?. Local only. |
| registry | `add_client` | Register a client under its brand slug with its `https://<name>.substack.com` URL. |
| registry | `list_browser_profiles` | Chrome-family profiles (directory, display name, email) from the plaintext Local State, plus the dedicated browsers and windows clients are bound to. No cookies, no Keychain prompt. |
| registry | `open_client_browser` | OPTIONAL. Show a client's dedicated browser or window for a one-time login; an open one is left as it is. |
| probe | `clients_status` | Per client: bound?, session valid / expired / missing, identity matches?, publication reachable?, `logged_in_as`. The readiness signal (`--check-clients` in the installer). |
| probe | `bind_client` | Bind a cookie source (`dedicated: true`, a window of its own read through DevTools, the default; or, macOS only, `profile` / `cookie_file`; `user_data_dir` for a dedicated browser) and pin `act_as`. Switching to a different identity needs `confirm_switch`; an account already pinned to another client needs `confirm_shared_identity`. |
| probe | `refresh_session` | Re-read the bound source, validate against publication and pin, store. A dedicated window without a login: opens it, `login_needed` + `tell_user`. |
| publication | `get_publication_settings` | Paid subscriptions?, sections, existing tags, unavailable values. Call before offering choices. |
| publication | `create_draft` | Draft from Markdown with the **slug pinned** + explicit settings → `draft_id`, `slug`, `post_url`, `editor_url`, settings **as stored**. |
| publication | `update_post_settings` / `apply_tags` / `set_slug` | Fix settings, attach tags (new ones only with `allow_new`), change the slug. |
| publication | `schedule_draft` / `unschedule_draft` | Schedule at an exact ISO instant (offset required; refuses without `confirm_send_email` when the post emails); cancel. |
| publication | `get_draft` / `list_drafts` | Inspect drafts (`list_drafts` is a narrower projection; its notes say what is absent). |
| publication | `publish_draft` / `delete_draft` | Publish **now** (explicit user request only; `confirm_send_email` when emailing); delete (a published post's id deletes the live post). |
| publication | `create_note_draft` | Save a Substack Note as a draft on the client's profile, optionally with a link card (a post, or a note for a quote-restack). |
| publication | `schedule_note` / `unschedule_note` | Give a note draft a publish time (offset required; Substack's own scheduler fires it) or take it back; the link card is kept. |
| publication | `update_note_draft` | Replace a note draft's text; schedule and card stay. |
| publication | `list_note_drafts` / `delete_note_draft` | Every note draft and scheduled note, soonest first; delete one (never a published note). |
| publication | `publish_note` | Publish a note **now** (explicit user request only; `confirm_publish_now`). |
| publication | `get_note_stats` | Up to 50 published Notes: impressions (total, hourly over the first 48 h), by surface, accounts reached (unconnected / followers / subscribers), interactions. One failing Note goes under `errors`. |
| publication | `get_reading_times` | When the publication's readers read (the stats Audience tab): reading sessions over the last 3 months per hour of the week, UTC, Monday 00:00 first. |
| publication | `list_post_stats` / `get_post_stats` | Published posts newest first with views, opens, open rate, clicks, signups, subscribes, shares, reactions, comments; one post adds its first week by day and its traffic sources. |

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
clients, one-shot auto-refresh, `bind_client` selection/switch and its dedicated default,
`refresh_session`, `clients_status`, and the migration: selection by reference, idempotent
import, journal conflicts, write-once backup, switch with and without failures, rollback with
hash check, `remove-entry`, and the dedicated window: attach vs headless start, the DevTools
endpoint check, one read per profile at a time, `login_needed`, the Windows paths), and the
stats tools' parsing of Substack's answers as probed. Needs no cookie and makes no network
calls. CI runs it on Linux and on Windows; the `extension` workflow also starts the built
`.mcpb` on macOS and Windows.

## Substack Notes (0.6.0)

Notes are scheduled through **Substack's own Notes scheduler**: `schedule_note` stores a
`trigger_at` on the note draft and Substack publishes it at that time, so nothing has to run
here (the laptop can sleep) and every scheduled note is visible and editable in the client's
own Notes editor.

- **Author guard.** A note goes out on the logged-in **user's** profile, not on the
  publication. Every note tool therefore refuses unless that user's primary publication is the
  client's (a team member's login would post on the team member's profile).
- **Images.** `create_note_draft` / `publish_note` take `image_url` (an https image, e.g. a
  card's Cloudinary URL): it is uploaded to Substack first and attached; one attachment per
  note, so not together with `link_url`.
- **Attachments survive edits.** Substack drops a note draft's link card or image on any edit
  that does not send it again; the tools always re-send it and warn if Substack still lost it.
- **Rate limits (0.6.1).** Any request to Substack (Notes and posts) that it refuses with 429
  (Too Many Requests) is retried up to 4 times after a growing pause or its Retry-After; a write
  is never retried after a lost answer, so nothing doubles. If it still fails, the error says
  "rate-limited" instead of "Invalid JSON", and the session tools (`clients_status`,
  `refresh_session`, `bind_client`) say so too instead of reporting an expired session.
- **Clashes (0.6.1).** `schedule_note` warns when another note is already scheduled less than
  10 minutes away (notes that close compete for the same readers; the same minute is usually a
  mistake); it still schedules. Notes exactly 10 minutes apart are not flagged.
- Not covered yet: scheduling a plain (comment-less) restack.

## Statistics (0.7.0)

`get_note_stats`, `list_post_stats`, `get_post_stats` and `get_reading_times` read what the
author sees in Substack's own stats pages (`note_stats/c-<id>` on substack.com,
`post_management/*` and `publication/stats/reading_hours` on the publication). Read-only and stored nowhere; the caller keeps what it needs.

- **Author guard.** As the note tools: only on the publication owner's own login.
- **Missing parts are `null`.** Substack leaves a card out when it has nothing to show (a
  Note's `interactions` until the first one); the field is then `null`, not 0.
- **Batches.** `get_note_stats` takes 1-50 ids (`123` or `c-123`) and reads them one by one;
  a Note Substack refuses (403 for another account's or a deleted Note, 404, 5xx) is listed
  under `errors`, while an expired session or a rate limit fails the whole call as for every
  tool (a 403 counts as the Note's only after one profile read shows the session still works).

## Caveats

- Unofficial API: a Substack change can break any tool here; nothing is destructive by
  default and `publish_draft` / `publish_note` are the only immediate-publish paths.
- A session belongs to a **user**, not a publication: bind the account that should own the
  posts (for a client publication: its owner's or the agency seat's with team access). Note
  tools need the owner's login (see "Substack Notes").
