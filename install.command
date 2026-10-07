#!/bin/bash
# tie-substack installer (macOS), 0.5.0: ONE server entry for every client.
#
#   1. One-liner (no clone needed), server.py is fetched from GitHub:
#        bash -c "$(curl -fsSL https://raw.githubusercontent.com/wearevolt/tie-substack/main/install.command)"
#   2. Double-click it (or ./install.command) inside a clone: the adjacent server.py is used.
#
#   Options (env):
#     TIE_SUBSTACK_DEFAULT_SLUG=tie   the client slug an old single-setup config migrates to
#     TIE_SUBSTACK_PYTHON=...          a specific Python 3.10+ interpreter
#   Flags:
#     --rollback        restore the pre-migration 0.4 state from ~/.tie-substack/backup-original
#     --rollback=last   restore only the latest per-run backup
#
# What it does: creates a venv in ~/.tie-substack/, installs deps, migrates any 0.4 configs
# (one per client) into the 0.5 client registry (clients.json + sessions/), backs everything
# up first (write-once backup-original/ + a per-run backup, the 0.4 server kept in legacy/),
# verifies every client (live), and adds the single 0.5 entry. A 0.4 entry is removed only
# when its client was imported AND verified ready; every other 0.4 entry (not ready, or its
# publication could not be determined) keeps working on the legacy server beside the new entry.
set -u

if [ -f "$0" ]; then
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
else
  SCRIPT_DIR=""
fi
INSTALL_DIR="$HOME/.tie-substack"
VENV="$INSTALL_DIR/venv"
UV="$INSTALL_DIR/bin/uv"
UV_PYTHON_DIR="$INSTALL_DIR/python"
UV_CACHE_DIR="$INSTALL_DIR/cache/uv"
CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
RAW_URL="https://raw.githubusercontent.com/wearevolt/tie-substack/main/server.py"
NEW_SERVER="$INSTALL_DIR/server.py.new"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*"; }
pause() { case "$0" in *.command) read -r -p "Press Enter to close..." _;; esac; }

bold "tie-substack installer (0.5.0)"
echo
mkdir -p "$INSTALL_DIR"
chmod 700 "$INSTALL_DIR"

# --- 1. python3 (>= 3.10 REQUIRED) -----------------------------------------
python_supported() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null
}
uv_run() {
  UV_PYTHON_INSTALL_DIR="$UV_PYTHON_DIR" UV_CACHE_DIR="$UV_CACHE_DIR" "$UV" "$@"
}
PY=""
if [ -n "${TIE_SUBSTACK_PYTHON:-}" ]; then
  if command -v "$TIE_SUBSTACK_PYTHON" >/dev/null 2>&1 && python_supported "$TIE_SUBSTACK_PYTHON"; then
    PY="$TIE_SUBSTACK_PYTHON"
  else
    fail "TIE_SUBSTACK_PYTHON must point to Python 3.10 or newer."; pause; exit 1
  fi
elif [ -x "$VENV/bin/python3" ] && python_supported "$VENV/bin/python3"; then
  PY="$VENV/bin/python3"
else
  for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && python_supported "$c"; then PY="$c"; break; fi
  done
fi
if [ -n "$PY" ]; then
  ok "python found: $("$PY" --version 2>&1) ($PY)"
else
  warn "no validated system Python found; installing private uv-managed Python 3.13"
  if [ ! -x "$UV" ]; then
    command -v curl >/dev/null 2>&1 || { fail "curl not found; install Python 3.13 from python.org and re-run"; pause; exit 1; }
    mkdir -p "$INSTALL_DIR/bin"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_UNMANAGED_INSTALL="$INSTALL_DIR/bin" sh \
      && ok "uv installed: $UV" || { fail "uv install failed"; exit 1; }
  fi
  uv_run python install 3.13 && ok "uv-managed Python 3.13 installed" || { fail "uv could not install Python 3.13"; exit 1; }
  PY="$(uv_run python find 3.13 2>/dev/null || true)"
  [ -n "$PY" ] && python_supported "$PY" || { fail "no supported Python 3.13 executable found"; pause; exit 1; }
  ok "python found: $("$PY" --version 2>&1) ($PY)"
fi

# --- 2. venv + dependencies ---------------------------------------------
if [ -x "$VENV/bin/python3" ] && ! python_supported "$VENV/bin/python3"; then
  ok "existing venv uses unsupported Python; rebuilding it with $PY"; rm -rf "$VENV"
