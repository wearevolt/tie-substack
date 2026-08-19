"""Regression checks for the v0.1.1 fixes. Offline — no live Substack, no cookie.

Run:  python3 test_server.py
"""
import importlib.util, os, sys

os.environ["TIE_SUBSTACK_CONFIG"] = "/tmp/tie-substack-test-config.json"
# The temp config survives across runs — an aborted run must not poison the next.
try:
    os.remove(os.environ["TIE_SUBSTACK_CONFIG"])
except FileNotFoundError:
    pass
spec = importlib.util.spec_from_file_location(
    "srv", os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
)
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

fails = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label + "  → %r" % (got,))
    if not ok:
        fails.append("%s: got %r want %r" % (label, got, want))


print("Bug 1 — publication_url normalization")
for raw, want in [
    ("iviq.substack.com", "https://iviq.substack.com"),
    ("https://iviq.substack.com/", "https://iviq.substack.com"),
    ("http://iviq.substack.com", "https://iviq.substack.com"),
    ("  thrivinginengineering.substack.com  ", "https://thrivinginengineering.substack.com"),
    ("", ""),
]:
    check("normalize(%r)" % raw, srv.normalize_publication_url(raw), want)

print("Bug 1 — subdomain extraction (drives the publication match)")
for raw, want in [
    ("iviq.substack.com", "iviq"),           # the exact value that broke auth
    ("https://iviq.substack.com", "iviq"),
    ("https://tie.example.com", None),       # custom domain → explicit error path
]:
    os.environ["SUBSTACK_PUBLICATION_URL"] = raw
    check("subdomain(%r)" % raw, srv.configured_subdomain(), want)

print("Bug 1 — errors name the right cause, in the right order")
os.environ["SUBSTACK_PUBLICATION_URL"] = "https://tie.example.com"
os.environ.pop("SUBSTACK_SESSION_TOKEN", None)
try:
    srv.get_api(fresh=True)
    check("no-cookie raises", False, True)
except RuntimeError as e:
    check("missing cookie is reported first", "substack.sid" in str(e), True)

# With a cookie present, an unusable URL must fail BEFORE any network call.
os.environ["SUBSTACK_SESSION_TOKEN"] = "dummy-not-a-real-session"
try:
    srv.get_api(fresh=True)
    check("custom domain raises", False, True)
except RuntimeError as e:
    check("custom-domain error names the required form", "substack.com" in str(e), True)
    check("custom-domain error is not the old TypeError", "NoneType" in str(e), False)

print("Bug 2 — /drafts payload unwrapping")
real_payload = {"posts": [{"id": 208981733, "publication_id": 10255107,
                           "is_published": False, "draft_title": "", "title": None,
                           "slug": None}]}
check("unwrap object form", len(srv.unwrap_items(real_payload, "posts")), 1)
check("unwrap bare list", len(srv.unwrap_items([{"id": 1}], "posts")), 1)
check("unwrap unknown dict", srv.unwrap_items({"weird": 1}, "posts"), [])
check("unwrap None", srv.unwrap_items(None, "posts"), [])
# The old code iterated the dict and hit .get on the key string "posts":
try:
    [srv.draft_summary(d) for d in real_payload]
    check("draft_summary survives raw-string entries", True, True)
except AttributeError as e:
    check("draft_summary survives raw-string entries (%s)" % e, False, True)

print("Bug 2 — brand-new draft summary is readable")
os.environ["SUBSTACK_PUBLICATION_URL"] = "https://iviq.substack.com"
s = srv.draft_summary(real_payload["posts"][0])
check("title falls back", s["title"], "(untitled draft)")
check("is_published from field", s["is_published"], False)
check("post_url is None without slug", s["post_url"], None)
check("explains missing slug", "set_slug" in s.get("post_url_note", ""), True)
check("editor_url built", s["editor_url"], "https://iviq.substack.com/publish/post/208981733")
s2 = srv.draft_summary({"id": 7, "draft_slug": "my-post", "draft_title": "T",
                        "is_published": False})
