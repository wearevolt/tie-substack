#!/usr/bin/env python3
"""tie-substack — local MCP server (stdio) for scheduling Substack posts.

Substack has no official write API, so this server wraps the community
`python-substack` library (Substack's internal endpoints) to create drafts,
pin the post slug (=> the public URL is known BEFORE publication), and
schedule publication. It exists so the tie-social skill can run its Substack
leg from Claude surfaces (Cowork/Desktop) without any credential ever
entering the model context.

Auth is a browser session cookie (`substack.sid`). It lives ONLY in this
server's local config file (mode 0600) — tools never echo it back, and the
refresh_cookie tool (pycookiecheat) pulls it straight from the local Chrome
profile into the config without displaying it. When a publication (or an
`act_as` pin) is configured, refresh_cookie SCANS ALL of the browser's
standard profiles (Default, Profile 1, ...) — the default profile gets no
special trust, since "reaches the publication" is not identity — and
proceeds ONLY when exactly one qualifying login exists; with several it
stores nothing and asks for an explicit `profile` (see list_profiles).
The optional `act_as` config pin restricts which logged-in identity
qualifies anywhere (including get_api's preflight, so every write tool is
gated). A per-client setup instead points `cookie_file` at a dedicated
browser's Cookies DB, which skips the scan entirely (see README
"Multiple clients").

Config file (~/.tie-substack/config.json, override via TIE_SUBSTACK_CONFIG):
  { "publication_url": "https://<pub>.substack.com",
    "cookie_file": "~/TIE-Browsers/<client>/Default/Cookies",   # optional
    "act_as": "<substack handle>",                              # optional identity pin
    "cookies": { "substack.sid": "...", ... } }

Multi-client: register one server entry PER CLIENT in claude_desktop_config
(e.g. "tie-substack-acme"), each with its own TIE_SUBSTACK_CONFIG +
SUBSTACK_PUBLICATION_URL, and a cookie_file pointing into that client's
dedicated browser. Sessions never mix across clients or Chrome profiles.

Env overrides:
  SUBSTACK_PUBLICATION_URL   publication URL (wins over config)
  SUBSTACK_SESSION_TOKEN     substack.sid value (wins over config cookies)

Protocol: MCP over stdio, newline-delimited JSON-RPC 2.0 (same plumbing as
wearevolt/tie-imagegen). Requires: python-substack; pycookiecheat optional
(only refresh_cookie needs it).
"""

import json
import os
import re
import stat
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

SERVER_NAME = "tie-substack"
SERVER_VERSION = "0.4.1"

# Substack's Publish-dialog settings. Every one of these gets a value whether or
# not the caller picks it, so the tools always send them explicitly — see
# AUDIENCE/COMMENT notes in create_draft.
AUDIENCE_VALUES = ("everyone", "only_free", "only_paid", "founding")
COMMENT_VALUES = ("none", "only_paid", "everyone")  # "none" == comments disabled
# Meaningless (and rejected by the UI) unless the publication sells subscriptions.
PAID_AUDIENCE_VALUES = ("only_free", "only_paid", "founding")
FALLBACK_PROTOCOL = "2025-06-18"

CONFIG_PATH = os.path.expanduser(
    os.environ.get("TIE_SUBSTACK_CONFIG", "~/.tie-substack/config.json")
)

_write_lock = threading.Lock()
_api_lock = threading.Lock()
_api_cache = {"api": None}


def log(msg):
    sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stderr.flush()


def send(obj):
    line = json.dumps(obj, separators=(",", ":"))
    with _write_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def reply(req_id, result):
    send({"jsonrpc": "2.0", "id": req_id, "result": result})


def reply_error(req_id, code, message):
    send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


# ---------------------------------------------------------------- config


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("config file %s is unreadable: %s" % (CONFIG_PATH, e))


