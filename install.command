#!/bin/bash
# tie-substack installer (macOS). Two ways to run:
#
#   1. One-liner (no clone needed) — server.py is fetched from GitHub:
#        bash -c "$(curl -fsSL https://raw.githubusercontent.com/wearevolt/tie-substack/main/install.command)"
#      Skip the prompts by presetting:
#        TIE_SUBSTACK_PUB=https://yourpub.substack.com     publication URL
#        TIE_SUBSTACK_CLIENT=acme                          per-client install (multi-tenant)
#        TIE_SUBSTACK_BROWSER_DIR=$HOME/TIE-Browsers/acme  dedicated browser user-data dir
#   2. Double-click it (or ./install.command) inside a clone — the adjacent
#      server.py is used, so local edits install as-is.
#
# Creates a venv in ~/.tie-substack/, installs deps, installs server.py, and
# registers the server in Claude Desktop's config. Re-running updates everything
# and keeps existing settings (incl. a saved cookie). With a client name it is
# MULTI-TENANT: per-client config (~/.tie-substack/<client>.json) + per-client
# server entry ('tie-substack-<client>' with env overrides) — re-run once per
# client publication (README "Multiple clients").
set -u

# In `bash -c "$(curl ...)"` mode $0 is "bash", not a file: there is no adjacent
# server.py to prefer, and we must not pick up a stray one from the cwd.
if [ -f "$0" ]; then
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
else
  SCRIPT_DIR=""
fi
INSTALL_DIR="$HOME/.tie-substack"
VENV="$INSTALL_DIR/venv"
CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
RAW_URL="https://raw.githubusercontent.com/wearevolt/tie-substack/main/server.py"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*"; }
# Only pause at the end when double-clicked (Terminal would close instantly).
pause() { case "$0" in *.command) read -r -p "Press Enter to close..." _;; esac; }
# Prompt only when there's a terminal to answer it; otherwise take the default,
# so a piped/non-interactive run can't hang. Result lands in $ANSWER.
ask() { # ask <prompt> <default>
  ANSWER=""
  if [ -t 0 ]; then read -r -p "$1" ANSWER; fi
  ANSWER="${ANSWER:-$2}"
}

bold "tie-substack installer"
echo

# --- 1. python3 (>= 3.10 REQUIRED) ---------------------------------------
# On Python < 3.10 pip silently resolves the 2023-era python-substack, whose
# Api has no create_draft_from_markdown — every draft tool then fails with an
# AttributeError (operator-hit 2026-08-25). Pick the newest capable interpreter.
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -n "$PY" ]; then
  ok "python found: $($PY --version 2>&1) ($PY)"
else
  fail "no Python 3.10+ found — this server requires it (python-substack >= 0.3)."
  echo "      Install a current Python (e.g. from python.org or 'brew install python'),"
  echo "      then re-run this installer."
  pause; exit 1
fi

# --- 2. venv + dependencies ---------------------------------------------
mkdir -p "$INSTALL_DIR"
# An existing venv built on an old Python carries the old library — rebuild it.
if [ -x "$VENV/bin/python3" ] && ! "$VENV/bin/python3" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
  ok "existing venv uses Python < 3.10 — rebuilding it with $PY"
  rm -rf "$VENV"
fi
if [ ! -x "$VENV/bin/python3" ]; then
  "$PY" -m venv "$VENV" || { fail "could not create venv at $VENV"; exit 1; }
  ok "venv created: $VENV"
else
  ok "venv exists: $VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip 'python-substack>=0.3.0,<0.5' pycookiecheat \
  && ok "dependencies installed (python-substack>=0.3, pycookiecheat)" \
  || { fail "pip install failed — check network and rerun"; exit 1; }

# --- 3. server.py --------------------------------------------------------
if [ -f "$SCRIPT_DIR/server.py" ]; then
  cp "$SCRIPT_DIR/server.py" "$INSTALL_DIR/server.py"
  ok "server.py installed from this folder → $INSTALL_DIR/server.py"
