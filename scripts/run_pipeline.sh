#!/usr/bin/env bash
#
# Single entry point: full PlanForge pipeline → JSON artifacts + 2D SVG under pipeline_output/.
#
# Configure in repo-root .env (required):
#   PLANFORGE_API_BASE_URL=http://127.0.0.1:8000
#   PLANFORGE_INPUT_FILE=./samples/retail_dummy_dimensions.xlsx   # or any image / DXF / sheet
#
# Optional:
#   PLANFORGE_INPUT_TYPE=image | sheet | cad   # if omitted, inferred from file extension
#   PLANFORGE_PIPELINE_OUTPUT_DIR=./pipeline_output
#   PLANFORGE_PROJECT_NAME="My layout"
#   BASE_URL=...   # overrides PLANFORGE_API_BASE_URL for this run
#
# Backward compatibility: if PLANFORGE_INPUT_FILE is unset, falls back to PLANFORGE_INPUT_IMAGE
# or PLANFORGE_INPUT_SHEET (first non-empty).
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ROOT/.env"
  set +a
fi

BASE_URL="${BASE_URL:-$PLANFORGE_API_BASE_URL}"
FILE="${PLANFORGE_INPUT_FILE:-}"
if [[ -z "${FILE:-}" ]]; then
  FILE="${PLANFORGE_INPUT_IMAGE:-}"
fi
if [[ -z "${FILE:-}" ]]; then
  FILE="${PLANFORGE_INPUT_SHEET:-${PLANFORGE_INPUT_XLSX:-}}"
fi

OUT_DIR="${OUT_DIR:-${PLANFORGE_PIPELINE_OUTPUT_DIR:-$ROOT/pipeline_output}}"
if [[ "$OUT_DIR" != /* ]]; then
  OUT_DIR="$ROOT/${OUT_DIR#./}"
fi
PROJECT_NAME="${PLANFORGE_PROJECT_NAME:-Pipeline}"

infer_type() {
  local ext lower
  ext="${1##*.}"
  lower=$(printf '%s' "$ext" | tr '[:upper:]' '[:lower:]')
  case "$lower" in
    png|jpg|jpeg|webp|tif|tiff|bmp) echo image ;;
    xlsx|xls|csv|zip) echo sheet ;;  # csv = unified or single-table; zip = bundle of CSVs
    pdf) echo sheet ;;
    dxf) echo cad ;;
    *) echo "" ;;
  esac
}

TYPE="${PLANFORGE_INPUT_TYPE:-}"

if [[ -z "${BASE_URL:-}" ]]; then
  echo "Set PLANFORGE_API_BASE_URL in .env (or export BASE_URL=...) — API base URL." >&2
  exit 1
fi
if [[ -z "${FILE:-}" ]]; then
  echo "Set PLANFORGE_INPUT_FILE in .env to the input file path." >&2
  exit 1
fi

if [[ "$FILE" != /* ]]; then
  FILE="$ROOT/${FILE#./}"
fi

if [[ ! -f "$FILE" ]]; then
  echo "File not found: $FILE" >&2
  exit 1
fi

if [[ -z "${TYPE:-}" ]]; then
  TYPE="$(infer_type "$FILE")"
fi
if [[ -z "${TYPE:-}" ]]; then
  echo "Cannot infer upload type from extension. Set PLANFORGE_INPUT_TYPE=image|sheet|cad in .env." >&2
  exit 1
fi

command -v curl >/dev/null || { echo "curl required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 required" >&2; exit 1; }

mkdir -p "$OUT_DIR"

echo "==> Uploading: $FILE (type=$TYPE)"
UP_JSON=$(curl -sS -X POST "$BASE_URL/upload" \
  -F "file=@${FILE}" \
  -F "type=${TYPE}" \
  -F "project_name=${PROJECT_NAME}")

FILE_ID=$(echo "$UP_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['file_id'])")
PROJECT_ID=$(echo "$UP_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['project_id'])")
echo "    file_id=$FILE_ID project_id=$PROJECT_ID"

echo "==> Process (parse)"
curl -sS -X POST "$BASE_URL/process/$FILE_ID" | python3 -m json.tool > "${OUT_DIR}/parsed_${FILE_ID}.json"
echo "    wrote ${OUT_DIR}/parsed_${FILE_ID}.json"

echo "==> Normalize"
curl -sS -X POST "$BASE_URL/normalize/$FILE_ID" | python3 -m json.tool > "${OUT_DIR}/normalized_${FILE_ID}.json"
echo "    wrote ${OUT_DIR}/normalized_${FILE_ID}.json"

echo "==> Generate layout (2D SVG)"
GEN_JSON=$(curl -sS -X POST "$BASE_URL/generate-layout/$PROJECT_ID")
echo "$GEN_JSON" | python3 -m json.tool > "${OUT_DIR}/generate_${PROJECT_ID}.json"

SVG_PATH=$(echo "$GEN_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('svg_path') or '')")
echo "    svg_path=$SVG_PATH"

curl -sS "$BASE_URL/output/$PROJECT_ID" | python3 -m json.tool > "${OUT_DIR}/output_${PROJECT_ID}.json"

FINAL_SVG="${OUT_DIR}/layout_${PROJECT_ID}.svg"
curl -sS "$BASE_URL/output/$PROJECT_ID/svg" > "$FINAL_SVG"
if [[ ! -s "$FINAL_SVG" ]] || ! grep -q '<svg' "$FINAL_SVG" 2>/dev/null; then
  if [[ -n "$SVG_PATH" && -f "$SVG_PATH" ]]; then
    cp -f "$SVG_PATH" "$FINAL_SVG"
  else
    echo "Warning: empty or invalid SVG; check ${OUT_DIR}/output_${PROJECT_ID}.json" >&2
    exit 2
  fi
fi

echo "==> Done. 2D SVG:"
echo "    $(realpath "$FINAL_SVG" 2>/dev/null || echo "$FINAL_SVG")"
[[ -n "$SVG_PATH" && -f "$SVG_PATH" ]] && echo "    (server copy: $SVG_PATH)"

echo ""
echo "Open: xdg-open \"$FINAL_SVG\""