def save_config(cfg):
    d = os.path.dirname(CONFIG_PATH)
    os.makedirs(d, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600 — it holds a session cookie


def normalize_publication_url(url):
    """Force the https://<name>.substack.com form python-substack's resolver requires.

    Its Api.__init__ extracts the subdomain with a regex containing a literal
    'https://', so a bare host or an http:// URL silently resolves to no
    publication and later blows up as 'NoneType' is not subscriptable.
    """
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if u.lower().startswith("http://"):
        u = "https://" + u[len("http://"):]
    elif not u.lower().startswith("https://"):
        u = "https://" + u
    return u


def raw_publication_url():
    return os.environ.get("SUBSTACK_PUBLICATION_URL") or load_config().get(
        "publication_url"
    ) or ""


def publication_url():
    url = normalize_publication_url(raw_publication_url())
    if not url:
        raise RuntimeError(
            "publication_url is not configured — set SUBSTACK_PUBLICATION_URL or add "
            '"publication_url" to %s (e.g. via install.command)' % CONFIG_PATH
        )
    return url


SUBDOMAIN_RE = re.compile(r"^https://([^./]+)\.substack\.com$", re.I)


def configured_subdomain():
    """The publication subdomain, or None if the URL isn't a *.substack.com one."""
    m = SUBDOMAIN_RE.match(publication_url())
    return m.group(1).lower() if m else None


def current_cookies():
    """Cookie dict from env override or config. Values NEVER leave this process."""
    token = os.environ.get("SUBSTACK_SESSION_TOKEN")
    if token:
        return {"substack.sid": token}
    return load_config().get("cookies") or {}


def cookies_string(cookies):
    return "; ".join("%s=%s" % (k, v) for k, v in cookies.items())


def resolve_cookie_file(arg_value):
    """Cookie-DB path for refresh_cookie: explicit arg > config 'cookie_file' > None
    (None = the browser's default profile). The multi-client setup stores a per-client
    path (a dedicated browser's <user-data-dir>/Default/Cookies) in each client config."""
    raw = arg_value or load_config().get("cookie_file")
    return os.path.expanduser(raw) if raw else None


CHROME_FAMILY_DATA_DIRS = {
    "chrome": "~/Library/Application Support/Google/Chrome",
    "chromium": "~/Library/Application Support/Chromium",
    "brave": "~/Library/Application Support/BraveSoftware/Brave-Browser",
}


def chrome_profile_cookie_files(browser, root=None):
    """(profile_name, cookie_file) for every profile of the browser's STANDARD data dir
    (Default, Profile 1, ...). Dedicated --user-data-dir browsers live elsewhere and are
    not discoverable — those are addressed explicitly via cookie_file (the multi-client
    model). macOS paths — this server's install story is macOS-only."""
    base = root or CHROME_FAMILY_DATA_DIRS.get(browser)
    if not base:
        return []
    base = os.path.expanduser(base)
    if not os.path.isdir(base):
        return []
    out = []
    for name in sorted(os.listdir(base)):
        if name == "Default" or name.startswith("Profile "):
            cf = os.path.join(base, name, "Cookies")
            if os.path.isfile(cf):
                out.append((name, cf))
    return out


def standard_install_browser(path):
    """Which browser family's STANDARD install contains this cookie path — or
    None for a dedicated --user-data-dir browser. Distinguishes scan/profile-
    persisted pins from the multi-client model's dedicated paths (only the
    former can silently hold a wrong login), and names the family so a legacy
    re-validation scans the browser that actually produced the path."""
    if not path:
        return None
    p = os.path.expanduser(path)
    for b, root in CHROME_FAMILY_DATA_DIRS.items():
        if p.startswith(os.path.expanduser(root) + os.sep):
            return b
    return None


def in_standard_install(path):
    return standard_install_browser(path) is not None


def legacy_unpinned(cfg):
    """Pre-0.4.0 state: a standard-install cookie_file with no identity pin.
    Every 0.4.0 success path that persists a standard-install path also pins
    act_as (explicit profile AND scan-validated matches), so this combination
    can only be inherited — and the stored session may be the wrong login (the
    old scan took the first match). Every tool that talks to Substack (reads
    included — get_api serves both) refuses in this state until a refresh
    re-validates it; one no-arg refresh_cookie heals it. An explicit
    SUBSTACK_SESSION_TOKEN is exempt: it overrides config cookies entirely
    (current_cookies), and that session is probed and identity-gated on its
    own — stale local config must not block a CI/env-driven setup."""
    if os.environ.get("SUBSTACK_SESSION_TOKEN"):
        return False
    return (not (cfg.get("act_as") or "").strip()
            and in_standard_install(cfg.get("cookie_file")))


def local_state_path(browser, root=None):
    """Path to the browser's plaintext 'Local State' JSON, or None for an
    unknown browser. The file maps profile dirs to display names/emails."""
    base = root or CHROME_FAMILY_DATA_DIRS.get(browser)
    if not base:
        return None
    return os.path.join(os.path.expanduser(base), "Local State")


def local_state_profiles(browser, root=None):
    """[{dir, name, email}] for every profile in the browser's 'Local State'
    (profile.info_cache). Names/emails only — never touches the Cookies DB or
    the Keychain. `root=` addresses a dedicated --user-data-dir browser (and
    is the test seam). Missing file -> [] so callers can degrade gracefully."""
    path = local_state_path(browser, root)
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("could not parse %s: %s" % (path, e))
    info = (data.get("profile") or {}).get("info_cache") or {}
    out = []
    for d in sorted(info):
        meta = info[d] or {}
        out.append({
            "dir": d,
            "name": meta.get("name") or meta.get("gaia_name") or "",
            "email": meta.get("user_name") or "",
        })
    return out


def resolve_profile_selector(selector, browser, root=None):
    """The one profile a human-friendly selector means. Matched case-insensitively
    against directory name, display name, and email — exact first, substring only
    when nothing matches exactly (so 'Person 1' is not ambiguous with 'Person 10').
    Anything but exactly one hit is an error listing what IS available."""
    profiles = local_state_profiles(browser, root)
    if not profiles:
        raise RuntimeError(
            'no browser profiles found — no "Local State" file under %s '
            "(is %s installed?)"
            % (root or CHROME_FAMILY_DATA_DIRS.get(browser), browser)
        )
    sel = (selector or "").strip().lower()
    if not sel:
        raise ValueError("profile selector is empty")

    def fields(p):
        return (p["dir"].lower(), p["name"].lower(), p["email"].lower())

    cands = [p for p in profiles if sel in fields(p)]
    if not cands:
        cands = [p for p in profiles if any(sel in f for f in fields(p) if f)]
    if len(cands) == 1:
        return cands[0]
    if not cands:
        raise ValueError(
            "profile %r does not match any %s profile — available: %s "
            "(see list_profiles)" % (selector, browser, profiles)
        )
    raise ValueError(
        "profile %r matches %d profiles: %s — use the exact directory name, "
        "display name, or email (see list_profiles)" % (selector, len(cands), cands)
    )


def read_browser_cookies(pycookiecheat, browser, cookie_file, errors):
    """One profile's substack.com cookies, or None. pycookiecheat's API moved between
    versions — try the new form, then the old."""
    url = "https://substack.com"
    try:
        bt = pycookiecheat.BrowserType(browser)
        return pycookiecheat.chrome_cookies(url, browser=bt, cookie_file=cookie_file) \
            if browser != "firefox" else pycookiecheat.firefox_cookies(url)
    except Exception as e:  # noqa: BLE001
        errors.append(str(e))
        try:
            return pycookiecheat.chrome_cookies(url, cookie_file=cookie_file)
        except Exception as e2:  # noqa: BLE001
            errors.append(str(e2))
    return None


def publication_accessible(probe, target):
    """Can this session act on publication `target`? The SINGLE access predicate —
    cookie selection (session_reaches), get_api's preflight, and substack_status must
    all agree, or refresh_cookie can pick a session that later fails api_ready.
    `primary` counts: a primary-only profile (primaryPublication set but absent from
    publicationUsers) is still an account of that publication. Case-insensitive —
    probe_session lowercases subdomains but not primary, and targets are lowercased."""
    if not target:
        return True
    t = target.lower()
    subs = [s.lower() for s in (probe.get("subdomains") or [])]
    prim = (probe.get("primary") or "").lower()
    return t in subs or t == prim


def accessible_publications(probe):
    """For error messages: everything the session can act on, primary included."""
    out = [s.lower() for s in (probe.get("subdomains") or [])]
    prim = (probe.get("primary") or "").lower()
    if prim and prim not in out:
        out.append(prim)
    return sorted(out)


def identity_matches(probe, act_as):
    """Does the probed session belong to the pinned identity? act_as matches the
    Substack handle (what the server itself persists) or, as a hand-typed
    convenience, the account email when the profile API reports one. Unset pin
    -> everything qualifies. Kept separate from session_reaches so callers can
    tell 'wrong publication' from 'wrong identity' in error messages."""
    if not act_as:
        return True
    a = act_as.strip().lower()
    return (
        ((probe or {}).get("handle") or "").strip().lower() == a
        or ((probe or {}).get("email") or "").strip().lower() == a
    )


def session_reaches(cookies, target):
    """(matches, probe): the session is valid AND can access publication `target`
    (any valid session counts when target is None)."""
    if not cookies or "substack.sid" not in cookies:
        return False, None
    try:
        ok, probe = probe_session(cookies)
    except Exception:  # noqa: BLE001
        return False, None
    if not ok:
        return False, probe
    if not publication_accessible(probe, target):
        return False, probe
    return True, probe


def probe_session(cookies):
    """Check the session WITHOUT depending on publication resolution.

    Returns (ok, info). Keeps 'session expired' distinguishable from
    'this account has no access to the configured publication' — the two
    failures look identical once python-substack's Api.__init__ is involved.
    """
    import requests  # noqa: PLC0415  (a python-substack dependency)

    r = requests.get(
        "https://substack.com/api/v1/user/profile/self", cookies=cookies, timeout=30
    )
    if r.status_code in (401, 403):
        return False, {"reason": "session_invalid", "status": r.status_code}
    r.raise_for_status()
    data = r.json() or {}
    subdomains = []
    for pu in data.get("publicationUsers") or []:
        pub = pu.get("publication") or {}
        if pub.get("subdomain"):
            subdomains.append(pub["subdomain"].lower())
    return True, {
        "handle": data.get("handle") or data.get("name"),
        "email": data.get("email"),
        "user_id": data.get("id"),
        "subdomains": subdomains,
        "primary": (data.get("primaryPublication") or {}).get("subdomain"),
    }


def get_api(fresh=False):
    """python-substack Api, cached (its __init__ does network round-trips)."""
    with _api_lock:
        if _api_cache["api"] is not None and not fresh:
            # The act_as pin must hold for CACHED clients too — the config can
            # change under a warm cache (hand-edit, another tool), and a cache
            # hit must never hand back a client the pin no longer trusts. On
            # mismatch (or a legacy-unpinned config), drop the cache and fall
            # through to the full preflight, which raises the canonical error.
            cfg_now = load_config()
            if not legacy_unpinned(cfg_now) and identity_matches(
                {"handle": _api_cache.get("handle"), "email": _api_cache.get("email")},
                (cfg_now.get("act_as") or "").strip(),
            ):
                return _api_cache["api"]
            _api_cache["api"] = None
        cookies = current_cookies()
        if not cookies.get("substack.sid"):
            raise RuntimeError(
                "no substack.sid cookie configured. Run the refresh_cookie tool (pulls it "
                "from your local Chrome via pycookiecheat), or paste it manually into %s as "
                '{"cookies": {"substack.sid": "<value>"}}. Never paste the cookie into chat.'
                % CONFIG_PATH
            )
        url = publication_url()
        sub = configured_subdomain()
        if not sub:
            raise RuntimeError(
                "publication_url %r is not a https://<name>.substack.com URL. The underlying "
                "python-substack library resolves the publication by exactly that form, so a "
                "custom domain cannot be used here — configure the canonical Substack URL in "
                "%s." % (url, CONFIG_PATH)
            )
        # Pre-flight so failures name their real cause instead of surfacing as
        # "'NoneType' object is not subscriptable" from inside change_publication().
        ok, probe = probe_session(cookies)
        if not ok:
            raise RuntimeError(
                "Substack session is invalid or expired (HTTP %s) — run refresh_cookie"
                % probe.get("status")
            )
        if not publication_accessible(probe, sub):
            raise RuntimeError(
                "the logged-in account (%s) has no access to publication %r. Publications "
                "available to this session: %s. Either fix publication_url in %s, or refresh "
                "the cookie from a browser logged in as a user of %r."
                % (probe["handle"], sub, accessible_publications(probe) or "(none)",
                   CONFIG_PATH, sub)
            )
        cfg_now = load_config()
        act_as = (cfg_now.get("act_as") or "").strip()
        if legacy_unpinned(cfg_now):
            # A wrong-but-valid session inherited from ≤0.3.0 must not reach
            # any Substack-facing tool (reads included — acting as the wrong
            # identity is misleading either way) before refresh_cookie runs.
            raise RuntimeError(
                "this config has a pre-0.4.0 standard-install cookie_file with no "
                "identity pin — the stored session (currently logged in as %r) may "
                "be the wrong login. Run refresh_cookie (it re-validates via the "
                'profile scan) or refresh_cookie with profile: "<login>" before '
                "reading or writing as this account. Config: %s"
                % (probe.get("handle"), CONFIG_PATH)
            )
        if not identity_matches(probe, act_as):
            # The pin gates every write tool here, not just refresh_cookie — a
            # stale, hand-pasted, or env-injected cookie must not act as the
            # wrong account just because it can reach the publication.
            raise RuntimeError(
                "the session is logged in as %r, not the pinned act_as=%r — run "
                "refresh_cookie (optionally with profile:, see list_profiles), or "
                "change/remove act_as in %s." % (probe["handle"], act_as, CONFIG_PATH)
            )
        from substack import Api  # noqa: PLC0415

        if not hasattr(Api, "create_draft_from_markdown"):
            raise RuntimeError(
                "the installed python-substack library is too old for this server "
                "(no Api.create_draft_from_markdown — typically a venv built on "
                "Python < 3.10, where pip silently resolves the 2023-era library). "
                "Fix: re-run the installer, which now requires Python 3.10+ and a "
                "pinned library: bash -c \"$(curl -fsSL https://raw.github"
                "usercontent.com/wearevolt/tie-substack/main/install.command)\""
            )
        api = Api(cookies_string=cookies_string(cookies), publication_url=url)
        _api_cache["api"] = api
        # Remember whose session this client wraps, so cache hits can re-check
        # the act_as pin without a network probe.
        _api_cache["handle"] = probe.get("handle")
        _api_cache["email"] = probe.get("email")
        return api


def reset_api():
    with _api_lock:
        _api_cache["api"] = None
        _api_cache["handle"] = None
        _api_cache["email"] = None


# ---------------------------------------------------------------- helpers


def publication_capabilities(api):
    """What this publication can actually do — so choices offered are real ones.

    Verified against a live personal-mode publication: `payments_state` is the
    paid-subscription signal, and get_sections() raises APIError(400) when the
    publication has none rather than returning an empty list.
    """
    pub = api.get_user_primary_publication() or {}
    state = pub.get("payments_state")
    caps = {
        "publication": pub.get("subdomain"),
        "name": pub.get("name"),
        "publication_url": pub.get("publication_url"),
        "payments_state": state,
        "paid_enabled": bool(state) and state != "disabled",
        "pledges_enabled": pub.get("pledges_enabled"),
        "personal_mode": pub.get("is_personal_mode"),
    }
    try:
        sections = api.get_sections() or []
        caps["sections"] = [
            {"id": s.get("id"), "name": s.get("name")}
            for s in sections
            if isinstance(s, dict)
        ]
    except Exception as e:  # noqa: BLE001
        caps["sections"] = []
        caps["sections_note"] = "no sections on this publication (%s)" % str(e)[:120]
    try:
        tags = api.get_publication_post_tags() or []
        caps["existing_tags"] = [
            {"id": t.get("id"), "name": t.get("name")} for t in tags if isinstance(t, dict)
        ]
    except Exception as e:  # noqa: BLE001
        caps["existing_tags"] = []
        caps["tags_note"] = "could not list tags: %s" % str(e)[:120]
    if not caps["paid_enabled"]:
        caps["unavailable"] = {
            "audience": list(PAID_AUDIENCE_VALUES),
            "comment_permissions": ["only_paid"],
            "reason": "publication has no paid subscriptions (payments_state=%r)" % state,
        }
    # Substack's publication payload carries no timezone, so every scheduling
    # call must pass an explicit UTC offset (the tools enforce that).
    caps["timezone"] = None
    return caps


def validate_settings(audience, comments, caps):
    if audience not in AUDIENCE_VALUES:
        raise ValueError(
            "audience %r is invalid — choose one of %s" % (audience, list(AUDIENCE_VALUES))
        )
    if comments not in COMMENT_VALUES:
        raise ValueError(
            "comment_permissions %r is invalid — choose one of %s ('none' disables comments)"
            % (comments, list(COMMENT_VALUES))
        )
    if not caps.get("paid_enabled"):
        if audience in PAID_AUDIENCE_VALUES:
            raise ValueError(
                "audience %r needs paid subscriptions, which this publication does not have "
                "(payments_state=%r) — use 'everyone'"
                % (audience, caps.get("payments_state"))
            )
        if comments == "only_paid":
            raise ValueError(
                "comment_permissions 'only_paid' needs paid subscriptions, which this "
                "publication does not have — use 'everyone' or 'none'"
            )


def resolve_tags(api, wanted, caps=None):
    """Split requested tags into existing vs new. Tags are PUBLICATION-level
    objects: applying an unknown one creates it permanently, so the caller must
    opt in to that."""
    existing = {
        (t.get("name") or "").strip().lower(): t
        for t in ((caps or {}).get("existing_tags") or [])
    }
    if caps is None:
        try:
            existing = {
                (t.get("name") or "").strip().lower(): t
                for t in (api.get_publication_post_tags() or [])
                if isinstance(t, dict)
            }
        except Exception:  # noqa: BLE001
            existing = {}
    norm, seen = [], set()
    for raw in wanted or []:
        t = re.sub(r"[^a-z0-9]+", "-", (raw or "").strip().lower()).strip("-")
        if t and t not in seen:
            seen.add(t)
            norm.append(t)
    return {
        "normalized": norm,
        "existing": [t for t in norm if t in existing],
        "new": [t for t in norm if t not in existing],
    }


def read_post_tags(api, post_id):
    """Tags actually attached, read from the association endpoint.

    The draft payload has NO postTags field (verified: the key is absent, not
    null), so a request echo would be the only alternative — and this is the last
    place where that would still be the case. `GET post/<id>/tag` returns
    association rows carrying post_tag_id (a UUID string), which we map to names
    via the publication's tag list.
    """
    rows = api.call("post/%s/tag" % post_id, "GET") or []
    if not isinstance(rows, list):
        return {"names": [], "note": "unexpected response shape from post/<id>/tag"}
    names_by_id = {}
    try:
        for t in api.get_publication_post_tags() or []:
            if isinstance(t, dict):
                names_by_id[str(t.get("id"))] = t.get("name")
    except Exception:  # noqa: BLE001
        pass
    names = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        tid = str(r.get("post_tag_id") or r.get("id") or "")
        names.append(names_by_id.get(tid) or ("tag:%s" % tid))
    return {"names": names}


def post_url_for_slug(slug):
    if not slug:
        return None
    try:
        return "%s/p/%s" % (publication_url(), slug)
    except RuntimeError:
        return None


def unwrap_items(raw, *keys):
    """Substack wraps collections in an object (e.g. {"posts": [...]}) — unwrap it."""
    if isinstance(raw, dict):
        for k in keys:
            v = raw.get(k)
            if isinstance(v, list):
                return v
        return []
    return raw or []


def draft_summary(draft):
    """Public, cookie-free summary of a draft dict returned by the API."""
    if not isinstance(draft, dict):
        return {"warning": "unexpected draft payload shape", "raw": str(draft)[:200]}
    slug = draft.get("slug") or draft.get("draft_slug")
    published = draft.get("is_published")
    if published is None:
        published = bool(draft.get("post_date"))
    draft_id = draft.get("id")
    summary = {
        "draft_id": draft_id,
        # Unpublished drafts keep the working title in draft_title and leave title null.
        "title": draft.get("draft_title") or draft.get("title") or "(untitled draft)",
        "slug": slug,
        "post_url": post_url_for_slug(slug),
        "is_published": bool(published),
        "updated_at": draft.get("draft_updated_at") or draft.get("updated_at"),
        # Settings as STORED by Substack (never as requested) — the caller shows
        # these to the user, so an echo of our own payload would be misleading.
        "audience": draft.get("audience"),
        "comment_permissions": draft.get("write_comment_permissions"),
        "send_email": draft.get("should_send_email"),
    }
    # The list payload is a narrower projection than get_draft: subtitle, SEO,
    # section and free-preview keys are ABSENT there (not null). Reporting null
    # for an absent key would read as "empty", so only report what the payload
    # actually carries.
    if "draft_subtitle" in draft or "subtitle" in draft:
        summary["subtitle"] = draft.get("draft_subtitle") or draft.get("subtitle")
    else:
        summary["subtitle_note"] = "not in this payload — call get_draft to read it"
    for key, field in (
        ("send_free_preview", "should_send_free_preview"),
        ("section_id", "draft_section_id"),
        ("seo_title", "search_engine_title"),
        ("seo_description", "search_engine_description"),
    ):
        if field in draft:
            summary[key] = draft.get(field)
    if draft.get("email_sent_at"):
        summary["email_already_sent_at"] = draft["email_sent_at"]
    # trigger_at lives in postSchedules, and ONLY on the single-draft payload —
    # the list payload omits the key entirely, so absence != "not scheduled".
    if "postSchedules" in draft:
        schedules = draft.get("postSchedules") or []
        trigger = None
        for s in schedules:
            if isinstance(s, dict) and s.get("trigger_at"):
                trigger = s["trigger_at"]
                break
        summary["scheduled_for"] = trigger
    else:
        summary["scheduled_for_note"] = "not in this payload — call get_draft to read it"
    try:
        summary["editor_url"] = "%s/publish/post/%s" % (publication_url(), draft_id)
    except RuntimeError:
        pass
    if not slug:
        summary["post_url_note"] = (
            "no slug set on this draft yet — call set_slug to pin the public URL"
        )
    return summary


def parse_iso_aware(value):
    """Parse an ISO 8601 timestamp and REQUIRE timezone info (naive = silent UTC bug)."""
    v = (value or "").strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        raise ValueError("invalid ISO 8601 datetime: %r" % value)
    if dt.tzinfo is None:
        raise ValueError(
            "datetime %r has no timezone — pass an offset (e.g. 2026-08-03T09:00:00-04:00) "
            "so the schedule can't silently shift" % value
        )
    return dt


SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def validate_slug(slug):
    if not SLUG_RE.match(slug or ""):
        raise ValueError(
            "slug %r is invalid — use lowercase words separated by single hyphens, "
            "e.g. 'program-management-another-year-another-obituary'" % slug
        )
    return slug


def text_result(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}]}