fi
if [ ! -x "$VENV/bin/python3" ]; then
  if [ -x "$UV" ]; then uv_run venv --python "$PY" "$VENV" || { fail "could not create venv"; exit 1; }
  else "$PY" -m venv "$VENV" || { fail "could not create venv"; exit 1; }; fi
  ok "venv created: $VENV"
else
  ok "venv exists: $VENV"
fi
if [ -x "$UV" ]; then
  uv_run pip install --python "$VENV/bin/python3" 'python-substack>=0.3.0,<0.5' pycookiecheat \
    && ok "dependencies installed with uv" || { fail "dependency install failed"; exit 1; }
else
  "$VENV/bin/pip" install -q --upgrade pip 'python-substack>=0.3.0,<0.5' pycookiecheat \
    && ok "dependencies installed" || { fail "pip install failed"; exit 1; }
fi
PYV="$VENV/bin/python3"

# --- 3. the new server.py (staged, not yet installed) --------------------
if [ -f "$SCRIPT_DIR/server.py" ]; then
  cp "$SCRIPT_DIR/server.py" "$NEW_SERVER" && ok "server.py staged from this folder"
elif curl -fsSL "$RAW_URL" -o "$NEW_SERVER" 2>/dev/null; then
  ok "server.py downloaded from GitHub"
else
  fail "Could not find server.py next to this script nor download it."; pause; exit 1
fi
"$PYV" -c "import ast; ast.parse(open('$NEW_SERVER').read())" 2>/dev/null \
  && ok "server.py syntax OK" || { fail "staged server.py does not parse; aborting"; rm -f "$NEW_SERVER"; exit 1; }

# --- rollback flags --------------------------------------------------------
for arg in "$@"; do
  case "$arg" in
    --rollback)
      bold "Rollback to the pre-migration 0.4 state"
      TIE_SUBSTACK_HOME="$INSTALL_DIR" "$PYV" "$NEW_SERVER" rollback --desktop-config "$CONFIG" \
        && ok "restored backup-original (clients.json, sessions/ and the journal stay in place)" \
        || { fail "rollback refused (see the message above)"; rm -f "$NEW_SERVER"; pause; exit 1; }
      rm -f "$NEW_SERVER"; echo "  Quit Claude completely (Cmd-Q) and reopen it."; pause; exit 0;;
    --rollback=last)
      bold "Rollback of the latest run"
      TIE_SUBSTACK_HOME="$INSTALL_DIR" "$PYV" "$NEW_SERVER" rollback --desktop-config "$CONFIG" --last \
        && ok "restored the latest per-run backup" || { fail "rollback refused"; rm -f "$NEW_SERVER"; pause; exit 1; }
      rm -f "$NEW_SERVER"; echo "  Quit Claude completely (Cmd-Q) and reopen it."; pause; exit 0;;
  esac
done

# --- 4. migration of 0.4 configs (one per client) ----------------------
echo
bold "Client registry"
DEFAULT_SLUG="${TIE_SUBSTACK_DEFAULT_SLUG:-tie}"
if [ -f "$CONFIG" ]; then
  cp "$CONFIG" "$CONFIG.bak-tie-substack" && ok "desktop config backed up → claude_desktop_config.json.bak-tie-substack"
  INSTALLED_ARG=""
  [ -f "$INSTALL_DIR/server.py" ] && INSTALLED_ARG="--installed-server $INSTALL_DIR/server.py"
  # shellcheck disable=SC2086
  TIE_SUBSTACK_HOME="$INSTALL_DIR" "$PYV" "$NEW_SERVER" migrate --desktop-config "$CONFIG" \
      --default-slug "$DEFAULT_SLUG" $INSTALLED_ARG > "$INSTALL_DIR/migration-report.json" \
    && ok "migration pass done (report: $INSTALL_DIR/migration-report.json)" \
    || { fail "migration failed; nothing was switched"; rm -f "$NEW_SERVER"; pause; exit 1; }
  "$PYV" - "$INSTALL_DIR/migration-report.json" <<'EOF'
import json, sys
r = json.load(open(sys.argv[1]))
for i in r.get("imported", []):
    print("  ✓ imported %-12s %s  (%s, act_as=%s, session=%s)" % (i["slug"], i["publication"], i["cookie_source"], i["act_as"], i["session"]))
