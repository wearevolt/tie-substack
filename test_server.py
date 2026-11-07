"""Offline regression checks for tie-substack 0.5.0. No live Substack, no cookies.

Run:  python3 test_server.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types

HOME = tempfile.mkdtemp(prefix="tie-substack-test-")
os.environ["TIE_SUBSTACK_HOME"] = HOME
os.environ.pop("SUBSTACK_SESSION_TOKEN", None)
os.environ.pop("TIE_SUBSTACK_CLIENT", None)

spec = importlib.util.spec_from_file_location(
    "srv", os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

REAL_CDP_COOKIES = srv.cdp_cookies
srv.on_windows = lambda: False  # the checks run the macOS behaviour on any OS; the Windows ones switch it on

fails = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label + ("" if ok else "  -> %r" % (got,)))
    if not ok:
        fails.append("%s: got %r want %r" % (label, got, want))


def raises(label, fn, exc=Exception, contains=()):
    try:
        fn()
        check(label, "no error", exc.__name__)
    except exc as e:
        missing = [c for c in contains if c not in str(e)]
        check(label + (" (message has %s)" % list(contains) if contains else ""), missing, [])
    except Exception as e:  # noqa: BLE001
        check(label, type(e).__name__, exc.__name__)


def payload(result):
    return json.loads(result["content"][0]["text"])


def reset_home():
    shutil.rmtree(HOME, ignore_errors=True)
    os.makedirs(HOME)
    srv.reset_api()


# ---------------------------------------------------------------- helpers (carried from 0.4)
print("helpers: URLs")
for raw, want in [("iviq.substack.com", "https://iviq.substack.com"),
                  ("https://iviq.substack.com/", "https://iviq.substack.com"),
                  ("http://iviq.substack.com", "https://iviq.substack.com"), ("", "")]:
    check("normalize(%r)" % raw, srv.normalize_publication_url(raw), want)
check("subdomain_of", srv.subdomain_of("iviq.substack.com"), "iviq")
check("subdomain_of custom domain", srv.subdomain_of("https://tie.example.com"), None)
for raw, want in [("https://Acme.substack.com/p/x?y=1", "acme.substack.com"),
                  ("acme.substack.com", "acme.substack.com"), ("www.acme.substack.com", "acme.substack.com")]:
    check("canonical_host(%r)" % raw, srv.canonical_host(raw), want)

print("helpers: drafts")
real_payload = {"posts": [{"id": 208981733, "is_published": False, "draft_title": "", "title": None, "slug": None}]}
check("unwrap object form", len(srv.unwrap_items(real_payload, "posts")), 1)
check("unwrap None", srv.unwrap_items(None, "posts"), [])
s = srv.draft_summary(real_payload["posts"][0], "https://iviq.substack.com")
check("title falls back", s["title"], "(untitled draft)")
check("post_url None without slug", s["post_url"], None)
check("editor_url built from the client's publication", s["editor_url"],
      "https://iviq.substack.com/publish/post/208981733")
check("post_url from draft_slug", srv.draft_summary({"id": 7, "draft_slug": "my-post"}, "iviq.substack.com")["post_url"],
      "https://iviq.substack.com/p/my-post")
sched = {"id": 9, "draft_title": "T", "should_send_email": True, "audience": "everyone",
         "write_comment_permissions": "none", "postSchedules": [{"trigger_at": "2026-08-03T13:00:00.000Z"}]}
check("scheduled_for from postSchedules", srv.draft_summary(sched)["scheduled_for"], "2026-08-03T13:00:00.000Z")
check("list payload explains unknown schedule", "scheduled_for_note" in srv.draft_summary({"id": 9}), True)
caps_free = {"paid_enabled": False, "payments_state": "disabled", "existing_tags": [{"name": "debugging"}]}
raises("only_paid audience rejected on free pub", lambda: srv.validate_settings("only_paid", "everyone", caps_free), ValueError)
plan = srv.resolve_tags(None, ["Debugging", "MCP tooling", "debugging"], caps_free)
check("tags normalized/split", (plan["existing"], plan["new"]), (["debugging"], ["mcp-tooling"]))
raises("naive datetime rejected", lambda: srv.parse_iso_aware("2026-08-03T09:00:00"), ValueError)
raises("bad slug rejected", lambda: srv.validate_slug("Bad Slug"), ValueError)

print("helpers: browser profiles")
root2 = tempfile.mkdtemp()
with open(os.path.join(root2, "Local State"), "w") as f:
    json.dump({"profile": {"info_cache": {
        "Default": {"name": "Person 1", "user_name": "me@x.com"},
        "Profile 1": {"name": "Work", "user_name": "work@client.com"},
        "Profile 9": {}}}}, f)
check("selector by display name", srv.resolve_profile_selector("work", "chrome", root=root2)["dir"], "Profile 1")
raises("ambiguous selector", lambda: srv.resolve_profile_selector("profile", "chrome", root=root2), ValueError)

# ---------------------------------------------------------------- fakes
BY_SID = {
    "sid-alpha": (True, {"handle": "alpha-owner", "email": "a@x.com", "subdomains": ["alpha"], "primary": "alpha"}),
    "sid-beta": (True, {"handle": "beta-owner", "email": "b@x.com", "subdomains": ["beta"], "primary": "beta"}),
    "sid-both": (True, {"handle": "agency", "email": "ag@x.com", "subdomains": ["alpha", "beta"], "primary": None}),
    "sid-stale": (False, {"reason": "session_invalid", "status": 401}),
    "sid-alpha-2": (True, {"handle": "alpha-owner", "email": "a@x.com", "subdomains": ["alpha"], "primary": "alpha"}),
    "sid-intruder": (True, {"handle": "intruder", "email": "i@x.com", "subdomains": ["alpha"], "primary": None}),
}
NET_CALLS = []


def fake_probe(cookies):
    NET_CALLS.append("probe")
    sid = (cookies or {}).get("substack.sid")
    return BY_SID.get(sid, (False, {"reason": "session_invalid", "status": 401}))


REAUTH_MSG = "For your security, please sign out and sign back in to do this."  # live, 2026-10-25


class NotesHTTPError(Exception):
    """SubstackAPIException shape: status_code + text."""

    def __init__(self, code, text=""):
        super().__init__("APIError(code=%d): %s" % (code, text))
        self.status_code = code


class FakeResponse:
    def __init__(self, status, data):
        self.status_code, self._data = status, data
        self.content = b"" if data is None else b"{}"

    def json(self):
        return self._data


class FakeNotes:
    """substack.com Notes as probed live 2026-10-22: drafts paged by cursor, and a PATCH without
    attachmentIds drops the link card."""

    def __init__(self):
        self.reset()

    def reset(self, page=20):
        self.drafts, self.requests, self.next_id, self.page = {}, [], 100, page
        self.drop_cards = self.publish_on_patch = self.reauth = False
        self.rate_limit = 0  # this many next requests answer 429
        self.urls, self.note_stats, self.posts, self.traffic = [], {}, [], {}
        self.reading_hours = {"sessions_by_utc_hour_of_week": [0] * 168}

    def request(self, method, url, json=None, params=None, sid=None):
        path = url.split("/api/v1/", 1)[1]
        self.urls.append(url)
        self.requests.append((method, path, json, dict(params or {})))
        if sid in globals().get("DEAD_SIDS", ()):
            return FakeResponse(401, {"error": "Not authorized"})
        if self.reauth:
            return FakeResponse(403, {"error": REAUTH_MSG, "type": "reauthentication_required"})
        if self.rate_limit:
            self.rate_limit -= 1
            return FakeResponse(429, {"error": "Too Many Requests"})
        cards =lambda ids: [{"id": i, "type": "image"} if i.startswith("img-") else  # noqa: E731
                             {"id": i, "type": "post", "post": {"id": 7}} for i in ids or []]
        if method == "POST" and path == "image":
            return FakeResponse(200, {"url": "https://substack-post-media.s3.amazonaws.com/public/images/x.png"})
        if method == "POST" and path == "comment/attachment":
            if json.get("type") == "image":
                return FakeResponse(200, {"id": "img-1", "type": "image"})
            return FakeResponse(200, {"id": "att-1", "type": "post"})
        if method == "POST" and path in ("comment/draft", "comment/feed"):
            self.next_id += 1
            note = {"id": self.next_id, "body": "text", "body_json": json["bodyJson"],
                    "reply_minimum_role": json["replyMinimumRole"], "attachments": cards(json.get("attachmentIds"))}
            if path == "comment/feed":
                return FakeResponse(200, dict(note, status="published"))
            note.update(status="draft", trigger_at=json.get("trigger_at"))
            self.drafts[note["id"]] = note
            return FakeResponse(200, dict(note))
        if method == "GET" and path == "feed/drafts":
            items = sorted(self.drafts.values(), key=lambda d: d["id"])
            start = int((params or {}).get("cursor") or 0)
            more = start + self.page < len(items)
            return FakeResponse(200, {"drafts": [{k: v for k, v in d.items() if k != "status"} for d in items[start:start + self.page]],
                                      "hasMore": more, "nextCursor": str(start + self.page) if more else None})
        if method == "PATCH" and path.startswith("feed/comment/"):
            d = self.drafts[int(path.rsplit("/", 1)[1])]
            d.update(body_json=json["bodyJson"], trigger_at=json["trigger_at"],
                     attachments=[] if self.drop_cards else cards(json.get("attachmentIds")))
            if self.publish_on_patch:
                self.drafts.pop(d["id"])
            return FakeResponse(200, dict(d))
        if method == "DELETE" and path.startswith("comment/"):
            self.drafts.pop(int(path.rsplit("/", 1)[1]), None)
            return FakeResponse(200, None)
        if method == "GET" and path.startswith("note_stats/c-"):
            answer = self.note_stats.get(int(path.rsplit("-", 1)[1]), 404)
            return FakeResponse(answer, {"error": "boom"}) if isinstance(answer, int) else FakeResponse(200, answer)
        if method == "GET" and path == "post_management/published":
            start, n = int(params["offset"]), int(params["limit"])
            return FakeResponse(200, {"posts": self.posts[start:start + n], "offset": start, "limit": n,
                                      "total": len(self.posts), "isCapped": False})
        if method == "GET" and path == "publication/stats/reading_hours":
            return FakeResponse(200, self.reading_hours)
        if method == "GET" and path.startswith("post_management/detail/"):
            post_id = int(path.split("/")[2])
            if path.endswith("/traffic"):
                return FakeResponse(200, self.traffic.get(post_id, {}))
            return FakeResponse(200, {"posts": [p for p in self.posts if p["id"] == post_id], "total": 1})
        return FakeResponse(404, {"error": "not found"})


FAKE_NOTES = FakeNotes()


class _NotesSession:
    def __init__(self, sid):
        self.sid = sid

    def request(self, method, url, json=None, params=None):
        return FAKE_NOTES.request(method, url, json=json, params=params, sid=self.sid)

    def mount(self, prefix, adapter):
        self.mounted = (prefix, adapter)


class FakeApi:
    instances = []
    base_url = "https://substack.com/api/v1"
    profile = {"handle": "alpha-owner", "primaryPublication": {"subdomain": "alpha"}}

    def __init__(self, cookies_string="", publication_url=""):
        # python-substack keeps the publication's API root there (change_publication)
        self.cookies_string, self.publication_url = cookies_string, publication_url.rstrip("/") + "/api/v1"
        self.calls = []
        self._session = _NotesSession(cookies_string.split("=", 1)[-1])
        FakeApi.instances.append(self)

    @staticmethod
    def _handle_response(response):
        if not 200 <= response.status_code < 300:
            raise NotesHTTPError(response.status_code, (response.json() or {}).get("error", ""))
        return response.json()

    def get_user_profile(self):
        return FakeApi.profile

    def create_draft_from_markdown(self, **kw):
        self.calls.append(("create", kw))
        return {"draft": {"id": 1, "draft_title": kw["title"], "slug": kw["slug"], "is_published": False}}

    def get_draft(self, i):
        self.calls.append(("get_draft", i))
        return {"id": i, "draft_title": "T", "slug": "my-post", "is_published": False, "audience": "everyone",
                "write_comment_permissions": "everyone", "should_send_email": True, "postSchedules": []}

    def put_draft(self, i, **kw):
        self.calls.append(("put", i, kw)); return self.get_draft(i)

    def delete_draft(self, i):
        self.calls.append(("delete", i))

    def get_user_primary_publication(self):
        return {"subdomain": "x", "payments_state": "disabled"}

    def get_sections(self):
        return []

    def get_publication_post_tags(self):
        return []

    def add_tags_to_post(self, *a):
        self.calls.append(("tags", a))

    def call(self, *a, **k):
        return []

    def get_drafts(self, **kw):
        return {"posts": [self.get_draft(5)]}


fake_sub = types.ModuleType("substack")
fake_sub.Api = FakeApi
sys.modules["substack"] = fake_sub
srv.probe_session = fake_probe
_real_retry_rate_limits = srv.retry_rate_limits
RATE_LIMITED_SESSIONS = []
srv.retry_rate_limits = RATE_LIMITED_SESSIONS.append  # the real one needs requests/urllib3; checked below

# ---------------------------------------------------------------- registry
print("registry: add_client, list_clients, unknown client")
reset_home()
p = payload(srv.dispatch_tool("add_client", {"client": "alpha", "publication_url": "alpha.substack.com"}))
check("add_client normalizes and reports", (p["added"], p["publication"]), ("alpha", "alpha.substack.com"))
raises("duplicate client refused", lambda: srv.dispatch_tool("add_client", {"client": "alpha", "publication_url": "x.substack.com"}),
       srv.ClientError, ["already exists"])
raises("bad slug refused", lambda: srv.dispatch_tool("add_client", {"client": "Alpha Co", "publication_url": "x.substack.com"}),
       srv.ClientError)
raises("custom domain refused", lambda: srv.dispatch_tool("add_client", {"client": "gamma", "publication_url": "https://gamma.example.com"}),
       srv.ClientError, ["substack.com"])
srv.dispatch_tool("add_client", {"client": "beta", "publication_url": "https://beta.substack.com"})
lst = payload(srv.dispatch_tool("list_clients", {}))
check("list_clients shows both, unbound", [(c["client"], c["bound"]) for c in lst["clients"]],
      [("alpha", False), ("beta", False)])
if sys.platform != "win32":  # Windows has no POSIX modes; the user profile's ACL guards the file
    check("registry file is 0600", oct(os.stat(srv.clients_path()).st_mode & 0o777), "0o600")
raises("unknown client lists configured ones", lambda: srv.dispatch_tool("get_draft", {
    "client": "gamma", "expected_publication": "gamma.substack.com", "draft_id": 1}), srv.ClientError, ["alpha", "beta"])
raises("client required on publication tools", lambda: srv.dispatch_tool("get_draft", {
    "expected_publication": "alpha.substack.com", "draft_id": 1}), srv.ClientError, ["`client` is required"])
raises("expected_publication required", lambda: srv.dispatch_tool("get_draft", {"client": "alpha", "draft_id": 1}),
       srv.ClientError, ["expected_publication"])
raises("client required on note tools", lambda: srv.dispatch_tool("create_note_draft", {
    "expected_publication": "alpha.substack.com", "text": "hi"}), srv.ClientError, ["`client` is required"])
raises("expected_publication required on note tools", lambda: srv.dispatch_tool("list_note_drafts", {"client": "alpha"}),
       srv.ClientError, ["expected_publication"])

# bind alpha and beta directly (sessions + pins), as bind_client would
srv.update_client("alpha", act_as="alpha-owner", cookie_source={"type": "user_data_dir", "browser": "chrome", "path": "/tmp/x/alpha"}, bound_at="2026-10-06T10:00:00+00:00")
srv.save_session("alpha", {"substack.sid": "sid-alpha"}, {"handle": "alpha-owner"})
srv.update_client("beta", act_as="beta-owner", cookie_source={"type": "user_data_dir", "browser": "chrome", "path": "/tmp/x/beta"}, bound_at="2026-10-06T10:00:00+00:00")
srv.save_session("beta", {"substack.sid": "sid-beta"}, {"handle": "beta-owner"})
if sys.platform != "win32":
    check("session file is 0600", oct(os.stat(srv.session_path("alpha")).st_mode & 0o777), "0o600")

# ---------------------------------------------------------------- expected_publication, three cases × read/write/cleanup
print("expected_publication check")
NOTE_TOOL_ARGS = (("create_note_draft", {"text": "hi"}), ("list_note_drafts", {}), ("delete_note_draft", {"note_id": 1}))
for tool, extra in (("get_draft", {"draft_id": 1}), ("create_draft", {"title": "T", "body_markdown": "b", "slug": "t"}),
                    ("delete_draft", {"draft_id": 1})) + NOTE_TOOL_ARGS:
    FakeApi.instances.clear()
    raises("%s: expected != registry refused before any network" % tool,
           lambda t=tool, x=extra: srv.dispatch_tool(t, dict(x, client="alpha", expected_publication="beta.substack.com")),
           srv.PublicationMismatch, ["expected_publication='beta.substack.com'", "registry='alpha.substack.com'"])
    check("%s: no Api was built" % tool, len(FakeApi.instances), 0)
# registry != live: beta's record points at alpha's publication, but beta's session reaches beta only
srv.update_client("beta", publication_url="https://alpha.substack.com")
for tool, extra in (("get_draft", {"draft_id": 1}), ("create_draft", {"title": "T", "body_markdown": "b", "slug": "t"}),
                    ("delete_draft", {"draft_id": 1})) + NOTE_TOOL_ARGS:
    FakeApi.instances.clear()
    raises("%s: live session not reaching the registry publication refused" % tool,
           lambda t=tool, x=extra: srv.dispatch_tool(t, dict(x, client="beta", expected_publication="alpha.substack.com")),
           srv.PublicationMismatch, ["live session reaches=['beta']"])
    check("%s: no Api was built (live mismatch)" % tool, len(FakeApi.instances), 0)
srv.update_client("beta", publication_url="https://beta.substack.com")
srv.reset_api()

print("identity and session gates")
srv.save_session("alpha", {"substack.sid": "sid-intruder"}, {"handle": "intruder"})
srv.update_client("alpha", cookie_source=None)
raises("identity mismatch refused", lambda: srv.dispatch_tool("get_draft", {"client": "alpha", "expected_publication": "alpha.substack.com", "draft_id": 1}),
       srv.ClientError, ["logged in as 'intruder'", "act_as='alpha-owner'"])
srv.update_client("alpha", act_as=None)
raises("unbound client refused", lambda: srv.dispatch_tool("get_draft", {"client": "alpha", "expected_publication": "alpha.substack.com", "draft_id": 1}),
       srv.ClientError, ["unbound", "bind_client"])
srv.update_client("alpha", act_as="alpha-owner")
srv.save_session("alpha", {"substack.sid": "sid-alpha"}, {"handle": "alpha-owner"})

print("publication tools echo client, publication and identity; interleaved clients keep separate sessions")
FakeApi.instances.clear()
out_a = payload(srv.dispatch_tool("get_draft", {"client": "alpha", "expected_publication": "alpha.substack.com", "draft_id": 1}))
out_b = payload(srv.dispatch_tool("get_draft", {"client": "beta", "expected_publication": "beta.substack.com", "draft_id": 2}))
out_a2 = payload(srv.dispatch_tool("get_draft", {"client": "alpha", "expected_publication": "https://alpha.substack.com/p/x", "draft_id": 3}))
check("echo on alpha", (out_a["client"], out_a["publication"], out_a["act_as"], out_a["logged_in_as"]),
      ("alpha", "alpha.substack.com", "alpha-owner", "alpha-owner"))
check("echo on beta", (out_b["client"], out_b["publication"]), ("beta", "beta.substack.com"))
check("post_url built from each client's publication", (out_a["post_url"], out_b["post_url"]),
      ("https://alpha.substack.com/p/my-post", "https://beta.substack.com/p/my-post"))
check("two Api instances, one per client", len(FakeApi.instances), 2)
check("each Api wraps its own session", sorted(i.cookies_string for i in FakeApi.instances),
      ["substack.sid=sid-alpha", "substack.sid=sid-beta"])
check("alpha reused its cached client on the third call", out_a2["client"], "alpha")
lst = payload(srv.dispatch_tool("list_drafts", {"client": "alpha", "expected_publication": "alpha.substack.com"}))
check("list results carry the echo too", (lst["client"], len(lst["items"])), ("alpha", 1))
cr = payload(srv.dispatch_tool("create_draft", {"client": "beta", "expected_publication": "beta.substack.com",
                                                "title": "T", "body_markdown": "b", "slug": "my-post"}))
check("create_draft on beta uses beta's Api", cr["editor_url"], "https://beta.substack.com/publish/post/1")

print("one-shot auto-refresh from the bound source")
reset_home()
srv.dispatch_tool("add_client", {"client": "alpha", "publication_url": "alpha.substack.com"})
ded = tempfile.mkdtemp()
os.makedirs(os.path.join(ded, "Default"))
open(os.path.join(ded, "Default", "Cookies"), "w").close()
srv.update_client("alpha", act_as="alpha-owner", cookie_source={"type": "user_data_dir", "browser": "chrome", "path": ded},
                  bound_at="2026-10-06T10:00:00+00:00")
srv.save_session("alpha", {"substack.sid": "sid-stale"}, {"handle": "alpha-owner"})
fake_pcc = types.ModuleType("pycookiecheat")
fake_pcc.BrowserType = lambda b: b
SOURCE_COOKIES = {os.path.join(ded, "Default", "Cookies"): {"substack.sid": "sid-alpha-2"}}
fake_pcc.chrome_cookies = lambda url, browser=None, cookie_file=None: dict(SOURCE_COOKIES.get(cookie_file) or {})
sys.modules["pycookiecheat"] = fake_pcc
NET_CALLS.clear()
out = payload(srv.dispatch_tool("get_draft", {"client": "alpha", "expected_publication": "alpha.substack.com", "draft_id": 1}))
check("expired session re-read once from the bound source", out["logged_in_as"], "alpha-owner")
check("the fresh session was stored", srv.load_session("alpha")["cookies"]["substack.sid"], "sid-alpha-2")
check("exactly two probes (stale, then fresh)", NET_CALLS.count("probe"), 2)
SOURCE_COOKIES[os.path.join(ded, "Default", "Cookies")] = {"substack.sid": "sid-stale"}
srv.save_session("alpha", {"substack.sid": "sid-stale"}, {"handle": "alpha-owner"})
srv.reset_api()
raises("source still stale -> actionable error", lambda: srv.dispatch_tool("get_draft", {
    "client": "alpha", "expected_publication": "alpha.substack.com", "draft_id": 1}), srv.ClientError, ["refresh_session"])

print("401/403 during a tool call: refresh once, re-run a read, name the fix for a multi-step writer")
DEAD_SIDS = set()


class Refused(Exception):
    """SubstackAPIException shape: python-substack raises (status_code, text)."""

    def __init__(self, code):
        super().__init__("APIError(code=%d): refused" % code)
        self.status_code = code


class SessionAwareApi(FakeApi):
    """Answers 401/403 once the session it wraps has died server-side (DEAD_SIDS)."""

    def _sid(self):
        return self.cookies_string.split("=", 1)[-1]

    def get_draft(self, i):
        if self._sid() in DEAD_SIDS:
            self.calls.append(("get_draft", i))
            raise Refused(401)
        return super().get_draft(i)

    def create_draft_from_markdown(self, **kw):
        if self._sid() in DEAD_SIDS:
            raise Refused(403)
        return super().create_draft_from_markdown(**kw)


_by_sid_backup = dict(BY_SID)
fake_sub.Api = SessionAwareApi
SOURCE_READS = []
_plain_chrome_cookies = fake_pcc.chrome_cookies


def _counting_chrome_cookies(url, browser=None, cookie_file=None):
    SOURCE_READS.append(cookie_file)
    return _plain_chrome_cookies(url, browser=browser, cookie_file=cookie_file)


fake_pcc.chrome_cookies = _counting_chrome_cookies
reset_home()
srv.dispatch_tool("add_client", {"client": "alpha", "publication_url": "alpha.substack.com"})
srv.update_client("alpha", act_as="alpha-owner", cookie_source={"type": "user_data_dir", "browser": "chrome", "path": ded},
                  bound_at="2026-10-06T10:00:00+00:00")
srv.save_session("alpha", {"substack.sid": "sid-alpha"}, {"handle": "alpha-owner"})
SOURCE_COOKIES[os.path.join(ded, "Default", "Cookies")] = {"substack.sid": "sid-alpha-2"}
payload(srv.dispatch_tool("get_draft", {"client": "alpha", "expected_publication": "alpha.substack.com", "draft_id": 1}))
# the validated, cached session dies server-side within the validation TTL
DEAD_SIDS.add("sid-alpha")
BY_SID["sid-alpha"] = (False, {"reason": "session_invalid", "status": 401})
NET_CALLS.clear()
SOURCE_READS.clear()
FakeApi.instances.clear()
out = payload(srv.dispatch_tool("get_draft", {"client": "alpha", "expected_publication": "alpha.substack.com", "draft_id": 1}))
check("401 mid-call: session re-read from the bound source, the read re-run", out["logged_in_as"], "alpha-owner")
check("the re-run used an Api on the fresh session", FakeApi.instances[-1].cookies_string, "substack.sid=sid-alpha-2")
check("the fresh session was stored", srv.load_session("alpha")["cookies"]["substack.sid"], "sid-alpha-2")
check("the browser source was read once, and only the fresh cookie probed", (len(SOURCE_READS), NET_CALLS.count("probe")), (1, 1))
# the stored cookie still passes the profile probe but the publication refuses it: the
# bound source is re-read anyway and the retry runs on the new cookie, never the old one
DEAD_SIDS.add("sid-alpha-2")                      # endpoint refuses it; BY_SID still says valid
BY_SID["sid-alpha-3"] = (True, {"handle": "alpha-owner", "email": "a@x.com", "subdomains": ["alpha"], "primary": "alpha"})
SOURCE_COOKIES[os.path.join(ded, "Default", "Cookies")] = {"substack.sid": "sid-alpha-3"}
NET_CALLS.clear()
SOURCE_READS.clear()
FakeApi.instances.clear()
out = payload(srv.dispatch_tool("get_draft", {"client": "alpha", "expected_publication": "alpha.substack.com", "draft_id": 1}))
check("probe-valid but refused cookie: the source was still read once", len(SOURCE_READS), 1)
check("the retry ran on the re-read cookie, not the refused one", FakeApi.instances[-1].cookies_string, "substack.sid=sid-alpha-3")
check("no retry on the old cookie", [c for i in FakeApi.instances for c in i.calls if i._sid() == "sid-alpha-2"], [])
check("the re-read session was stored", srv.load_session("alpha")["cookies"]["substack.sid"], "sid-alpha-3")
# a multi-step writer gets the re-read but is not re-run; the error names the fix
DEAD_SIDS.add("sid-alpha-3")
BY_SID["sid-alpha-4"] = (True, {"handle": "alpha-owner", "email": "a@x.com", "subdomains": ["alpha"], "primary": "alpha"})
SOURCE_COOKIES[os.path.join(ded, "Default", "Cookies")] = {"substack.sid": "sid-alpha-4"}
raises("403 during create_draft: source re-read, not re-run, fix named",
       lambda: srv.dispatch_tool("create_draft", {"client": "alpha", "expected_publication": "alpha.substack.com",
                                                  "title": "T", "body_markdown": "b", "slug": "t"}),
       srv.ClientError, ["HTTP 403", "create_draft", "run it again"])
check("the session was re-read anyway", srv.load_session("alpha")["cookies"]["substack.sid"], "sid-alpha-4")
# the re-read yields a cookie the publication still refuses: no second retry, the fix is named
DEAD_SIDS.add("sid-alpha-4")
FakeApi.instances.clear()
SOURCE_READS.clear()
raises("still refused after the re-read -> one retry only, login named",
       lambda: srv.dispatch_tool("get_draft", {"client": "alpha", "expected_publication": "alpha.substack.com", "draft_id": 1}),
       srv.ClientError, ["still answered HTTP 401", "alpha-owner", "alpha.substack.com", "refresh_session"])
check("one source read and one new Api (the single retry; the refused cookie was the cached Api)",
      (len(SOURCE_READS), len(FakeApi.instances), FakeApi.instances[-1].cookies_string), (1, 1, "substack.sid=sid-alpha-4"))
# the re-read source itself being dead gives the usual actionable error, and nothing is re-run
BY_SID["sid-alpha-4"] = (False, {"reason": "session_invalid", "status": 401})
FakeApi.instances.clear()
raises("401 and the bound source dead -> refresh_session named",
       lambda: srv.dispatch_tool("get_draft", {"client": "alpha", "expected_publication": "alpha.substack.com", "draft_id": 1}),
       srv.ClientError, ["refresh_session"])
check("no Api built on a failed re-read", len(FakeApi.instances), 0)
# no bound source: nothing to re-read, bind_client named
BY_SID["sid-alpha-9"] = (True, {"handle": "alpha-owner", "email": "a@x.com", "subdomains": ["alpha"], "primary": "alpha"})
DEAD_SIDS.add("sid-alpha-9")
srv.update_client("alpha", cookie_source=None)
srv.save_session("alpha", {"substack.sid": "sid-alpha-9"}, {"handle": "alpha-owner"})
srv.reset_api()
raises("401 with no bound source -> bind_client named",
       lambda: srv.dispatch_tool("get_draft", {"client": "alpha", "expected_publication": "alpha.substack.com", "draft_id": 1}),
       srv.ClientError, ["no bound cookie source", "bind_client"])
fake_pcc.chrome_cookies = _plain_chrome_cookies
# what counts as an auth error
check("SubstackAPIException-shaped 401/403/404", tuple(srv.auth_error_status(Refused(c)) for c in (401, 403, 404)), (401, 403, None))


class HttpErr(Exception):
    response = types.SimpleNamespace(status_code=403)


check("requests.HTTPError shape", srv.auth_error_status(HttpErr("403 Client Error: Forbidden for url")), 403)
check("text-only unauthorized", srv.auth_error_status(Exception("Unauthorized")), 401)
check("a 404 that mentions 401 as a number is not", srv.auth_error_status(Exception("post 401 not found")), None)
check("our own refusals never are", (srv.is_auth_error(srv.ClientError("forbidden")), srv.is_auth_error(ValueError("401 forbidden"))),
      (False, False))
BY_SID.clear()
BY_SID.update(_by_sid_backup)
DEAD_SIDS.clear()
fake_sub.Api = FakeApi
srv.reset_api()

print("probe tools: bind_client, refresh_session, clients_status")
reset_home()
srv.dispatch_tool("add_client", {"client": "alpha", "publication_url": "alpha.substack.com"})
srv.dispatch_tool("add_client", {"client": "beta", "publication_url": "beta.substack.com"})
std = tempfile.mkdtemp()
for prof in ("Default", "Profile 1", "Profile 2"):
    os.makedirs(os.path.join(std, prof))
    open(os.path.join(std, prof, "Cookies"), "w").close()
PROFILE_COOKIES = {
    os.path.join(std, "Default", "Cookies"): {"substack.sid": "sid-beta"},
    os.path.join(std, "Profile 1", "Cookies"): {"other": "x"},
    os.path.join(std, "Profile 2", "Cookies"): {"substack.sid": "sid-alpha"},
}
fake_pcc.chrome_cookies = lambda url, browser=None, cookie_file=None: dict(PROFILE_COOKIES.get(cookie_file) or {})
_orig_files, _orig_ls = srv.chrome_profile_cookie_files, srv.local_state_profiles
srv.chrome_profile_cookie_files = lambda browser, root=None: [(n, os.path.join(std, n, "Cookies")) for n in ("Default", "Profile 1", "Profile 2")]
srv.local_state_profiles = lambda browser, root=None: [
    {"dir": "Default", "name": "Personal", "email": "me@x.com"},
    {"dir": "Profile 1", "name": "Empty", "email": ""},
    {"dir": "Profile 2", "name": "Alpha Work", "email": "alpha@x.com"}]
st = payload(srv.dispatch_tool("clients_status", {}))
check("status before binding: missing sessions, fix bind_client",
      [(r["client"], r["session"], r["fix"]) for r in st["clients"]],
      [("alpha", "missing", "bind_client"), ("beta", "missing", "bind_client")])
b = payload(srv.dispatch_tool("bind_client", {"client": "alpha", "profile": "alpha work"}))
check("explicit profile binds and pins on macOS", (b["cookie_source"], b["act_as"]), ("chrome profile 'Profile 2'", "alpha-owner"))
check("no profile scan in the result (gone in 0.9.0)", "profiles_scanned" in b, False)
check("session persisted", srv.load_session("alpha")["cookies"]["substack.sid"], "sid-alpha")
PROFILE_COOKIES[os.path.join(std, "Profile 1", "Cookies")] = {"substack.sid": "sid-intruder"}
raises("bind_client on an unknown client refused", lambda: srv.dispatch_tool("bind_client", {"client": "gamma-nope"}),
       srv.ClientError, ["unknown client"])
raises("rebinding to another identity needs confirm_switch",
       lambda: srv.dispatch_tool("bind_client", {"client": "alpha", "profile": "Profile 1"}), srv.ClientError, ["confirm_switch"])
b = payload(srv.dispatch_tool("bind_client", {"client": "alpha", "profile": "Profile 1", "confirm_switch": True}))
check("confirmed switch re-pins and reports", b["act_as_changed"], {"from": "alpha-owner", "to": "intruder"})
b = payload(srv.dispatch_tool("bind_client", {"client": "alpha", "profile": "Profile 2", "confirm_switch": True}))
check("switched back", b["act_as"], "alpha-owner")
raises("profile that does not reach the publication refused",
       lambda: srv.dispatch_tool("bind_client", {"client": "beta", "profile": "Profile 2"}), srv.ClientError, ["no access"])
b = payload(srv.dispatch_tool("bind_client", {"client": "beta", "profile": "Personal"}))
check("beta bound to the Default profile", (b["cookie_source"], b["act_as"]), ("chrome profile 'Default'", "beta-owner"))
raises("refresh_session on an unbound client refused", lambda: (srv.update_client("beta", act_as=None),
       srv.dispatch_tool("refresh_session", {"client": "beta"})), srv.ClientError, ["unbound"])
srv.update_client("beta", act_as="beta-owner")
PROFILE_COOKIES[os.path.join(std, "Default", "Cookies")] = {"substack.sid": "sid-both"}
raises("refresh_session: source now another identity -> refuse, nothing stored",
       lambda: srv.dispatch_tool("refresh_session", {"client": "beta"}), srv.ClientError, ["agency", "beta-owner"])
check("beta's stored session unchanged", srv.load_session("beta")["cookies"]["substack.sid"], "sid-beta")
PROFILE_COOKIES[os.path.join(std, "Default", "Cookies")] = {"substack.sid": "sid-beta"}
r = payload(srv.dispatch_tool("refresh_session", {"client": "beta"}))
check("refresh_session ok", (r["session"], r["logged_in_as"]), ("valid", "beta-owner"))
st = payload(srv.dispatch_tool("clients_status", {}))
check("status after binding: both ready", (st["all_ready"], [r["ready"] for r in st["clients"]]), (True, [True, True]))
st1 = payload(srv.dispatch_tool("clients_status", {"client": "alpha"}))
check("status for one client", [r["client"] for r in st1["clients"]], ["alpha"])
lp = payload(srv.dispatch_tool("list_browser_profiles", {"root": std}))
check("list_browser_profiles shows bindings", [(pr["dir"], pr.get("bound_to")) for pr in lp["profiles"]] if lp["profiles"] else "no profiles",
      "no profiles")  # root has no Local State here; dedicated list still works
check("list_browser_profiles never read cookies", "no cookies were read" in lp["note"], True)

print("scope drift guard")
check("every tool has a scope", sorted(srv.TOOL_SCOPES), sorted(srv.TOOL_HANDLERS))
check("tools/list hides the scope key", any("scope" in t for t in srv.PUBLIC_TOOLS), False)
for name in ("get_draft", "put_draft", "create_draft_from_markdown", "call", "get_drafts", "delete_draft",
             "schedule_draft", "publish_draft", "get_user_primary_publication"):
    check("ProbeFacade has no %s" % name, hasattr(srv.ProbeFacade("alpha"), name), False)
_probe_backup = srv.probe_session


def _boom(*a, **k):
    raise AssertionError("network call from a registry tool")


srv.probe_session = _boom
for name, args in (("list_clients", {}), ("add_client", {"client": "gamma", "publication_url": "gamma.substack.com"}),
                   ("list_browser_profiles", {"root": std})):
    try:
        srv.dispatch_tool(name, args)
        check("registry tool %s makes no network call" % name, True, True)
    except AssertionError as e:
        check("registry tool %s makes no network call" % name, str(e), "")
srv.probe_session = _probe_backup
raises("publication handler cannot run without the dispatcher's facade",
       lambda: srv.tool_get_draft({"draft_id": 1}), TypeError)
raises("probe handler cannot run without a facade", lambda: srv.tool_bind_client({"client": "alpha"}), TypeError)

srv.chrome_profile_cookie_files, srv.local_state_profiles = _orig_files, _orig_ls

# ---------------------------------------------------------------- migration
print("migration: select by reference, import once, journal, backups, rollback, switch")
reset_home()
desktop_path = os.path.join(HOME, "claude_desktop_config.json")
installed_server = os.path.join(HOME, "server.py")
with open(installed_server, "w") as f:
    f.write("# v0.4.2 server stand-in\n")
legacy_default = os.path.join(HOME, "config.json")
legacy_acme = os.path.join(HOME, "acme.json")
legacy_envpub = os.path.join(HOME, "envpub.json")   # publication only in the entry's env (0.4 accepted that alone)
legacy_nopub = os.path.join(HOME, "nopub.json")     # no publication anywhere: cannot be imported, must survive
stray = os.path.join(HOME, "stray.json")
reserved_dir = os.path.join(HOME, "backup-old")
os.makedirs(reserved_dir)
reserved_cfg = os.path.join(reserved_dir, "config.json")
acme_browser = tempfile.mkdtemp()
for path, data in ((legacy_default, {"publication_url": "https://tie-pub.substack.com", "act_as": "owner",
                                      "cookie_file": os.path.join(os.path.expanduser(srv.browser_data_dir("chrome")), "Profile 2", "Cookies"),
                                      "cookies": {"substack.sid": "sid-tie"}}),
                   (legacy_acme, {"publication_url": "https://acme.substack.com", "act_as": "acme-owner",
                                  "cookie_file": os.path.join(acme_browser, "Default", "Cookies"),
                                  "cookies": {"substack.sid": "sid-acme"}}),
                   (legacy_envpub, {"act_as": "envpub-owner", "cookies": {"substack.sid": "sid-envpub"}}),
                   (legacy_nopub, {"cookies": {"substack.sid": "sid-nopub"}}),
                   (stray, {"publication_url": "https://stray.substack.com", "cookies": {"substack.sid": "sid-stray"}}),
                   (reserved_cfg, {"publication_url": "https://reserved.substack.com"})):
    with open(path, "w") as f:
        json.dump(data, f)
desktop = {"mcpServers": {
    "tie-substack": {"command": "python3", "args": [installed_server]},
    "tie-substack-acme": {"command": "python3", "args": [installed_server],
                          "env": {"TIE_SUBSTACK_CONFIG": legacy_acme, "SUBSTACK_PUBLICATION_URL": "https://acme.substack.com"}},
    "tie-substack-reserved": {"command": "python3", "args": [installed_server], "env": {"TIE_SUBSTACK_CONFIG": reserved_cfg}},
    "tie-substack-envpub": {"command": "python3", "args": [installed_server],
                            "env": {"TIE_SUBSTACK_CONFIG": legacy_envpub, "SUBSTACK_PUBLICATION_URL": "https://envpub.substack.com"}},
    "tie-substack-nopub": {"command": "python3", "args": [installed_server], "env": {"TIE_SUBSTACK_CONFIG": legacy_nopub}},
    "tie-substack-nofile": {"command": "python3", "args": [installed_server],
                            "env": {"TIE_SUBSTACK_CONFIG": os.path.join(HOME, "missing.json"),
                                    "SUBSTACK_PUBLICATION_URL": "https://nofile.substack.com"}},
    "other-server": {"command": "x"},
}}
with open(desktop_path, "w") as f:
    json.dump(desktop, f)
rep = srv.migrate_import(desktop_path, default_slug="tie", installed_server=installed_server)
check("selected by reference: default, acme, env-only publication and env publication without a file; not the stray",
      sorted(s["slug"] for s in rep["selected"]), ["acme", "envpub", "nofile", "tie"])
check("four imports", sorted(i["slug"] for i in rep["imported"]), ["acme", "envpub", "nofile", "tie"])
check("entries without a determinable publication are reported, never imported",
      sorted((u["entry"], "no publication" in u["reason"] or "reserved" in u["reason"]) for u in rep["unresolvable"]),
      [("tie-substack-nopub", True), ("tie-substack-reserved", True)])
clients = srv.load_clients()["clients"]
check("tie imported with its pin and a profile source", (clients["tie"]["act_as"], clients["tie"]["cookie_source"]["type"], clients["tie"]["cookie_source"]["profile"]),
      ("owner", "profile", "Profile 2"))
check("acme imported with a dedicated-browser source", clients["acme"]["cookie_source"], {"type": "user_data_dir", "browser": "chrome", "path": acme_browser})
check("envpub took its publication from the entry env, with its pin and session",
      (clients["envpub"]["publication_url"], clients["envpub"]["act_as"], srv.load_session("envpub")["cookies"]["substack.sid"]),
      ("https://envpub.substack.com", "envpub-owner", "sid-envpub"))
check("nofile imported unbound (no session, no source)", (clients["nofile"]["act_as"], clients["nofile"]["cookie_source"]), (None, None))
check("nopub never reached the registry", "nopub" in clients, False)
check("sessions imported", (srv.load_session("tie")["cookies"]["substack.sid"], srv.load_session("acme")["cookies"]["substack.sid"]), ("sid-tie", "sid-acme"))
check("backup-original written once", rep.get("written"), True)
manifest = json.load(open(os.path.join(HOME, "backup-original", "MANIFEST.json")))
check("manifest covers desktop config, the selected configs and the v0.4 server",
      sorted(os.path.basename(m["source"]) for m in manifest["files"].values()),
      ["acme.json", "claude_desktop_config.json", "config.json", "envpub.json", "server.py"])
check("legacy server kept", os.path.isfile(os.path.join(HOME, "legacy", "server.py")), True)
check("per-run backup exists", os.path.isdir(rep["run_backup"]), True)
rep2 = srv.migrate_import(desktop_path, default_slug="tie", installed_server=installed_server)
check("re-run imports nothing twice", (len(rep2["imported"]), len(rep2["skipped"])), (0, 4))
check("backup-original not rewritten", rep2.get("written", False), False)
# a changed source re-imports that one client
with open(legacy_acme, "w") as f:
    json.dump({"publication_url": "https://acme.substack.com", "act_as": "acme-owner",
               "cookie_file": os.path.join(acme_browser, "Default", "Cookies"), "cookies": {"substack.sid": "sid-acme-2"}}, f)
rep3 = srv.migrate_import(desktop_path, default_slug="tie", installed_server=installed_server)
check("changed source re-imported", [i["slug"] for i in rep3["imported"]], ["acme"])
check("new session taken", srv.load_session("acme")["cookies"]["substack.sid"], "sid-acme-2")
# a rebound registry record wins over a later source change
srv.update_client("acme", act_as="acme-new", bound_at="2099-01-01T00:00:00+00:00")
with open(legacy_acme, "w") as f:
    json.dump({"publication_url": "https://acme.substack.com", "act_as": "acme-owner",
               "cookie_file": os.path.join(acme_browser, "Default", "Cookies"), "cookies": {"substack.sid": "sid-acme-3"}}, f)
rep4 = srv.migrate_import(desktop_path, default_slug="tie", installed_server=installed_server)
check("rebound record reported as a conflict, not overwritten",
      ([c["slug"] for c in rep4["conflicts"]], srv.load_clients()["clients"]["acme"]["act_as"]), (["acme"], "acme-new"))
# switch: nothing verified (an empty or crashed verification) removes nothing; every 0.4 entry keeps working
sw = srv.switch_entries(desktop_path, "/venv/bin/python3", "/home/server.py", verified=[], legacy_server=os.path.join(HOME, "legacy", "server.py"))
dcfg = json.load(open(desktop_path))["mcpServers"]
check("nothing verified -> nothing removed, not switched", (sw["removed"], sw["switched"]), ([], False))
check("every 0.4 entry kept on the legacy server", sorted(sw["kept_legacy_entries"]),
      ["tie-substack-acme", "tie-substack-envpub", "tie-substack-legacy", "tie-substack-nofile", "tie-substack-nopub", "tie-substack-reserved"])
check("kept entries point at the legacy server", dcfg["tie-substack-acme"]["args"], [os.path.join(HOME, "legacy", "server.py")])
check("kept entries keep their env", dcfg["tie-substack-nopub"]["env"], {"TIE_SUBSTACK_CONFIG": legacy_nopub})
check("new single entry added with the home", dcfg["tie-substack"]["env"], {"TIE_SUBSTACK_HOME": HOME})
check("unrelated servers untouched", "other-server" in dcfg, True)
rep5 = srv.migrate_import(desktop_path, default_slug="tie", installed_server=installed_server)
check("legacy entry is not re-imported as a client", "legacy" in {s["slug"] for s in rep5["selected"]}, False)
# a verified slug that no 0.4 entry maps to removes nothing
sw_ghost = srv.switch_entries(desktop_path, "/venv/bin/python3", "/home/server.py", verified=["ghost"], legacy_server=None)
check("unknown verified slug removes nothing", sw_ghost["removed"], [])
# every importable client verified: only their entries go; the unimportable ones survive, so it is not a full switch
sw2 = srv.switch_entries(desktop_path, "/venv/bin/python3", "/home/server.py", verified=["tie", "acme", "envpub", "nofile"], legacy_server=None)
dcfg = json.load(open(desktop_path))["mcpServers"]
check("verified clients' entries removed (the renamed default entry included)", sorted(sw2["removed"]),
      ["tie-substack-acme", "tie-substack-envpub", "tie-substack-legacy", "tie-substack-nofile"])
check("entries that could not be imported survive a full verification",
      sorted(n for n in dcfg if n.startswith("tie-substack")), ["tie-substack", "tie-substack-nopub", "tie-substack-reserved"])
check("not switched while a 0.4 entry remains, and it says which", (sw2["switched"], sorted(sw2["unresolvable"])),
      (False, ["tie-substack-nopub", "tie-substack-reserved"]))
# rollback: tampered snapshot refused; intact snapshot restores the pure v0.4 state
snap = os.path.join(HOME, "backup-original", "claude_desktop_config.json")
orig_bytes = open(snap, "rb").read()
with open(snap, "ab") as f:
    f.write(b"\n# tampered\n")
raises("hash mismatch refuses the rollback", lambda: srv.rollback(desktop_path), RuntimeError, ["hash"])
with open(snap, "wb") as f:
    f.write(orig_bytes)
rb = srv.rollback(desktop_path)
dcfg = json.load(open(desktop_path))["mcpServers"]
check("rollback restores the original desktop entries", sorted(n for n in dcfg if n.startswith("tie-substack")),
      ["tie-substack", "tie-substack-acme", "tie-substack-envpub", "tie-substack-nofile", "tie-substack-nopub", "tie-substack-reserved"])
check("rollback restores the v0.4 server", open(installed_server).read().startswith("# v0.4.2"), True)
check("rollback restores the original acme config", json.load(open(legacy_acme))["cookies"]["substack.sid"], "sid-acme")
check("registry survives a rollback", os.path.isfile(srv.clients_path()), True)
rb_last = srv.rollback(desktop_path, last=True)
check("--last restores from a per-run backup", os.path.basename(rb_last["restored_from"]).startswith("backup-2"), True)
raises("no snapshot -> rollback refuses", lambda: (shutil.rmtree(os.path.join(HOME, "backup-original")), srv.rollback(desktop_path)),
       RuntimeError, ["backup-original"])

print("migration: two 0.4 entries mapping to one slug are refused, never imported, never removed")
reset_home()
dup_desktop = os.path.join(HOME, "dup_desktop.json")
cfg_one, cfg_two = os.path.join(HOME, "one.json"), os.path.join(HOME, "two.json")
with open(cfg_one, "w") as f:
    json.dump({"publication_url": "https://one.substack.com", "act_as": "one", "cookies": {"substack.sid": "sid-one"}}, f)
with open(cfg_two, "w") as f:
    json.dump({"publication_url": "https://two.substack.com", "act_as": "two", "cookies": {"substack.sid": "sid-two"}}, f)
with open(dup_desktop, "w") as f:
    json.dump({"mcpServers": {
        "tie-substack": {"command": "python3", "args": ["/old/server.py"], "env": {"TIE_SUBSTACK_CONFIG": cfg_one}},
        "tie-substack-tie": {"command": "python3", "args": ["/old/server.py"], "env": {"TIE_SUBSTACK_CONFIG": cfg_two}},
        "tie-substack-solo": {"command": "python3", "args": ["/old/server.py"],
                              "env": {"SUBSTACK_PUBLICATION_URL": "https://solo.substack.com"}},
    }}, f)
rep = srv.migrate_import(dup_desktop, default_slug="tie")
check("only the unclashed entry is selected", [s["entry"] for s in rep["selected"]], ["tie-substack-solo"])
check("both claimants reported with the clash", sorted((u["entry"], "slug 'tie' is claimed by 2 entries" in u["reason"]) for u in rep["unresolvable"]),
      [("tie-substack", True), ("tie-substack-tie", True)])
check("neither claimant reached the registry", sorted(srv.load_clients()["clients"]), ["solo"])
sw = srv.switch_entries(dup_desktop, "/venv/bin/python3", "/home/server.py", verified=["tie", "solo"], legacy_server=None)
dcfg = json.load(open(dup_desktop))["mcpServers"]
check("a verified 'tie' removes neither claimant; solo goes", (sorted(sw["removed"]), sorted(sw["kept_legacy_entries"])),
      (["tie-substack-solo"], ["tie-substack-legacy", "tie-substack-tie"]))
check("the claimants keep their configs", (dcfg["tie-substack-legacy"]["env"], dcfg["tie-substack-tie"]["env"]),
      ({"TIE_SUBSTACK_CONFIG": cfg_one}, {"TIE_SUBSTACK_CONFIG": cfg_two}))
rep2 = srv.migrate_import(dup_desktop, default_slug="tie")
check("re-run after the switch still sees the clash (legacy + tie)", sorted(u["entry"] for u in rep2["unresolvable"]),
      ["tie-substack-legacy", "tie-substack-tie"])

print("remove-entry (0.9.0): only the installer's 0.5 entry goes, the kept 0.4 entries stay")
with open(dup_desktop) as f:
    _d = json.load(f)
_d["mcpServers"]["other-server"] = {"command": "node", "args": ["x.js"]}
with open(dup_desktop, "w") as f:
    json.dump(_d, f)
re_ = srv.remove_entry(dup_desktop)
dcfg = json.load(open(dup_desktop))["mcpServers"]
check("the 0.5 entry is removed; legacy entries and other servers stay",
      (re_["removed"], sorted(dcfg), re_["kept_legacy_entries"]),
      (True, ["other-server", "tie-substack-legacy", "tie-substack-tie"], ["tie-substack-legacy", "tie-substack-tie"]))
check("a per-run backup holds the config as it was", "tie-substack" in json.load(
    open(os.path.join(re_["backup"], "dup_desktop.json")))["mcpServers"], True)
check("a second run finds nothing to do", srv.remove_entry(dup_desktop)["removed"], False)
srv.rollback(dup_desktop, last=True)
check("rollback --last brings the entry back", "tie-substack" in json.load(open(dup_desktop))["mcpServers"], True)
plain04 = os.path.join(HOME, "plain04_desktop.json")
with open(plain04, "w") as f:
    json.dump({"mcpServers": {"tie-substack": {"command": "python3", "args": ["/old/server.py"]}}}, f)
check("a 0.4 default entry (no TIE_SUBSTACK_HOME) is not ours to remove",
      (srv.remove_entry(plain04)["removed"], sorted(json.load(open(plain04))["mcpServers"])), (False, ["tie-substack"]))
check("the CLI runs it", srv.cli(["remove-entry", "--desktop-config", dup_desktop]), 0)

# ---------------------------------------------------------------- notes
print("notes: text -> ProseMirror, schedule times")
doc = srv.note_body_json("Read it: https://alpha.substack.com/p/x.\n\n[the post](https://alpha.substack.com/p/y) is out")
check("one paragraph per line, the blank line kept", [p.get("content") is None for p in doc["content"]], [False, True, False])
first = doc["content"][0]["content"]
check("bare URL becomes a link mark, trailing period left outside",
      [(n["text"], (n.get("marks") or [{}])[0].get("attrs", {}).get("href")) for n in first],
      [("Read it: ", None), ("https://alpha.substack.com/p/x", "https://alpha.substack.com/p/x"), (".", None)])
check("[text](url) keeps the text and links it", doc["content"][2]["content"][0]["marks"][0]["attrs"]["href"],
      "https://alpha.substack.com/p/y")
check("doc attrs as the editor sends them", doc["attrs"], {"schemaVersion": "v1", "title": None})
raises("empty note refused", lambda: srv.note_body_json("  \n "), ValueError, ["empty"])
check("offset converted to the UTC string Substack stores", srv.note_trigger_at("2099-01-01T10:00:00+03:00"),
      "2099-01-01T07:00:00.000Z")
check("milliseconds kept", srv.note_trigger_at("2099-01-01T10:00:00.750+03:00"), "2099-01-01T07:00:00.750Z")
wiki = srv.note_body_json("See https://en.wikipedia.org/wiki/Foo_(bar), then (https://x.com/a).")["content"][0]["content"]
check("balanced parentheses stay in a bare URL, sentence punctuation does not",
      [n.get("marks", [{}])[0].get("attrs", {}).get("href") for n in wiki if n.get("marks")],
      ["https://en.wikipedia.org/wiki/Foo_(bar)", "https://x.com/a"])
md = srv.note_body_json("[article](https://en.wikipedia.org/wiki/Foo_(bar)) here")["content"][0]["content"]
check("balanced parentheses stay in a [text](url) destination",
      [(n["text"], n.get("marks", [{}])[0].get("attrs", {}).get("href")) for n in md],
      [("article", "https://en.wikipedia.org/wiki/Foo_(bar)"), (" here", None)])
raises("naive time refused", lambda: srv.note_trigger_at("2099-01-01T10:00:00"), ValueError, ["timezone"])
raises("past time refused", lambda: srv.note_trigger_at("2020-01-01T10:00:00+00:00"), ValueError, ["past"])

print("notes: tools through the dispatcher")
reset_home()
fake_sub.Api = FakeApi
srv.dispatch_tool("add_client", {"client": "alpha", "publication_url": "alpha.substack.com"})
BY_SID["sid-notes"] = (True, {"handle": "alpha-owner", "email": "a@x.com", "subdomains": ["alpha"], "primary": "alpha"})
srv.update_client("alpha", act_as="alpha-owner", cookie_source={"type": "user_data_dir", "browser": "chrome", "path": ded},
                  bound_at="2026-10-22T10:00:00+00:00")
srv.save_session("alpha", {"substack.sid": "sid-notes"}, {"handle": "alpha-owner"})
NA = {"client": "alpha", "expected_publication": "alpha.substack.com"}
FAKE_NOTES.reset(page=1)
c1 = payload(srv.dispatch_tool("create_note_draft", dict(NA, text="New post", link_url="https://alpha.substack.com/p/x")))
check("create_note_draft: the link card is made first, then the draft carries it",
      [(m, p) for m, p, _, _ in FAKE_NOTES.requests if m == "POST"], [("POST", "comment/attachment"), ("POST", "comment/draft")])
check("create_note_draft: no trigger_at sent, a plain draft", [j.get("trigger_at") for m, p, j, _ in FAKE_NOTES.requests
                                                               if p == "comment/draft"], [None])
check("create_note_draft: the stored draft is reported", (c1["status"], c1["scheduled_for"], len(c1["attachments"]), c1["client"]),
      ("draft", None, 1, "alpha"))
c2 = payload(srv.dispatch_tool("create_note_draft", dict(NA, text="Plain note")))
s2 = payload(srv.dispatch_tool("schedule_note", dict(NA, note_id=c2["note_id"], datetime_iso="2099-01-01T10:00:00+03:00")))
check("schedule_note finds a draft on the second page and schedules it",
      (s2["status"], s2["scheduled_for"], "warning" in s2), ("scheduled", "2099-01-01T07:00:00.000Z", False))
check("feed/drafts was paged by cursor", any(q.get("cursor") for m, p, _, q in FAKE_NOTES.requests if p == "feed/drafts"), True)
FAKE_NOTES.requests.clear()
s1 = payload(srv.dispatch_tool("schedule_note", dict(NA, note_id=c1["note_id"], datetime_iso="2099-01-02T10:00:00+03:00")))
patches = [j for m, p, j, _ in FAKE_NOTES.requests if m == "PATCH"]
check("schedule_note re-sends the draft's attachmentIds and its current body",
      (patches[0]["attachmentIds"], patches[0]["bodyJson"]["content"][0]["content"][0]["text"]), (["att-1"], "New post"))
check("schedule_note keeps the card", (len(s1["attachments"]), "warning" in s1), (1, False))
u1 = payload(srv.dispatch_tool("unschedule_note", dict(NA, note_id=c1["note_id"])))
check("unschedule_note: trigger_at null, card kept", (u1["status"], u1["scheduled_for"], len(u1["attachments"])), ("draft", None, 1))
e2 = payload(srv.dispatch_tool("update_note_draft", dict(NA, note_id=c2["note_id"], text="Edited note")))
check("update_note_draft keeps the schedule", (e2["status"], e2["scheduled_for"]), ("scheduled", "2099-01-01T07:00:00.000Z"))
check("update_note_draft sends the new text", [j["bodyJson"]["content"][0]["content"][0]["text"]
                                               for m, p, j, _ in FAKE_NOTES.requests if m == "PATCH"][-1], "Edited note")
FAKE_NOTES.drop_cards = True  # Substack loses the card despite the attachmentIds
lost = payload(srv.dispatch_tool("schedule_note", dict(NA, note_id=c1["note_id"], datetime_iso="2099-01-03T10:00:00+03:00")))
check("a card lost on the PATCH is reported, not hidden", "dropped the attachment" in lost.get("warning", ""), True)
both = {"warning": "Substack still reports a schedule (2099-01-01T07:00:00.000Z)"}
srv._patch_warnings(both, ["att-1"], True)
check("a lost card adds to an earlier warning instead of replacing it",
      ("still reports a schedule" in both["warning"], "dropped the attachment" in both["warning"]), (True, True))
FAKE_NOTES.publish_on_patch = True  # a near-term note publishes between the PATCH and the check
gone = payload(srv.dispatch_tool("schedule_note", dict(NA, note_id=c1["note_id"], datetime_iso="2099-01-04T10:00:00+03:00")))
check("a note that publishes right after the PATCH is reported from the PATCH answer, not as an error",
      (gone["note_id"], gone["scheduled_for"], "most likely published" in gone.get("warning", "")),
      (c1["note_id"], "2099-01-04T07:00:00.000Z", True))
FAKE_NOTES.publish_on_patch = False
c1 = payload(srv.dispatch_tool("create_note_draft", dict(NA, text="New post", link_url="https://alpha.substack.com/p/x")))
FAKE_NOTES.drop_cards = False
payload(srv.dispatch_tool("unschedule_note", dict(NA, note_id=c1["note_id"])))

print("notes: images")
_real_requests = sys.modules.get("requests")
fake_requests = types.ModuleType("requests")
fake_requests.RequestException = type("RequestException", (Exception,), {})
fake_requests.get = lambda url, timeout=None: (_ for _ in ()).throw(fake_requests.RequestException("connection refused"))
sys.modules["requests"] = fake_requests
raises("fetch_image: a failed download is a ValueError naming the URL",
       lambda: srv.fetch_image("https://res.cloudinary.com/x/card.png"), ValueError,
       ["could not be downloaded", "res.cloudinary.com/x/card.png", "connection refused"])
if _real_requests is not None:
    sys.modules["requests"] = _real_requests
else:
    del sys.modules["requests"]
srv.fetch_image =lambda url: (b"\x89PNG", "image/png") if url.endswith(".png") else (b"<html>", "text/html")
FAKE_NOTES.requests.clear()
ci = payload(srv.dispatch_tool("create_note_draft", dict(NA, text="With a picture", image_url="https://res.cloudinary.com/x/card.png")))
check("create_note_draft with image_url: upload, image attachment, then the draft",
      [(m, p, (j or {}).get("type")) for m, p, j, _ in FAKE_NOTES.requests if m == "POST"],
      [("POST", "image", None), ("POST", "comment/attachment", "image"), ("POST", "comment/draft", None)])
check("the upload is a data URI of the fetched bytes", [j["image"] for m, p, j, _ in FAKE_NOTES.requests if p == "image"],
      ["data:image/png;base64,iVBORw=="])
check("the draft carries the image", [a["type"] for a in ci["attachments"]], ["image"])
si = payload(srv.dispatch_tool("schedule_note", dict(NA, note_id=ci["note_id"], datetime_iso="2099-02-01T10:00:00+03:00")))
check("schedule_note keeps the image", ([a["type"] for a in si["attachments"]], "warning" in si), (["image"], False))
FAKE_NOTES.drop_cards = True
li = payload(srv.dispatch_tool("unschedule_note", dict(NA, note_id=ci["note_id"])))
check("a lost image is reported with image_url as the fix", ("a link card or an image" in li.get("warning", ""),
                                                            "image_url" in li.get("warning", "")), (True, True))
FAKE_NOTES.drop_cards = False
payload(srv.dispatch_tool("delete_note_draft", dict(NA, note_id=ci["note_id"])))
FAKE_NOTES.requests.clear()
for label, extra, msg in (("http:// image_url", {"image_url": "http://res.cloudinary.com/x/card.png"}, "https URL"),
                          ("non-image content", {"image_url": "https://example.com/page"}, "is not an image"),
                          ("link_url and image_url together", {"image_url": "https://res.cloudinary.com/x/card.png",
                                                               "link_url": "https://alpha.substack.com/p/x"}, "not both")):
    raises("create_note_draft refuses %s" % label,
           lambda x=extra: srv.dispatch_tool("create_note_draft", dict(NA, text="hi", **x)), ValueError, [msg])
check("no note was created by the refused calls", [p for m, p, _, _ in FAKE_NOTES.requests if p == "comment/draft"], [])
pi = payload(srv.dispatch_tool("publish_note", dict(NA, text="Now with a picture", image_url="https://res.cloudinary.com/x/card.png",
                                                    confirm_publish_now=True)))
check("publish_note posts comment/feed with the image", (pi["status"], [a["type"] for a in pi["attachments"]]),
      ("published", ["image"]))
lst = payload(srv.dispatch_tool("list_note_drafts", dict(NA)))
check("list_note_drafts: every page, scheduled first", [(n["note_id"], n["status"]) for n in lst["items"]],
      [(c2["note_id"], "scheduled"), (c1["note_id"], "draft")])
FAKE_NOTES.requests.clear()
raises("delete_note_draft: an id outside the drafts is refused",
       lambda: srv.dispatch_tool("delete_note_draft", dict(NA, note_id=999)), srv.ClientError, ["list_note_drafts"])
check("no DELETE sent for the unknown id", [m for m, _, _, _ in FAKE_NOTES.requests if m == "DELETE"], [])
d2 = payload(srv.dispatch_tool("delete_note_draft", dict(NA, note_id=c2["note_id"])))
check("delete_note_draft deletes and verifies", (d2["deleted"], c2["note_id"] in FAKE_NOTES.drafts), (True, False))
FAKE_NOTES.requests.clear()
raises("publish_note refused without confirm_publish_now",
       lambda: srv.dispatch_tool("publish_note", dict(NA, text="Now")), ValueError, ["confirm_publish_now"])
check("nothing posted without the confirmation", [p for m, p, _, _ in FAKE_NOTES.requests if m == "POST"], [])
pn = payload(srv.dispatch_tool("publish_note", dict(NA, text="Now", confirm_publish_now=True)))
check("publish_note posts to comment/feed and links the note", (pn["status"], pn["note_url"].startswith(
    "https://substack.com/@alpha-owner/note/c-")), ("published", True))

print("notes: the author guard")
_profile_reads = []
_get_user_profile = FakeApi.get_user_profile
FakeApi.get_user_profile = lambda self: (_profile_reads.append(self), _get_user_profile(self))[1]
srv.reset_api("alpha")  # the guard's pass is remembered per validated session
FakeApi.profile = {"handle": "alpha-owner", "publicationUsers": [
    {"is_primary": False, "publication": {"subdomain": "beta"}}, {"is_primary": True, "publication": {"subdomain": "alpha"}}]}
check("primary via publicationUsers[is_primary] accepted",
      payload(srv.dispatch_tool("list_note_drafts", dict(NA)))["client"], "alpha")
srv.dispatch_tool("list_note_drafts", dict(NA))
check("a second note tool on the same session does not ask Substack for the profile again", len(_profile_reads), 1)
srv.reset_api("alpha")
srv.dispatch_tool("list_note_drafts", dict(NA))
check("a new session checks the author again", len(_profile_reads), 2)
FakeApi.get_user_profile = _get_user_profile
for label, profile in (
        ("several publications, none marked primary", {"handle": "agency", "publicationUsers": [
            {"publication": {"subdomain": "alpha"}}, {"publication": {"subdomain": "beta"}}]}),
        ("another primary publication", {"handle": "agency", "primaryPublication": {"subdomain": "beta"}})):
    FakeApi.profile = profile
    srv.reset_api("alpha")
    FAKE_NOTES.requests.clear()
    raises("author guard: %s refused" % label,
           lambda: srv.dispatch_tool("create_note_draft", dict(NA, text="hi")), srv.ClientError, ["own profile"])
    check("author guard: %s, no Notes request made" % label, FAKE_NOTES.requests, [])
FakeApi.profile = {"handle": "alpha-owner", "primaryPublication": {"subdomain": "alpha"}}

print("notes: 401 mid-call")
_profile_chrome_cookies = fake_pcc.chrome_cookies  # the bind_client section reads PROFILE_COOKIES
fake_pcc.chrome_cookies = lambda url, browser=None, cookie_file=None: dict(SOURCE_COOKIES.get(cookie_file) or {})
DEAD_SIDS.add("sid-notes")
BY_SID["sid-notes-2"] = (True, {"handle": "alpha-owner", "email": "a@x.com", "subdomains": ["alpha"], "primary": "alpha"})
SOURCE_COOKIES[os.path.join(ded, "Default", "Cookies")] = {"substack.sid": "sid-notes-2"}
raises("401 during create_note_draft: not re-run, list_note_drafts named",
       lambda: srv.dispatch_tool("create_note_draft", dict(NA, text="hi")), srv.ClientError,
       ["HTTP 401", "create_note_draft", "check list_note_drafts"])
DEAD_SIDS.add("sid-notes-2")
BY_SID["sid-notes-3"] = (True, {"handle": "alpha-owner", "email": "a@x.com", "subdomains": ["alpha"], "primary": "alpha"})
SOURCE_COOKIES[os.path.join(ded, "Default", "Cookies")] = {"substack.sid": "sid-notes-3"}
out = payload(srv.dispatch_tool("list_note_drafts", dict(NA)))
check("401 during list_note_drafts: re-read and re-run", out["client"], "alpha")
check("note writers are not re-run (write + verify read: a retry after a done write would report a false failure)",
      [t for t in ("create_note_draft", "schedule_note", "unschedule_note", "update_note_draft", "delete_note_draft",
                   "publish_note") if t in srv.RETRY_SAFE_TOOLS], [])
DEAD_SIDS.clear()

print("reauthentication_required: a fresh login is named, the same cookie is not re-read")
SOURCE_COOKIES[os.path.join(ded, "Default", "Cookies")] = {"substack.sid": "sid-notes-4"}
FAKE_NOTES.reauth = True
raises("reauthentication_required during create_note_draft",
       lambda: srv.dispatch_tool("create_note_draft", dict(NA, text="hi")), srv.ClientError,
       ["fresh login", "sign out of Substack", "alpha-owner", "refresh_session", "create_note_draft",
        "check list_note_drafts for a partial result"])
FAKE_NOTES.reauth = False


def _reauth_schedule(self, draft, when):
    raise NotesHTTPError(403, REAUTH_MSG)


FakeApi.schedule_draft = _reauth_schedule
raises("reauthentication_required during schedule_draft",
       lambda: srv.dispatch_tool("schedule_draft", dict(NA, draft_id=1, datetime_iso="2099-01-01T10:00:00+00:00",
                                                        confirm_send_email=True)),
       srv.ClientError, ["fresh login", "sign out of Substack", "refresh_session", "schedule_draft",
                         "check list_drafts / get_draft for a partial result"])
FAKE_NOTES.reauth = True
try:
    srv.dispatch_tool("list_note_drafts", dict(NA))
    read_msg = ""
except srv.ClientError as e:
    read_msg = str(e)
check("reauthentication_required during a read: fresh login named, no partial-result hint",
      ("list_note_drafts again" in read_msg, "partial result" in read_msg), (True, False))
FAKE_NOTES.reauth = False
del FakeApi.schedule_draft
check("the stored session was kept (re-reading the same cookie cannot fix a reauth)",
      srv.load_session("alpha")["cookies"]["substack.sid"], "sid-notes-3")
fake_pcc.chrome_cookies = _profile_chrome_cookies

print("429 Too Many Requests: every method retried on the client's session, then named plainly")
check("get_api puts every client session through retry_rate_limits",
      FakeApi.instances[-1]._session in RATE_LIMITED_SESSIONS, True)
_saved = {m: sys.modules.get(m) for m in ("requests", "requests.adapters", "urllib3", "urllib3.util", "urllib3.util.retry")}
for m in _saved:
    sys.modules[m] = types.ModuleType(m)
sys.modules["requests.adapters"].HTTPAdapter = lambda max_retries: types.SimpleNamespace(max_retries=max_retries)


class _FakeRetry:
    """urllib3's Retry as far as retry_rate_limits uses it: keyword config, history, a backoff."""

    def __init__(self, **kw):
        self.kw, self.history, self.backoff_factor = kw, (), kw.get("backoff_factor", 0)


