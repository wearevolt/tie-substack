#!/bin/bash
# tie-substack installer (macOS). Double-click to run.
# Creates a venv in ~/.tie-substack/, installs deps, copies server.py, and
# registers the server in Claude Desktop's config. Re-running updates everything
# and keeps existing settings (incl. a saved cookie).
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$HOME/.tie-substack"
VENV="$INSTALL_DIR/venv"
CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
RAW_URL="https://raw.githubusercontent.com/wearevolt/tie-substack/main/server.py"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*"; }

bold "tie-substack installer"
echo

# --- 1. python3 ---------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
  ok "python3 found: $(python3 --version 2>&1)"
else
  fail "python3 not found. Install Xcode Command Line Tools first:"
  echo "      xcode-select --install"
  read -r -p "Press Enter to close..." _; exit 1
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
  read -r -p "Press Enter to close..." _; exit 1
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
read -r -p "  Publication URL [$DEFAULT_PUB]: " PUB
PUB="${PUB:-$DEFAULT_PUB}"
PUB_URL="$PUB" INSTALL_DIR="$INSTALL_DIR" "$VENV/bin/python3" <<'EOF'
import json, os, stat
p = os.path.join(os.environ["INSTALL_DIR"], "config.json")
try:
    cfg = json.load(open(p))
except Exception:
    cfg = {}
cfg["publication_url"] = os.environ["PUB_URL"].rstrip("/")
json.dump(cfg, open(p, "w"), indent=2)
os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
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
read -r -p "Press Enter to close..." _
