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
profile into the config without displaying it.

Config file (~/.tie-substack/config.json, override via TIE_SUBSTACK_CONFIG):
  { "publication_url": "https://<pub>.substack.com",
    "cookies": { "substack.sid": "...", ... } }

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
SERVER_VERSION = "0.1.0"
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


def publication_url():
    url = os.environ.get("SUBSTACK_PUBLICATION_URL") or load_config().get("publication_url")
    if not url:
        raise RuntimeError(
            "publication_url is not configured — set SUBSTACK_PUBLICATION_URL or add "
            '"publication_url" to %s (e.g. via install.command)' % CONFIG_PATH
        )
    return url.rstrip("/")


def current_cookies():
    """Cookie dict from env override or config. Values NEVER leave this process."""
    token = os.environ.get("SUBSTACK_SESSION_TOKEN")
    if token:
        return {"substack.sid": token}
    return load_config().get("cookies") or {}


def cookies_string(cookies):
    return "; ".join("%s=%s" % (k, v) for k, v in cookies.items())


def get_api(fresh=False):
    """python-substack Api, cached (its __init__ does network round-trips)."""
    with _api_lock:
        if _api_cache["api"] is not None and not fresh:
            return _api_cache["api"]
        cookies = current_cookies()
        if not cookies.get("substack.sid"):
            raise RuntimeError(
                "no substack.sid cookie configured. Run the refresh_cookie tool (pulls it "
                "from your local Chrome via pycookiecheat), or paste it manually into %s as "
                '{"cookies": {"substack.sid": "<value>"}}. Never paste the cookie into chat.'
                % CONFIG_PATH
            )
        from substack import Api  # noqa: PLC0415

        api = Api(cookies_string=cookies_string(cookies), publication_url=publication_url())
        _api_cache["api"] = api
        return api


def reset_api():
    with _api_lock:
        _api_cache["api"] = None


# ---------------------------------------------------------------- helpers


def post_url_for_slug(slug):
    return "%s/p/%s" % (publication_url(), slug) if slug else None