sys.modules["urllib3.util.retry"].Retry = _FakeRetry
probe_sess = _NotesSession("sid-x")
_real_retry_rate_limits(probe_sess)
for m, mod in _saved.items():
    if mod is None:
        del sys.modules[m]
    else:
        sys.modules[m] = mod
prefix, adapter = probe_sess.mounted
retry = adapter.max_retries
check("the https adapter retries 429 on every method, never after a read error",
      (prefix, retry.kw["status_forcelist"], retry.kw["allowed_methods"], retry.kw["read"], retry.kw["other"],
       retry.kw["respect_retry_after_header"], retry.kw["raise_on_status"]),
      ("https://", (429,), None, 0, 0, True, False))
pauses = []
for n in range(1, 5):
    retry.history = (None,) * n
    pauses.append(retry.get_backoff_time())
check("pauses before each retry: 2, 4, 8, 16 s (no immediate first retry)", pauses, [2, 4, 8, 16])
FAKE_NOTES.reset()
FAKE_NOTES.rate_limit = 99
raises("429 from a writer's handler: named, with the partial-result hint",
       lambda: srv.dispatch_tool("create_note_draft", dict(NA, text="hi")), srv.ClientError,
       ["rate-limited create_note_draft", "HTTP 429", "run create_note_draft again",
        "check list_note_drafts for a partial result"])
