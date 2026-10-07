#!/usr/bin/env python3
"""tie-substack: local MCP server (stdio) for scheduling Substack posts, one server for
several client publications.

Substack has no official write API, so this server wraps the community
`python-substack` library (Substack's internal endpoints) to create drafts, pin the
post slug (=> the public URL is known BEFORE publication), and schedule publication.
It exists so the tie-social skill can run its Substack leg from Claude surfaces
(Cowork/Desktop) without any credential ever entering the model context.

0.5.0: ONE server entry serves every client.
  - A client registry (~/.tie-substack/clients.json, 0600) keys on the brand slug used
    by the tie-social plugin (brands/<slug>/brand.json -> substack.client) and holds the
    publication URL, the bound cookie source (a standard browser profile or a dedicated
    user-data dir) and the pinned identity (`act_as`).
  - One session per client (~/.tie-substack/sessions/<slug>.json, 0600). Values never
    leave this process: tools report cookie NAMES, handles and outcomes only.
  - Every Substack-facing tool takes `client`; there is no "current client". Every
    publication-scoped tool also takes `expected_publication`: before the handler runs
    the dispatcher compares the canonical host of that value, the registry's publication
    for the client and the publication the live session actually reaches, and refuses on
    any mismatch, naming all three. Every response echoes client, publication and identity.
  - Three tool scopes, each handed only its own facade (structural, not a convention):
      registry    local only, no network: list_clients, add_client, list_browser_profiles,
                  open_client_browser
      probe       identity and access probes: clients_status, bind_client, refresh_session
      publication the full python-substack client, only after the check above
  - Sessions switch per call; an expired session is re-read once from the client's bound
    cookie source, then the call fails with the fix named.

Env:
  TIE_SUBSTACK_HOME        registry home (default ~/.tie-substack); tests point it at a temp dir
  TIE_SUBSTACK_CLIENT +    an explicit substack.sid for exactly that client (CI-style setups);
  SUBSTACK_SESSION_TOKEN   ignored for every other client

CLI (used by install.command for the 0.4 -> 0.5 migration; offline except check-clients):
  server.py migrate --desktop-config PATH [--default-slug tie] [--installed-server PATH]
  server.py check-clients
  server.py switch --desktop-config PATH --command PY --server SERVER [--failed a,b]
  server.py rollback --desktop-config PATH [--last]

Protocol: MCP over stdio, newline-delimited JSON-RPC 2.0. Requires: python-substack;
pycookiecheat optional (bind_client / refresh_session need it).
"""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

SERVER_NAME = "tie-substack"
SERVER_VERSION = "0.5.0"
CONFIG_FORMAT_VERSION = 1

AUDIENCE_VALUES = ("everyone", "only_free", "only_paid", "founding")
COMMENT_VALUES = ("none", "only_paid", "everyone")  # "none" == comments disabled
PAID_AUDIENCE_VALUES = ("only_free", "only_paid", "founding")
FALLBACK_PROTOCOL = "2025-06-18"
SESSION_TTL_SECONDS = 600  # a validated session is trusted this long between probes
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
CLIENT_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
RESERVED_NAMES = ("clients.json", "migration.json")
RESERVED_DIR_PREFIXES = ("sessions", "backup-", "legacy", "venv", "python", "cache", "bin")

_write_lock = threading.Lock()
_api_lock = threading.Lock()
_api_cache = {}  # slug -> {"api", "handle", "email", "sid", "validated_at"}


def home():
    return os.path.expanduser(os.environ.get("TIE_SUBSTACK_HOME", "~/.tie-substack"))


def clients_path():
    return os.path.join(home(), "clients.json")


def sessions_dir():
    return os.path.join(home(), "sessions")


def session_path(slug):
    return os.path.join(sessions_dir(), "%s.json" % slug)


def journal_path():
    return os.path.join(home(), "migration.json")


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


class ClientError(RuntimeError):
    """A refusal the caller can act on (unknown client, unbound, mismatch)."""


class PublicationMismatch(ClientError):
    def __init__(self, client, expected, registry, live):
        self.client, self.expected, self.registry, self.live = client, expected, registry, live
        super().__init__(
            "publication mismatch for client %r: expected_publication=%r, registry=%r, "
            "live session reaches=%r. Nothing was done. Fix the brand (publication.domain), the "
            "registry (add_client / bind_client) or the session (refresh_session) so all three "
            "agree." % (client, expected, registry, live if live is not None else "(not probed)")
        )


# ---------------------------------------------------------------- private storage


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("%s is unreadable: %s" % (path, e))


def write_private_json(path, data):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    try:
        os.chmod(d, stat.S_IRWXU)
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600: sessions hold cookies
    os.replace(tmp, path)


def load_clients():
    data = read_json(clients_path(), {"version": CONFIG_FORMAT_VERSION, "clients": {}})
    if not isinstance(data, dict) or not isinstance(data.get("clients"), dict):
        raise RuntimeError("%s has an unexpected shape" % clients_path())
    return data


def save_clients(data):
    data["version"] = CONFIG_FORMAT_VERSION
    write_private_json(clients_path(), data)


def validate_client_slug(slug):
    s = (slug or "").strip().lower()
    if not CLIENT_RE.match(s):
        raise ClientError(
            "client %r is not a valid slug (lowercase letters, digits, hyphens; the brand's "
            "identity.slug)" % slug
        )
    return s


def client_record(slug):
    s = validate_client_slug(slug)
    clients = load_clients()["clients"]
    rec = clients.get(s)
    if rec is None:
        raise ClientError(
            "unknown client %r; configured clients: %s. Add it with add_client, then "
            "bind_client." % (s, ", ".join(sorted(clients)) or "(none)")
        )
    return s, rec


def update_client(slug, **fields):
    data = load_clients()
    rec = data["clients"].setdefault(slug, {})
    rec.update(fields)
    save_clients(data)
    return rec


def load_session(slug):
    return read_json(session_path(slug), {})


def save_session(slug, cookies, probe=None, source_path=None):
    write_private_json(session_path(slug), {
        "cookies": cookies,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "handle": (probe or {}).get("handle"),
        "source_path": source_path,
    })


def session_cookies(slug):
    """The client's stored cookies. An env token applies to exactly the client named by
    TIE_SUBSTACK_CLIENT (CI-style setups) and never to anyone else."""
    token = os.environ.get("SUBSTACK_SESSION_TOKEN")
    if token and os.environ.get("TIE_SUBSTACK_CLIENT", "").strip().lower() == slug:
        return {"substack.sid": token}
    return load_session(slug).get("cookies") or {}


# ---------------------------------------------------------------- URLs


def normalize_publication_url(url):
    """Force the https://<name>.substack.com form python-substack's resolver requires."""
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if u.lower().startswith("http://"):
        u = "https://" + u[len("http://"):]
    elif not u.lower().startswith("https://"):
        u = "https://" + u
    return u


SUBDOMAIN_RE = re.compile(r"^https://([^./]+)\.substack\.com$", re.I)


def subdomain_of(url):
    m = SUBDOMAIN_RE.match(normalize_publication_url(url))
    return m.group(1).lower() if m else None


def canonical_host(value):
    """Lower-case host of a publication given as URL, host, or host/path."""
    v = (value or "").strip().lower()
    v = re.sub(r"^https?://", "", v)
    v = v.split("/")[0].split("?")[0]
    return v[4:] if v.startswith("www.") else v


def cookies_string(cookies):
    return "; ".join("%s=%s" % (k, v) for k, v in cookies.items())


# ---------------------------------------------------------------- browsers (macOS)

CHROME_FAMILY_DATA_DIRS = {
    "chrome": "~/Library/Application Support/Google/Chrome",
    "chromium": "~/Library/Application Support/Chromium",
    "brave": "~/Library/Application Support/BraveSoftware/Brave-Browser",
}
BROWSER_APPS = {"chrome": "Google Chrome", "chromium": "Chromium", "brave": "Brave Browser"}


def chrome_profile_cookie_files(browser, root=None):
    """(profile_name, cookie_file) for every profile of the browser's STANDARD data dir
    (Default, Profile 1, ...). A dedicated --user-data-dir lives elsewhere and is bound
    explicitly (bind_client user_data_dir=)."""
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
    """Which browser family's STANDARD install contains this cookie path, else None."""
    if not path:
        return None
    p = os.path.expanduser(path)
    for b, root in CHROME_FAMILY_DATA_DIRS.items():
        if p.startswith(os.path.expanduser(root) + os.sep):
            return b
    return None


def local_state_path(browser, root=None):
    base = root or CHROME_FAMILY_DATA_DIRS.get(browser)
    if not base:
        return None
    return os.path.join(os.path.expanduser(base), "Local State")


