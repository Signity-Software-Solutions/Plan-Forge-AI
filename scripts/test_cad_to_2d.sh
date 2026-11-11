#!/usr/bin/env bash
set -euo pipefail
B="${PLANFORGE_API_BASE_URL:-}"; O="${OLLAMA_HOST:-}"; I="${1:-}"
[[ -n "$B" && -n "$O" && -n "$I" && -f "$I" ]] || {
  echo "Missing PLANFORGE_API_BASE_URL, OLLAMA_HOST, or image file. Usage: export PLANFORGE_API_BASE_URL=... OLLAMA_HOST=... $0 /path/to/floorplan.png" >&2
  exit 1
}
B="${B%/}"
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
D="$R/pipeline_output"
mkdir -p "$D"
S="$D/result.svg"
T=$(mktemp)
trap 'rm -f "$T"' EXIT
hc(){ local c; c=$(curl -sS -o "$T" -w "%{http_code}" "$@") || exit 1
[[ "$c" == "200" ]] || { echo "HTTP $c" >&2; cat "$T" >&2; exit 1; }; }
hc -X POST -F "file=@${I}" -F "type=image" -F "project_name=test_cad_to_2d" "${B}/upload"
F=$(jq -r .file_id "$T"); P=$(jq -r .project_id "$T")
echo "[1/6] Uploading image... done (file_id: $F)"
hc -X POST "${B}/process/${F}"
echo "[2/6] Processing...     done"
hc -X POST "${B}/normalize/${F}"
echo "[3/6] Normalizing...    done"
hc -X POST "${B}/generate-layout/${P}"
echo "[4/6] Generate layout... done"
hc "${B}/output/${P}"
jq -r .svg "$T" >"$S"
echo "[5/6] Fetch output...    done -> $S"
if command -v xdg-open >/dev/null 2>&1; then xdg-open "$S"
elif command -v open >/dev/null 2>&1; then open "$S"; fi
echo "[6/6] Opened result (if supported)."
