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
| `refresh_cookie` | Pull session cookies from the local browser into the config (names-only output). Takes `profile:` to pick a specific login of the standard install. |
| `list_profiles` | List browser profiles (directory, display name, signed-in email) from the plaintext Local State — no cookie reads, no Keychain prompt. |
| `get_publication_settings` | Paid subscriptions enabled?, sections, existing tags, and which audience/comment values are therefore unavailable. Call before offering choices. |
| `create_draft` | Draft from Markdown with **slug pinned** + explicit settings → `draft_id`, `slug`, `post_url`, `editor_url` and the settings **as stored**. |
| `update_post_settings` | Fix settings on an existing draft without recreating it. |
| `apply_tags` | Attach tags, reporting existing vs new; refuses to create new ones without `allow_new`. |
| `set_slug` | Change an existing draft's slug. |
| `schedule_draft` | Schedule publication at an exact ISO instant (offset required). Refuses without `confirm_send_email` when the post emails subscribers. |
| `unschedule_draft` | Cancel a scheduled publication. |
| `get_draft` / `list_drafts` | Inspect drafts (title, slug, post_url, settings, scheduled_for). |
| `publish_draft` | Publish **now** — needs `confirm_send_email` when emailing; explicit user request only. |
| `delete_draft` | Delete a draft. |

## Settings, defaults, and the confirmation gate

Substack's Publish dialog has more switches than a title and a slug, and **every
one of them gets a value whether or not you choose it**. So the tools make them
explicit rather than inheriting library defaults invisibly:

| Setting | Default | Notes |
|---|---|---|
| `audience` | `everyone` | `only_free` / `only_paid` / `founding` need paid subscriptions — rejected with a reason otherwise. |
| `comment_permissions` | `everyone` | `none` **is** "comments disabled". Always sent explicitly — see the trap below. |
| `send_email` | `true` | Emails every subscriber on publish. **Cannot be unsent.** |
| `send_free_preview` | `false` | Only meaningful for a paid audience. |
| `section_id` | none | Validated against the publication's real sections. |
| `seo_title` / `seo_description` | fall back to title / subtitle | |
| `share_automatically` (publish only) | `false` | Posts publicly elsewhere; never enabled implicitly. |
| tags | none | Publication-level objects — applying an unknown one **creates it permanently**. |

