#!/usr/bin/env bash
# tie-substack Windows extension (.mcpb): Claude Desktop installs it from Settings -> Extensions,
# with no system Python. Bundles the official Windows embeddable Python and the server's
# dependencies; server type "binary" points Claude at the bundled python.exe.
# Runs on Linux or macOS (CI: .github/workflows/mcpb.yml): every dependency is a prebuilt wheel.
#
#   ./build_mcpb.sh            -> dist/tie-substack-windows.mcpb (the version is in its manifest; a fixed
#                                 name keeps .../releases/latest/download/tie-substack-windows.mcpb stable)
set -euo pipefail

PY_VERSION="3.12.10"
PY_ZIP="python-${PY_VERSION}-embed-amd64.zip"
PY_SHA256="4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"  # pinned at first download, 2026-11-05
DEPS=('python-substack>=0.3.0,<0.5' 'websocket-client')

cd "$(dirname "$0")"
VERSION="$(python3 -c 'import re,sys; print(re.search(r"^SERVER_VERSION = \"([^\"]+)\"", open("server.py").read(), re.M).group(1))')"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
OUT="dist/tie-substack-windows.mcpb"

curl -fsSL "https://www.python.org/ftp/python/${PY_VERSION}/${PY_ZIP}" -o "$BUILD/$PY_ZIP"
echo "${PY_SHA256}  $BUILD/$PY_ZIP" | shasum -a 256 -c - >/dev/null || { echo "checksum mismatch for $PY_ZIP" >&2; exit 1; }

mkdir -p "$BUILD/ext/python"
unzip -q "$BUILD/$PY_ZIP" -d "$BUILD/ext/python"
printf 'python312.zip\n.\n..\\lib\n' > "$BUILD/ext/python/python312._pth"  # the bundled packages; no site, no user paths

python3 -m pip install --quiet --disable-pip-version-check --target "$BUILD/ext/lib" \
  --platform win_amd64 --python-version 3.12 --implementation cp --only-binary=:all: "${DEPS[@]}"
rm -rf "$BUILD/ext/lib/bin"

cp server.py "$BUILD/ext/server.py"
cat > "$BUILD/ext/manifest.json" <<EOF
{
  "manifest_version": "0.2",
  "name": "tie-substack",
  "display_name": "TIE Substack",
  "version": "${VERSION}",
  "description": "Substack drafts, Notes and restacks for the TIE Social plugin's clients. A client's Substack login is taken from a dedicated browser window of its own and stays on this computer.",
  "author": { "name": "Volt" },
  "server": {
    "type": "binary",
    "entry_point": "python/python.exe",
    "mcp_config": { "command": "\${__dirname}/python/python.exe", "args": ["\${__dirname}/server.py"] }
  },
  "compatibility": { "platforms": ["win32"] }
}
EOF

mkdir -p dist
rm -f "$OUT"
(cd "$BUILD/ext" && zip -qr - manifest.json server.py python lib) > "$OUT"
echo "$OUT ${VERSION} ($(du -h "$OUT" | cut -f1))"