# ---------------------------------------------------------------- tools

TOOLS = [
    {
        "name": "substack_status",
        "description": (
            "Health check: is a session cookie configured and still valid, which user and "
            "publication it maps to, python-substack/pycookiecheat availability. Run this "
            "first; if the cookie is dead it says so and points at refresh_cookie."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "refresh_cookie",
        "description": (
            "Pull the current substack.com session cookies from the LOCAL browser profile "
            "(via pycookiecheat; macOS will prompt for Keychain access) and store them in "
            "the server's 0600 config file. When a publication is configured (and no "
            "cookie_file is set), ALL of the browser's standard profiles are scanned — "
            "the default profile gets no special trust; when EXACTLY ONE login reaches "
            "the publication it is chosen, persisted, and pinned as act_as, and with "
            "several distinct logins nothing is stored and the error lists the "
            "candidates so you re-run with `profile`. An `act_as` config pin restricts which logged-in identity "
            "qualifies at all. The cookie value is never returned or shown — the result "
            "only lists cookie NAMES, profile names/handles, and the validation outcome. "
            "Requires the user to be logged in to Substack in that browser. Run only when "
            "the user asks to refresh/repair Substack auth."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "browser": {
                    "type": "string",
                    "enum": ["chrome", "chromium", "brave", "firefox"],
                    "default": "chrome",
                    "description": "Which local browser profile to read the cookie from.",
                },
                "cookie_file": {
                    "type": "string",
                    "description": (
                        "Absolute path to a specific Chrome-family 'Cookies' SQLite file "
                        "to read INSTEAD of the browser's default profile — the "
                        "multi-client setup points this at a dedicated per-client "
                        "browser, e.g. ~/TIE-Browsers/<client>/Default/Cookies. Omit to "
                        "use this server config's stored 'cookie_file' (if any), else "
                        "the default profile. Not supported with browser=firefox."
                    ),
                },
                "profile": {
                    "type": "string",
                    "description": (
                        "Pick ONE profile of the standard browser install by directory "
                        "name ('Profile 2'), display name, or signed-in email — "
                        "case-insensitive, exact or unique substring (run list_profiles "
                        "first). Use when several logins reach the same publication. "
                        "Mutually exclusive with cookie_file; not supported with "
                        "browser=firefox."
                    ),
                },
                "confirm_switch": {
                    "type": "boolean",
                    "description": (
                        "Required true to SWITCH the pinned identity: when `profile` "
                        "resolves to a login different from the configured act_as, the "
                        "call refuses unless this is set, and on success the pin is "
                        "rewritten to the new login. Only meaningful together with "
                        "`profile`. Pass it ONLY when the user explicitly asked to act "
                        "as a different account — never on your own initiative."
                    ),
                },
            },
        },
    },
    {
        "name": "list_profiles",
        "description": (
            "List the local Chrome-family browser's profiles (directory, display name, "
            "signed-in email) by parsing the browser's plaintext 'Local State' file. "
            "Reads NO cookie data and triggers NO Keychain prompt — use it to pick the "
            "`profile` argument for refresh_cookie when several Substack logins reach "
            "the same publication."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "browser": {
                    "type": "string",
                    "enum": ["chrome", "chromium", "brave"],
                    "default": "chrome",
                    "description": "Which browser's profiles to list.",
                },
                "root": {
                    "type": "string",
                    "description": (
                        "Override the browser user-data directory (e.g. a dedicated "
                        "per-client browser like ~/TIE-Browsers/<client>). Default: "
                        "the standard install location for `browser`."
                    ),
                },
            },
        },
    },
    {
        "name": "get_publication_settings",
        "description": (
            "Read what the publication can actually do, so you only offer valid choices: "
            "paid subscriptions enabled?, available sections, existing publication tags, "
            "and which audience/comment values are therefore unavailable. Call this BEFORE "
            "assembling a settings proposal for the user."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_draft",
        "description": (
            "Create a Substack draft from Markdown with the slug pinned, so the public URL "
            "(<publication>/p/<slug>) is known before publication. Returns draft_id, slug, "
            "post_url, editor_url and the settings AS STORED by Substack. Body images "
            "referenced as local paths/URLs are uploaded by the library. Does NOT publish "
            "or schedule.\n\n"
            "Every Publish-dialog setting gets a value whether or not you pass one, so the "
            "defaults here are explicit and visible. Show them to the user before "
            "scheduling. Note: comment_permissions is ALWAYS sent explicitly, because the "
            "underlying library silently copies `audience` into it when omitted (so an "
            "only_paid audience would quietly make comments paid-only). Validate choices "
            "against get_publication_settings first — paid-only values fail on a "
            "publication without paid subscriptions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "subtitle": {"type": "string", "default": ""},
                "body_markdown": {
                    "type": "string",
                    "description": (
                        "Full post body as Markdown. Can be a short placeholder — the team "
                        "usually pastes/polishes the real text in the Substack editor; the "
                        "point of creating the draft here is pinning the slug + schedule."
                    ),
                },
                "slug": {
                    "type": "string",
                    "description": "The post URL slug to pin (lowercase-hyphenated).",
                },
                "audience": {
                    "type": "string",
                    "enum": list(AUDIENCE_VALUES),
                    "default": "everyone",
                    "description": "Who can read it. Non-'everyone' values need paid subs.",
                },
                "comment_permissions": {
                    "type": "string",
                    "enum": list(COMMENT_VALUES),
                    "default": "everyone",
                    "description": "Who may comment; 'none' disables comments.",
                },
                "send_email": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Whether publishing emails subscribers. Sending is IRREVERSIBLE; "
                        "schedule_draft/publish_draft additionally require an explicit "
                        "confirm_send_email when this is true."
                    ),
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Publication-level tags. Applying an unknown tag CREATES it "
                        "permanently, so new tags are refused unless allow_new_tags is true."
                    ),
                },
                "allow_new_tags": {"type": "boolean", "default": False},
                "section_id": {
                    "type": ["integer", "string", "null"],
                    "description": "Publication section id (see get_publication_settings).",
                },
                "seo_title": {"type": "string", "description": "Defaults to title."},
                "seo_description": {
                    "type": "string",
                    "description": "Defaults to subtitle.",
                },
            },
            "required": ["title", "body_markdown", "slug"],
        },
    },
    {
        "name": "update_post_settings",
        "description": (
            "Change settings on an existing draft without recreating it: audience, "
            "comment_permissions, send_email, send_free_preview, section_id, seo_title, "
            "seo_description, title, subtitle. Returns the settings as stored afterwards."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "draft_id": {"type": ["integer", "string"]},
                "audience": {"type": "string", "enum": list(AUDIENCE_VALUES)},
                "comment_permissions": {"type": "string", "enum": list(COMMENT_VALUES)},
                "send_email": {"type": "boolean"},
                "send_free_preview": {"type": "boolean"},
                "section_id": {"type": ["integer", "string", "null"]},
                "seo_title": {"type": "string"},
                "seo_description": {"type": "string"},
                "title": {"type": "string"},
                "subtitle": {"type": "string"},
            },
            "required": ["draft_id"],
        },
    },
    {
        "name": "apply_tags",
        "description": (
            "Attach publication tags to a draft. Tags are publication-level objects: an "
            "unknown tag is CREATED permanently and typos are durable, so this reports "
            "which tags are existing vs new and refuses to create new ones unless "
            "allow_new is true. Call it as its own confirmed step, not silently."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "draft_id": {"type": ["integer", "string"]},
                "tags": {"type": "array", "items": {"type": "string"}},
                "allow_new": {"type": "boolean", "default": False},
            },
            "required": ["draft_id", "tags"],
        },
    },
    {
        "name": "set_slug",
        "description": (
            "Set/replace the URL slug of an existing draft. Returns the updated slug and "
            "post_url. Fails on already-published posts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "draft_id": {"type": ["integer", "string"]},
                "slug": {"type": "string"},
            },
            "required": ["draft_id", "slug"],
        },
    },
    {
        "name": "schedule_draft",
        "description": (
            "Schedule a draft to publish at an exact instant. datetime_iso MUST carry a "
            "timezone offset (e.g. 2026-08-03T09:00:00-04:00); naive timestamps and past "
            "times are rejected. Returns the schedule AS STORED by Substack (read back "
            "from postSchedules), never an echo of the request.\n\n"
            "If the draft is set to email subscribers, this call REFUSES unless "
            "confirm_send_email is true — scheduling is a time-triggered public action and "
            "the email cannot be unsent. Show the user the full settings summary first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "draft_id": {"type": ["integer", "string"]},
                "datetime_iso": {"type": "string"},
                "confirm_send_email": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Set true ONLY after the user has confirmed that publishing will "
                        "email subscribers. Ignored when the draft has send_email false."
                    ),
                },
            },
            "required": ["draft_id", "datetime_iso"],
        },
    },
    {
        "name": "unschedule_draft",
        "description": "Cancel a draft's scheduled publication (it stays a draft).",
        "inputSchema": {
            "type": "object",
            "properties": {"draft_id": {"type": ["integer", "string"]}},
            "required": ["draft_id"],
        },
    },
    {
        "name": "get_draft",
        "description": (
            "Fetch one draft/post by id — the COMPLETE view: title, subtitle, slug, "
            "post_url, settings, attached tags (read from the association endpoint) and "
            "scheduled_for. Use this rather than list_drafts when the details matter."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"draft_id": {"type": ["integer", "string"]}},
            "required": ["draft_id"],
        },
    },
    {
        "name": "list_drafts",
        "description": (
            "List recent DRAFTS ONLY (id, title, slug, post_url, audience, comments, "
            "send_email), newest first; published posts are excluded. limit is capped "
            "at 25 — Substack rejects more with a bare 400. Substack's list payload is "
            "a NARROWER projection than get_draft: subtitle, SEO fields, section, tags "
            "and the schedule are not in it, and the response says so per field "
            "instead of reporting null — call get_draft for those."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 25}
            },
        },
    },
    {
        "name": "publish_draft",
        "description": (
            "PUBLISH a draft immediately, optionally emailing subscribers. The email cannot "
            "be unsent, so send_email=true additionally requires confirm_send_email=true. "
            "Call ONLY on an explicit user instruction to publish right now — the normal "
            "flow is schedule_draft."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "draft_id": {"type": ["integer", "string"]},
                "send_email": {"type": "boolean", "default": True},
                "confirm_send_email": {
                    "type": "boolean",
                    "default": False,
                    "description": "Required when send_email is true; user must have agreed.",
                },
                "share_automatically": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Auto-share to connected social accounts. Posts publicly elsewhere — "
                        "never enable without asking."
                    ),
                },
            },
            "required": ["draft_id"],
        },
    },
    {
        "name": "delete_draft",
        "description": "Delete a draft by id (published posts cannot be deleted here).",
        "inputSchema": {
            "type": "object",
            "properties": {"draft_id": {"type": ["integer", "string"]}},
            "required": ["draft_id"],
        },
    },
]


