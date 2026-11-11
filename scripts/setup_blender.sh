#!/usr/bin/env bash
# Fetches a portable Blender install for the photoreal render pipeline
# (app/services/photoreal.py, app/blender/render_scene.py).
#
# No root required: this is the official Linux x64 tarball, extracted under
# .tools/blender/ (gitignored — ~1.3GB, far too large to commit). Re-run
# safely; it skips the download if already present.
#
# Usage:  bash scripts/setup_blender.sh
# Then:   export BLENDER_BIN="$(pwd)/.tools/blender/blender"
#         (or set blender_bin in .env — see app/core/config.py)
set -euo pipefail

VERSION="4.2.4"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS_DIR="${REPO_ROOT}/.tools"
BLENDER_DIR="${TOOLS_DIR}/blender"

if [ -x "${BLENDER_DIR}/blender" ]; then
  echo "Blender already installed at ${BLENDER_DIR}/blender"
  "${BLENDER_DIR}/blender" --version
  exit 0
fi

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) ARCHIVE="blender-${VERSION}-linux-x64.tar.xz" ;;
  *)
    echo "This script only automates the Linux x64 build." >&2
    echo "Download the matching build for your platform from https://www.blender.org/download/lts/ and" >&2
    echo "extract it to ${BLENDER_DIR}/ yourself (so that ${BLENDER_DIR}/blender exists)." >&2
    exit 1
    ;;
esac

URL="https://download.blender.org/release/Blender${VERSION%.*}/${ARCHIVE}"
mkdir -p "${TOOLS_DIR}"
echo "Downloading ${URL} ..."
curl -L --fail -o "${TOOLS_DIR}/${ARCHIVE}" "${URL}"

echo "Extracting ..."
tar xf "${TOOLS_DIR}/${ARCHIVE}" -C "${TOOLS_DIR}"
rm "${TOOLS_DIR}/${ARCHIVE}"
mv "${TOOLS_DIR}/blender-${VERSION}-linux-x64" "${BLENDER_DIR}"

echo "Installed: ${BLENDER_DIR}/blender"
"${BLENDER_DIR}/blender" --version