check("post_url from draft_slug", s2["post_url"], "https://iviq.substack.com/p/my-post")


print("v0.2.0 — settings validation")
caps_free = {"paid_enabled": False, "payments_state": "disabled", "existing_tags": [{"name": "debugging"}]}
caps_paid = {"paid_enabled": True, "payments_state": "enabled", "existing_tags": []}
def expect_raises(label, fn):
    try:
        fn(); check(label, "no error", "ValueError")
    except ValueError as e:
        check(label, True, True)
srv.validate_settings("everyone", "everyone", caps_free)
check("everyone/everyone allowed on free pub", True, True)
expect_raises("only_paid audience rejected on free pub",
              lambda: srv.validate_settings("only_paid", "everyone", caps_free))
expect_raises("only_paid comments rejected on free pub",
              lambda: srv.validate_settings("everyone", "only_paid", caps_free))
expect_raises("bogus audience rejected",
              lambda: srv.validate_settings("subscribers", "everyone", caps_free))
srv.validate_settings("only_paid", "only_paid", caps_paid)
check("paid values allowed when payments enabled", True, True)
check("'none' disables comments and is valid", "none" in srv.COMMENT_VALUES, True)

print("v0.2.0 — tag resolution (new vs existing, normalized)")
plan = srv.resolve_tags(None, ["Debugging", "MCP tooling", "debugging", "  ", "a/b!"], caps_free)
check("normalizes + dedupes", plan["normalized"], ["debugging", "mcp-tooling", "a-b"])
check("existing detected", plan["existing"], ["debugging"])
check("new detected", plan["new"], ["mcp-tooling", "a-b"])

print("v0.2.0 — schedule/settings read from server state")
sched = {"id": 9, "draft_title": "T", "is_published": False, "should_send_email": True,
         "audience": "everyone", "write_comment_permissions": "none",
         "postSchedules": [{"trigger_at": "2026-08-03T13:00:00.000Z"}]}
s9 = srv.draft_summary(sched)
check("scheduled_for from postSchedules", s9["scheduled_for"], "2026-08-03T13:00:00.000Z")
check("audience surfaced", s9["audience"], "everyone")
check("comments surfaced", s9["comment_permissions"], "none")
check("send_email surfaced", s9["send_email"], True)
# Real payloads have no postTags key at all, so the summary must not claim tags —
# they come from the association endpoint (see read_post_tags below).
check("summary claims no tags of its own", "tags" in s9, False)
# The list payload has no postSchedules key at all — absence must not read as "not scheduled".
s10 = srv.draft_summary({"id": 9, "draft_title": "T", "is_published": False})
check("list payload flags unknown schedule", "scheduled_for" in s10, False)
check("list payload explains why", "scheduled_for_note" in s10, True)

print("v0.2.0 — irreversible-email gate")
check("schedule_draft advertises confirm_send_email",
      any(t["name"] == "schedule_draft" and "confirm_send_email" in t["inputSchema"]["properties"]
          for t in srv.TOOLS), True)
check("publish_draft advertises confirm_send_email",
      any(t["name"] == "publish_draft" and "confirm_send_email" in t["inputSchema"]["properties"]
          for t in srv.TOOLS), True)
check("new tools registered",
      all(n in srv.TOOL_HANDLERS for n in
          ("get_publication_settings", "update_post_settings", "apply_tags")), True)


print("v0.2.1 — narrower list projection is not reported as empty")
# Verified live: the list payload carries NEITHER subtitle nor draft_subtitle.
list_row = {"id": 1, "draft_title": "T", "is_published": False, "audience": "everyone"}
r = srv.draft_summary(list_row)
check("no null subtitle from list payload", "subtitle" in r, False)
check("explains absent subtitle", "get_draft" in r.get("subtitle_note", ""), True)
single = {"id": 1, "draft_title": "T", "draft_subtitle": "A first post", "is_published": False}
check("subtitle read when present", srv.draft_summary(single)["subtitle"], "A first post")
check("empty subtitle stays reported", "subtitle" in srv.draft_summary(
    {"id": 1, "draft_title": "T", "subtitle": None}), True)

