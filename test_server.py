"""Regression checks for the v0.1.1 fixes. Offline — no live Substack, no cookie.

Run:  python3 test_server.py
"""
import importlib.util, os, sys

os.environ["TIE_SUBSTACK_CONFIG"] = "/tmp/tie-substack-test-config.json"
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

print("\n%d failure(s)" % len(fails))
for f in fails:
    print(" -", f)
sys.exit(1 if fails else 0)