def tool_substack_status(_args):
    """Layered diagnosis: deps → config → session → publication access → Api ready."""
    info = {"config_path": CONFIG_PATH, "server_version": SERVER_VERSION}
    raw = raw_publication_url()
    try:
        info["publication_url"] = publication_url()
        if raw and raw.strip().rstrip("/") != info["publication_url"]:
            info["publication_url_note"] = (
                "normalized from %r — python-substack only resolves the "
                "https://<name>.substack.com form; consider fixing it in the config" % raw
            )
    except RuntimeError as e:
        info["publication_url"] = "NOT CONFIGURED: %s" % e
    try:
        import substack  # noqa: PLC0415

        info["python_substack"] = getattr(substack, "__version__", "installed")
    except ImportError:
        info["python_substack"] = "NOT INSTALLED — pip install python-substack"
    try:
        import pycookiecheat  # noqa: PLC0415,F401

        info["pycookiecheat"] = "installed (refresh_cookie available)"
    except ImportError:
        info["pycookiecheat"] = "not installed — refresh_cookie unavailable"
    cfg = load_config()
    configured_cf = cfg.get("cookie_file")
    if configured_cf:
        info["cookie_file"] = configured_cf  # per-client browser this config reads from
    act_as = (cfg.get("act_as") or "").strip()
    if act_as:
        info["act_as"] = act_as  # identity pin — refresh_cookie only accepts this login
    elif configured_cf and in_standard_install(configured_cf):
        info["identity_note"] = (
            "standard-install cookie_file with no identity pin (pre-0.4.0 state) — "
            "with several logins the wrong account can persist silently; the next "
            "no-arg refresh_cookie re-validates via the scan, or pin explicitly with "
            "refresh_cookie profile:"
        )

    cookies = current_cookies()
    info["cookie_configured"] = bool(cookies.get("substack.sid"))
    # Names only, and only the auth-relevant ones — analytics cookies are noise here,
    # and no cookie VALUE is ever reported.
    info["auth_cookies_present"] = sorted(
        k for k in cookies if k.startswith("substack.") or k == "cf_clearance"
    )
    info["cookies_stored"] = len(cookies)
    if not info["cookie_configured"]:
        info["api_ready"] = False
        info["fix"] = (
            "no substack.sid — run refresh_cookie, or paste it into %s" % CONFIG_PATH
        )
        return text_result(info)

    # 1) Session validity, independent of any publication resolution.
    try:
        ok, probe = probe_session(cookies)
    except Exception as e:  # noqa: BLE001
        info["session_valid"] = "unknown"
        info["api_ready"] = False
        info["session_error"] = "could not reach Substack: %s" % str(e)[:200]
        return text_result(info)
    info["session_valid"] = ok
    if not ok:
        info["api_ready"] = False
        info["fix"] = "session cookie is invalid or expired — run refresh_cookie"
        return text_result(info)
    info["logged_in_as"] = probe["handle"]
    info["primary_publication"] = probe["primary"]
    info["available_publications"] = probe["subdomains"]
    if act_as:
        info["act_as_matches"] = identity_matches(probe, act_as)
        if not info["act_as_matches"]:
            # Same gate as get_api — a wrong-identity session must not report
            # api_ready, or the caller would draft/publish as the wrong account.
            info["api_ready"] = False
            info["fix"] = (
                "session is logged in as %r, not the pinned act_as=%r — run "
                "refresh_cookie (optionally with profile:, see list_profiles), "
                "or change/remove act_as in %s" % (probe["handle"], act_as, CONFIG_PATH)
            )
            return text_result(info)
    elif legacy_unpinned(cfg):
        # Mirror get_api's write block: the legacy state must never report
        # api_ready — logged_in_as above shows WHO the stale session really is.
        info["api_ready"] = False
        info["fix"] = (
            "pre-0.4.0 standard-install cookie_file with no identity pin — the "
            "stored session (logged_in_as above) may be the wrong login; run "
            "refresh_cookie (re-validates via the profile scan) or refresh_cookie "
            "profile: before reading or writing as this account"
        )
        return text_result(info)

    # 2) Does the configured publication belong to this account?
    try:
        sub = configured_subdomain()
    except RuntimeError:
        info["api_ready"] = False
        info["fix"] = "configure publication_url in %s" % CONFIG_PATH
        return text_result(info)
    if not sub:
        info["api_ready"] = False
        info["fix"] = (
            "publication_url must be https://<name>.substack.com (custom domains are not "
            "resolvable by python-substack)"
        )
        return text_result(info)
    info["configured_publication"] = sub
    if not publication_accessible(probe, sub):
        info["api_ready"] = False
        info["fix"] = (
            "account %s has no access to %r — fix publication_url (available: %s) or refresh "
            "the cookie from a session that owns it"
            % (probe["handle"], sub, accessible_publications(probe) or "(none)")
        )
        return text_result(info)

    # 3) Full client construction (the step that used to fail opaquely).
    try:
        get_api(fresh=True)
        info["api_ready"] = True
    except Exception as e:  # noqa: BLE001
        reset_api()
        info["api_ready"] = False
        info["api_error"] = str(e)[:300]
    return text_result(info)