check("a 429 is not taken for an expired session", srv.load_session("alpha")["cookies"]["substack.sid"], "sid-notes-3")
FAKE_NOTES.rate_limit = 0


def _http_429():
    """requests.HTTPError shape, as probe_session's raise_for_status() raises it."""
    e = Exception("429 Client Error: Too Many Requests for url")
    e.response = types.SimpleNamespace(status_code=429)
    return e


srv.reset_api("alpha")
FAKE_NOTES.requests.clear()
srv.probe_session = lambda cookies: (_ for _ in ()).throw(_http_429())
try:
    srv.dispatch_tool("create_note_draft", dict(NA, text="hi"))
    probe_msg = ""
except srv.ClientError as e:
    probe_msg = str(e)
srv.probe_session = fake_probe
check("429 while building the client: named, no partial-result hint (nothing was written)",
      ("rate-limited create_note_draft" in probe_msg, "partial result" in probe_msg), (True, False))
check("nothing was sent to Notes", FAKE_NOTES.requests, [])
fake_pcc.chrome_cookies = lambda url, browser=None, cookie_file=None: dict(SOURCE_COOKIES.get(cookie_file) or {})
DEAD_SIDS.add("sid-notes-3")
BY_SID["sid-notes-5"] = (True, {"handle": "alpha-owner", "email": "a@x.com", "subdomains": ["alpha"], "primary": "alpha"})
SOURCE_COOKIES[os.path.join(ded, "Default", "Cookies")] = {"substack.sid": "sid-notes-5"}
FAKE_NOTES.rate_limit = 99  # the dead session answers 401 first; the re-run on the fresh one gets 429
FAKE_NOTES.requests.clear()
raises("401, re-read, then 429 on the re-run: still named as a rate limit",
       lambda: srv.dispatch_tool("list_note_drafts", dict(NA)), srv.ClientError,
       ["rate-limited list_note_drafts", "HTTP 429"])