**The `write_comment_permissions` trap.** `python-substack` copies `audience`
into this field when it is omitted (its own source comment reads "this field is
a mess"), so an `only_paid` audience would silently make comments paid-only.
These tools always send it explicitly.

**The irreversible bit is gated.** Scheduling is a time-triggered public action
and the email cannot be recalled, so `schedule_draft` and `publish_draft`
**refuse** when the post would email subscribers unless you pass
`confirm_send_email: true`. Intended flow: read capabilities → propose settings
→ show the user a summary → get a yes → schedule. `unschedule_draft` is the
escape hatch while a schedule is still pending.

**Results report server state, not your request.** After every mutation the
tools re-read the draft and report what Substack stored. A requested value that
differs from the stored one is surfaced as `settings_drift` / `tags_not_attached`
instead of being reported as success. Two fields need their own endpoint, because
the draft payload cannot answer for them:

| Field | Where the truth lives |
|---|---|
| schedule | `postSchedules` — present only on the **single**-draft payload |
| attached tags | `GET post/<id>/tag` association rows (`post_tag_id` is a UUID), mapped to names via the publication tag list. The draft payload has **no** `postTags` field at all. |

**`list_drafts` is a narrower projection than `get_draft`.** Substack's list
response omits subtitle, SEO fields, section, tags and the schedule *as keys* —
so the tools report a per-field note there rather than `null`, which would read
as "empty". Call `get_draft` when the details matter.

## The intended flow (tie-social)

1. At the campaign's strategy checkpoint the slug is decided.
2. `get_publication_settings` → what's actually available.
3. `create_draft` (title + slug + settings + placeholder or full Markdown body) →
   `post_url` is now real.
4. Human pastes/polishes the final text in the Substack editor (`editor_url`).
5. Show the settings + publish time; on an explicit yes, `schedule_draft(...,
   confirm_send_email=true)` for publish date D.
6. Social posts scheduled in Zernio use `post_url` — no guessed links.

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

## Multiple clients (one operator, several publications)

`refresh_cookie` reads the browser's **default profile** unless told otherwise — with
several Substack logins across Chrome profiles that used to grab the wrong (or no)
session. Two mechanisms fix it, and they compose:

**A. Automatic profile scan (no setup — covers "several profiles in my main browser").**
When no `cookie_file` is configured, `refresh_cookie` walks ALL of the browser's
standard profiles (`Default`, `Profile 1`, …) and validates each session against the
publication — the default profile gets **no special trust** (since v0.4.0): being the
default doesn't make it the right *account*. When exactly one **login** reaches the
publication it is picked, its path **persisted**, and its identity **pinned as
`act_as`** so later refreshes go straight to it (two profiles signed into the *same*
account don't count as ambiguity). When several
distinct logins reach it, the scan stores nothing and errors with the candidate list —
the choice between accounts is yours, not directory-sort order's; re-run with
`profile:` (below). Deterministic code — cookie values never surface; the result
reports only profile names + handles. Limits: it can only see the standard install's
profiles (a dedicated `--user-data-dir` browser is invisible to the scan), and each
secure-storage read may prompt for Keychain access once per browser app.

**Choosing the right account (several of your OWN logins reach the same publication —
the M:1 case).** Being on the publication's team is not identity: with two logins that
both reach it, "reaches the publication" cannot tell them apart. Run `list_profiles`
(names + emails from the browser's plaintext profile cache, no Keychain prompt), then
`refresh_cookie` with `profile: "Work"` — matched case-insensitively against directory
name, display name, or email, exact or unique substring. After an explicit choice the
server pins `act_as: "<that handle>"` in the config, so every later no-arg refresh only
accepts that identity (never silently another login, even if new profiles appear). The
pin is plain config: edit or remove `act_as` by hand anytime, or set it up front —
a Substack handle is the reliable form (an email only matches when Substack's profile
API reports one). `substack_status` shows the pin and whether the current session
matches it. Two profiles logged into the SAME account can only be split by `profile:`,
not by `act_as`.

**Switching accounts** is deliberate, never silent: `refresh_cookie` with `profile:`
naming a login that differs from the pin refuses and names both identities; re-run with
`confirm_switch: true` (the agent passes it only on your explicit ask — same contract as
`confirm_send_email`) and the pin is rewritten to the new login, reported as
`act_as_changed`. No config editing needed. Upgrading from ≤0.3.0: a scan-persisted
standard-install `cookie_file` with no pin is re-validated via the scan on the next
no-arg `refresh_cookie` (it may ask you to pick once; the browser family is inferred
from the stored path, so a Brave/Chromium pin is re-checked against that browser's own
profiles) — a wrong account persisted by the
old first-match behavior cannot survive the upgrade silently, and **until that refresh
runs, every Substack-facing tool refuses — reads included** (`substack_status` shows the
state with `api_ready: false`).
Removing `act_as` by hand while a standard-install `cookie_file` is persisted puts the
config back into this re-validate-before-write state. Dedicated per-client
browsers (model B) are not affected. Just logged in on a new profile? Fully quit the
browser once (Cmd-Q) so the fresh session reaches the on-disk Cookies DB.

**B. Dedicated browser per client (the recommended model for real multi-client work) —
one dedicated browser + one config + one server entry per client.**

The installer automates the whole setup — re-run it once per client:

```
TIE_SUBSTACK_CLIENT=acme ./install.command        # or run interactively and answer the prompts
```

It asks for the publication URL and the browser user-data dir (Enter accepts
`~/TIE-Browsers/<client>`), writes `~/.tie-substack/<client>.json` (with `cookie_file`
pointing into that browser), and registers the `tie-substack-<client>` server entry with
the env overrides. The manual equivalent, for reference:

1. **Dedicated browser per client** — a separate instance, not a profile in your daily
   browser:
   ```
   open -na "Google Chrome" --args --user-data-dir="$HOME/TIE-Browsers/<client>"
   ```
   Log in to *that client's* Substack there, once. Nothing else needs to live in it.
   (Cookie decryption still works: Chrome's Safe Storage Keychain key is per-app, not
   per-user-data-dir.)
2. **One server entry per client** in `claude_desktop_config.json`:
   ```json
   "tie-substack-<client>": {
     "command": "python3", "args": ["<path>/server.py"],
     "env": {
       "TIE_SUBSTACK_CONFIG": "~/.tie-substack/<client>.json",
       "SUBSTACK_PUBLICATION_URL": "https://<client>.substack.com"
     }
   }
   ```
3. **Point the config at the dedicated browser** — run `refresh_cookie` once with
   `cookie_file: "~/TIE-Browsers/<client>/Default/Cookies"`; the path is persisted into
   that client's config, so every later no-arg `refresh_cookie` on this server reads the
   right browser automatically. `substack_status` reports the configured `cookie_file`
   and which publication the session maps to — the caller should treat that as the proof
   it is talking to the right client before creating drafts.

`cookie_file` targets Chrome-family browsers (Chrome/Chromium/Brave); it cannot be
combined with `browser: "firefox"`.

## Tests

`python3 test_server.py` — offline regression checks (URL normalization, subdomain
resolution, error attribution, `/drafts` payload unwrapping, draft summaries,
per-client `cookie_file` resolution, profile listing/selection, multi-match refusal,
`act_as` pinning). Needs no cookie and makes no network calls.

## Caveats

- Unofficial API — a Substack change can break any tool here; nothing is
  destructive by default and `publish_draft` is the only immediate-publish path.
- The cookie belongs to a **user**, not a publication: use a session of the
  account that should own the posts (for TIE: the publication owner's).
- Substack Notes are out of scope for now (native Notes scheduling exists in
  the Substack UI since April 2026).