elif curl -fsSL "$RAW_URL" -o "$INSTALL_DIR/server.py" 2>/dev/null; then
  ok "server.py downloaded from GitHub → $INSTALL_DIR/server.py"
else
  fail "Could not find server.py next to this script nor download it."
  pause; exit 1
fi
"$VENV/bin/python3" -c "import ast; ast.parse(open('$INSTALL_DIR/server.py').read())" 2>/dev/null \
  && ok "server.py syntax OK" \
  || { fail "installed server.py does not parse — aborting"; exit 1; }

# --- 4. client (one server per publication) -------------------------------
echo
bold "Client"
echo "  One tie-substack server per client publication. Leave empty for the"
echo "  default single setup (config.json, server name 'tie-substack')."
if [ -n "${TIE_SUBSTACK_CLIENT:-}" ]; then
  CLIENT="$TIE_SUBSTACK_CLIENT"
  ok "taken from TIE_SUBSTACK_CLIENT: $CLIENT"
else
  ask "  Client name (e.g. acme; Enter = single setup): " ""
  CLIENT="$ANSWER"
fi
CLIENT="$(printf '%s' "$CLIENT" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | sed 's/^-*//; s/-*$//')"
if [ -n "$CLIENT" ]; then
  CLIENT_CONFIG="$INSTALL_DIR/$CLIENT.json"
  SERVER_NAME="tie-substack-$CLIENT"
  ok "per-client install → config $CLIENT_CONFIG, server '$SERVER_NAME'"
else
  CLIENT_CONFIG="$INSTALL_DIR/config.json"
  SERVER_NAME="tie-substack"
fi

# --- 5. publication URL ---------------------------------------------------
EXISTING_PUB="$(CLIENT_CONFIG="$CLIENT_CONFIG" "$VENV/bin/python3" - <<'EOF' 2>/dev/null
import json, os
try:
    print(json.load(open(os.environ["CLIENT_CONFIG"])).get("publication_url", ""))
except Exception:
    print("")
EOF
)"
if [ -n "$CLIENT" ]; then
  DEFAULT_PUB="${EXISTING_PUB:-https://$CLIENT.substack.com}"
else
  DEFAULT_PUB="${EXISTING_PUB:-https://thrivinginengineering.substack.com}"
fi
echo
bold "Substack publication"
if [ -n "${TIE_SUBSTACK_PUB:-}" ]; then
  PUB="$TIE_SUBSTACK_PUB"
  ok "taken from TIE_SUBSTACK_PUB: $PUB"
else
  ask "  Publication URL [$DEFAULT_PUB]: " "$DEFAULT_PUB"
  PUB="$ANSWER"
fi

# --- 6. cookie source (optional dedicated browser) ------------------------
echo
bold "Cookie source"
echo "  Enter = your main Chrome (refresh_cookie scans its profiles for the"
echo "  right session). For the dedicated per-client browser model, give its"
echo "  user-data dir — its Default/Cookies DB is then read directly."
DEFAULT_BDIR=""
if [ -n "$CLIENT" ]; then DEFAULT_BDIR="$HOME/TIE-Browsers/$CLIENT"; fi
if [ -n "${TIE_SUBSTACK_BROWSER_DIR:-}" ]; then
  BROWSER_DIR="$TIE_SUBSTACK_BROWSER_DIR"
  ok "taken from TIE_SUBSTACK_BROWSER_DIR: $BROWSER_DIR"
else
  ask "  Browser user-data dir ('-' = main Chrome) [$DEFAULT_BDIR]: " "$DEFAULT_BDIR"
  BROWSER_DIR="$ANSWER"
fi
[ "$BROWSER_DIR" = "-" ] && BROWSER_DIR=""