FAKE_NOTES.rate_limit = 0
DEAD_SIDS.clear()
DEAD_SIDS.add("sid-notes-5")
SOURCE_COOKIES[os.path.join(ded, "Default", "Cookies")] = {"substack.sid": "sid-notes-6"}
srv.probe_session = lambda cookies: ((_ for _ in ()).throw(_http_429()) if cookies.get("substack.sid") == "sid-notes-6"
                                     else fake_probe(cookies))
raises("a writer's 401, then 429 on the session re-read: the partial-result hint is kept",
       lambda: srv.dispatch_tool("create_note_draft", dict(NA, text="hi")), srv.ClientError,
       ["rate-limited create_note_draft", "check list_note_drafts for a partial result"])
srv.probe_session = fake_probe
DEAD_SIDS.clear()
fake_pcc.chrome_cookies = _profile_chrome_cookies

print("session tools: a 429 on the session check is a rate limit, not an expired session")
srv.chrome_profile_cookie_files = lambda browser, root=None: [(n, os.path.join(std, n, "Cookies")) for n in ("Default", "Profile 1", "Profile 2")]
srv.local_state_profiles = lambda browser, root=None: [{"dir": "Default", "name": "Personal", "email": "me@x.com"}]
PROFILE_COOKIES[os.path.join(std, "Default", "Cookies")] = {"substack.sid": "sid-beta"}
srv.dispatch_tool("add_client", {"client": "beta", "publication_url": "beta.substack.com"})  # the registry was reset above
srv.save_session("beta", {"substack.sid": "sid-beta"})
srv.update_client("beta", act_as="beta-owner", cookie_source={"type": "profile", "browser": "chrome", "profile": "Default"})
_beta_before = (srv.load_session("beta")["cookies"], srv.client_record("beta")[1].get("act_as"))
srv.probe_session = lambda cookies: (_ for _ in ()).throw(_http_429())
row = payload(srv.dispatch_tool("clients_status", {"client": "beta"}))["clients"][0]
check("clients_status: rate_limited, not ready, fix says wait (not refresh_session)",
      (row["session"], row["ready"], "wait a minute" in row["fix"], "refresh_session" in row["fix"]),
      ("rate_limited", False, True, False))