def local_state_profiles(browser, root=None):
    """[{dir, name, email}] from the browser's plaintext 'Local State' (profile.info_cache).
    Names/emails only; never touches the Cookies DB or the Keychain."""
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
    """The one profile a human-friendly selector means: directory name, display name or
    email, case-insensitive, exact first, substring only when nothing matches exactly."""
    profiles = local_state_profiles(browser, root)
    if not profiles:
        raise RuntimeError(
            'no browser profiles found: no "Local State" file under %s (is %s installed?)'
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
            "profile %r does not match any %s profile; available: %s (see list_browser_profiles)"
            % (selector, browser, profiles)
        )
    raise ValueError(
        "profile %r matches %d profiles: %s; use the exact directory name, display name or "
        "email (see list_browser_profiles)" % (selector, len(cands), cands)
    )


def read_browser_cookies(pycookiecheat, browser, cookie_file, errors):
    """One profile's substack.com cookies, or None (pycookiecheat's API moved between
    versions; try the new form, then the old)."""
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


def import_pycookiecheat():
    try:
        import pycookiecheat  # noqa: PLC0415
    except ImportError:
        raise RuntimeError(
            "pycookiecheat is not installed in the server's environment; re-run the installer "
            "(it installs it) before binding or refreshing sessions"
        )
    return pycookiecheat


# ---------------------------------------------------------------- probes


def probe_session(cookies):
    """(ok, info) for a cookie set WITHOUT depending on publication resolution, so
    'session expired' stays distinguishable from 'no access to this publication'."""
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


def publication_accessible(probe, target):
    """Can this session act on publication `target`? The SINGLE access predicate."""
    if not target:
        return True
    t = target.lower()
    subs = [s.lower() for s in (probe.get("subdomains") or [])]
    prim = (probe.get("primary") or "").lower()
    return t in subs or t == prim


def accessible_publications(probe):
    out = [s.lower() for s in ((probe or {}).get("subdomains") or [])]
    prim = ((probe or {}).get("primary") or "").lower()
    if prim and prim not in out:
        out.append(prim)
    return sorted(out)


def identity_matches(probe, act_as):
    """Does the probed session belong to the pinned identity (handle, or email as a
    hand-typed convenience)? An unset pin matches everything."""
    if not act_as:
        return True
    a = act_as.strip().lower()
    return (
        ((probe or {}).get("handle") or "").strip().lower() == a
        or ((probe or {}).get("email") or "").strip().lower() == a
    )


# ---------------------------------------------------------------- cookie sources


def cookie_source_file(source):
    """The Cookies DB a registry cookie_source points at, or None."""
    if not source:
        return None
    kind = source.get("type")
    if kind == "profile":
        files = dict(chrome_profile_cookie_files(source.get("browser") or "chrome"))
        return files.get(source.get("profile"))
    if kind == "user_data_dir":
        return os.path.join(os.path.expanduser(source.get("path") or ""), "Default", "Cookies")
    if kind == "cookie_file":
        return os.path.expanduser(source.get("path") or "")
    return None


def describe_source(source):
    if not source:
        return "(unbound)"
    kind = source.get("type")
    if kind == "profile":
        return "%s profile %r" % (source.get("browser") or "chrome", source.get("profile"))
    if kind == "user_data_dir":
        return "dedicated browser %s" % source.get("path")
    return "cookie file %s" % source.get("path")


def read_source_cookies(source):
    """Cookies from the bound source. Raises with the fix named."""
    pycookiecheat = import_pycookiecheat()
    cf = cookie_source_file(source)
    if not cf or not os.path.isfile(cf):
        raise ClientError(
            "the bound cookie source %s has no Cookies database at %s; launch that browser, "
            "log in to Substack there once, then refresh_session" % (describe_source(source), cf)
        )
    errors = []
    cookies = read_browser_cookies(pycookiecheat, source.get("browser") or "chrome", cf, errors)
    if not cookies:
        raise ClientError(
            "could not read cookies from %s (is the browser installed, are you logged in to "
            "substack.com there, was Keychain access granted?): %s"
            % (describe_source(source), " | ".join(errors)[:400])
        )
    if "substack.sid" not in cookies:
        raise ClientError(
            "no substack.sid among the cookies of %s; log in to Substack in that browser first"
            % describe_source(source)
        )
    return cookies, cf


# ---------------------------------------------------------------- session manager


def reset_api(slug=None):
    with _api_lock:
        if slug is None:
            _api_cache.clear()
        else:
            _api_cache.pop(slug, None)


def _context(slug, rec, probe):
    return {
        "client": slug,
        "publication": canonical_host(rec.get("publication_url")),
        "act_as": rec.get("act_as"),
        "logged_in_as": (probe or {}).get("handle"),
    }


def get_api(slug, expected_publication, fresh=False):
    """(api, context) for one client, after every check: registry entry, expected vs
    registry publication, a valid session (one auto-refresh from the bound source), the
    session reaching the publication, and the pinned identity. Nothing defaults."""
    slug, rec = client_record(slug)
    pub_url = normalize_publication_url(rec.get("publication_url"))
    sub = subdomain_of(pub_url)
    if not sub:
        raise ClientError(
            "client %r has publication_url %r, which is not a https://<name>.substack.com URL; "
            "python-substack resolves only that form (custom domains cannot be used)"
            % (slug, rec.get("publication_url"))
        )
    expected_host = canonical_host(expected_publication)
    registry_host = canonical_host(pub_url)
    if not expected_host:
        raise ClientError("expected_publication is required on every publication-scoped tool")
    if expected_host != registry_host:
        raise PublicationMismatch(slug, expected_host, registry_host, None)
    act_as = (rec.get("act_as") or "").strip()
    if not act_as:
        raise ClientError(
            "client %r is unbound (no act_as identity): run bind_client first, then retry" % slug
        )
    with _api_lock:
        cookies = session_cookies(slug)
        source = rec.get("cookie_source")
        refreshed = False
        if not cookies.get("substack.sid"):
            if not source:
                raise ClientError(
                    "client %r has no session and no bound cookie source: run bind_client" % slug
                )
            cookies, cf = read_source_cookies(source)
            refreshed = True
        entry = _api_cache.get(slug)
        if (entry and not fresh and not refreshed
                and entry.get("sid") == cookies.get("substack.sid")
                and time.time() - entry.get("validated_at", 0) < SESSION_TTL_SECONDS
                and identity_matches(entry, act_as)):
            return entry["api"], _context(slug, rec, entry)
        ok, probe = probe_session(cookies)
        if not ok and source and not refreshed:
            # One automatic re-read of the bound source, then fail with the fix named.
            cookies, cf = read_source_cookies(source)
            ok, probe = probe_session(cookies)
            refreshed = True
        if not ok:
            raise ClientError(
                "client %r: Substack session is invalid or expired (HTTP %s) and the bound "
                "source %s did not yield a valid one; log in to Substack in that browser, then "
                "refresh_session" % (slug, probe.get("status"), describe_source(source))
            )
        if not publication_accessible(probe, sub):
            raise PublicationMismatch(slug, expected_host, registry_host, accessible_publications(probe))
        if not identity_matches(probe, act_as):
            raise ClientError(
                "client %r: the session is logged in as %r, not the pinned act_as=%r; run "
                "refresh_session(client) (the bound source changed login?) or bind_client with "
                "confirm_switch to re-pin deliberately" % (slug, probe.get("handle"), act_as)
            )
        if refreshed and not os.environ.get("SUBSTACK_SESSION_TOKEN"):
            save_session(slug, cookies, probe, cf)
        from substack import Api  # noqa: PLC0415

        if not hasattr(Api, "create_draft_from_markdown"):
            raise RuntimeError(
                "the installed python-substack library is too old for this server (no "
                "Api.create_draft_from_markdown); re-run the installer, which requires Python "
                "3.10+ and a pinned library"
            )
        api = Api(cookies_string=cookies_string(cookies), publication_url=pub_url)
        _api_cache[slug] = {
            "api": api, "handle": probe.get("handle"), "email": probe.get("email"),
            "sid": cookies.get("substack.sid"), "validated_at": time.time(),
        }
        return api, _context(slug, rec, probe)


class ProbeFacade:
    """What probe-scoped tools may do: look at one client's registry record, probe a
    session's identity and access, read the bound cookie source, persist a session or a
    binding. No drafts, no posts, no settings; there is no attribute for them."""

    def __init__(self, slug):
        self.slug, self.record = client_record(slug)

    def subdomain(self):
        return subdomain_of(self.record.get("publication_url"))

    def stored_cookies(self):
        return session_cookies(self.slug)

    def identity(self, cookies):
        if not cookies or "substack.sid" not in cookies:
            return False, {"reason": "no_session"}
        try:
            return probe_session(cookies)
        except Exception as e:  # noqa: BLE001
            return False, {"reason": "unreachable", "error": str(e)[:200]}

    def reaches(self, probe):
        return publication_accessible(probe, self.subdomain())

    def read_source(self, source):
        return read_source_cookies(source)

    def persist(self, cookies, probe, source_path, **record_fields):
        save_session(self.slug, cookies, probe, source_path)
        if record_fields:
            update_client(self.slug, **record_fields)
        reset_api(self.slug)
        self.slug, self.record = client_record(self.slug)


# ---------------------------------------------------------------- substack helpers