def tool_refresh_cookie(args):
    try:
        import pycookiecheat  # noqa: PLC0415
    except ImportError:
        raise RuntimeError(
            "pycookiecheat is not installed in the server's environment — "
            "pip install pycookiecheat, or paste the cookie into %s manually" % CONFIG_PATH
        )

    raw_browser = (args.get("browser") or "").strip().lower()
    browser = raw_browser or "chrome"
    act_as = (load_config().get("act_as") or "").strip()
    profile_arg = (args.get("profile") or "").strip()
    if profile_arg and args.get("cookie_file"):
        raise ValueError(
            "pass either profile or cookie_file, not both — profile selects a standard-"
            "install browser profile, cookie_file targets an arbitrary Cookies DB"
        )
    if args.get("confirm_switch") and not profile_arg:
        raise ValueError(
            "confirm_switch only applies to an explicit profile choice — pass it "
            "together with profile"
        )
    if profile_arg and browser == "firefox":
        raise RuntimeError(
            "profile selection reads Chrome-family profiles and cannot be combined "
            "with browser=firefox"
        )
    legacy_revalidated = False
    if profile_arg:
        # An explicit profile deliberately bypasses a config-persisted cookie_file —
        # it is the only way to correct a stored path that points at the wrong login.
        prof = resolve_profile_selector(profile_arg, browser)
        cookie_file = dict(chrome_profile_cookie_files(browser)).get(prof["dir"])
        if not cookie_file:
            raise RuntimeError(
                "profile %r (%s) has no Cookies database — launch the browser with "
                "that profile and log in to substack.com there first"
                % (prof["name"] or prof["dir"], prof["dir"])
            )
        chosen_profile = prof["dir"]
    else:
        cookie_file = resolve_cookie_file(args.get("cookie_file"))
        if cookie_file and browser == "firefox":
            raise RuntimeError(
                "cookie_file targets a Chrome-family Cookies SQLite file and cannot be "
                "combined with browser=firefox"
            )
        legacy_family = (
            standard_install_browser(cookie_file)
            if cookie_file and not args.get("cookie_file") and not act_as
            else None
        )
        if legacy_family and (not raw_browser or raw_browser == legacy_family):
            # Legacy-unpinned state: a standard-install path persisted by a
            # pre-0.4.0 scan (or first-match pick) with no identity pin — the
            # explicit-profile flow always pins, so this combination can only
            # be inherited. A wrong login persisted back then would otherwise
            # survive every guard (pin skips the scan, no act_as to fail), so
            # ignore the stored path once and re-validate via the scan below —
            # scanning the family that produced the path (a Brave/Chromium pin
            # must not be "re-validated" against Chrome's profiles). An
            # EXPLICIT browser argument naming a different family wins: the
            # stored path is honored as-is.
            browser = legacy_family
            cookie_file = None
            legacy_revalidated = True
        if cookie_file and not os.path.isfile(cookie_file):
            raise RuntimeError(
                "cookie_file %s does not exist — for a dedicated per-client browser the "
                "path is <user-data-dir>/Default/Cookies, and the browser must have been "
                "launched (and logged in to Substack) at least once" % cookie_file
            )
        chosen_profile = cookie_file or "(default profile)"
    errors = []
    cookies = read_browser_cookies(pycookiecheat, browser, cookie_file, errors)
    try:
        target = configured_subdomain()
    except RuntimeError:
        target = None

    profiles_scanned = []
    matched_pub, probe = session_reaches(cookies, target)
    matched = matched_pub and identity_matches(probe, act_as)
    act_as_switch = False
    if (profile_arg and act_as and matched_pub and not matched
            and args.get("confirm_switch")):
        # The human explicitly confirmed acting as this other login (same trust
        # contract as schedule_draft's confirm_send_email) — accept the session
        # now; the pin is rewritten in the persistence block below.
        matched = True
        act_as_switch = True
    if (target or act_as) and not cookie_file and browser != "firefox":
        # The scan (deterministic code — cookie values never surface, the model only
        # sees profile names + handles): check EVERY profile of the standard browser
        # install, even when the default profile qualifies — "the default reaches
        # the publication" is not identity, and trusting it silently is exactly the
        # wrong-account pick this exists to prevent. One qualifying IDENTITY
        # proceeds; several mean the choice is the operator's, not directory-sort
        # order's. A dedicated per-client browser is NOT discoverable this way —
        # that's what cookie_file is for.
        # Human-readable names for every scanned profile, so profiles_scanned and
        # the refusal/no-access errors are self-sufficient (the profile argument
        # accepts dir name, display name, or email — don't force a list_profiles
        # round-trip to tell 'Profile 2' from 'Juliet — TIE'). Best-effort: a
        # missing Local State just leaves names blank, never blocks the scan.
        ls_meta = {}
        try:
            ls_meta = {p["dir"]: p for p in local_state_profiles(browser)}
        except Exception:  # noqa: BLE001
            pass
        hits = []
        for name, cf in chrome_profile_cookie_files(browser):
            if name == "Default":
                # The scan only runs with no cookie_file, so the initial read
                # above WAS this profile (pycookiecheat's default) — reuse it
                # rather than decrypting the same DB again (each decrypt is a
                # separate macOS Keychain prompt).
                c, got_pub, p = cookies, matched_pub, probe
            else:
                c = read_browser_cookies(pycookiecheat, browser, cf, errors)
                got_pub, p = session_reaches(c, target)
            got = got_pub and identity_matches(p, act_as)
            meta = ls_meta.get(name) or {}
            entry = {"profile": name, "reaches_publication": got_pub,
                     "logged_in_as": (p or {}).get("handle")}
            if meta.get("name"):
                entry["name"] = meta["name"]
            if meta.get("email"):
                entry["email"] = meta["email"]
            if act_as:
                entry["matches_act_as"] = got
            profiles_scanned.append(entry)
            if got:
                hits.append((name, cf, c, p))
        # Group by identity: two profiles logged into the SAME account are not an
        # ambiguity (either acts as that identity); two different handles are —
        # no matter which of them the default profile happens to be.
        idents = {}
        for h in hits:
            idents.setdefault((h[3] or {}).get("handle") or h[0], []).append(h)
        default_handle = (probe or {}).get("handle")
        if matched and default_handle not in idents:
            idents[default_handle or "(default profile)"] = []
        if len(idents) > 1:
            cands = [{"profile": n, "name": (ls_meta.get(n) or {}).get("name", ""),
                      "email": (ls_meta.get(n) or {}).get("email", ""),
                      "logged_in_as": (p or {}).get("handle")} for n, cf, c, p in hits]
            if matched and default_handle not in {c["logged_in_as"] for c in cands}:
                cands.insert(0, {"profile": "(default profile)", "name": "",
                                 "logged_in_as": default_handle})
            raise RuntimeError(
                "%d Substack logins reach %r — refusing to guess between accounts, "
                "nothing was stored. Candidates: %s. Re-run refresh_cookie with "
                'profile: "<name>" (or set act_as in %s to pin an identity).'
                % (len(idents), target, cands, CONFIG_PATH)
            )
        if hits and not matched:
            # A single qualifying identity, found by the scan: prefer its Default-
            # dir session when it has several, for continuity with older behavior.
            only = next(iter(idents.values()))
            pick = next((h for h in only if h[0] == "Default"), only[0])
            chosen_profile, cookie_file, cookies, probe = pick
            matched = True
    if not cookies:
        raise RuntimeError(
            "could not read cookies from %s (is the browser installed, are you logged in "
            "to substack.com there, was Keychain access granted?): %s"
            % (chosen_profile, " | ".join(errors)[:400])
        )
    if "substack.sid" not in cookies:
        raise RuntimeError(
            "no substack.sid among %s cookies for substack.com — log in to Substack in "
            "that browser first (found: %s)%s"
            % (chosen_profile, sorted(cookies.keys()),
               "; profiles scanned: %s" % profiles_scanned if profiles_scanned else "")
        )
    if not matched and act_as and (
        matched_pub or any(e.get("reaches_publication") for e in profiles_scanned)
    ):
        if profile_arg:
            raise RuntimeError(
                "profile %r is logged in as %r, but the config pins act_as=%r — "
                "re-run with confirm_switch: true to SWITCH the pinned identity "
                "(only on the user's explicit ask), pick a profile matching the "
                "pin (list_profiles), or edit act_as in %s."
                % (chosen_profile, (probe or {}).get("handle"), act_as, CONFIG_PATH)
            )
        raise RuntimeError(
            "found Substack session(s) reaching %r, but none logged in as the pinned "
            "act_as=%r (found: %s). Log in as that identity, pick a profile explicitly "
            "(list_profiles), or change/remove act_as in %s. Note: act_as matches the "
            "Substack handle — if you configured an email that never matches, use the "
            "handle."
            % (target, act_as,
               profiles_scanned
               or [{"profile": chosen_profile,
                    "logged_in_as": (probe or {}).get("handle")}],
               CONFIG_PATH)
        )
    if (target or act_as) and not matched:
        raise RuntimeError(
            "found Substack session(s), but none that can access %r. Checked: %s. "
            "Log in to that publication's account in one of this browser's profiles, "
            "pass profile to pick a specific login, or pass cookie_file pointing at "
            "the right (e.g. dedicated per-client) browser."
            % (target, profiles_scanned or [chosen_profile])
        )

    cfg = load_config()
    cfg["cookies"] = cookies
    cfg.setdefault("publication_url", os.environ.get("SUBSTACK_PUBLICATION_URL", ""))
    if args.get("cookie_file") or profile_arg or (profiles_scanned and matched and cookie_file):
        # Persist the path that worked — explicitly passed, chosen by profile, or
        # found by the scan — so the next no-arg refresh reads the same profile.
        cfg["cookie_file"] = cookie_file
    elif legacy_revalidated and matched and not cookie_file:
        # The re-validation was won by the DEFAULT profile, not the stale
        # pre-0.4.0 path — drop that path, or the config would loop in the
        # legacy write-blocked state forever.
        cfg.pop("cookie_file", None)
    act_as_persisted = False
    act_as_prev = None
    if profile_arg and (probe or {}).get("handle"):
        if not cfg.get("act_as"):
            # Pin the identity the user just chose explicitly, so later no-arg
            # refreshes stay deterministic even if more logins appear.
            cfg["act_as"] = probe["handle"]
            act_as_persisted = True
        elif act_as_switch:
            # Confirmed switch: rewrite the pin to the newly chosen login.
            act_as_prev = cfg["act_as"]
            cfg["act_as"] = probe["handle"]
    elif (profiles_scanned and matched
          and not cfg.get("act_as") and (probe or {}).get("handle")):
        # The scan validated exactly one identity — pin it, whether it was
        # adopted from a profile hit or the default profile won. There was no
        # choice to make, and the pin keeps "standard-install cookie_file with
        # no act_as" an exclusively pre-0.4.0 state that write tools refuse.
        cfg["act_as"] = probe["handle"]
        act_as_persisted = True
    save_config(cfg)
    reset_api()

    result = {
        "saved_to": CONFIG_PATH,
        # Names only — cookie VALUES never leave the server.
        "auth_cookies_saved": sorted(
            k for k in cookies if k.startswith("substack.") or k == "cf_clearance"
        ),
        "cookies_stored": len(cookies),
        "browser": browser,
        "cookie_file": cookie_file or "(default profile)",
        "profile": chosen_profile,
    }
    if cfg.get("act_as"):
        result["act_as"] = cfg["act_as"]
    if act_as_persisted:
        result["act_as_persisted"] = True
    if act_as_prev:
        result["act_as_changed"] = {"from": act_as_prev, "to": cfg.get("act_as")}
    if profiles_scanned:
        result["profiles_scanned"] = profiles_scanned
    try:
        api = get_api(fresh=True)
        profile = api.get_user_profile()
        result["cookie_valid"] = True
        result["logged_in_as"] = profile.get("handle") or profile.get("name")
    except Exception as e:  # noqa: BLE001
        reset_api()
        result["cookie_valid"] = False
        result["validation_error"] = str(e)[:300]
    return text_result(result)


