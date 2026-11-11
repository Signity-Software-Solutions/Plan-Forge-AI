#!/usr/bin/env python3
"""
Smoke-test the same Ollama chat flow PlanForge uses for /normalize.

Requires:
  pip install ollama

Environment (same names as the FastAPI app):
  OLLAMA_HOST   e.g. http://10.5.0.15:11434
  OLLAMA_MODEL  e.g. gpt-oss:20b

Usage:
  export OLLAMA_HOST=http://YOUR_SERVER:11434
  export OLLAMA_MODEL=your-model
  python3 scripts/test_remote_ollama.py

Google Colab: use the Colab prompt from the assistant reply; Colab cannot reach private
LAN IPs unless Ollama is exposed (VPN, tunnel, or public host).
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request


OLLAMA_SYSTEM_PROMPT = """You are a layout normalization engine.
Input may include CAD extracted JSON, OCR text, and dimension sheet data.
Output MUST be a single JSON object matching this shape exactly:
{
  "units": "mm",
  "walls": [{"x1": number, "y1": number, "x2": number, "y2": number}],
  "rooms": [{"name": string, "area": number or null, "polygon": [[x,y], ...]}],
  "fixtures": [{"kind": string, "x": number, "y": number, "label": string or null}],
  "openings": [{"kind": string, "x1": number, "y1": number, "x2": number, "y2": number, "label": string or null}],
  "meta": object (optional)
}
Resolve naming inconsistencies; infer missing dimensions only when reasonably justified.
Use consistent numeric units as given in input.units when present; default mm.

Respond with ONLY valid JSON matching that schema.
No markdown fences, no code blocks, no explanation before or after the JSON."""

SAMPLE_PAYLOAD = {
    "units": "mm",
    "walls": [
        {"x1": 0, "y1": 0, "x2": 100, "y2": 0},
        {"x1": 100, "y1": 0, "x2": 100, "y2": 80},
    ],
    "rooms": [{"name": "Test room", "area": None, "polygon": [[0, 0], [100, 0], [100, 80], [0, 80]]}],
    "fixtures": [],
    "openings": [],
    "meta": {"test": True},
}


def _strip_markdown_fences(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*\n?", t)
    if m:
        t = t[m.end() :]
    t = t.strip()
    if t.endswith("```"):
        t = t[:-3].rstrip()
    return t.strip()


def _parse_layout_json(content: str) -> dict:
    text = _strip_markdown_fences(content)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def ping_api_tags(host: str, timeout: float = 15.0) -> dict:
    """GET /api/tags — confirms Ollama HTTP is reachable."""
    url = host.rstrip("/") + "/api/tags"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    host = (os.environ.get("OLLAMA_HOST") or "").strip().rstrip("/")
    model = (os.environ.get("OLLAMA_MODEL") or "").strip()
    if not host or not model:
        print("ERROR: Set OLLAMA_HOST and OLLAMA_MODEL in the environment.", file=sys.stderr)
        print("Example:", file=sys.stderr)
        print('  export OLLAMA_HOST="http://your-server:11434"', file=sys.stderr)
        print('  export OLLAMA_MODEL="gpt-oss:20b"', file=sys.stderr)
        return 1

    try:
        tags = ping_api_tags(host)
    except urllib.error.URLError as e:
        print(f"ERROR: cannot reach Ollama at {host} (/api/tags): {e}", file=sys.stderr)
        print("If using Google Colab, private LAN IPs (10.x, 192.168.x) are not reachable — use a tunnel or run this script on your network.", file=sys.stderr)
        return 1

    names = [m.get("name", "") for m in tags.get("models", [])]
    print(f"OK: reached Ollama at {host} ({len(names)} model(s) reported).")
    if names and not any(model in n or n.startswith(model) for n in names):
        print(f"WARNING: model {model!r} not obvious in /api/tags list: {names[:8]}...")

    try:
        import ollama
    except ImportError:
        print("ERROR: pip install ollama", file=sys.stderr)
        return 1

    client = ollama.Client(host=host)
    user_content = json.dumps(SAMPLE_PAYLOAD, default=str)[:120000]
    print(f"Calling chat(model={model!r}) …")
    response = client.chat(
        model=model,
        messages=[
            {"role": "system", "content": OLLAMA_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        options={"temperature": 0},
    )
    content = response["message"]["content"]
    data = _parse_layout_json(content)
    assert isinstance(data, dict)
    assert "walls" in data and "rooms" in data
    print("OK: model returned valid JSON with walls/rooms keys.")
    print(json.dumps(data, indent=2)[:2000])
    if len(json.dumps(data)) > 2000:
        print("… (truncated)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