raises("refresh_session: named as a rate limit", lambda: srv.dispatch_tool("refresh_session", {"client": "beta"}),
       srv.ClientError, ["rate-limited the session check", "HTTP 429", "run refresh_session again"])
raises("bind_client with a profile: named as a rate limit",
       lambda: srv.dispatch_tool("bind_client", {"client": "beta", "profile": "Personal"}), srv.ClientError,
       ["rate-limited the session check", "HTTP 429", "run bind_client again"])
check("nothing stored after the 429s", (srv.load_session("beta")["cookies"], srv.client_record("beta")[1].get("act_as")),
      _beta_before)
srv.probe_session = lambda cookies: (_ for _ in ()).throw(Exception("connection reset"))
row = payload(srv.dispatch_tool("clients_status", {"client": "beta"}))["clients"][0]
check("other probe errors unchanged: unreachable, fix refresh_session", (row["session"], row["fix"]),
      ("unreachable", "refresh_session"))
srv.probe_session = fake_probe
srv.chrome_profile_cookie_files, srv.local_state_profiles = _orig_files, _orig_ls
_reg = srv.load_clients()
_reg["clients"].pop("beta")
srv.save_clients(_reg)

print("schedule_note: warns about another note scheduled less than 10 minutes away")
FAKE_NOTES.reset()
na = payload(srv.dispatch_tool("create_note_draft", dict(NA, text="First")))
nb = payload(srv.dispatch_tool("create_note_draft", dict(NA, text="Second")))
sa = payload(srv.dispatch_tool("schedule_note", dict(NA, note_id=na["note_id"], datetime_iso="2099-03-01T10:00:00+00:00")))
check("the first note alone: no warning", "warning" in sa, False)
sb = payload(srv.dispatch_tool("schedule_note", dict(NA, note_id=nb["note_id"], datetime_iso="2099-03-01T03:00:00-07:00")))
check("the same minute in another offset: warned, naming the other note and still scheduled",
      ("less than 10 minutes" in sb.get("warning", ""), str(na["note_id"]) in sb.get("warning", ""), sb["status"]),
      (True, True, "scheduled"))