def tool_list_profiles(args):
    """Names/emails from the browser's plaintext profile cache — reads no cookie
    data and triggers no Keychain prompt."""
    browser = (args.get("browser") or "chrome").lower()
    root = args.get("root")
    if root:
        root = os.path.expanduser(root)
    path = local_state_path(browser, root)
    if not path or not os.path.isfile(path):
        raise RuntimeError(
            'no "Local State" file at %s — is %s installed? For a dedicated '
            "--user-data-dir browser pass root=<user-data-dir>."
            % (path or "(unknown browser %r)" % browser, browser)
        )
    profiles = local_state_profiles(browser, root)
    return text_result({
        "browser": browser,
        "root": os.path.dirname(path),
        "profiles": profiles,
        "count": len(profiles),
        "note": ("names/emails from the browser's plaintext profile cache — "
                 "no cookies were read"),
    })


def tool_get_publication_settings(_args):
    return text_result(publication_capabilities(get_api()))


def tool_create_draft(args):
    title = (args.get("title") or "").strip()
    body = args.get("body_markdown") or ""
    slug = validate_slug((args.get("slug") or "").strip())
    if not title or not body:
        raise ValueError("title and body_markdown are required")
    api = get_api()
    caps = publication_capabilities(api)
    audience = args.get("audience") or "everyone"
    # ALWAYS explicit: python-substack copies `audience` into this when it is None,
    # which would silently restrict comments on a paid-audience post.
    comments = args.get("comment_permissions") or "everyone"
    validate_settings(audience, comments, caps)

    section_id = args.get("section_id")
    if section_id not in (None, ""):
        known = {str(s.get("id")) for s in caps.get("sections") or []}
        if known and str(section_id) not in known:
            raise ValueError(
                "section_id %r is not one of this publication's sections %s"
                % (section_id, sorted(known))
            )

    tag_plan = resolve_tags(api, args.get("tags"), caps)
    if tag_plan["new"] and not args.get("allow_new_tags"):
        raise ValueError(
            "these tags do not exist on the publication and would be created permanently: "
            "%s (existing: %s). Confirm with the user, then retry with allow_new_tags=true "
            "or reuse existing tags." % (tag_plan["new"], tag_plan["existing"])
        )

    subtitle = (args.get("subtitle") or "").strip()
    out = api.create_draft_from_markdown(
        title=title,
        markdown=body,
        subtitle=subtitle,
        slug=slug,
        audience=audience,
        write_comment_permissions=comments,
        search_engine_title=args.get("seo_title") or title,
        search_engine_description=args.get("seo_description") or subtitle or None,
        draft_section_id=section_id if section_id not in (None, "") else None,
        tags=None,  # applied below so new-tag creation stays opt-in
    )
    draft = out["draft"]
    draft_id = draft.get("id")

    # should_send_email isn't a create parameter — set it via put_draft.
    send_email = args.get("send_email", True)
    if send_email is not True:
        api.put_draft(draft_id, should_send_email=bool(send_email))
    if tag_plan["normalized"]:
        api.add_tags_to_post(draft_id, tag_plan["normalized"])

    # Report what Substack STORED, not what we asked for.
    draft = api.get_draft(draft_id)
    summary = draft_summary(draft)
    if tag_plan["normalized"]:
        attached = read_post_tags(api, draft_id)
        summary["tags"] = attached["names"]
        summary["tags_requested"] = tag_plan["normalized"]
        missing = [t for t in tag_plan["normalized"] if t not in attached["names"]]
        if missing:
            summary["tags_not_attached"] = missing
    requested = {
        "audience": audience,
        "comment_permissions": comments,
        "send_email": bool(send_email),
    }
    drift = {
        k: {"requested": v, "stored": summary.get(k)}
        for k, v in requested.items()
        if summary.get(k) is not None and summary.get(k) != v
    }
    if drift:
        summary["settings_drift"] = drift
        summary["settings_drift_note"] = (
            "Substack stored different values than requested — report the STORED ones"
        )
    got = summary.get("slug")
    if got and got != slug:
        summary["warning"] = (
            "requested slug %r but Substack stored %r (taken or normalized) — the post_url "
            "above reflects what was STORED; update social copy accordingly or set_slug again"
            % (slug, got)
        )
    elif not got:
        summary["warning"] = (
            "the draft was created but the response did not confirm slug %r — verify with "
            "get_draft (or re-apply via set_slug) before using any post URL in social copy"
            % slug
        )
    return text_result(summary)