def draft_summary(draft):
    """Public, cookie-free summary of a draft dict returned by the API."""
    slug = draft.get("slug") or draft.get("draft_slug")
    return {
        "draft_id": draft.get("id"),
        "title": draft.get("draft_title") or draft.get("title"),
        "subtitle": draft.get("draft_subtitle") or draft.get("subtitle"),
        "slug": slug,
        "post_url": post_url_for_slug(slug),
        "editor_url": "%s/publish/post/%s" % (publication_url(), draft.get("id")),
        "is_published": bool(draft.get("post_date")),
        "scheduled_for": draft.get("trigger_at"),
        "updated_at": draft.get("draft_updated_at") or draft.get("updated_at"),
    }


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
            "the server's 0600 config file. The cookie value is never returned or shown — "
            "the result only lists cookie NAMES and the validation outcome. Requires the "
            "user to be logged in to Substack in that browser. Run only when the user asks "
            "to refresh/repair Substack auth."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "browser": {
                    "type": "string",
                    "enum": ["chrome", "chromium", "brave", "firefox"],
                    "default": "chrome",
                    "description": "Which local browser profile to read the cookie from.",
                }
            },
        },
    },
    {
        "name": "create_draft",
        "description": (
            "Create a Substack draft from Markdown with the slug pinned, so the public URL "
            "(<publication>/p/<slug>) is known before publication. Returns draft_id, slug, "
            "post_url, editor_url. Body images referenced as local paths/URLs are uploaded "
            "by the library. Does NOT publish or schedule."
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
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "body_markdown", "slug"],
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
            "timezone offset (e.g. 2026-08-03T09:00:00-04:00); naive timestamps are "
            "rejected. Returns the draft summary incl. post_url."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "draft_id": {"type": ["integer", "string"]},
                "datetime_iso": {"type": "string"},
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
            "Fetch one draft/post by id: title, slug, post_url, scheduled_for, published?"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"draft_id": {"type": ["integer", "string"]}},
            "required": ["draft_id"],
        },
    },
    {
        "name": "list_drafts",
        "description": "List recent drafts (id, title, slug, post_url, scheduled_for).",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}},
        },
    },
    {
        "name": "publish_draft",
        "description": (
            "PUBLISH a draft immediately (optionally emailing subscribers). Irreversible in "
            "the email sense — call ONLY on an explicit user instruction to publish now; "
            "the normal flow is schedule_draft."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "draft_id": {"type": ["integer", "string"]},
                "send_email": {"type": "boolean", "default": True},
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
    info = {"config_path": CONFIG_PATH, "server_version": SERVER_VERSION}
    try:
        info["publication_url"] = publication_url()
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

    cookies = current_cookies()
    info["cookie_configured"] = bool(cookies.get("substack.sid"))
    info["cookie_names"] = sorted(cookies.keys())
    if info["cookie_configured"] and "NOT INSTALLED" not in str(info["python_substack"]):
        try:
            api = get_api(fresh=True)
            profile = api.get_user_profile()
            pub = api.get_user_primary_publication() or {}
            info["cookie_valid"] = True
            info["logged_in_as"] = profile.get("handle") or profile.get("name")
            info["primary_publication"] = pub.get("subdomain")
        except Exception as e:  # noqa: BLE001
            reset_api()
            info["cookie_valid"] = False
            info["cookie_error"] = str(e)[:300]
            info["fix"] = "cookie is likely expired — run refresh_cookie"
    return text_result(info)


def tool_refresh_cookie(args):
    try:
        import pycookiecheat  # noqa: PLC0415
    except ImportError:
        raise RuntimeError(
            "pycookiecheat is not installed in the server's environment — "
            "pip install pycookiecheat, or paste the cookie into %s manually" % CONFIG_PATH
        )

    browser = (args.get("browser") or "chrome").lower()
    url = "https://substack.com"
    cookies = None
    errors = []
    # pycookiecheat's API moved between versions; try new then old form.
    try:
        bt = pycookiecheat.BrowserType(browser)
        cookies = pycookiecheat.chrome_cookies(url, browser=bt) \
            if browser != "firefox" else pycookiecheat.firefox_cookies(url)
    except Exception as e:  # noqa: BLE001
        errors.append(str(e))
        try:
            cookies = pycookiecheat.chrome_cookies(url)
        except Exception as e2:  # noqa: BLE001
            errors.append(str(e2))
    if not cookies:
        raise RuntimeError(
            "could not read cookies from %s (is the browser installed, are you logged in "
            "to substack.com there, was Keychain access granted?): %s"
            % (browser, " | ".join(errors)[:400])
        )
    if "substack.sid" not in cookies:
        raise RuntimeError(
            "no substack.sid among %s cookies for substack.com — log in to Substack in "
            "that browser first (found: %s)" % (browser, sorted(cookies.keys()))
        )

    cfg = load_config()
    cfg["cookies"] = cookies
    cfg.setdefault("publication_url", os.environ.get("SUBSTACK_PUBLICATION_URL", ""))
    save_config(cfg)
    reset_api()

    result = {
        "saved_to": CONFIG_PATH,
        "cookie_names": sorted(cookies.keys()),  # names only — values never leave the server
        "browser": browser,
    }
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


def tool_create_draft(args):
    title = (args.get("title") or "").strip()
    body = args.get("body_markdown") or ""
    slug = validate_slug((args.get("slug") or "").strip())
    if not title or not body:
        raise ValueError("title and body_markdown are required")
    api = get_api()
    out = api.create_draft_from_markdown(
        title=title,
        markdown=body,
        subtitle=(args.get("subtitle") or "").strip(),
        slug=slug,
        tags=args.get("tags"),
    )
    draft = out["draft"]
    summary = draft_summary(draft)
    got = summary.get("slug")
    if got and got != slug:
        summary["warning"] = (
            "requested slug %r but Substack stored %r (taken or normalized) — the post_url "
            "above reflects what was STORED; update social copy accordingly or set_slug again"
            % (slug, got)
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


def tool_schedule_draft(args):
    dt = parse_iso_aware(args.get("datetime_iso"))
    if dt <= datetime.now(timezone.utc):
        raise ValueError("datetime_iso %s is in the past" % args.get("datetime_iso"))
    api = get_api()
    api.schedule_draft(args["draft_id"], dt)
    draft = api.get_draft(args["draft_id"])
    summary = draft_summary(draft)
    summary["scheduled_for"] = summary.get("scheduled_for") or dt.isoformat()
    return text_result(summary)


def tool_unschedule_draft(args):
    api = get_api()
    api.unschedule_draft(args["draft_id"])
    return text_result(draft_summary(api.get_draft(args["draft_id"])))


def tool_get_draft(args):
    api = get_api()
    return text_result(draft_summary(api.get_draft(args["draft_id"])))


def tool_list_drafts(args):
    api = get_api()
    limit = max(1, min(50, int(args.get("limit") or 10)))
    drafts = api.get_drafts(limit=limit) or []
    return text_result([draft_summary(d) for d in drafts])


def tool_publish_draft(args):
    api = get_api()
    api.prepublish_draft(args["draft_id"])
    out = api.publish_draft(args["draft_id"], send=bool(args.get("send_email", True)))
    summary = draft_summary(out if isinstance(out, dict) else api.get_draft(args["draft_id"]))
    summary["published"] = True
    return text_result(summary)


def tool_delete_draft(args):
    api = get_api()
    api.delete_draft(args["draft_id"])
    return text_result({"deleted": True, "draft_id": args["draft_id"]})


TOOL_HANDLERS = {
    "substack_status": tool_substack_status,
    "refresh_cookie": tool_refresh_cookie,
    "create_draft": tool_create_draft,
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
