"""normalize_with_llm sheet modes (no live Ollama)."""

from app.core.llm_client import heuristic_normalize, normalize_with_llm


def test_sheet_heuristic_only_default():
    payload = {
        "file_type": "sheet",
        "parsed": {
            "units": "mm",
            "walls": [{"x1": 0, "y1": 0, "x2": 100, "y2": 0}],
            "rooms": [{"name": "R1", "area": None, "polygon": [[0, 0], [100, 0], [100, 50], [0, 50], [0, 0]]}],
            "fixtures": [],
            "openings": [],
            "meta": {},
        },
    }
    out = normalize_with_llm(payload)
    assert out.meta.get("normalized_by") == "heuristic"
    assert len(out.walls) == 1


def test_sheet_heuristic_matches_direct():
    payload = {
        "file_type": "sheet",
        "parsed": {
            "units": "mm",
            "walls": [],
            "rooms": [],
            "meta": {},
        },
    }
    a = heuristic_normalize(payload)
    b = normalize_with_llm(payload)
    assert a.walls == b.walls
    assert a.meta.get("normalized_by") == b.meta.get("normalized_by")