def tool_set_slug(args):
    slug = validate_slug((args.get("slug") or "").strip())
    api = get_api()
    draft = api.put_draft(args["draft_id"], slug=slug)
    summary = draft_summary(draft)
    if summary.get("slug") != slug:
        summary["warning"] = "Substack stored slug %r, not %r" % (summary.get("slug"), slug)
    return text_result(summary)


def tool_update_post_settings(args):
    api = get_api()
    draft_id = args["draft_id"]
    caps = publication_capabilities(api)
    current = api.get_draft(draft_id)
    audience = args.get("audience") or current.get("audience") or "everyone"
    comments = (
        args.get("comment_permissions")
        or current.get("write_comment_permissions")
        or "everyone"
    )
    validate_settings(audience, comments, caps)

    payload = {"audience": audience, "write_comment_permissions": comments}
    for arg, field in (
        ("send_email", "should_send_email"),
        ("send_free_preview", "should_send_free_preview"),
        ("seo_title", "search_engine_title"),
        ("seo_description", "search_engine_description"),
        ("title", "draft_title"),
        ("subtitle", "draft_subtitle"),
    ):
        if arg in args and args[arg] is not None:
            payload[field] = args[arg]
    if "section_id" in args:
        payload["draft_section_id"] = args["section_id"] or None
    api.put_draft(draft_id, **payload)
    return text_result(draft_summary(api.get_draft(draft_id)))