print("v0.2.1 — attached tags read from the association endpoint")
class FakeApi:  # postTags is absent from real payloads, so this is the only truth
    def __init__(self, rows): self.rows = rows; self.calls = []
    def call(self, endpoint, method, **kw): self.calls.append((endpoint, method)); return self.rows
    def get_publication_post_tags(self):
        return [{"id": "65ac113b-uuid", "name": "debugging"},
                {"id": "69c0ed7c-uuid", "name": "tooling"}]
fake = FakeApi([{"post_id": 9, "post_tag_id": "65ac113b-uuid"},
                {"post_id": 9, "post_tag_id": "69c0ed7c-uuid"}])
got = srv.read_post_tags(fake, 9)
check("uuid ids mapped to names", got["names"], ["debugging", "tooling"])
check("hits the association endpoint", fake.calls, [("post/9/tag", "GET")])
check("unknown id degrades visibly",
      srv.read_post_tags(FakeApi([{"post_tag_id": "ghost-uuid"}]), 9)["names"], ["tag:ghost-uuid"])
check("no attachments -> empty, not echo", srv.read_post_tags(FakeApi([]), 9)["names"], [])
check("odd shape flagged", "note" in srv.read_post_tags(FakeApi({"x": 1}), 9), True)

print("v0.3.0 — per-client cookie_file resolution")
import json as _json
_cfg_path = os.environ["TIE_SUBSTACK_CONFIG"]
def _write_cfg(d):
    with open(_cfg_path, "w") as f:
        _json.dump(d, f)
_write_cfg({})
check("no arg, no config -> default profile", srv.resolve_cookie_file(None), None)
check("explicit arg wins", srv.resolve_cookie_file("/tmp/x/Cookies"), "/tmp/x/Cookies")
check("~ expands", srv.resolve_cookie_file("~/x/Cookies"),
      os.path.expanduser("~/x/Cookies"))
_write_cfg({"cookie_file": "~/TIE-Browsers/acme/Default/Cookies"})
check("config fallback used when no arg", srv.resolve_cookie_file(None),
      os.path.expanduser("~/TIE-Browsers/acme/Default/Cookies"))
check("arg still wins over config", srv.resolve_cookie_file("/tmp/y/Cookies"),
      "/tmp/y/Cookies")
_write_cfg({})
check("refresh_cookie schema exposes cookie_file",
      "cookie_file" in [t for t in srv.TOOLS if t["name"] == "refresh_cookie"
                        ][0]["inputSchema"]["properties"], True)
try:
    srv.tool_refresh_cookie({"browser": "firefox", "cookie_file": "/tmp/x/Cookies"})
    check("firefox + cookie_file rejected", "no error", "RuntimeError")
except RuntimeError:
    check("firefox + cookie_file rejected", "RuntimeError", "RuntimeError")
except Exception as e:  # pycookiecheat missing would raise before the guard — order matters
    check("firefox + cookie_file rejected", type(e).__name__, "RuntimeError")

print("v0.3.0 — publication_accessible: one predicate for scan, get_api and status")
check("subdomain member -> accessible",
      srv.publication_accessible({"subdomains": ["acme"], "primary": None}, "acme"), True)
check("primary-only profile -> accessible",
      srv.publication_accessible({"subdomains": [], "primary": "acme"}, "acme"), True)
check("primary case-insensitive",
      srv.publication_accessible({"subdomains": [], "primary": "Acme"}, "acme"), True)
check("no access -> rejected",
      srv.publication_accessible({"subdomains": ["other"], "primary": "else"}, "acme"), False)