check("the warning says why it matters", "compete for the same readers" in sb.get("warning", ""), True)
sb = payload(srv.dispatch_tool("schedule_note", dict(NA, note_id=nb["note_id"], datetime_iso="2099-03-01T10:09:00+00:00")))
check("9 minutes apart: warned", "less than 10 minutes" in sb.get("warning", ""), True)
sb = payload(srv.dispatch_tool("schedule_note", dict(NA, note_id=nb["note_id"], datetime_iso="2099-03-01T10:10:00+00:00")))
check("exactly 10 minutes apart: no warning", "warning" in sb, False)
sb = payload(srv.dispatch_tool("schedule_note", dict(NA, note_id=nb["note_id"], datetime_iso="2099-03-01T10:15:00+00:00")))
check("15 minutes apart, and the note's own old time ignored: no warning", "warning" in sb, False)

print("stats: notes and posts as the author sees them (shapes probed on the test publication)")
FAKE_NOTES.reset()
FAKE_NOTES.note_stats[11] = {"lastUpdatedAt": "2026-10-29T18:44:43Z", "cards": [
    {"type": "note", "cardId": "note", "note": {}},
    {"type": "graphCard", "cardId": "impressions", "headers": [{"title": "Impressions", "value": 13}],
     "graphData": {"type": "line", "series": [
         {"label": "Typical", "isPrimary": False, "values": [{"timestamp": "2026-10-28T14:13:17Z", "value": 9}]},
         {"label": "This note", "isPrimary": True, "values": [{"timestamp": "2026-10-28T14:13:17Z", "value": 1},
                                                             {"timestamp": "2026-10-28T15:13:17Z", "value": 4}]}]}},
    {"type": "barList", "cardId": "surfaces", "items": [{"title": "Profile page", "value": 11, "percent": 0.846},
                                                        {"title": "Search", "value": 2, "percent": 0.154}]},
    {"type": "barList", "cardId": "audience", "items": [{"title": "Unconnected", "value": 3, "percent": 0.6},
                                                        {"title": "Followers", "value": 2, "percent": 0.4},
                                                        {"title": "Subscribers", "value": 0, "percent": 0}]},
    {"type": "listCard", "cardId": "interactions", "headers": [{"title": "Interactions", "value": 2}],
     "items": [{"title": "Restack", "value": 2}]}]}
