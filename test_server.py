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


class FakeApi:
    instances = []

    def __init__(self, cookies_string="", publication_url=""):
        self.cookies_string, self.publication_url = cookies_string, publication_url
        self.calls = []
        FakeApi.instances.append(self)

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
check("registry file is 0600", oct(os.stat(srv.clients_path()).st_mode & 0o777), "0o600")
raises("unknown client lists configured ones", lambda: srv.dispatch_tool("get_draft", {
    "client": "gamma", "expected_publication": "gamma.substack.com", "draft_id": 1}), srv.ClientError, ["alpha", "beta"])
raises("client required on publication tools", lambda: srv.dispatch_tool("get_draft", {
    "expected_publication": "alpha.substack.com", "draft_id": 1}), srv.ClientError, ["`client` is required"])
raises("expected_publication required", lambda: srv.dispatch_tool("get_draft", {"client": "alpha", "draft_id": 1}),
       srv.ClientError, ["expected_publication"])

# bind alpha and beta directly (sessions + pins), as bind_client would
srv.update_client("alpha", act_as="alpha-owner", cookie_source={"type": "user_data_dir", "browser": "chrome", "path": "/tmp/x/alpha"}, bound_at="2026-10-06T10:00:00+00:00")
srv.save_session("alpha", {"substack.sid": "sid-alpha"}, {"handle": "alpha-owner"})
srv.update_client("beta", act_as="beta-owner", cookie_source={"type": "user_data_dir", "browser": "chrome", "path": "/tmp/x/beta"}, bound_at="2026-10-06T10:00:00+00:00")
srv.save_session("beta", {"substack.sid": "sid-beta"}, {"handle": "beta-owner"})
check("session file is 0600", oct(os.stat(srv.session_path("alpha")).st_mode & 0o777), "0o600")

# ---------------------------------------------------------------- expected_publication, three cases × read/write/cleanup
print("expected_publication check")
for tool, extra in (("get_draft", {"draft_id": 1}), ("create_draft", {"title": "T", "body_markdown": "b", "slug": "t"}),
                    ("delete_draft", {"draft_id": 1})):
    FakeApi.instances.clear()
    raises("%s: expected != registry refused before any network" % tool,
           lambda t=tool, x=extra: srv.dispatch_tool(t, dict(x, client="alpha", expected_publication="beta.substack.com")),
           srv.PublicationMismatch, ["expected_publication='beta.substack.com'", "registry='alpha.substack.com'"])
    check("%s: no Api was built" % tool, len(FakeApi.instances), 0)
# registry != live: beta's record points at alpha's publication, but beta's session reaches beta only
srv.update_client("beta", publication_url="https://alpha.substack.com")
for tool, extra in (("get_draft", {"draft_id": 1}), ("create_draft", {"title": "T", "body_markdown": "b", "slug": "t"}),
                    ("delete_draft", {"draft_id": 1})):
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
FakeApi.instances.clear()
out = payload(srv.dispatch_tool("get_draft", {"client": "alpha", "expected_publication": "alpha.substack.com", "draft_id": 1}))
check("401 mid-call: session re-read from the bound source, the read re-run", out["logged_in_as"], "alpha-owner")
check("the re-run used an Api on the fresh session", FakeApi.instances[-1].cookies_string, "substack.sid=sid-alpha-2")
check("the fresh session was stored", srv.load_session("alpha")["cookies"]["substack.sid"], "sid-alpha-2")
check("two probes: the dead session, then the fresh one", NET_CALLS.count("probe"), 2)
# a multi-step writer is refreshed but not re-run; the error names the fix
DEAD_SIDS.add("sid-alpha-2")
BY_SID["sid-alpha-2"] = (False, {"reason": "session_invalid", "status": 401})
BY_SID["sid-alpha-3"] = (True, {"handle": "alpha-owner", "email": "a@x.com", "subdomains": ["alpha"], "primary": "alpha"})
SOURCE_COOKIES[os.path.join(ded, "Default", "Cookies")] = {"substack.sid": "sid-alpha-3"}
raises("403 during create_draft: refreshed, not re-run, fix named",
       lambda: srv.dispatch_tool("create_draft", {"client": "alpha", "expected_publication": "alpha.substack.com",
                                                  "title": "T", "body_markdown": "b", "slug": "t"}),
       srv.ClientError, ["HTTP 403", "create_draft", "run it again"])
check("the session was refreshed anyway", srv.load_session("alpha")["cookies"]["substack.sid"], "sid-alpha-3")
# the refresh itself failing gives the usual actionable error, and the handler is not re-run
DEAD_SIDS.add("sid-alpha-3")
BY_SID["sid-alpha-3"] = (False, {"reason": "session_invalid", "status": 401})
FakeApi.instances.clear()
raises("401 and the bound source still dead -> refresh_session named",
       lambda: srv.dispatch_tool("get_draft", {"client": "alpha", "expected_publication": "alpha.substack.com", "draft_id": 1}),
       srv.ClientError, ["refresh_session"])
check("no Api built on a failed refresh", len(FakeApi.instances), 0)
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
b = payload(srv.dispatch_tool("bind_client", {"client": "alpha"}))
check("scan found the one login reaching alpha", (b["cookie_source"], b["act_as"]), ("chrome profile 'Profile 2'", "alpha-owner"))
check("scan result lists every profile", len(b["profiles_scanned"]), 3)
check("session persisted", srv.load_session("alpha")["cookies"]["substack.sid"], "sid-alpha")
PROFILE_COOKIES[os.path.join(std, "Profile 1", "Cookies")] = {"substack.sid": "sid-intruder"}
raises("bind_client on an unknown client refused", lambda: srv.dispatch_tool("bind_client", {"client": "gamma-nope"}),
       srv.ClientError, ["unknown client"])
srv.update_client("alpha", act_as=None, cookie_source=None)
raises("two logins reach the publication -> refuse to guess", lambda: srv.dispatch_tool("bind_client", {"client": "alpha"}),
       srv.ClientError, ["2 Substack logins", "alpha-owner", "intruder"])
b = payload(srv.dispatch_tool("bind_client", {"client": "alpha", "profile": "alpha work"}))
check("explicit profile binds and pins", (b["cookie_source"], b["act_as"]), ("chrome profile 'Profile 2'", "alpha-owner"))
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
                                      "cookie_file": os.path.expanduser("~/Library/Application Support/Google/Chrome/Profile 2/Cookies"),
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

print("registration & version")
check("server version", srv.SERVER_VERSION, "0.5.0")
check("old tool names gone", any(n in srv.TOOL_HANDLERS for n in ("substack_status", "refresh_cookie", "list_profiles")), False)
check("publication tools require client + expected_publication",
      all(set(t["inputSchema"]["required"]) >= {"client", "expected_publication"}
          for t in srv.TOOLS if t["scope"] == "publication"), True)

print("\n%d failure(s)" % len(fails))
for f in fails:
    print(" -", f)
shutil.rmtree(HOME, ignore_errors=True)
sys.exit(1 if fails else 0)