check("no target -> any valid session",
      srv.publication_accessible({"subdomains": [], "primary": None}, None), True)
check("error listing merges primary",
      srv.accessible_publications({"subdomains": ["beta"], "primary": "Acme"}),
      ["acme", "beta"])

print("v0.3.0 — profile scan: pick the profile whose session reaches the publication")
import tempfile, types
tmp = tempfile.mkdtemp()
for prof in ("Default", "Profile 1", "Profile 2", "System Profile"):
    os.makedirs(os.path.join(tmp, prof), exist_ok=True)
for prof in ("Default", "Profile 1", "Profile 2"):
    open(os.path.join(tmp, prof, "Cookies"), "w").close()
check("enumerates Default + Profile N with a Cookies DB",
      [n for n, _ in srv.chrome_profile_cookie_files("chrome", root=tmp)],
      ["Default", "Profile 1", "Profile 2"])
check("unknown browser -> empty", srv.chrome_profile_cookie_files("firefox"), [])

fake_pcc = types.ModuleType("pycookiecheat")
fake_pcc.BrowserType = lambda b: b
BY_FILE = {
    None: {"substack.sid": "sid-personal"},                                  # default: personal acct
    os.path.join(tmp, "Profile 1", "Cookies"): {"other": "x"},               # not logged in
    os.path.join(tmp, "Profile 2", "Cookies"): {"substack.sid": "sid-client"},  # the client acct
}
fake_pcc.chrome_cookies = lambda url, browser=None, cookie_file=None: dict(
    BY_FILE.get(cookie_file) or {})
sys.modules["pycookiecheat"] = fake_pcc

def _fake_probe(cookies):
    if cookies.get("substack.sid") == "sid-client":
        # primary-only profile: acme is the primaryPublication but absent from
        # publicationUsers — must still be selectable (shared predicate).
        return True, {"handle": "client", "primary": "acme", "subdomains": []}
    return True, {"handle": "me", "primary": "personal", "subdomains": ["personal"]}

class _FakeApi:
    def get_user_profile(self):
        return {"handle": "client"}

_orig = (srv.probe_session, srv.chrome_profile_cookie_files, srv.get_api, srv.reset_api)
srv.probe_session = _fake_probe
srv.chrome_profile_cookie_files = lambda browser, root=None: [
    (n, os.path.join(tmp, n, "Cookies")) for n in ("Default", "Profile 1", "Profile 2")]
srv.get_api = lambda fresh=False: _FakeApi()
srv.reset_api = lambda: None
os.environ["SUBSTACK_PUBLICATION_URL"] = "https://acme.substack.com"
_write_cfg({})
_payload = _json.loads(srv.tool_refresh_cookie({})["content"][0]["text"])
check("scan picked the profile that reaches the publication",
      _payload.get("profile"), "Profile 2")
with open(_cfg_path) as f:
    _saved = _json.load(f)
check("scan hit persisted as cookie_file", _saved.get("cookie_file"),
      os.path.join(tmp, "Profile 2", "Cookies"))
check("the matching session was stored", _saved["cookies"]["substack.sid"], "sid-client")

os.environ["SUBSTACK_PUBLICATION_URL"] = "https://nowhere.substack.com"
_write_cfg({})
try:
    srv.tool_refresh_cookie({})
    check("no profile reaches the publication -> explicit error", "no error", "RuntimeError")
except RuntimeError:
    check("no profile reaches the publication -> explicit error", "RuntimeError", "RuntimeError")

srv.probe_session, srv.chrome_profile_cookie_files, srv.get_api, srv.reset_api = _orig
del sys.modules["pycookiecheat"]
os.environ.pop("SUBSTACK_PUBLICATION_URL", None)

print("v0.4.0 — Local State parsing (list_profiles reads names/emails, never cookies)")
root2 = tempfile.mkdtemp()
with open(os.path.join(root2, "Local State"), "w") as f:
    _json.dump({"profile": {"info_cache": {
        "Default": {"name": "Person 1", "user_name": "me@x.com", "gaia_name": "Me"},
        "Profile 1": {"name": "Work", "user_name": "work@client.com"},
        "Profile 9": {},
    }}}, f)
