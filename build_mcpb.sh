#!/usr/bin/env bash
# tie-substack Claude Desktop extension (.mcpb), macOS and Windows. Server type "uv": Claude Desktop
# finds uv (or downloads its own pinned one), runs `uv sync` against the bundled pyproject.toml and
# uv.lock into a venv inside the extension folder, then starts `uv run ... server.py`. No Python is
# bundled; install needs network (uv, a Python 3.12 build, PyPI). Needs uv here (CI: setup-uv).
#
#   ./build_mcpb.sh            -> dist/tie-substack.mcpb (the version is in its manifest; the fixed
#                                 name keeps .../releases/latest/download/tie-substack.mcpb stable)
set -euo pipefail

DEPS=('python-substack>=0.3.0,<0.5' 'websocket-client' "pycookiecheat; sys_platform == 'darwin'")

cd "$(dirname "$0")"
VERSION="$(python3 -c 'import re,sys; print(re.search(r"^SERVER_VERSION = \"([^\"]+)\"", open("server.py").read(), re.M).group(1))')"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
OUT="dist/tie-substack.mcpb"
EXT="$BUILD/ext"
mkdir -p "$EXT"

cp server.py "$EXT/server.py"
{
  echo '[project]'
  echo 'name = "tie-substack"'
  echo "version = \"${VERSION}\""
  echo 'requires-python = ">=3.12,<3.13"'
  echo 'dependencies = ['
  for d in "${DEPS[@]}"; do echo "    \"${d}\","; done
  echo ']'
} > "$EXT/pyproject.toml"
(cd "$EXT" && uv lock --quiet)  # universal lock: the same pinned versions on macOS and Windows

cat > "$EXT/manifest.json" <<EOF
{
  "manifest_version": "0.4",
  "name": "tie-substack",
  "display_name": "TIE Substack",
  "version": "${VERSION}",
  "description": "Substack drafts, Notes, restacks and stats for the TIE Social plugin's clients. A client's Substack login is taken from a dedicated browser window of its own and stays on this computer.",
  "author": { "name": "Volt" },
  "server": {
    "type": "uv",
    "entry_point": "server.py",
    "mcp_config": { "command": "uv", "args": ["run", "--directory", "\${__dirname}", "server.py"] }
  },
  "compatibility": { "platforms": ["darwin", "win32"] }
}
EOF

mkdir -p dist
rm -f "$OUT"
(cd "$EXT" && zip -qr - manifest.json pyproject.toml uv.lock server.py) > "$OUT"
echo "$OUT ${VERSION} ($(du -h "$OUT" | cut -f1))"
