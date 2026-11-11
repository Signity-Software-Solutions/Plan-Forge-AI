"""Structural gates and sheet label merge (LLM safety)."""

from app.models.layout_schema import Fixture, Opening, Room, UnifiedLayout, WallSegment
from app.services.layout_llm_guard import (
    accept_llm_or_fallback,
    gates_llm_vs_baseline,
    merge_sheet_label_patches,
)


def _sample_baseline() -> UnifiedLayout:
    return UnifiedLayout(
        units="mm",
        walls=[
            WallSegment(x1=0, y1=0, x2=1000, y2=0),
            WallSegment(x1=1000, y1=0, x2=1000, y2=800),
            WallSegment(x1=1000, y1=800, x2=0, y2=800),
            WallSegment(x1=0, y1=800, x2=0, y2=0),
        ],
        rooms=[
            Room(
                name="A",
                area=None,
                polygon=[[0, 0], [1000, 0], [1000, 800], [0, 800], [0, 0]],
            )
        ],
        fixtures=[Fixture(kind="display", x=500, y=400, label="TV")],
        openings=[Opening(kind="door", x1=100, y1=0, x2=200, y2=0, label=None)],
        meta={"normalized_by": "heuristic"},
    )


def test_gates_pass_identical():
    b = _sample_baseline()
    assert gates_llm_vs_baseline(b, b) == (True, "")


def test_gates_reject_collapsed_walls():
    b = _sample_baseline()
    collapsed = UnifiedLayout(
        units="mm",
        walls=[WallSegment(x1=0, y1=0, x2=1, y2=0)],
        rooms=b.rooms,
        fixtures=b.fixtures,
        openings=b.openings,
        meta={},
    )
    ok, reason = gates_llm_vs_baseline(collapsed, b)
    assert ok is False
    assert "wall_count" in reason or "bbox" in reason


def test_accept_llm_or_fallback_rejects_and_tags_meta():
    b = _sample_baseline()
    collapsed = UnifiedLayout(
        units="mm",
        walls=[],
        rooms=[],
        fixtures=[],
        openings=[],
        meta={},
    )
    out = accept_llm_or_fallback(collapsed, b, source="ollama")
    assert out.walls == b.walls
    assert out.meta.get("normalized_by") == "heuristic_llm_rejected"
    assert out.meta.get("llm_candidate_rejected") is True
    assert out.meta.get("rejection_reason")


def test_merge_sheet_label_patches():
    b = _sample_baseline()
    patches = {
        "room_names": [{"index": 0, "name": "Retail"}],
        "opening_labels": [{"index": 0, "label": "Main door"}],
        "fixture_labels": [{"index": 0, "label": "Display 1"}],
        "meta_patches": {"note": "ok"},
    }
    m = merge_sheet_label_patches(b, patches)
    assert m.rooms[0].name == "Retail"
    assert m.openings[0].label == "Main door"
    assert m.fixtures[0].label == "Display 1"
    assert m.meta.get("note") == "ok"
    assert m.walls == b.walls
    assert m.meta.get("normalized_by") == "heuristic_llm_labels"