_profs = srv.local_state_profiles("chrome", root=root2)
check("profiles sorted by dir", [p["dir"] for p in _profs],
      ["Default", "Profile 1", "Profile 9"])
check("display name + email surfaced", (_profs[0]["name"], _profs[0]["email"]),
      ("Person 1", "me@x.com"))
check("empty meta -> empty strings", (_profs[2]["name"], _profs[2]["email"]), ("", ""))
check("missing Local State -> []",
      srv.local_state_profiles("chrome", root=tempfile.mkdtemp()), [])
_payload = _json.loads(srv.tool_list_profiles({"root": root2})["content"][0]["text"])
check("list_profiles count", _payload["count"], 3)
check("list_profiles emails", _payload["profiles"][1]["email"], "work@client.com")
try:
    srv.tool_list_profiles({"root": os.path.join(root2, "nope")})
    check("bogus root -> explicit error", "no error", "RuntimeError")
except RuntimeError:
    check("bogus root -> explicit error", "RuntimeError", "RuntimeError")

print("v0.4.0 — profile selector: exact-or-substring, exactly one hit")
check("dir name, case-insensitive",
      srv.resolve_profile_selector("profile 1", "chrome", root=root2)["dir"], "Profile 1")
check("display-name substring",
      srv.resolve_profile_selector("work", "chrome", root=root2)["dir"], "Profile 1")
check("email exact",
      srv.resolve_profile_selector("work@client.com", "chrome", root=root2)["dir"],
      "Profile 1")
check("email substring, unique",
      srv.resolve_profile_selector("me@x", "chrome", root=root2)["dir"], "Default")
try:
    srv.resolve_profile_selector("profile", "chrome", root=root2)
    check("ambiguous selector -> error listing candidates", "no error", "ValueError")
except ValueError as e:
    check("ambiguous selector -> error listing candidates",
          "Profile 1" in str(e) and "Profile 9" in str(e), True)
try:
    srv.resolve_profile_selector("zzz", "chrome", root=root2)
    check("no match -> error", "no error", "ValueError")
except ValueError:
    check("no match -> error", "ValueError", "ValueError")
root3 = tempfile.mkdtemp()
with open(os.path.join(root3, "Local State"), "w") as f:
    _json.dump({"profile": {"info_cache": {
        "Default": {"name": "Person 1"}, "Profile 1": {"name": "Person 10"},
    }}}, f)
check("exact match beats substring ('Person 1' vs 'Person 10')",
      srv.resolve_profile_selector("person 1", "chrome", root=root3)["dir"], "Default")

print("v0.4.0 — multi-match scan stops; act_as pins the identity")
fake_pcc = types.ModuleType("pycookiecheat")
fake_pcc.BrowserType = lambda b: b
BY_FILE2 = {
    None: {"substack.sid": "sid-personal"},
    os.path.join(tmp, "Default", "Cookies"): {"substack.sid": "sid-personal"},
    os.path.join(tmp, "Profile 1", "Cookies"): {"substack.sid": "sid-alice"},
    os.path.join(tmp, "Profile 2", "Cookies"): {"substack.sid": "sid-bob"},
}
fake_pcc.chrome_cookies = lambda url, browser=None, cookie_file=None: dict(
    BY_FILE2.get(cookie_file) or {})
sys.modules["pycookiecheat"] = fake_pcc

def _fake_probe2(cookies):
    sid = cookies.get("substack.sid")
    if sid == "sid-alice":
        return True, {"handle": "alice", "primary": None, "subdomains": ["acme"]}
    if sid == "sid-bob":
        return True, {"handle": "bob", "primary": None, "subdomains": ["acme"]}
    return True, {"handle": "me", "primary": "personal", "subdomains": ["personal"]}