def tool_apply_tags(args):
    api = get_api()
    draft_id = args["draft_id"]
    plan = resolve_tags(api, args.get("tags"), publication_capabilities(api))
    if not plan["normalized"]:
        raise ValueError("no usable tags after normalization")
    if plan["new"] and not args.get("allow_new"):
        raise ValueError(
            "these tags would be CREATED on the publication permanently: %s (existing: %s). "
            "Confirm with the user, then retry with allow_new=true."
            % (plan["new"], plan["existing"])
        )
    api.add_tags_to_post(draft_id, plan["normalized"])
    summary = draft_summary(api.get_draft(draft_id))
    # Report the attachments Substack stored, not the list we just sent.
    attached = read_post_tags(api, draft_id)
    summary["tags"] = attached["names"]
    summary["tags_requested"] = plan["normalized"]
    if attached.get("note"):
        summary["tags_note"] = attached["note"]
    missing = [t for t in plan["normalized"] if t not in attached["names"]]
    if missing:
        summary["tags_not_attached"] = missing
        summary["warning"] = (
            "these tags were requested but are not attached according to the server: %s"
            % missing
        )
    return text_result(summary)


def tool_schedule_draft(args):
    dt = parse_iso_aware(args.get("datetime_iso"))
    if dt <= datetime.now(timezone.utc):
        raise ValueError(
            "datetime_iso %s is in the past (now %s UTC)"
            % (args.get("datetime_iso"), datetime.now(timezone.utc).isoformat(timespec="seconds"))
        )
    api = get_api()
    draft_id = args["draft_id"]
    before = api.get_draft(draft_id)
    if before.get("is_published"):
        raise ValueError("draft %s is already published — cannot schedule it" % draft_id)
    # Scheduling is a time-triggered public action, and the email cannot be unsent.
    if before.get("should_send_email") and not args.get("confirm_send_email"):
        raise ValueError(
            "this post is set to EMAIL SUBSCRIBERS when it publishes, which cannot be "
            "undone. Show the user the settings summary (audience=%r, comments=%r, "
            "send_email=True, publish at %s) and get an explicit yes, then retry with "
            "confirm_send_email=true — or call update_post_settings(send_email=false) first."
            % (
                before.get("audience"),
                before.get("write_comment_permissions"),
                dt.isoformat(),
            )
        )
    api.schedule_draft(draft_id, dt)
    # Read the schedule back from the server — postSchedules is the only place it
    # lives, and echoing the request would defeat the point of verifying.
    summary = draft_summary(api.get_draft(draft_id))
    stored = summary.get("scheduled_for")
    summary["requested_for"] = dt.isoformat()
    summary["utc_equivalent"] = dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    if not stored:
        summary["warning"] = (
            "Substack did not report a schedule after the call — verify in the editor "
            "(%s) before relying on it" % summary.get("editor_url")
        )
    elif parse_iso_aware(stored) != dt:
        summary["warning"] = (
            "stored schedule %s differs from the requested %s — the STORED value is what "
            "will fire" % (stored, dt.isoformat())
        )
    summary["undo"] = "call unschedule_draft to cancel while it is still pending"
    return text_result(summary)


def tool_unschedule_draft(args):
    api = get_api()
    api.unschedule_draft(args["draft_id"])
    return text_result(draft_summary(api.get_draft(args["draft_id"])))


def tool_get_draft(args):
    api = get_api()
    draft_id = args["draft_id"]
    summary = draft_summary(api.get_draft(draft_id))
    # Tags live only in the association endpoint, so read them here (one extra
    # call). list_drafts deliberately skips this to avoid N+1 requests.
    try:
        attached = read_post_tags(api, draft_id)
        summary["tags"] = attached["names"]
        if attached.get("note"):
            summary["tags_note"] = attached["note"]
    except Exception as e:  # noqa: BLE001
        summary["tags_note"] = "could not read attached tags: %s" % str(e)[:150]
    return text_result(summary)


def tool_list_drafts(args):
    api = get_api()
    # Substack rejects limit > 25 with a bare APIError(400): Invalid value.
    limit = max(1, min(25, int(args.get("limit") or 10)))
    # Without filter="draft" the endpoint ALSO returns published posts, so on an
    # old publication the page fills with years-old articles and real drafts
    # never surface (operator-reported 2026-08-25; verified live).
    drafts = unwrap_items(
        api.get_drafts(filter="draft", limit=limit), "posts", "drafts", "results"
    )
    drafts.sort(
        key=lambda d: (
            (d.get("draft_updated_at") or d.get("draft_created_at") or "")
            if isinstance(d, dict) else ""
        ),
        reverse=True,
    )
    return text_result([draft_summary(d) for d in drafts])


def tool_publish_draft(args):
    api = get_api()
    draft_id = args["draft_id"]
    send = bool(args.get("send_email", True))
    if send and not args.get("confirm_send_email"):
        raise ValueError(
            "publishing now with send_email=true emails every subscriber and cannot be "
            "undone. Get an explicit yes from the user, then retry with "
            "confirm_send_email=true — or pass send_email=false to publish without email."
        )
    share = bool(args.get("share_automatically", False))
    api.prepublish_draft(draft_id)
    api.publish_draft(draft_id, send=send, share_automatically=share)
    summary = draft_summary(api.get_draft(draft_id))
    summary["published"] = True
    summary["emailed_subscribers"] = send
    summary["shared_automatically"] = share
    return text_result(summary)


def tool_delete_draft(args):
    api = get_api()
    api.delete_draft(args["draft_id"])
    return text_result({"deleted": True, "draft_id": args["draft_id"]})


TOOL_HANDLERS = {
    "substack_status": tool_substack_status,
    "refresh_cookie": tool_refresh_cookie,
    "list_profiles": tool_list_profiles,
    "get_publication_settings": tool_get_publication_settings,
    "create_draft": tool_create_draft,
    "update_post_settings": tool_update_post_settings,
    "apply_tags": tool_apply_tags,
    "set_slug": tool_set_slug,
    "schedule_draft": tool_schedule_draft,
    "unschedule_draft": tool_unschedule_draft,
    "get_draft": tool_get_draft,
    "list_drafts": tool_list_drafts,
    "publish_draft": tool_publish_draft,
    "delete_draft": tool_delete_draft,
}


# ---------------------------------------------------------------- rpc plumbing


def handle_request(msg):
    req_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}

    if method == "initialize":
        client_proto = params.get("protocolVersion") or FALLBACK_PROTOCOL
        reply(
            req_id,
            {
                "protocolVersion": client_proto,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    elif method == "ping":
        reply(req_id, {})
    elif method == "tools/list":
        reply(req_id, {"tools": TOOLS})
    elif method == "resources/list":
        reply(req_id, {"resources": []})
    elif method == "prompts/list":
        reply(req_id, {"prompts": []})
    elif method == "tools/call":
        name = params.get("name")
        handler = TOOL_HANDLERS.get(name)
        if not handler:
            reply_error(req_id, -32602, "unknown tool: %s" % name)
            return
        try:
            reply(req_id, handler(params.get("arguments") or {}))
        except Exception as e:  # noqa: BLE001
            log("tool %s error: %s\n%s" % (name, e, traceback.format_exc()))
            reply(
                req_id,
                {"content": [{"type": "text", "text": "ERROR: %s" % e}], "isError": True},
            )
    else:
        if req_id is not None:
            reply_error(req_id, -32601, "method not found: %s" % method)
        # notifications (initialized, cancelled, ...) are ignored


def main():
    log("%s v%s starting (config=%s)" % (SERVER_NAME, SERVER_VERSION, CONFIG_PATH))
    workers = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log("bad JSON line ignored")
            continue
        if msg.get("method") == "tools/call":
            t = threading.Thread(target=handle_request, args=(msg,), daemon=True)
            t.start()
            workers.append(t)
        else:
            handle_request(msg)
    # stdin closed: let in-flight tool calls flush their replies before exiting
    deadline = time.time() + 30
    for t in workers:
        t.join(max(0, deadline - time.time()))
    log("stdin closed — exiting")


if __name__ == "__main__":
    main()