for s in r.get("skipped", []):
    print("  · skipped  %-12s %s" % (s["slug"], s["reason"]))
for c in r.get("conflicts", []):
    print("  ! conflict %-12s %s" % (c["slug"], c["reason"]))
for u in r.get("unresolvable", []):
    print("  ! not imported: entry %s (%s); it keeps working on the 0.4 server" % (u["entry"], u["reason"]))
if r.get("warning"):
    print("  ! " + r["warning"])
if not r.get("selected") and not r.get("unresolvable"):
    print("  · no 0.4 configs referenced by claude_desktop_config.json; fresh install")
EOF
else
  warn "no Claude Desktop config at $CONFIG yet; it will be created"
fi

# --- 5. install the new server ------------------------------------------
mv "$NEW_SERVER" "$INSTALL_DIR/server.py" && ok "server.py installed → $INSTALL_DIR/server.py"

# --- 6. verify every client (live), then switch the entries -------------
echo
bold "Verification"
# VERIFIED = the clients check-clients reported ready. Only their 0.4 entries are removed;
# a crashed or empty verification verifies nothing, so nothing is removed.
VERIFIED=""
if TIE_SUBSTACK_HOME="$INSTALL_DIR" "$PYV" "$INSTALL_DIR/server.py" check-clients > "$INSTALL_DIR/check-clients.json"; then
  ok "every registered client is ready"
else
  warn "not every client is ready (details in $INSTALL_DIR/check-clients.json)"
fi
VERIFIED="$("$PYV" - "$INSTALL_DIR/check-clients.json" <<'EOF'
import json, sys
try:
    r = json.load(open(sys.argv[1]))
except Exception:  # noqa: BLE001
    r = {}
print(",".join(c["client"] for c in r.get("clients", []) if c.get("ready")))
EOF
)"
"$PYV" - "$INSTALL_DIR/check-clients.json" <<'EOF'
import json, sys
try:
    r = json.load(open(sys.argv[1]))
except Exception:  # noqa: BLE001
    print("  ! verification produced no report; no 0.4 entry will be removed"); sys.exit(0)
for c in r.get("clients", []):
    print("  %-12s %-28s bound=%-5s session=%-8s ready=%-5s %s" % (
        c["client"], c["publication"], c["bound"], c.get("session"), c.get("ready"), c.get("fix", "")))
if not r.get("clients"):
    print("  (no clients yet: add_client + bind_client in a chat)")
EOF

LEGACY_ARG=""
[ -f "$INSTALL_DIR/legacy/server.py" ] && LEGACY_ARG="--legacy-server $INSTALL_DIR/legacy/server.py"
# shellcheck disable=SC2086
TIE_SUBSTACK_HOME="$INSTALL_DIR" "$PYV" "$INSTALL_DIR/server.py" switch --desktop-config "$CONFIG" \
    --command "$PYV" --server "$INSTALL_DIR/server.py" --verified "$VERIFIED" --default-slug "$DEFAULT_SLUG" \
    $LEGACY_ARG > "$INSTALL_DIR/switch-report.json" \
  && ok "Claude Desktop config updated (server 'tie-substack')" \
  || { fail "could not update $CONFIG"; pause; exit 1; }
"$PYV" - "$INSTALL_DIR/switch-report.json" <<'EOF'
import json, sys
r = json.load(open(sys.argv[1]))
for n in r.get("removed", []):
    print("  ✓ 0.4 entry %s removed (its client verified ready)" % n)
for n in r.get("kept_legacy_entries", []):
    print("  ! 0.4 entry %s kept, working on the legacy server" % n)
if r.get("kept_legacy_entries"):
    print("      Fix in a chat: bind_client (or refresh_session) for each client, then re-run this installer.")
if r.get("unresolvable"):
    print("      Entries whose publication could not be determined are never imported: add_client +")
    print("      bind_client for each in a chat, then remove the old entry from claude_desktop_config.json.")
EOF

echo
bold "Done. Final steps:"
echo "  1. Quit Claude completely (Cmd-Q) and reopen it."
echo "  2. In a chat, ask Claude to run clients_status. New client: add_client, then bind_client."
echo "  3. Undo: re-run with --rollback (pre-migration state) or --rollback=last (this run only)."
echo
pause