_orig = (srv.probe_session, srv.chrome_profile_cookie_files, srv.local_state_profiles,
         srv.get_api, srv.reset_api)
srv.probe_session = _fake_probe2
srv.chrome_profile_cookie_files = lambda browser, root=None: [
    (n, os.path.join(tmp, n, "Cookies")) for n in ("Default", "Profile 1", "Profile 2")]
srv.local_state_profiles = lambda browser, root=None: [
    {"dir": "Default", "name": "Personal", "email": "me@x.com"},
    {"dir": "Profile 1", "name": "Alice", "email": "alice@x.com"},
    {"dir": "Profile 2", "name": "Bob", "email": "bob@x.com"},
    {"dir": "Profile 9", "name": "Ghost", "email": ""},
]
srv.get_api = lambda fresh=False: _FakeApi()
srv.reset_api = lambda: None
os.environ["SUBSTACK_PUBLICATION_URL"] = "https://acme.substack.com"

_write_cfg({})
try:
    srv.tool_refresh_cookie({})
    check("two qualifying logins -> refuse to guess", "no error", "RuntimeError")
except RuntimeError as e:
    check("two qualifying logins -> refuse to guess",
          "alice" in str(e) and "bob" in str(e) and "profile" in str(e), True)
with open(_cfg_path) as f:
    _saved = _json.load(f)
check("multi-match stored NOTHING", "cookies" in _saved, False)

_write_cfg({"act_as": "bob"})
_payload = _json.loads(srv.tool_refresh_cookie({})["content"][0]["text"])
check("act_as reduces two hits to one", _payload.get("profile"), "Profile 2")
check("act_as reported in result", _payload.get("act_as"), "bob")
with open(_cfg_path) as f:
    _saved = _json.load(f)
check("the pinned identity's session was stored",
      _saved["cookies"]["substack.sid"], "sid-bob")

_write_cfg({"act_as": "nobody"})
try:
    srv.tool_refresh_cookie({})
    check("act_as matching no login -> explicit error", "no error", "RuntimeError")
except RuntimeError as e:
    check("act_as matching no login -> explicit error",
          "act_as" in str(e) and "nobody" in str(e), True)

print("v0.4.0 — explicit profile argument; act_as persistence")
_write_cfg({})
_payload = _json.loads(
    srv.tool_refresh_cookie({"profile": "alice"})["content"][0]["text"])
check("profile arg picked by display name", _payload.get("profile"), "Profile 1")
check("explicit choice pins act_as", _payload.get("act_as_persisted"), True)
with open(_cfg_path) as f:
    _saved = _json.load(f)
check("act_as persisted as the handle", _saved.get("act_as"), "alice")
check("profile's Cookies path persisted", _saved.get("cookie_file"),
      os.path.join(tmp, "Profile 1", "Cookies"))

_write_cfg({"act_as": "bob"})
_payload = _json.loads(
    srv.tool_refresh_cookie({"profile": "Profile 2"})["content"][0]["text"])
check("existing act_as never overwritten", "act_as_persisted" in _payload, False)
with open(_cfg_path) as f:
    _saved = _json.load(f)
check("act_as still the original pin", _saved.get("act_as"), "bob")
_write_cfg({"act_as": "bob"})
try:
    srv.tool_refresh_cookie({"profile": "alice"})
    check("explicit profile vs pin mismatch -> error", "no error", "RuntimeError")
except RuntimeError as e:
    check("explicit profile vs pin mismatch -> error", "bob" in str(e), True)

_write_cfg({})
try:
    srv.tool_refresh_cookie({"profile": "x", "cookie_file": "/tmp/y"})
    check("profile + cookie_file rejected", "no error", "ValueError")
except ValueError:
    check("profile + cookie_file rejected", "ValueError", "ValueError")
try:
    srv.tool_refresh_cookie({"profile": "x", "browser": "firefox"})
    check("profile + firefox rejected", "no error", "RuntimeError")
