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

print("\n%d failure(s)" % len(fails))
for f in fails:
    print(" -", f)
sys.exit(1 if fails else 0)
