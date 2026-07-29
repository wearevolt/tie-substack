#!/bin/bash
# tie-substack installer (macOS). Two ways to run:
#
#   1. One-liner (no clone needed) — server.py is fetched from GitHub:
#        bash -c "$(curl -fsSL https://raw.githubusercontent.com/wearevolt/tie-substack/main/install.command)"
#      Skip the prompt by presetting the publication:
#        TIE_SUBSTACK_PUB=https://yourpub.substack.com bash -c "$(curl -fsSL .../install.command)"
#   2. Double-click it (or ./install.command) inside a clone — the adjacent
#      server.py is used, so local edits install as-is.
#
# Creates a venv in ~/.tie-substack/, installs deps, installs server.py, and
# registers the server in Claude Desktop's config. Re-running updates everything
# and keeps existing settings (incl. a saved cookie).
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

# --- 1. python3 ---------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
  ok "python3 found: $(python3 --version 2>&1)"
else
  fail "python3 not found. Install Xcode Command Line Tools first:"
  echo "      xcode-select --install"
  pause; exit 1
fi

# --- 2. venv + dependencies ---------------------------------------------
mkdir -p "$INSTALL_DIR"
if [ ! -x "$VENV/bin/python3" ]; then
  python3 -m venv "$VENV" || { fail "could not create venv at $VENV"; exit 1; }
  ok "venv created: $VENV"
else
  ok "venv exists: $VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip python-substack pycookiecheat \
  && ok "dependencies installed (python-substack, pycookiecheat)" \
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

# --- 4. publication URL ---------------------------------------------------
EXISTING_PUB="$("$VENV/bin/python3" - <<EOF 2>/dev/null
import json
try:
    print(json.load(open("$INSTALL_DIR/config.json")).get("publication_url", ""))
except Exception:
    print("")
EOF
)"
DEFAULT_PUB="${EXISTING_PUB:-https://thrivinginengineering.substack.com}"
echo
bold "Substack publication"
if [ -n "${TIE_SUBSTACK_PUB:-}" ]; then
  PUB="$TIE_SUBSTACK_PUB"
  ok "taken from TIE_SUBSTACK_PUB: $PUB"
else
  ask "  Publication URL [$DEFAULT_PUB]: " "$DEFAULT_PUB"
  PUB="$ANSWER"
fi
PUB_URL="$PUB" INSTALL_DIR="$INSTALL_DIR" "$VENV/bin/python3" <<'EOF'
import json, os, stat
p = os.path.join(os.environ["INSTALL_DIR"], "config.json")
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
json.dump(cfg, open(p, "w"), indent=2)
os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
print("  publication_url = %s" % u)
EOF
ok "publication saved to $INSTALL_DIR/config.json (0600)"

# --- 5. Claude Desktop config -------------------------------------------
if [ -f "$CONFIG" ]; then
  cp "$CONFIG" "$CONFIG.bak-tie-substack" && ok "config backed up → claude_desktop_config.json.bak-tie-substack"
fi
VENV="$VENV" INSTALL_DIR="$INSTALL_DIR" CONFIG="$CONFIG" python3 <<'EOF'
import json, os
cfg_path = os.environ["CONFIG"]
try:
    cfg = json.load(open(cfg_path))
except Exception:
    cfg = {}
cfg.setdefault("mcpServers", {})["tie-substack"] = {
    "command": os.path.join(os.environ["VENV"], "bin", "python3"),
    "args": [os.path.join(os.environ["INSTALL_DIR"], "server.py")],
}
os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
json.dump(cfg, open(cfg_path, "w"), indent=2)
EOF
ok "Claude Desktop config updated"

echo
bold "Done. Final steps:"
echo "  1. Quit Claude completely (Cmd-Q) and reopen it."
echo "  2. Make sure you are logged in to Substack in Chrome."
echo "  3. In a chat, ask Claude to run substack_status — then refresh_cookie"
echo "     (macOS will ask for Keychain access; the cookie goes straight into"
echo "      $INSTALL_DIR/config.json and is never shown)."
echo
pause