except RuntimeError:
    check("profile + firefox rejected", "RuntimeError", "RuntimeError")
try:
    srv.tool_refresh_cookie({"profile": "ghost"})
    check("profile without a Cookies DB -> explicit error", "no error", "RuntimeError")
except RuntimeError as e:
    check("profile without a Cookies DB -> explicit error", "Cookies" in str(e), True)

check("identity_matches: handle, case-insensitive",
      srv.identity_matches({"handle": "Bob"}, "bob"), True)
check("identity_matches: email fallback",
      srv.identity_matches({"handle": "x", "email": "Bob@Y.com"}, "bob@y.com"), True)
check("identity_matches: no pin -> everything qualifies",
      srv.identity_matches({"handle": "x"}, ""), True)
check("identity_matches: mismatch",
      srv.identity_matches({"handle": "x"}, "bob"), False)

(srv.probe_session, srv.chrome_profile_cookie_files, srv.local_state_profiles,
 srv.get_api, srv.reset_api) = _orig
del sys.modules["pycookiecheat"]
os.environ.pop("SUBSTACK_PUBLICATION_URL", None)
_write_cfg({})

print("v0.4.0 — default profile gets no special trust (review fix)")
fake_pcc = types.ModuleType("pycookiecheat")
fake_pcc.BrowserType = lambda b: b
BY_FILE3 = {
    None: {"substack.sid": "sid-carol"},
    os.path.join(tmp, "Default", "Cookies"): {"substack.sid": "sid-carol"},
    os.path.join(tmp, "Profile 1", "Cookies"): {"other": "x"},
    os.path.join(tmp, "Profile 2", "Cookies"): {"substack.sid": "sid-bob"},
}
fake_pcc.chrome_cookies = lambda url, browser=None, cookie_file=None: dict(
    BY_FILE3.get(cookie_file) or {})
sys.modules["pycookiecheat"] = fake_pcc

def _fake_probe3(cookies):
    sid = cookies.get("substack.sid")
    if sid in ("sid-carol", "sid-carol2"):
        return True, {"handle": "carol", "primary": None, "subdomains": ["acme"]}
    if sid == "sid-bob":
        return True, {"handle": "bob", "primary": None, "subdomains": ["acme"]}
    return False, {"reason": "session_invalid", "status": 401}

_orig = (srv.probe_session, srv.chrome_profile_cookie_files, srv.local_state_profiles,
         srv.get_api, srv.reset_api)
srv.probe_session = _fake_probe3
srv.chrome_profile_cookie_files = lambda browser, root=None: [
    (n, os.path.join(tmp, n, "Cookies")) for n in ("Default", "Profile 1", "Profile 2")]
srv.local_state_profiles = lambda browser, root=None: []
srv.get_api = lambda fresh=False: _FakeApi()
srv.reset_api = lambda: None
os.environ["SUBSTACK_PUBLICATION_URL"] = "https://acme.substack.com"

# Default (carol) AND Profile 2 (bob) both reach acme — the default must NOT win.
_write_cfg({})
try:
    srv.tool_refresh_cookie({})
    check("default + another login both reach -> refuse", "no error", "RuntimeError")
except RuntimeError as e:
    check("default + another login both reach -> refuse",
          "carol" in str(e) and "bob" in str(e), True)
with open(_cfg_path) as f:
    _saved = _json.load(f)
check("default-vs-other ambiguity stored NOTHING", "cookies" in _saved, False)
# act_as resolves the same ambiguity without a profile argument.
_write_cfg({"act_as": "bob"})
_payload = _json.loads(srv.tool_refresh_cookie({})["content"][0]["text"])
check("act_as disambiguates default-vs-other", _payload.get("profile"), "Profile 2")
# Two profiles signed into the SAME account are not an ambiguity — default kept.
BY_FILE3[os.path.join(tmp, "Profile 2", "Cookies")] = {"substack.sid": "sid-carol2"}
_write_cfg({})
_payload = _json.loads(srv.tool_refresh_cookie({})["content"][0]["text"])
check("same identity twice -> default kept, no refusal",
      _payload.get("profile"), "(default profile)")