FAKE_NOTES.note_stats[12] = {"lastUpdatedAt": None, "cards": [{"type": "note", "cardId": "note", "note": {}}]}
FAKE_NOTES.note_stats[13] = 500
ns = payload(srv.dispatch_tool("get_note_stats", dict(NA, note_ids=["c-11", 12, "13", 11])))
n11 = ns["notes"][0]
check("note_stats: total, the primary series only, surfaces, reached, interactions",
      (n11["note_id"], n11["impressions"], n11["impressions_series"], n11["surfaces"][0], n11["reached"], n11["interactions"]),
      (11, 13, [["2026-10-28T14:13:17Z", 1], ["2026-10-28T15:13:17Z", 4]],
       {"surface": "Profile page", "impressions": 11, "share": 0.846},
       {"unconnected": 3, "followers": 2, "subscribers": 0}, {"total": 2, "by_type": {"restack": 2}}))
check("note_stats: cards Substack left out are None",
      [ns["notes"][1][k] for k in ("impressions", "impressions_series", "surfaces", "reached", "interactions")], [None] * 5)
check("note_stats: a failing Note is listed under errors, the rest come back, a repeated id is read once",
      ([n["note_id"] for n in ns["notes"]], [e["note_id"] for e in ns["errors"]], "500" in ns["errors"][0]["error"]),
      ([11, 12], [13], True))
check("note_stats: read from substack.com", {u.split("/api/v1/")[0] for u in FAKE_NOTES.urls}, {"https://substack.com"})
for label, ids, msg in (("an empty list", [], "1-50"), ("51 ids", list(range(1, 52)), "1-50"),
                        ("a non-numeric id", ["abc"], "not a number")):
    raises("get_note_stats refuses %s" % label,
           lambda x=ids: srv.dispatch_tool("get_note_stats", dict(NA, note_ids=x)), ValueError, [msg])
FAKE_NOTES.note_stats[14] = FAKE_NOTES.note_stats[15] = 403
_profile_reads = []
FakeApi.get_user_profile = lambda self: (_profile_reads.append(self), _get_user_profile(self))[1]
ns = payload(srv.dispatch_tool("get_note_stats", dict(NA, note_ids=[14, 11, 15])))
check("note_stats: another account's Notes (403 on a valid session) are errors, the rest come back, the session asked once",
      ([n["note_id"] for n in ns["notes"]], [e["note_id"] for e in ns["errors"]], "not this account's Note" in ns["errors"][0]["error"],
       len(_profile_reads)), ([11], [14, 15], True, 1))
FakeApi.get_user_profile = lambda self: (_ for _ in ()).throw(NotesHTTPError(401, "Not authorized"))
raises("note_stats: a 403 while the session itself is refused fails the call (dispatch re-reads it)",
       lambda: srv.tool_get_note_stats({"note_ids": [14]}, srv._api_cache["alpha"]["api"],
                                       {"client": "alpha", "publication": "alpha.substack.com"}),
       NotesHTTPError, ["403"])
FakeApi.get_user_profile = lambda self: (_ for _ in ()).throw(NotesHTTPError(429, "Too Many Requests"))
raises("note_stats: a 429 on that profile check is a rate limit, not a session problem",
       lambda: srv.dispatch_tool("get_note_stats", dict(NA, note_ids=[14])), srv.ClientError, ["rate-limited"])
FakeApi.get_user_profile = _get_user_profile
FAKE_NOTES.note_stats[13] = 429
FAKE_NOTES.rate_limit = 0
raises("note_stats: a 429 fails the call (rate-limited), not one Note",
       lambda: srv.dispatch_tool("get_note_stats", dict(NA, note_ids=[11, 13])), srv.ClientError, ["rate-limited"])

FAKE_NOTES.posts = [
    {"id": 300 - i, "title": "Post %d" % i, "slug": "post-%d" % i, "post_date": "2026-10-%02dT13:00:00Z" % (26 - i),
     "audience": "everyone", "email_sent_at": None, "reaction_count": i, "comment_count": 1,
     "stats": {"views": 10 * i, "opens": i, "open_rate": 0.5, "clicks": 0, "click_through_rate": 0, "sent": 4,
               "delivered": 4, "signups": 1, "subscribes": 0, "shares": 2, "estimated_value": 0,
               "firstWeekDailyStats": [{"day_n": 0, "dt": "2026-10-26T00:00:00.000Z", "views": 1, "cumulative_views": 1,
                                        "signups": 0, "subscribes": 0, "comps": {"avg_cumulative_views": 7}}]}}
    for i in range(3)]
FAKE_NOTES.traffic[300] = {
    "referrers": [{"source": "substack app", "source_category": "Substack", "views": 1, "percent_of_total_views": "0.500000",
                   "signups": 0, "subscribes": 0}],
    "categories": [{"category_type": "Direct", "category_views": 1, "view_count": 1, "percent_of_total_views": 0.5}],
    "devices": [{"device_type": "Desktop Web", "views": 2, "percent_of_total_views": 1}]}
FAKE_NOTES.urls.clear()
lp = payload(srv.dispatch_tool("list_post_stats", dict(NA, limit=2, offset=1)))
check("list_post_stats: the page asked for, newest first, from the publication's API",
      ([p["post_id"] for p in lp["posts"]], lp["total"], FAKE_NOTES.requests[-1][3]["order_by"],
       {u.split("/api/v1/")[0] for u in FAKE_NOTES.urls}),
      ([299, 298], 3, "post_date", {"https://alpha.substack.com"}))
check("list_post_stats: post fields and stats, reactions and comments from the post",
      {k: lp["posts"][0][k] for k in ("title", "url", "views", "opens", "open_rate", "shares", "reactions", "comments")},
      {"title": "Post 1", "url": "https://alpha.substack.com/p/post-1", "views": 10, "opens": 1, "open_rate": 0.5,
       "shares": 2, "reactions": 1, "comments": 1})
check("list_post_stats: limit clamped to 50", payload(srv.dispatch_tool("list_post_stats", dict(NA, limit=500)))["limit"], 50)
gp = payload(srv.dispatch_tool("get_post_stats", dict(NA, post_id=300)))
check("get_post_stats: first week by day and traffic",
      (gp["views"], gp["first_week_daily"], gp["traffic"]),
      (0, [{"day": 0, "date": "2026-10-26", "views": 1, "cumulative_views": 1, "signups": 0, "subscribes": 0,
            "typical_cumulative_views": 7}],
       {"referrers": [{"source": "substack app", "category": "Substack", "views": 1, "share": 0.5, "signups": 0, "subscribes": 0}],
        "categories": [{"category": "Direct", "views": 1, "share": 0.5}],
        "devices": [{"device": "Desktop Web", "views": 2, "share": 1.0}]}))
check("get_post_stats: a post without traffic gets empty lists",
      payload(srv.dispatch_tool("get_post_stats", dict(NA, post_id=299)))["traffic"],
      {"referrers": [], "categories": [], "devices": []})
raises("get_post_stats: an unknown post is refused", lambda: srv.dispatch_tool("get_post_stats", dict(NA, post_id=1)),
       srv.ClientError, ["list_post_stats"])
raises("get_post_stats: a non-numeric id is refused", lambda: srv.dispatch_tool("get_post_stats", dict(NA, post_id="x")),
       ValueError, ["post id"])
FAKE_NOTES.reading_hours = {"sessions_by_utc_hour_of_week": [h % 24 for h in range(168)]}
FAKE_NOTES.urls.clear()
rt = payload(srv.dispatch_tool("get_reading_times", dict(NA)))
check("get_reading_times: 168 hourly counts as given, Monday 00:00 UTC first, from the publication's API",
      (len(rt["sessions_by_utc_hour_of_week"]), rt["sessions_by_utc_hour_of_week"][:3], rt["week_starts"], rt["total"],
       {u.split("/api/v1/")[0] for u in FAKE_NOTES.urls}),
      (168, [0, 1, 2], "Monday 00:00 UTC", 7 * sum(range(24)), {"https://alpha.substack.com"}))
FAKE_NOTES.reading_hours = {"sessions_by_utc_hour_of_week": [1] * 24}
raises("get_reading_times: another shape is refused, not reshaped", lambda: srv.dispatch_tool("get_reading_times", dict(NA)),
       srv.ClientError, ["unknown shape"])
FAKE_NOTES.reading_hours = {"sessions_by_utc_hour_of_week": [0] * 168}
FakeApi.profile = {"handle": "agency", "primaryPublication": {"subdomain": "beta"}}
for tool, extra in (("get_note_stats", {"note_ids": [11]}), ("list_post_stats", {}), ("get_post_stats", {"post_id": 300}),
                    ("get_reading_times", {})):
    srv.reset_api("alpha")
    FAKE_NOTES.requests.clear()
    raises("author guard: %s refused for another user's login" % tool,
           lambda t=tool, x=extra: srv.dispatch_tool(t, dict(NA, **x)), srv.ClientError, ["own profile"])
    check("author guard: %s made no stats request" % tool, FAKE_NOTES.requests, [])
FakeApi.profile = {"handle": "alpha-owner", "primaryPublication": {"subdomain": "alpha"}}
srv.reset_api("alpha")
check("stats tools are read-only, so re-run after a session re-read",
      {"get_note_stats", "list_post_stats", "get_post_stats", "get_reading_times"} <= srv.RETRY_SAFE_TOOLS, True)

print("dedicated window (cdp): reading through DevTools")


class FakeProc:
    def __init__(self, exited=False):
        self.pid, self.exited, self.killed = 4242, exited, False

    def poll(self):
        return 0 if self.exited else None

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


_cdp = {name: getattr(srv, name) for name in ("devtools_endpoint", "launch_dedicated", "wait_devtools", "cdp_call",
                                              "cdp_cookies", "open_dedicated_window")}
DEVTOOLS, LAUNCHED, CALLS = {}, [], []
srv.devtools_endpoint = lambda path: DEVTOOLS.get(path)
srv.launch_dedicated = lambda browser, path, headless=False, url="about:blank", devtools=True: (LAUNCHED.append((path, headless, url)), FakeProc())[1]
srv.wait_devtools = lambda path, proc: DEVTOOLS.get(("after-launch", path))
CDP_COOKIES = [{"name": "substack.sid", "value": "sid-w", "domain": ".substack.com"},
               {"name": "visit_id", "value": "v", "domain": "substack.com"},
               {"name": "pub", "value": "p", "domain": "tim.substack.com"}]
srv.cdp_call = lambda ws, method: (CALLS.append((ws, method)), {"cookies": CDP_COOKIES} if method == "Storage.getCookies" else {})[1]
srv.cdp_cookies = REAL_CDP_COOKIES
DEVTOOLS["/prof/open"] = "ws://open"
check("an open window is read in place, nothing launched or closed",
      (srv.cdp_cookies("/prof/open", "chrome"), LAUNCHED, CALLS), ({"substack.sid": "sid-w", "visit_id": "v"}, [], [("ws://open", "Storage.getCookies")]))