def publication_capabilities(api):
    """What this publication can actually do, so choices offered are real ones."""
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
            {"id": s.get("id"), "name": s.get("name")} for s in sections if isinstance(s, dict)
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
    caps["timezone"] = None  # Substack's payload carries none; every schedule passes an offset
    return caps


def validate_settings(audience, comments, caps):
    if audience not in AUDIENCE_VALUES:
        raise ValueError(
            "audience %r is invalid; choose one of %s" % (audience, list(AUDIENCE_VALUES))
        )
    if comments not in COMMENT_VALUES:
        raise ValueError(
            "comment_permissions %r is invalid; choose one of %s ('none' disables comments)"
            % (comments, list(COMMENT_VALUES))
        )
    if not caps.get("paid_enabled"):
        if audience in PAID_AUDIENCE_VALUES:
            raise ValueError(
                "audience %r needs paid subscriptions, which this publication does not have "
                "(payments_state=%r); use 'everyone'" % (audience, caps.get("payments_state"))
            )
        if comments == "only_paid":
            raise ValueError(
                "comment_permissions 'only_paid' needs paid subscriptions, which this "
                "publication does not have; use 'everyone' or 'none'"
            )


def resolve_tags(api, wanted, caps=None):
    """Split requested tags into existing vs new (tags are publication-level objects:
    applying an unknown one creates it permanently, so the caller opts in)."""
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
    """Tags actually attached, from the association endpoint (the draft payload has no
    postTags field)."""
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


def post_url_for_slug(pub_url, slug):
    if not slug or not pub_url:
        return None
    return "%s/p/%s" % (normalize_publication_url(pub_url), slug)


def unwrap_items(raw, *keys):
    if isinstance(raw, dict):
        for k in keys:
            v = raw.get(k)
            if isinstance(v, list):
                return v
        return []
    return raw or []


def draft_summary(draft, pub_url=None):
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
        "title": draft.get("draft_title") or draft.get("title") or "(untitled draft)",
        "slug": slug,
        "post_url": post_url_for_slug(pub_url, slug),
        "is_published": bool(published),
        "updated_at": draft.get("draft_updated_at") or draft.get("updated_at"),
        "audience": draft.get("audience"),
        "comment_permissions": draft.get("write_comment_permissions"),
        "send_email": draft.get("should_send_email"),
    }
    if "draft_subtitle" in draft or "subtitle" in draft:
        summary["subtitle"] = draft.get("draft_subtitle") or draft.get("subtitle")
    else:
        summary["subtitle_note"] = "not in this payload; call get_draft to read it"
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
    if "postSchedules" in draft:
        trigger = None
        for s in draft.get("postSchedules") or []:
            if isinstance(s, dict) and s.get("trigger_at"):
                trigger = s["trigger_at"]
                break
        summary["scheduled_for"] = trigger
    else:
        summary["scheduled_for_note"] = "not in this payload; call get_draft to read it"
    if pub_url:
        summary["editor_url"] = "%s/publish/post/%s" % (normalize_publication_url(pub_url), draft_id)
    if not slug:
        summary["post_url_note"] = "no slug set on this draft yet; call set_slug to pin the public URL"
    return summary


def parse_iso_aware(value):
    v = (value or "").strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        raise ValueError("invalid ISO 8601 datetime: %r" % value)
    if dt.tzinfo is None:
        raise ValueError(
            "datetime %r has no timezone; pass an offset (e.g. 2026-08-03T09:00:00-04:00) "
            "so the schedule can't silently shift" % value
        )
    return dt


def validate_slug(slug):
    if not SLUG_RE.match(slug or ""):
        raise ValueError(
            "slug %r is invalid; use lowercase words separated by single hyphens" % slug
        )
    return slug


def text_result(payload, ctx=None):
    if ctx:
        if isinstance(payload, dict):
            payload = dict(payload)
            for k in ("client", "publication", "act_as", "logged_in_as"):
                payload.setdefault(k, ctx.get(k))
        elif isinstance(payload, list):
            payload = {"client": ctx.get("client"), "publication": ctx.get("publication"),
                       "act_as": ctx.get("act_as"), "logged_in_as": ctx.get("logged_in_as"),
                       "items": payload}
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}]}


# ---------------------------------------------------------------- tool schemas

CLIENT_PROP = {
    "client": {
        "type": "string",
        "description": "The client slug (tie-social brand identity.slug / brand.json substack.client). Required; there is no current client.",
    }
}
EXPECTED_PROP = {
    "expected_publication": {
        "type": "string",
        "description": "The publication this call is for (brand.json publication.domain, e.g. name.substack.com). The server refuses when it differs from the registry or from the live session.",
    }
}
DRAFT_ID = {"draft_id": {"type": ["integer", "string"]}}


def _schema(props, required):
    return {"type": "object", "properties": props, "required": required}


def _pub_schema(extra_props, extra_required=()):
    props = dict(CLIENT_PROP)
    props.update(EXPECTED_PROP)
    props.update(extra_props)
    return _schema(props, ["client", "expected_publication"] + list(extra_required))