with open(_cfg_path) as f:
    _saved = _json.load(f)
check("pure default hit still persists no cookie_file",
      "cookie_file" in _saved, False)

(srv.probe_session, srv.chrome_profile_cookie_files, srv.local_state_profiles,
 srv.get_api, srv.reset_api) = _orig
del sys.modules["pycookiecheat"]

print("v0.4.0 — act_as gates get_api and substack_status (review fix)")
os.environ.pop("SUBSTACK_SESSION_TOKEN", None)  # set at the top — wins over config
fake_sub = types.ModuleType("substack")
class _FakeSubApi:
    def __init__(self, **kw):
        pass
fake_sub.Api = _FakeSubApi
sys.modules["substack"] = fake_sub
_orig_probe = srv.probe_session
srv.probe_session = _fake_probe3
_write_cfg({"cookies": {"substack.sid": "sid-carol"}, "act_as": "bob"})
srv.reset_api()
try:
    srv.get_api(fresh=True)
    check("get_api refuses a wrong-identity session", "no error", "RuntimeError")
except RuntimeError as e:
    check("get_api refuses a wrong-identity session",
          "act_as" in str(e) and "bob" in str(e) and "carol" in str(e), True)
_status = _json.loads(srv.tool_substack_status({})["content"][0]["text"])
check("status: wrong identity -> api_ready False", _status.get("api_ready"), False)
check("status: wrong identity -> act_as fix hint", "act_as" in _status.get("fix", ""),
      True)
_write_cfg({"cookies": {"substack.sid": "sid-bob"}, "act_as": "bob"})
srv.reset_api()
check("get_api passes the pinned identity",
      isinstance(srv.get_api(fresh=True), _FakeSubApi), True)
_status = _json.loads(srv.tool_substack_status({})["content"][0]["text"])
check("status: matching identity -> api_ready True", _status.get("api_ready"), True)
check("status: act_as_matches reported", _status.get("act_as_matches"), True)
# The live C3 failure (2026-08-19): the pin changes AFTER the cache is warm —
# a cache hit must not hand back the now-untrusted client.
check("cache is warm going into the pin flip",
      isinstance(srv.get_api(), _FakeSubApi), True)
_write_cfg({"cookies": {"substack.sid": "sid-bob"}, "act_as": "nobody"})
try:
    srv.get_api()  # no fresh — the exact create_draft path
    check("warm cache does not bypass act_as", "no error", "RuntimeError")
except RuntimeError as e:
    check("warm cache does not bypass act_as",
          "act_as" in str(e) and "nobody" in str(e), True)
_write_cfg({"cookies": {"substack.sid": "sid-bob"}, "act_as": "bob"})
check("restoring the pin restores cached access",
      isinstance(srv.get_api(), _FakeSubApi), True)
srv.probe_session = _orig_probe
srv.reset_api()
del sys.modules["substack"]
os.environ.pop("SUBSTACK_PUBLICATION_URL", None)
_write_cfg({})

print("v0.4.0 — registration & version")
check("refresh_cookie schema exposes profile",
      "profile" in [t for t in srv.TOOLS if t["name"] == "refresh_cookie"
                    ][0]["inputSchema"]["properties"], True)
check("list_profiles registered", "list_profiles" in srv.TOOL_HANDLERS, True)
check("list_profiles never offers firefox",
      "firefox" in [t for t in srv.TOOLS if t["name"] == "list_profiles"
                    ][0]["inputSchema"]["properties"]["browser"]["enum"], False)
check("server version", srv.SERVER_VERSION, "0.4.0")

print("\n%d failure(s)" % len(fails))
for f in fails:
    print(" -", f)
sys.exit(1 if fails else 0)