CALLS.clear()
DEVTOOLS[("after-launch", "/prof/closed")] = "ws://headless"
check("a closed profile is started headless, read, then closed",
      (sorted(srv.cdp_cookies("/prof/closed", "chrome")), LAUNCHED, [m for _, m in CALLS]),
      (["substack.sid", "visit_id"], [("/prof/closed", True, "about:blank")], ["Storage.getCookies", "Browser.close"]))
raises("a profile already open without DevTools -> close it and retry", lambda: srv.cdp_cookies("/prof/locked", "chrome"),
       srv.ClientError, ["close it and retry"])

reset_home()
srv.dispatch_tool("add_client", {"client": "win", "publication_url": "win.substack.com"})
WIN = {"client": "win", "expected_publication": "win.substack.com"}
ded = srv.dedicated_profile_dir("win")
DED_COOKIES, OPENED = {}, []
srv.cdp_cookies = lambda path, browser: dict(DED_COOKIES.get(path) or {})
srv.open_dedicated_window = lambda source, url="https://substack.com/sign-in": (OPENED.append(source["path"]), "opened")[1]
r = payload(srv.dispatch_tool("bind_client", {"client": "win", "dedicated": True}))
check("dedicated bind without a login: window opened, login_needed, nothing stored",
      (r["status"], OPENED, srv.client_record("win")[1]["cookie_source"], srv.load_session("win")),
      ("login_needed", [ded], None, {}))
os.makedirs(ded)
DED_COOKIES[ded] = {"substack.sid": "sid-anon"}
r = payload(srv.dispatch_tool("bind_client", {"client": "win", "dedicated": True}))
check("an anonymous session in the window is still login_needed", r["status"], "login_needed")
BY_SID["sid-win"] = (True, {"handle": "win-owner", "email": "w@x.com", "subdomains": ["win"], "primary": "win"})
DED_COOKIES[ded] = {"substack.sid": "sid-win"}
r = payload(srv.dispatch_tool("bind_client", {"client": "win", "dedicated": True}))
rec = srv.client_record("win")[1]
check("after the login: bound to the dedicated window, act_as pinned",
      (r["act_as"], r["cookie_source"], rec["cookie_source"]["type"], rec["cookie_source"]["path"]),
      ("win-owner", "dedicated window %s" % ded, "cdp", ded))
check("the session is stored with no Cookies DB path", (srv.load_session("win")["cookies"]["substack.sid"], srv.load_session("win")["source_path"]),
      ("sid-win", None))
raises("dedicated with another source refused", lambda: srv.dispatch_tool("bind_client", {"client": "win", "dedicated": True, "profile": "Default"}),
       ValueError, ["dedicated cannot be combined"])
check("cookie_source_file is None for cdp", srv.cookie_source_file(rec["cookie_source"]), None)

srv.save_session("win", {"substack.sid": "sid-win-stale"}, {"handle": "win-owner"})
srv.reset_api()
BY_SID["sid-win-2"] = (True, {"handle": "win-owner", "email": "w@x.com", "subdomains": ["win"], "primary": "win"})
DED_COOKIES[ded] = {"substack.sid": "sid-win-2"}
out = payload(srv.dispatch_tool("get_draft", dict(WIN, draft_id=1)))
check("an expired session is re-read from the dedicated window", (out["logged_in_as"], srv.load_session("win")["cookies"]["substack.sid"]),
      ("win-owner", "sid-win-2"))
DED_COOKIES[ded] = {}
raises("refresh with no login left in the window names the fix", lambda: srv.dispatch_tool("refresh_session", {"client": "win"}),
       srv.ClientError, ["open_client_browser", "refresh_session"])
OPENED.clear()
r = payload(srv.dispatch_tool("open_client_browser", {"client": "win"}))
check("open_client_browser shows the dedicated window", (r["window"], OPENED), ("opened", [ded]))
lb = payload(srv.dispatch_tool("list_browser_profiles", {}))
check("list_browser_profiles lists it as a DevTools-read dedicated browser",
      [(d["client"], d["read_through"]) for d in lb["dedicated_browsers"]], [("win", "devtools")])

srv.update_client("win", cookie_source=None, act_as=None)
DED_COOKIES[ded] = {}
r = payload(srv.dispatch_tool("bind_client", {"client": "win", "dedicated": True, "browser": "brave"}))
check("login_needed for another browser names it in the retry", 'browser: "brave"' in r["next"], True)
DED_COOKIES[ded] = {"substack.sid": "sid-win"}
srv.update_client("win", cookie_source={"type": "cdp", "browser": "brave", "path": ded})
r = payload(srv.dispatch_tool("bind_client", {"client": "win", "dedicated": True, "confirm_switch": True}))
check("a rebind without browser keeps the profile's own browser", srv.client_record("win")[1]["cookie_source"]["browser"], "brave")


class StuckProc(FakeProc):
    def __init__(self):
        super().__init__()
        self.waits = 0

    def wait(self, timeout=None):
        self.waits += 1
        if timeout is not None:
            raise srv.subprocess.TimeoutExpired("chrome", timeout)
        return 0


r = payload(srv.dispatch_tool("bind_client", {"client": "win"}))
check("on macOS too (0.9.0), bind_client without a source uses the dedicated window", r["cookie_source"], "dedicated window %s" % ded)

stuck = StuckProc()
_cdp["stop_browser"] = srv.stop_browser
srv.stop_browser(None, stuck)
check("a browser that ignores Browser.close is killed and reaped (macOS)", (stuck.killed, stuck.waits), (True, 2))

srv.on_windows = lambda: True
try:
    r = payload(srv.dispatch_tool("bind_client", {"client": "win"}))
    check("on Windows, bind_client without a source uses the dedicated window", r["cookie_source"], "dedicated window %s" % ded)
    progs = tempfile.mkdtemp()
    os.makedirs(os.path.join(progs, "Google", "Chrome", "Application"))
    open(os.path.join(progs, "Google", "Chrome", "Application", "chrome.exe"), "w").close()
    _env = {k: os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")}
    for k in _env:
        os.environ.pop(k, None)
    os.environ["LOCALAPPDATA"] = progs
    check("chrome_executable finds a per-user Chrome on Windows", srv.chrome_executable("chrome"),
          os.path.join(progs, "Google", "Chrome", "Application", "chrome.exe"))
    raises("no Brave on Windows -> named", lambda: srv.chrome_executable("brave"), srv.ClientError, ["not installed"])
    os.makedirs(os.path.join(progs, "Chromium", "Application"))
    open(os.path.join(progs, "Chromium", "Application", "chrome.exe"), "w").close()
    check("chrome_executable finds a per-user Chromium on Windows", srv.chrome_executable("chromium"),
          os.path.join(progs, "Chromium", "Application", "chrome.exe"))
    check("browser data dirs come from LOCALAPPDATA on Windows",
          (srv.browser_data_dir("chrome"), srv.browser_data_dir("brave"), srv.browser_data_dir("firefox")),
          (os.path.join(progs, "Google", "Chrome", "User Data"), os.path.join(progs, "BraveSoftware", "Brave-Browser", "User Data"), None))
    check("local_state_path follows them", srv.local_state_path("chrome"), os.path.join(progs, "Google", "Chrome", "User Data", "Local State"))
    raises("a browser profile cannot be bound on Windows -> dedicated named",
           lambda: srv.dispatch_tool("bind_client", {"client": "win", "profile": "Default"}), srv.ClientError, ["dedicated: true"])
    raises("nor a cookie file", lambda: srv.dispatch_tool("bind_client", {"client": "win", "cookie_file": "C:/x/Cookies"}),
           srv.ClientError, ["dedicated: true"])
    for k, v in _env.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
finally:
    srv.on_windows = lambda: False
for name, fn in _cdp.items():
    setattr(srv, name, fn)

print("dedicated window (cdp): the endpoint is this profile's, one read at a time")
prof = tempfile.mkdtemp()
with open(os.path.join(prof, "DevToolsActivePort"), "w") as f:
    f.write("9333\n/devtools/browser/mine\n")


class FakeVersion:
    def __init__(self, ws):
        self.ws = ws

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps({"webSocketDebuggerUrl": self.ws}).encode()


_urlopen = srv.urllib.request.urlopen
srv.urllib.request.urlopen = lambda url, timeout=None: FakeVersion("ws://127.0.0.1:9333/devtools/browser/mine")
check("the profile's own browser on its port is used", srv.devtools_endpoint(prof), "ws://127.0.0.1:9333/devtools/browser/mine")
srv.urllib.request.urlopen = lambda url, timeout=None: FakeVersion("ws://127.0.0.1:9333/devtools/browser/someone-else")
check("a port reused by another browser is not taken for it", srv.devtools_endpoint(prof), None)
srv.urllib.request.urlopen = _urlopen
check("one lock per profile, whatever the spelling", srv.profile_lock(prof) is srv.profile_lock(prof + os.sep), True)

import threading  # noqa: E402
ACTIVE, PEAK = [0], [0]


def _slow_call(ws, method):
    ACTIVE[0] += 1
    PEAK[0] = max(PEAK[0], ACTIVE[0])
    srv.time.sleep(0.05)
    ACTIVE[0] -= 1
    return {"cookies": []}


srv.devtools_endpoint = lambda path: None
srv.launch_dedicated = lambda browser, path, headless=False, url="about:blank", devtools=True: FakeProc()
srv.wait_devtools = lambda path, proc: "ws://headless"
srv.cdp_call = _slow_call
srv.cdp_cookies = REAL_CDP_COOKIES
threads = [threading.Thread(target=srv.cdp_cookies, args=(prof, "chrome")) for _ in range(3)]
[t.start() for t in threads]
[t.join() for t in threads]
check("concurrent reads of one profile run one at a time", PEAK[0], 1)

LOGIN, CLAIMED = [], []
_wait_claimed = srv.wait_claimed
srv.wait_claimed = lambda path, proc: (CLAIMED.append(path), True)[1]
srv.devtools_endpoint = lambda path: None
srv.launch_dedicated = lambda browser, path, headless=False, url="about:blank", devtools=True: (
    LOGIN.append((path, headless, url, devtools)), FakeProc())[1]
check("the login window opens without DevTools, on the sign-in page",
      (srv.open_dedicated_window({"path": prof}), LOGIN), ("opened", [(prof, False, "https://substack.com/sign-in", False)]))
check("the profile is held until the window has claimed it", CLAIMED, [prof])
srv.wait_claimed = lambda path, proc: False
LOGIN.clear()
raises("a window that fails to start is reported", lambda: srv.open_dedicated_window({"path": prof}),
       srv.ClientError, ["did not start"])
srv.wait_claimed = _wait_claimed
marker = os.path.join(prof, "SingletonLock")
open(marker, "w").close()
t0 = srv.time.time()
check("a claimed profile releases at once", (srv.wait_claimed(prof, FakeProc()), srv.time.time() - t0 < 0.2), (True, True))
check("a launch that handed off to the open window is fine", srv.wait_claimed(prof, FakeProc(exited=True)), True)
os.remove(marker)
check("a launch that exited without claiming the profile failed", srv.wait_claimed(prof, FakeProc(exited=True)), False)
srv.devtools_endpoint = lambda path: "ws://window"
LOGIN.clear()
check("a window already open with DevTools is left as it is",
      (srv.open_dedicated_window({"path": prof}), LOGIN), ("already open", []))

POPEN = []
_popen, _exe = srv.subprocess.Popen, srv.chrome_executable
srv.subprocess.Popen = lambda cmd, **kw: POPEN.append(cmd)
srv.chrome_executable = lambda browser: "chrome"
try:
    _cdp["launch_dedicated"]("chrome", prof, url="https://substack.com/sign-in", devtools=False)
    _cdp["launch_dedicated"]("chrome", prof, headless=True)
finally:
    srv.subprocess.Popen, srv.chrome_executable = _popen, _exe
check("no debugging port on the login window", "--remote-debugging-port=0" in POPEN[0], False)
check("the headless read keeps its debugging port", ("--remote-debugging-port=0" in POPEN[1], "--headless=new" in POPEN[1]), (True, True))

srv.cdp_cookies = lambda path, browser: {"substack.sid": "sid-ud"}
srv.on_windows = lambda: True
try:
    check("on Windows a user_data_dir source is read through DevTools",
          srv.read_source_cookies({"type": "user_data_dir", "browser": "chrome", "path": prof}), ({"substack.sid": "sid-ud"}, None))
finally:
    srv.on_windows = lambda: False
for name, fn in _cdp.items():
    setattr(srv, name, fn)

print("registration & version")
check("server version", srv.SERVER_VERSION, "0.9.0")
sys.modules["pycookiecheat"] = None  # an extension without it (Windows): import fails
raises("without pycookiecheat the fix is the dedicated window", srv.import_pycookiecheat, RuntimeError, ["dedicated: true"])
sys.modules["pycookiecheat"] = fake_pcc
check("old tool names gone", any(n in srv.TOOL_HANDLERS for n in ("substack_status", "refresh_cookie", "list_profiles")), False)
check("publication tools require client + expected_publication",
      all(set(t["inputSchema"]["required"]) >= {"client", "expected_publication"}
          for t in srv.TOOLS if t["scope"] == "publication"), True)

print("\n%d failure(s)" % len(fails))
for f in fails:
    print(" -", f)
shutil.rmtree(HOME, ignore_errors=True)
sys.exit(1 if fails else 0)