TOOLS = [
    # ---- registry scope (local only)
    {
        "name": "list_clients",
        "scope": "registry",
        "description": "The configured clients: slug, publication, bound cookie source, pinned identity, whether a session is stored. Local only, no network.",
        "inputSchema": _schema({}, []),
    },
    {
        "name": "add_client",
        "scope": "registry",
        "description": "Register a client publication under its brand slug (https://<name>.substack.com). Local only; bind_client then attaches a browser session.",
        "inputSchema": _schema(dict(CLIENT_PROP, publication_url={"type": "string"}), ["client", "publication_url"]),
    },
    {
        "name": "list_browser_profiles",
        "scope": "registry",
        "description": "Chrome-family browser profiles (directory, display name, signed-in email) from the browser's plaintext 'Local State', plus the dedicated browsers registered clients are bound to. Reads NO cookies, triggers NO Keychain prompt.",
        "inputSchema": _schema({
            "browser": {"type": "string", "enum": ["chrome", "chromium", "brave"], "default": "chrome"},
            "root": {"type": "string", "description": "Override the user-data directory (a dedicated browser)."},
        }, []),
    },
    {
        "name": "open_client_browser",
        "scope": "registry",
        "description": "OPTIONAL. Launch the client's dedicated browser (its bound user_data_dir) for a one-time Substack login. macOS only.",
        "inputSchema": _schema(dict(CLIENT_PROP), ["client"]),
    },
    # ---- probe scope (identity and access only)
    {
        "name": "clients_status",
        "scope": "probe",
        "description": "One table for every client (or one client): bound?, session valid / expired / missing, identity matches the pin?, publication reachable?, logged_in_as. The readiness signal; the per-call check guards each write.",
        "inputSchema": _schema(dict(CLIENT_PROP), []),
    },
    {
        "name": "bind_client",
        "scope": "probe",
        "description": "Bind a client to a cookie source and pin its identity: a standard browser profile (`profile`, by directory name / display name / email), a dedicated browser (`user_data_dir`), or an explicit `cookie_file`. With none of them, every standard profile of `browser` is scanned and exactly ONE login reaching the publication is accepted (several: refuses with the candidates). The session is validated against the publication and stored; `act_as` is pinned to that login. Rebinding to a DIFFERENT identity needs confirm_switch=true, only on the user's explicit ask. Cookie values never surface.",
        "inputSchema": _schema(dict(CLIENT_PROP, **{
            "profile": {"type": "string"},
            "user_data_dir": {"type": "string"},
            "cookie_file": {"type": "string"},
            "browser": {"type": "string", "enum": ["chrome", "chromium", "brave"], "default": "chrome"},
            "confirm_switch": {"type": "boolean", "default": False},
        }), ["client"]),
    },
    {
        "name": "refresh_session",
        "scope": "probe",
        "description": "Re-read the client's bound cookie source, validate the session against the publication and the pinned identity, store it. Routine after a Substack logout/expiry; refuses for an unbound client (bind_client first). Names only, never values.",
        "inputSchema": _schema(dict(CLIENT_PROP), ["client"]),
    },
    # ---- publication scope (full client, after the expected_publication check)
    {
        "name": "get_publication_settings",
        "scope": "publication",
        "description": "What the publication can actually do: paid subscriptions enabled?, sections, existing tags, and which audience/comment values are unavailable. Call BEFORE proposing settings.",
        "inputSchema": _pub_schema({}),
    },
    {
        "name": "create_draft",
        "scope": "publication",
        "description": (
            "Create a Substack draft from Markdown with the slug pinned, so the public URL "
            "(<publication>/p/<slug>) is known before publication. Returns draft_id, slug, post_url, "
            "editor_url and the settings AS STORED by Substack. Does NOT publish or schedule. Every "
            "Publish-dialog setting gets a value whether or not you pass one; comment_permissions is "
            "ALWAYS sent explicitly (the library otherwise copies `audience` into it). Validate against "
            "get_publication_settings first."
        ),
        "inputSchema": _pub_schema({
            "title": {"type": "string"},
            "subtitle": {"type": "string", "default": ""},
            "body_markdown": {"type": "string", "description": "Full post body as Markdown (a short placeholder is fine; the team polishes in the editor)."},
            "slug": {"type": "string", "description": "The post URL slug to pin (lowercase-hyphenated)."},
            "audience": {"type": "string", "enum": list(AUDIENCE_VALUES), "default": "everyone"},
            "comment_permissions": {"type": "string", "enum": list(COMMENT_VALUES), "default": "everyone"},
            "send_email": {"type": "boolean", "default": True, "description": "Whether publishing emails subscribers (irreversible; schedule/publish additionally need confirm_send_email)."},
            "tags": {"type": "array", "items": {"type": "string"}},
            "allow_new_tags": {"type": "boolean", "default": False},
            "section_id": {"type": ["integer", "string", "null"]},
            "seo_title": {"type": "string"},
            "seo_description": {"type": "string"},
        }, ["title", "body_markdown", "slug"]),
    },
    {
        "name": "update_post_settings",
        "scope": "publication",
        "description": "Change settings on an existing draft: audience, comment_permissions, send_email, send_free_preview, section_id, seo_title, seo_description, title, subtitle. Returns the settings as stored.",
        "inputSchema": _pub_schema(dict(DRAFT_ID, **{
            "audience": {"type": "string", "enum": list(AUDIENCE_VALUES)},
            "comment_permissions": {"type": "string", "enum": list(COMMENT_VALUES)},
            "send_email": {"type": "boolean"},
            "send_free_preview": {"type": "boolean"},
            "section_id": {"type": ["integer", "string", "null"]},
            "seo_title": {"type": "string"},
            "seo_description": {"type": "string"},
            "title": {"type": "string"},
            "subtitle": {"type": "string"},
        }), ["draft_id"]),
    },
    {
        "name": "apply_tags",
        "scope": "publication",
        "description": "Attach publication tags to a draft; reports existing vs new and refuses to CREATE new ones unless allow_new is true (tags are permanent publication objects).",
        "inputSchema": _pub_schema(dict(DRAFT_ID, tags={"type": "array", "items": {"type": "string"}}, allow_new={"type": "boolean", "default": False}), ["draft_id", "tags"]),
    },
    {
        "name": "set_slug",
        "scope": "publication",
        "description": "Set/replace the URL slug of an existing draft. Returns the stored slug and post_url.",
        "inputSchema": _pub_schema(dict(DRAFT_ID, slug={"type": "string"}), ["draft_id", "slug"]),
    },
    {
        "name": "schedule_draft",
        "scope": "publication",
        "description": "Schedule a draft to publish at an exact instant (datetime_iso MUST carry an offset; past times rejected). Returns the schedule AS STORED. REFUSES without confirm_send_email when the post emails subscribers.",
        "inputSchema": _pub_schema(dict(DRAFT_ID, datetime_iso={"type": "string"}, confirm_send_email={"type": "boolean", "default": False}), ["draft_id", "datetime_iso"]),
    },
    {
        "name": "unschedule_draft",
        "scope": "publication",
        "description": "Cancel a draft's scheduled publication (it stays a draft).",
        "inputSchema": _pub_schema(dict(DRAFT_ID), ["draft_id"]),
    },
    {
        "name": "get_draft",
        "scope": "publication",
        "description": "One draft/post, the COMPLETE view: title, subtitle, slug, post_url, settings, attached tags, scheduled_for.",
        "inputSchema": _pub_schema(dict(DRAFT_ID), ["draft_id"]),
    },
    {
        "name": "list_drafts",
        "scope": "publication",
        "description": "Recent DRAFTS only, newest first (limit <= 25). A narrower projection than get_draft; per-field notes say what is absent.",
        "inputSchema": _pub_schema({"limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 25}}),
    },
    {
        "name": "publish_draft",
        "scope": "publication",
        "description": "PUBLISH a draft now (needs confirm_send_email when emailing). Only on an explicit user instruction; the normal flow is schedule_draft.",
        "inputSchema": _pub_schema(dict(DRAFT_ID, send_email={"type": "boolean", "default": True}, confirm_send_email={"type": "boolean", "default": False}, share_automatically={"type": "boolean", "default": False}), ["draft_id"]),
    },
    {
        "name": "delete_draft",
        "scope": "publication",
        "description": "Delete a draft by id.",
        "inputSchema": _pub_schema(dict(DRAFT_ID), ["draft_id"]),
    },
]
TOOL_SCOPES = {t["name"]: t["scope"] for t in TOOLS}
PUBLIC_TOOLS = [{k: v for k, v in t.items() if k != "scope"} for t in TOOLS]


# ---------------------------------------------------------------- registry tools


def tool_list_clients(_args):
    clients = load_clients()["clients"]
    out = []
    for slug in sorted(clients):
        rec = clients[slug]
        out.append({
            "client": slug,
            "publication": canonical_host(rec.get("publication_url")),
            "cookie_source": describe_source(rec.get("cookie_source")),
            "act_as": rec.get("act_as"),
            "session_stored": bool((load_session(slug).get("cookies") or {}).get("substack.sid")),
            "bound": bool(rec.get("act_as") and rec.get("cookie_source")),
        })
    return text_result({"clients": out, "count": len(out), "registry": clients_path()})


def tool_add_client(args):
    slug = validate_client_slug(args.get("client"))
    url = normalize_publication_url(args.get("publication_url"))
    if not subdomain_of(url):
        raise ClientError(
            "publication_url %r must be https://<name>.substack.com (custom domains cannot be "
            "resolved by python-substack)" % args.get("publication_url")
        )
    data = load_clients()
    if slug in data["clients"]:
        raise ClientError(
            "client %r already exists (publication %s); use bind_client / refresh_session, or edit "
            "%s by hand to change its publication" % (slug, data["clients"][slug].get("publication_url"), clients_path())
        )
    data["clients"][slug] = {
        "publication_url": url, "cookie_source": None, "act_as": None,
        "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "bound_at": None,
    }
    save_clients(data)
    return text_result({"added": slug, "publication": canonical_host(url), "next": "bind_client"})


def tool_list_browser_profiles(args):
    browser = (args.get("browser") or "chrome").lower()
    root = os.path.expanduser(args["root"]) if args.get("root") else None
    clients = load_clients()["clients"]
    bound = {}
    for slug, rec in clients.items():
        src = rec.get("cookie_source") or {}
        if src.get("type") == "profile":
            bound[(src.get("browser") or "chrome", src.get("profile"))] = slug
    path = local_state_path(browser, root)
    profiles = local_state_profiles(browser, root) if path and os.path.isfile(path) else []
    for p in profiles:
        p["bound_to"] = bound.get((browser, p["dir"]))
    dedicated = [
        {"client": slug, "user_data_dir": rec["cookie_source"].get("path"),
         "browser": rec["cookie_source"].get("browser") or "chrome"}
        for slug, rec in sorted(clients.items())
        if (rec.get("cookie_source") or {}).get("type") == "user_data_dir"
    ]
    out = {"browser": browser, "root": os.path.dirname(path) if path else None,
           "profiles": profiles, "count": len(profiles), "dedicated_browsers": dedicated,
           "note": "names/emails from the browser's plaintext profile cache; no cookies were read"}
    if not profiles:
        out["profiles_note"] = 'no "Local State" file at %s (is %s installed?)' % (path, browser)
    return text_result(out)


def tool_open_client_browser(args):
    slug, rec = client_record(args.get("client"))
    src = rec.get("cookie_source") or {}
    if src.get("type") != "user_data_dir":
        raise ClientError(
            "client %r is not bound to a dedicated browser (%s); bind_client with user_data_dir "
            "first" % (slug, describe_source(src))
        )
    app = BROWSER_APPS.get(src.get("browser") or "chrome", "Google Chrome")
    path = os.path.expanduser(src["path"])
    cmd = ["open", "-na", app, "--args", "--user-data-dir=%s" % path]
    try:
        subprocess.Popen(cmd)  # noqa: S603
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("could not launch %s: %s (run: %s)" % (app, e, " ".join(cmd)))
    return text_result({"launched": app, "user_data_dir": path, "client": slug,
                        "next": "log in to Substack in that window, then refresh_session"})


# ---------------------------------------------------------------- probe tools


def _status_for(slug):
    f = ProbeFacade(slug)
    rec = f.record
    row = {
        "client": slug,
        "publication": canonical_host(rec.get("publication_url")),
        "cookie_source": describe_source(rec.get("cookie_source")),
        "act_as": rec.get("act_as"),
        "bound": bool(rec.get("act_as") and rec.get("cookie_source")),
    }
    cookies = f.stored_cookies()
    if not cookies.get("substack.sid"):
        row.update(session="missing", ready=False,
                   fix="bind_client" if not row["bound"] else "refresh_session")
        return row
    ok, probe = f.identity(cookies)
    if not ok:
        row.update(session="expired" if probe.get("reason") == "session_invalid" else probe.get("reason", "unknown"),
                   ready=False, fix="refresh_session")
        return row
    row["session"] = "valid"
    row["logged_in_as"] = probe.get("handle")
    row["identity_matches"] = identity_matches(probe, rec.get("act_as"))
    row["publication_reachable"] = f.reaches(probe)
    row["ready"] = bool(row["bound"] and row["identity_matches"] and row["publication_reachable"])
    if not row["identity_matches"]:
        row["fix"] = "refresh_session (or bind_client with confirm_switch to re-pin)"
    elif not row["publication_reachable"]:
        row["fix"] = "the session (%s) has no access to %s (reaches: %s); bind a login of that publication" % (
            probe.get("handle"), row["publication"], accessible_publications(probe))
    elif not row["bound"]:
        row["fix"] = "bind_client"
    return row


def tool_clients_status(args, facade=None):
    slugs = [facade.slug] if facade else sorted(load_clients()["clients"])
    rows = [_status_for(s) for s in slugs]
    return text_result({"clients": rows, "all_ready": all(r.get("ready") for r in rows) if rows else False,
                        "count": len(rows)})


def _scan_profiles(facade, browser, act_as):
    """Every standard profile of `browser` whose session reaches the publication, grouped
    by identity. Deterministic; cookie values never surface."""
    pycookiecheat = import_pycookiecheat()
    meta = {}
    try:
        meta = {p["dir"]: p for p in local_state_profiles(browser)}
    except Exception:  # noqa: BLE001
        pass
    scanned, hits = [], {}
    errors = []
    for name, cf in chrome_profile_cookie_files(browser):
        cookies = read_browser_cookies(pycookiecheat, browser, cf, errors)
        ok, probe = facade.identity(cookies) if cookies else (False, {"reason": "no_session"})
        reaches = bool(ok and facade.reaches(probe))
        entry = {"profile": name, "reaches_publication": reaches,
                 "logged_in_as": (probe or {}).get("handle") if ok else None}
        if meta.get(name, {}).get("name"):
            entry["name"] = meta[name]["name"]
        if meta.get(name, {}).get("email"):
            entry["email"] = meta[name]["email"]
        if act_as:
            entry["matches_act_as"] = bool(reaches and identity_matches(probe, act_as))
        scanned.append(entry)
        if reaches and identity_matches(probe, act_as):
            hits.setdefault((probe or {}).get("handle") or name, []).append((name, cf, cookies, probe))
    return scanned, hits, errors


def tool_bind_client(args, facade):
    slug, rec = facade.slug, facade.record
    browser = (args.get("browser") or "chrome").lower()
    chosen = [k for k in ("profile", "user_data_dir", "cookie_file") if args.get(k)]
    if len(chosen) > 1:
        raise ValueError("pass only one of profile, user_data_dir, cookie_file")
    current_pin = (rec.get("act_as") or "").strip()
    target = facade.subdomain()
    if not target:
        raise ClientError("client %r has no https://<name>.substack.com publication_url" % slug)

    if chosen:
        kind = chosen[0]
        if kind == "profile":
            prof = resolve_profile_selector(args["profile"], browser)
            source = {"type": "profile", "browser": browser, "profile": prof["dir"]}
        elif kind == "user_data_dir":
            source = {"type": "user_data_dir", "browser": browser,
                      "path": os.path.expanduser(args["user_data_dir"]).rstrip("/")}
        else:
            source = {"type": "cookie_file", "browser": browser,
                      "path": os.path.expanduser(args["cookie_file"])}
        cookies, cf = facade.read_source(source)
        ok, probe = facade.identity(cookies)
        if not ok:
            raise ClientError("the session in %s is invalid or expired (%s); log in to Substack there first"
                              % (describe_source(source), probe.get("reason")))
        if not facade.reaches(probe):
            raise ClientError(
                "the login in %s (%s) has no access to publication %r (reaches: %s); log in as a "
                "user of that publication" % (describe_source(source), probe.get("handle"), target,
                                              accessible_publications(probe)))
        scanned = None
    else:
        scanned, hits, errors = _scan_profiles(facade, browser, None)
        if not hits:
            raise ClientError(
                "no %s profile holds a Substack login that reaches %r. Checked: %s. Log in to that "
                "publication's account in one of the browser's profiles, pass profile:, or bind a "
                "dedicated browser with user_data_dir:. Read errors: %s"
                % (browser, target, scanned, " | ".join(errors)[:300]))
        if len(hits) > 1:
            raise ClientError(
                "%d Substack logins reach %r; refusing to guess between accounts, nothing was stored. "
                "Candidates: %s. Re-run bind_client with profile: \"<name>\"."
                % (len(hits), target, [{"profile": n, "logged_in_as": (p or {}).get("handle"),
                                         "name": next((e.get("name", "") for e in scanned if e["profile"] == n), "")}
                                        for hs in hits.values() for n, cf, c, p in hs]))
        only = next(iter(hits.values()))
        name, cf, cookies, probe = next((h for h in only if h[0] == "Default"), only[0])
        source = {"type": "profile", "browser": browser, "profile": name}

    handle = probe.get("handle")
    if current_pin and not identity_matches(probe, current_pin):
        if not args.get("confirm_switch"):
            raise ClientError(
                "client %r is pinned to act_as=%r but %s is logged in as %r; re-run bind_client with "
                "confirm_switch: true to SWITCH the pinned identity (only on the user's explicit ask)"
                % (slug, current_pin, describe_source(source), handle))
        switched = {"from": current_pin, "to": handle}
    else:
        switched = None
    facade.persist(cookies, probe, cf, cookie_source=source, act_as=handle,
                   bound_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    result = {
        "client": slug, "publication": target, "cookie_source": describe_source(source),
        "act_as": handle, "logged_in_as": handle, "session": "valid",
        "auth_cookies_saved": sorted(k for k in cookies if k.startswith("substack.") or k == "cf_clearance"),
        "cookies_stored": len(cookies),
    }
    if switched:
        result["act_as_changed"] = switched
    if scanned is not None:
        result["profiles_scanned"] = scanned
    return text_result(result)


def tool_refresh_session(args, facade):
    slug, rec = facade.slug, facade.record
    source = rec.get("cookie_source")
    act_as = (rec.get("act_as") or "").strip()
    if not source or not act_as:
        raise ClientError("client %r is unbound; run bind_client first" % slug)
    cookies, cf = facade.read_source(source)
    ok, probe = facade.identity(cookies)
    if not ok:
        raise ClientError(
            "the session in %s is invalid or expired (%s); log in to Substack in that browser, "
            "then refresh_session again" % (describe_source(source), probe.get("reason")))
    if not facade.reaches(probe):
        raise ClientError(
            "the login in %s (%s) no longer reaches %r (reaches: %s)"
            % (describe_source(source), probe.get("handle"), facade.subdomain(), accessible_publications(probe)))
    if not identity_matches(probe, act_as):
        raise ClientError(
            "%s is now logged in as %r, not the pinned act_as=%r; nothing stored. Log back in as the "
            "pinned account, or bind_client with confirm_switch to re-pin deliberately"
            % (describe_source(source), probe.get("handle"), act_as))
    facade.persist(cookies, probe, cf)
    return text_result({
        "client": slug, "publication": facade.subdomain(), "act_as": act_as,
        "logged_in_as": probe.get("handle"), "session": "valid",
        "cookie_source": describe_source(source),
        "auth_cookies_saved": sorted(k for k in cookies if k.startswith("substack.") or k == "cf_clearance"),
        "cookies_stored": len(cookies),
    })


# ---------------------------------------------------------------- publication tools


def tool_get_publication_settings(_args, api, ctx):
    return text_result(publication_capabilities(api), ctx)


def tool_create_draft(args, api, ctx):
    pub_url = ctx["publication"]
    title = (args.get("title") or "").strip()
    body = args.get("body_markdown") or ""
    slug = validate_slug((args.get("slug") or "").strip())
    if not title or not body:
        raise ValueError("title and body_markdown are required")
    caps = publication_capabilities(api)
    audience = args.get("audience") or "everyone"
    comments = args.get("comment_permissions") or "everyone"
    validate_settings(audience, comments, caps)
    section_id = args.get("section_id")
    if section_id not in (None, ""):
        known = {str(s.get("id")) for s in caps.get("sections") or []}
        if known and str(section_id) not in known:
            raise ValueError("section_id %r is not one of this publication's sections %s"
                             % (section_id, sorted(known)))
    tag_plan = resolve_tags(api, args.get("tags"), caps)
    if tag_plan["new"] and not args.get("allow_new_tags"):
        raise ValueError(
            "these tags do not exist on the publication and would be created permanently: %s "
            "(existing: %s). Confirm with the user, then retry with allow_new_tags=true or reuse "
            "existing tags." % (tag_plan["new"], tag_plan["existing"]))
    subtitle = (args.get("subtitle") or "").strip()
    out = api.create_draft_from_markdown(
        title=title, markdown=body, subtitle=subtitle, slug=slug, audience=audience,
        write_comment_permissions=comments,
        search_engine_title=args.get("seo_title") or title,
        search_engine_description=args.get("seo_description") or subtitle or None,
        draft_section_id=section_id if section_id not in (None, "") else None,
        tags=None,
    )
    draft = out["draft"]
    draft_id = draft.get("id")
    send_email = args.get("send_email", True)
    if send_email is not True:
        api.put_draft(draft_id, should_send_email=bool(send_email))
    if tag_plan["normalized"]:
        api.add_tags_to_post(draft_id, tag_plan["normalized"])
    draft = api.get_draft(draft_id)
    summary = draft_summary(draft, pub_url)
    if tag_plan["normalized"]:
        attached = read_post_tags(api, draft_id)
        summary["tags"] = attached["names"]
        summary["tags_requested"] = tag_plan["normalized"]
        missing = [t for t in tag_plan["normalized"] if t not in attached["names"]]
        if missing:
            summary["tags_not_attached"] = missing
    requested = {"audience": audience, "comment_permissions": comments, "send_email": bool(send_email)}
    drift = {k: {"requested": v, "stored": summary.get(k)}
             for k, v in requested.items() if summary.get(k) is not None and summary.get(k) != v}
    if drift:
        summary["settings_drift"] = drift
        summary["settings_drift_note"] = "Substack stored different values than requested; report the STORED ones"
    got = summary.get("slug")
    if got and got != slug:
        summary["warning"] = ("requested slug %r but Substack stored %r (taken or normalized); the "
                              "post_url above reflects what was STORED" % (slug, got))
    elif not got:
        summary["warning"] = ("the draft was created but the response did not confirm slug %r; verify "
                              "with get_draft before using any post URL in social copy" % slug)
    return text_result(summary, ctx)


def tool_set_slug(args, api, ctx):
    slug = validate_slug((args.get("slug") or "").strip())
    draft = api.put_draft(args["draft_id"], slug=slug)
    summary = draft_summary(draft, ctx["publication"])
    if summary.get("slug") != slug:
        summary["warning"] = "Substack stored slug %r, not %r" % (summary.get("slug"), slug)
    return text_result(summary, ctx)


def tool_update_post_settings(args, api, ctx):
    draft_id = args["draft_id"]
    caps = publication_capabilities(api)
    current = api.get_draft(draft_id)
    audience = args.get("audience") or current.get("audience") or "everyone"
    comments = args.get("comment_permissions") or current.get("write_comment_permissions") or "everyone"
    validate_settings(audience, comments, caps)
    payload = {"audience": audience, "write_comment_permissions": comments}
    for arg, field in (("send_email", "should_send_email"), ("send_free_preview", "should_send_free_preview"),
                       ("seo_title", "search_engine_title"), ("seo_description", "search_engine_description"),
                       ("title", "draft_title"), ("subtitle", "draft_subtitle")):
        if arg in args and args[arg] is not None:
            payload[field] = args[arg]
    if "section_id" in args:
        payload["draft_section_id"] = args["section_id"] or None
    api.put_draft(draft_id, **payload)
    return text_result(draft_summary(api.get_draft(draft_id), ctx["publication"]), ctx)


def tool_apply_tags(args, api, ctx):
    draft_id = args["draft_id"]
    plan = resolve_tags(api, args.get("tags"), publication_capabilities(api))
    if not plan["normalized"]:
        raise ValueError("no usable tags after normalization")
    if plan["new"] and not args.get("allow_new"):
        raise ValueError("these tags would be CREATED on the publication permanently: %s (existing: %s). "
                         "Confirm with the user, then retry with allow_new=true." % (plan["new"], plan["existing"]))
    api.add_tags_to_post(draft_id, plan["normalized"])
    summary = draft_summary(api.get_draft(draft_id), ctx["publication"])
    attached = read_post_tags(api, draft_id)
    summary["tags"] = attached["names"]
    summary["tags_requested"] = plan["normalized"]
    if attached.get("note"):
        summary["tags_note"] = attached["note"]
    missing = [t for t in plan["normalized"] if t not in attached["names"]]
    if missing:
        summary["tags_not_attached"] = missing
        summary["warning"] = "these tags were requested but are not attached according to the server: %s" % missing
    return text_result(summary, ctx)


def tool_schedule_draft(args, api, ctx):
    dt = parse_iso_aware(args.get("datetime_iso"))
    if dt <= datetime.now(timezone.utc):
        raise ValueError("datetime_iso %s is in the past (now %s UTC)"
                         % (args.get("datetime_iso"), datetime.now(timezone.utc).isoformat(timespec="seconds")))
    draft_id = args["draft_id"]
    before = api.get_draft(draft_id)
    if before.get("is_published"):
        raise ValueError("draft %s is already published; cannot schedule it" % draft_id)
    if before.get("should_send_email") and not args.get("confirm_send_email"):
        raise ValueError(
            "this post is set to EMAIL SUBSCRIBERS when it publishes, which cannot be undone. Show the "
            "user the settings summary (audience=%r, comments=%r, send_email=True, publish at %s) and "
            "get an explicit yes, then retry with confirm_send_email=true, or call "
            "update_post_settings(send_email=false) first."
            % (before.get("audience"), before.get("write_comment_permissions"), dt.isoformat()))
    api.schedule_draft(draft_id, dt)
    summary = draft_summary(api.get_draft(draft_id), ctx["publication"])
    stored = summary.get("scheduled_for")
    summary["requested_for"] = dt.isoformat()
    summary["utc_equivalent"] = dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    if not stored:
        summary["warning"] = ("Substack did not report a schedule after the call; verify in the editor "
                              "(%s) before relying on it" % summary.get("editor_url"))
    elif parse_iso_aware(stored) != dt:
        summary["warning"] = ("stored schedule %s differs from the requested %s; the STORED value is what "
                              "will fire" % (stored, dt.isoformat()))
    summary["undo"] = "call unschedule_draft to cancel while it is still pending"
    return text_result(summary, ctx)


def tool_unschedule_draft(args, api, ctx):
    api.unschedule_draft(args["draft_id"])
    return text_result(draft_summary(api.get_draft(args["draft_id"]), ctx["publication"]), ctx)


def tool_get_draft(args, api, ctx):
    draft_id = args["draft_id"]
    summary = draft_summary(api.get_draft(draft_id), ctx["publication"])
    try:
        attached = read_post_tags(api, draft_id)
        summary["tags"] = attached["names"]
        if attached.get("note"):
            summary["tags_note"] = attached["note"]
    except Exception as e:  # noqa: BLE001
        summary["tags_note"] = "could not read attached tags: %s" % str(e)[:150]
    return text_result(summary, ctx)


def tool_list_drafts(args, api, ctx):
    limit = max(1, min(25, int(args.get("limit") or 10)))
    drafts = unwrap_items(api.get_drafts(filter="draft", limit=limit), "posts", "drafts", "results")
    drafts.sort(key=lambda d: ((d.get("draft_updated_at") or d.get("draft_created_at") or "")
                               if isinstance(d, dict) else ""), reverse=True)
    return text_result([draft_summary(d, ctx["publication"]) for d in drafts], ctx)


def tool_publish_draft(args, api, ctx):
    draft_id = args["draft_id"]
    send = bool(args.get("send_email", True))
    if send and not args.get("confirm_send_email"):
        raise ValueError("publishing now with send_email=true emails every subscriber and cannot be undone. "
                         "Get an explicit yes from the user, then retry with confirm_send_email=true, or pass "
                         "send_email=false to publish without email.")
    share = bool(args.get("share_automatically", False))
    api.prepublish_draft(draft_id)
    api.publish_draft(draft_id, send=send, share_automatically=share)
    summary = draft_summary(api.get_draft(draft_id), ctx["publication"])
    summary.update(published=True, emailed_subscribers=send, shared_automatically=share)
    return text_result(summary, ctx)


def tool_delete_draft(args, api, ctx):
    api.delete_draft(args["draft_id"])
    return text_result({"deleted": True, "draft_id": args["draft_id"]}, ctx)


TOOL_HANDLERS = {
    "list_clients": tool_list_clients,
    "add_client": tool_add_client,
    "list_browser_profiles": tool_list_browser_profiles,
    "open_client_browser": tool_open_client_browser,
    "clients_status": tool_clients_status,
    "bind_client": tool_bind_client,
    "refresh_session": tool_refresh_session,
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


AUTH_ERROR_RE = re.compile(r"\b(401|403) client error|unauthori[sz]ed|forbidden", re.I)
# Handlers that make exactly one Substack call, or only reads: safe to re-run after a
# session refresh. Multi-step writers (create_draft runs create -> put -> tags -> read) are
# not re-run blindly: a 401 half-way would leave a first draft behind.
RETRY_SAFE_TOOLS = {"get_publication_settings", "get_draft", "list_drafts", "set_slug",
                    "unschedule_draft", "delete_draft"}


def auth_error_status(exc):
    """401 or 403 when Substack refused the session during a call, else None. python-substack
    raises SubstackAPIException(status_code, text); requests.HTTPError carries .response. Our
    own refusals (ClientError, validation ValueErrors) are never auth errors, whatever they say."""
    if isinstance(exc, (ClientError, ValueError, TypeError, KeyError)):
        return None
    code = getattr(exc, "status_code", None)
    if code is None:
        code = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = None
    if code is not None:
        return code if code in (401, 403) else None
    m = AUTH_ERROR_RE.search(str(exc))
    if not m:
        return None
    return 403 if "403" in m.group(0) or "forbidden" in m.group(0).lower() else 401


def is_auth_error(exc):
    return auth_error_status(exc) is not None


def dispatch_tool(name, args):
    """The one place a handler gets its facade. registry: nothing. probe: a ProbeFacade for the
    named client (clients_status may run over all). publication: the full client, only after
    `client` and `expected_publication` passed every check in get_api. A 401/403 from Substack
    during the call drops the cached client, re-reads the bound cookie source once (get_api
    fresh=True), then re-runs a retry-safe tool or fails with the fix named."""
    handler = TOOL_HANDLERS.get(name)
    scope = TOOL_SCOPES.get(name)
    if handler is None or scope is None:
        raise ClientError("unknown tool: %s" % name)
    args = args or {}
    if scope == "registry":
        return handler(args)
    if scope == "probe":
        if name == "clients_status" and not args.get("client"):
            return handler(args, None)
        return handler(args, ProbeFacade(args.get("client")))
    if not args.get("client"):
        raise ClientError("`client` is required on every Substack-facing tool; configured clients: %s"
                          % (", ".join(sorted(load_clients()["clients"])) or "(none)"))
    if not args.get("expected_publication"):
        raise ClientError("`expected_publication` is required on %s (the brand's publication.domain)" % name)
    client, expected = args["client"], args["expected_publication"]
    api, ctx = get_api(client, expected)
    try:
        return handler(args, api, ctx)
    except Exception as exc:  # noqa: BLE001
        status = auth_error_status(exc)
        if status is None:
            raise
        log("tool %s: Substack answered HTTP %s for client %s; refreshing the session once" % (name, status, client))
        reset_api(client)
        # Probes the stored session again and re-reads the bound source when it is expired;
        # raises the usual actionable ClientError when neither yields a valid session.
        api, ctx = get_api(client, expected, fresh=True)
        if name not in RETRY_SAFE_TOOLS:
            raise ClientError(
                "client %r: Substack answered HTTP %s during %s; the session was refreshed and is "
                "valid again, but %s writes in several steps and is not re-run automatically: check "
                "list_drafts / get_draft for a partial result, then run it again"
                % (client, status, name, name)
            )
        log("tool %s: session refreshed for client %s; re-running once" % (name, client))
        return handler(args, api, ctx)


# ---------------------------------------------------------------- rpc plumbing


def handle_request(msg):
    req_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "initialize":
        reply(req_id, {
            "protocolVersion": params.get("protocolVersion") or FALLBACK_PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    elif method == "ping":
        reply(req_id, {})
    elif method == "tools/list":
        reply(req_id, {"tools": PUBLIC_TOOLS})
    elif method == "resources/list":
        reply(req_id, {"resources": []})
    elif method == "prompts/list":
        reply(req_id, {"prompts": []})
    elif method == "tools/call":
        name = params.get("name")
        if name not in TOOL_HANDLERS:
            reply_error(req_id, -32602, "unknown tool: %s" % name)
            return
        try:
            reply(req_id, dispatch_tool(name, params.get("arguments") or {}))
        except Exception as e:  # noqa: BLE001
            log("tool %s error: %s\n%s" % (name, e, traceback.format_exc()))
            reply(req_id, {"content": [{"type": "text", "text": "ERROR: %s" % e}], "isError": True})
    else:
        if req_id is not None:
            reply_error(req_id, -32601, "method not found: %s" % method)


def serve():
    log("%s v%s starting (home=%s)" % (SERVER_NAME, SERVER_VERSION, home()))
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
    deadline = time.time() + 30
    for t in workers:
        t.join(max(0, deadline - time.time()))
    log("stdin closed; exiting")


# ---------------------------------------------------------------- 0.4 -> 0.5 migration (CLI)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def is_reserved_path(path):
    rel = os.path.relpath(os.path.expanduser(path), home())
    if rel.startswith(".."):
        return False
    first = rel.split(os.sep)[0]
    return first in RESERVED_NAMES or any(first == p or first.startswith(p) for p in RESERVED_DIR_PREFIXES)


def is_legacy_config(data):
    return isinstance(data, dict) and "clients" not in data


def legacy_entries(desktop_config):
    """Every 0.4 server entry in claude_desktop_config: name -> entry. A 0.5 entry carries
    TIE_SUBSTACK_HOME; `tie-substack-legacy` is the kept-working 0.4 default after a partial
    switch and is treated like the default entry it came from."""
    servers = (desktop_config or {}).get("mcpServers") or {}
    return {n: e for n, e in servers.items()
            if (n == "tie-substack" or n.startswith("tie-substack-"))
            and "TIE_SUBSTACK_HOME" not in ((e or {}).get("env") or {})}


def select_legacy_configs(desktop_config, default_slug):
    """(selected, unresolvable) for the 0.4 entries in claude_desktop_config.

    selected = [(entry name, slug, config path, config data, env publication)] for every entry
    whose publication is known: SUBSTACK_PUBLICATION_URL in the entry's env (the 0.4 server
    accepted that alone) or publication_url in the referenced config file (TIE_SUBSTACK_CONFIG,
    else <home>/config.json). Selection is by reference, never by glob; reserved 0.5 paths are
    never candidates. An entry whose publication cannot be determined is `unresolvable`: it is
    reported and kept working, never imported and never removed."""
    selected, unresolvable = [], []
    for name, entry in legacy_entries(desktop_config).items():
        env = (entry or {}).get("env") or {}
        path = os.path.expanduser(env.get("TIE_SUBSTACK_CONFIG") or os.path.join(home(), "config.json"))
        reason, data = None, {}
        if is_reserved_path(path):
            reason = "config path %s is reserved for 0.5" % path
        elif os.path.isfile(path):
            try:
                data = read_json(path, {}) or {}
            except RuntimeError as exc:
                reason = str(exc)
            if not reason and not is_legacy_config(data):
                reason = "%s is not a 0.4 config" % path
        env_pub = env.get("SUBSTACK_PUBLICATION_URL")
        if not reason and not normalize_publication_url(env_pub or data.get("publication_url")):
            reason = ("no publication: neither SUBSTACK_PUBLICATION_URL in the entry nor "
                      "publication_url in %s" % path)
        if name in ("tie-substack", "tie-substack-legacy"):
            slug_raw = os.environ.get("TIE_SUBSTACK_CLIENT") or default_slug
        else:
            slug_raw = name[len("tie-substack-"):]
        try:
            slug = validate_client_slug(slug_raw)
        except ClientError as exc:
            reason, slug = reason or str(exc), None
        if reason:
            unresolvable.append({"entry": name, "slug": slug, "config": path, "reason": reason})
            continue
        selected.append((name, slug, path, data, env_pub))
    return selected, unresolvable


def legacy_source_from(cfg):
    cf = cfg.get("cookie_file")
    if not cf:
        return None
    cf = os.path.expanduser(cf)
    fam = standard_install_browser(cf)
    if fam:
        return {"type": "profile", "browser": fam, "profile": os.path.basename(os.path.dirname(cf))}
    if os.path.basename(cf) == "Cookies" and os.path.basename(os.path.dirname(cf)) == "Default":
        return {"type": "user_data_dir", "browser": "chrome", "path": os.path.dirname(os.path.dirname(cf))}
    return {"type": "cookie_file", "browser": "chrome", "path": cf}


def write_backup_original(desktop_config_path, legacy_paths, installed_server):
    """The write-once pre-migration snapshot (backup-original/ + MANIFEST.json)."""
    dest = os.path.join(home(), "backup-original")
    if os.path.isdir(dest):
        return {"backup_original": dest, "written": False}
    os.makedirs(dest, mode=0o700)
    files = {}
    for src in [desktop_config_path] + list(legacy_paths) + ([installed_server] if installed_server else []):
        if src and os.path.isfile(src):
            name = os.path.basename(src)
            if name in files:
                name = "%s__%s" % (hashlib.sha256(src.encode()).hexdigest()[:8], name)
            shutil.copy2(src, os.path.join(dest, name))
            files[name] = {"source": src, "sha256": sha256_file(src)}
    write_private_json(os.path.join(dest, "MANIFEST.json"), {
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_version": "0.4.x", "files": files,
    })
    return {"backup_original": dest, "written": True}


def write_run_backup(desktop_config_path, legacy_paths):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = os.path.join(home(), "backup-%s" % stamp)
    os.makedirs(dest, mode=0o700, exist_ok=True)
    files = {}
    for src in [desktop_config_path] + list(legacy_paths):
        if src and os.path.isfile(src):
            name = os.path.basename(src)
            if name in files:
                name = "%s__%s" % (hashlib.sha256(src.encode()).hexdigest()[:8], name)
            shutil.copy2(src, os.path.join(dest, name))
            files[name] = {"source": src, "sha256": sha256_file(src)}
    write_private_json(os.path.join(dest, "MANIFEST.json"), {
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "files": files})
    return dest


def migrate_import(desktop_config_path, default_slug="tie", installed_server=None):
    """Steps 1-3 of the migration: select legacy configs by reference, back up (original once,
    plus per run), import idempotently into clients.json + sessions/ with a journal."""
    desktop = read_json(desktop_config_path, {}) if desktop_config_path else {}
    selected, unresolvable = select_legacy_configs(desktop, default_slug)
    legacy_paths = [p for _, _, p, _, _ in selected if os.path.isfile(p)]
    journal = read_json(journal_path(), {"imports": {}})
    pre_migration = not any(
        n == "tie-substack" and "TIE_SUBSTACK_HOME" in ((e or {}).get("env") or {})
        for n, e in ((desktop.get("mcpServers") or {}).items())
    ) and not journal["imports"]
    report = {"selected": [{"entry": n, "slug": s, "config": p} for n, s, p, _, _ in selected],
              "unresolvable": unresolvable,
              "imported": [], "skipped": [], "conflicts": [], "legacy_server": None}
    if pre_migration and (selected or unresolvable):
        report.update(write_backup_original(desktop_config_path, legacy_paths, installed_server))
    elif not os.path.isdir(os.path.join(home(), "backup-original")):
        report["warning"] = ("no backup-original snapshot exists and this is not a pre-migration state; "
                             "rollback can only restore the latest per-run backup")
    report["run_backup"] = write_run_backup(desktop_config_path, legacy_paths)
    if installed_server and os.path.isfile(installed_server):
        legacy_dir = os.path.join(home(), "legacy")
        os.makedirs(legacy_dir, mode=0o700, exist_ok=True)
        dest = os.path.join(legacy_dir, "server.py")
        if not os.path.isfile(dest):
            shutil.copy2(installed_server, dest)
        report["legacy_server"] = dest
    clients = load_clients()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for entry, slug, path, cfg, env_pub in selected:
        digest = sha256_file(path) if os.path.isfile(path) else "no-config-file"
        prior = journal["imports"].get(path)
        if prior and prior.get("sha256") == digest and prior.get("result") == "imported":
            report["skipped"].append({"slug": slug, "config": path, "reason": "already imported, unchanged"})
            continue
        rec = clients["clients"].get(slug)
        if prior and rec and (rec.get("bound_at") or "") > (prior.get("at") or ""):
            report["conflicts"].append({
                "slug": slug, "config": path,
                "reason": "the source changed since import, but the registry record was rebound later "
                          "(bind_client); the registry is kept"})
            journal["imports"][path] = {"sha256": digest, "slug": slug, "result": "conflict", "at": now}
            continue
        pub = normalize_publication_url(env_pub or cfg.get("publication_url"))
        source = legacy_source_from(cfg)
        clients["clients"][slug] = {
            "publication_url": pub, "cookie_source": source,
            "act_as": (cfg.get("act_as") or "").strip() or None,
            "added_at": (rec or {}).get("added_at") or now,
            "bound_at": (rec or {}).get("bound_at") or (now if cfg.get("act_as") else None),
            "migrated_from": path,
        }
        cookies = cfg.get("cookies") or {}
        if cookies.get("substack.sid"):
            save_session(slug, cookies, {"handle": cfg.get("act_as")}, os.path.expanduser(cfg.get("cookie_file") or "") or None)
        journal["imports"][path] = {"sha256": digest, "slug": slug, "result": "imported", "at": now}
        report["imported"].append({"slug": slug, "config": path, "publication": canonical_host(pub),
                                   "cookie_source": describe_source(source),
                                   "act_as": clients["clients"][slug]["act_as"],
                                   "session": bool(cookies.get("substack.sid"))})
    save_clients(clients)
    write_private_json(journal_path(), journal)
    return report


def check_clients_report():
    """Step 4: a per-client table (live network probes), no cookie values."""
    rows = [_status_for(s) for s in sorted(load_clients()["clients"])]
    return {"clients": rows, "all_ready": bool(rows) and all(r.get("ready") for r in rows)}


def switch_entries(desktop_config_path, command, server, verified=(), legacy_server=None,
                   default_slug="tie"):
    """Step 5: add the single `tie-substack` 0.5 entry. A 0.4 entry is removed only when it
    was imported AND its client verified ready (`verified` slugs); every other 0.4 entry,
    including one whose publication could not be resolved, keeps working on legacy/server.py
    under a name that cannot collide with the new entry (the default entry becomes
    `tie-substack-legacy`), with its env overrides intact."""
    desktop = read_json(desktop_config_path, {})
    servers = desktop.setdefault("mcpServers", {})
    old = legacy_entries(desktop)
    selected, unresolvable = select_legacy_configs(desktop, default_slug)
    slug_of = {name: slug for name, slug, _, _, _ in selected}
    verified = {v for v in verified if v}
    removed, kept = [], []
    for name, entry in old.items():
        servers.pop(name, None)
        slug = slug_of.get(name)
        if slug and slug in verified:
            removed.append(name)
            continue
        entry = dict(entry or {})
        if legacy_server:
            entry["args"] = [legacy_server]
        new_name = "tie-substack-legacy" if name == "tie-substack" else name
        servers[new_name] = entry
        kept.append(new_name)
    servers["tie-substack"] = {"command": command, "args": [server], "env": {"TIE_SUBSTACK_HOME": home()}}
    write_json_plain(desktop_config_path, desktop)
    return {"switched": not kept, "removed": removed, "kept_legacy_entries": kept,
            "verified": sorted(verified), "unresolvable": [u["entry"] for u in unresolvable]}


def write_json_plain(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def rollback(desktop_config_path, last=False):
    """Restore backup-original/ (the pure pre-migration state; manifest hashes verified) or,
    with last=True, the latest per-run backup. clients.json, sessions/ and the journal stay."""
    if last:
        runs = sorted(
            d for d in (os.listdir(home()) if os.path.isdir(home()) else [])
            if d.startswith("backup-") and d != "backup-original"
            and os.path.isfile(os.path.join(home(), d, "MANIFEST.json")))
        if not runs:
            raise RuntimeError("no per-run backup to restore")
        src_dir = os.path.join(home(), runs[-1])
    else:
        src_dir = os.path.join(home(), "backup-original")
        if not os.path.isdir(src_dir):
            raise RuntimeError("no backup-original snapshot; use --last to restore the latest per-run backup")
    manifest = read_json(os.path.join(src_dir, "MANIFEST.json"), None)
    if not manifest or not isinstance(manifest.get("files"), dict):
        raise RuntimeError("%s has no readable MANIFEST.json; refusing to restore" % src_dir)
    for name, meta in manifest["files"].items():
        p = os.path.join(src_dir, name)
        if not os.path.isfile(p) or sha256_file(p) != meta.get("sha256"):
            raise RuntimeError("snapshot file %s is missing or its hash differs from the manifest; refusing to "
                               "restore a corrupted snapshot" % name)
    restored = []
    for name, meta in manifest["files"].items():
        dest = meta["source"]
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        shutil.copy2(os.path.join(src_dir, name), dest)
        restored.append(dest)
    return {"restored_from": src_dir, "restored": restored,
            "note": "clients.json, sessions/ and migration.json are left in place (inert without the "
                    "v0.5 server entry), so a later retry resumes from them"}


def cli(argv):
    import argparse  # noqa: PLC0415

    ap = argparse.ArgumentParser(prog="server.py", description="tie-substack MCP server and migration CLI")
    sub = ap.add_subparsers(dest="cmd")
    m = sub.add_parser("migrate")
    m.add_argument("--desktop-config", required=True)
    m.add_argument("--default-slug", default="tie")
    m.add_argument("--installed-server")
    sub.add_parser("check-clients")
    s = sub.add_parser("switch")
    s.add_argument("--desktop-config", required=True)
    s.add_argument("--command", required=True)
    s.add_argument("--server", required=True)
    s.add_argument("--verified", default="", help="comma-separated client slugs that check-clients reported ready")
    s.add_argument("--legacy-server")
    s.add_argument("--default-slug", default="tie")
    r = sub.add_parser("rollback")
    r.add_argument("--desktop-config", required=True)
    r.add_argument("--last", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "migrate":
        out = migrate_import(args.desktop_config, args.default_slug, args.installed_server)
    elif args.cmd == "check-clients":
        out = check_clients_report()
    elif args.cmd == "switch":
        verified = [v.strip() for v in args.verified.split(",") if v.strip()]
        out = switch_entries(args.desktop_config, args.command, args.server, verified,
                             args.legacy_server, args.default_slug)
    elif args.cmd == "rollback":
        out = rollback(args.desktop_config, args.last)
    else:
        ap.print_help()
        return 2
    print(json.dumps(out, indent=2))
    if args.cmd == "check-clients":
        return 0 if out["all_ready"] else 1
    return 0


def main():
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    serve()


if __name__ == "__main__":
    main()