PUB_NORM="$(PUB_URL="$PUB" CLIENT_CONFIG="$CLIENT_CONFIG" BROWSER_DIR="$BROWSER_DIR" "$VENV/bin/python3" <<'EOF'
import json, os, stat
p = os.environ["CLIENT_CONFIG"]
try:
    cfg = json.load(open(p))
except Exception:
    cfg = {}
# python-substack resolves the publication from the https://<name>.substack.com
# form only — a bare host silently breaks every authenticated call.
u = os.environ["PUB_URL"].strip().rstrip("/")
if u.lower().startswith("http://"):
    u = "https://" + u[len("http://"):]
elif not u.lower().startswith("https://"):
    u = "https://" + u
cfg["publication_url"] = u
bdir = os.environ.get("BROWSER_DIR", "").strip().rstrip("/")
if bdir:
    cfg["cookie_file"] = os.path.join(bdir, "Default", "Cookies")
else:
    cfg.pop("cookie_file", None)
json.dump(cfg, open(p, "w"), indent=2)
os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
print(u)
EOF
)"
ok "config written: $CLIENT_CONFIG (0600) — publication_url = $PUB_NORM"
if [ -n "$BROWSER_DIR" ]; then
  if [ -f "$BROWSER_DIR/Default/Cookies" ]; then
    ok "cookie_file → $BROWSER_DIR/Default/Cookies"
  else
    warn "cookie_file set, but no Cookies DB yet at $BROWSER_DIR/Default/"
    echo "      Launch the dedicated browser once and log in to Substack there:"
    echo "      open -na \"Google Chrome\" --args --user-data-dir=\"$BROWSER_DIR\""
  fi
fi

# --- 7. Claude Desktop config -------------------------------------------
if [ -f "$CONFIG" ]; then
  cp "$CONFIG" "$CONFIG.bak-tie-substack" && ok "config backed up → claude_desktop_config.json.bak-tie-substack"
fi
VENV="$VENV" INSTALL_DIR="$INSTALL_DIR" CONFIG="$CONFIG" \
CLIENT="$CLIENT" CLIENT_CONFIG="$CLIENT_CONFIG" SERVER_NAME="$SERVER_NAME" \
PUB_NORM="$PUB_NORM" python3 <<'EOF'
import json, os
cfg_path = os.environ["CONFIG"]
try:
    cfg = json.load(open(cfg_path))
except Exception:
    cfg = {}
entry = {
    "command": os.path.join(os.environ["VENV"], "bin", "python3"),
    "args": [os.path.join(os.environ["INSTALL_DIR"], "server.py")],
}
if os.environ.get("CLIENT"):
    # Per-client tenancy rides on the server's env overrides.
    entry["env"] = {
        "TIE_SUBSTACK_CONFIG": os.environ["CLIENT_CONFIG"],
        "SUBSTACK_PUBLICATION_URL": os.environ["PUB_NORM"],
    }
cfg.setdefault("mcpServers", {})[os.environ["SERVER_NAME"]] = entry
os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
json.dump(cfg, open(cfg_path, "w"), indent=2)
EOF
ok "Claude Desktop config updated (server '$SERVER_NAME')"

echo
bold "Done. Final steps:"
echo "  1. Quit Claude completely (Cmd-Q) and reopen it."
if [ -n "$BROWSER_DIR" ]; then
  echo "  2. Launch the dedicated browser once and log in to this client's Substack:"
  echo "       open -na \"Google Chrome\" --args --user-data-dir=\"$BROWSER_DIR\""
else
  echo "  2. Make sure you are logged in to Substack in Chrome (any profile —"
  echo "     refresh_cookie scans them for the right session)."
fi
echo "  3. In a chat, ask Claude to run substack_status — then refresh_cookie"
echo "     (macOS will ask for Keychain access; the cookie goes straight into"
echo "      $CLIENT_CONFIG and is never shown)."
echo "  Re-run this installer with another client name to add more publications."
echo
pause
