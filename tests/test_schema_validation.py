from app.core.llm_client import heuristic_normalize
from app.models.layout_schema import UnifiedLayout
from app.services.geometry_engine import layout_to_svg
from app.utils.validators import validate_layout


def test_heuristic_normalize_unwraps_nested_parsed():
    """Same envelope as normalize_stored_file → normalize_with_llm without LLM."""
    envelope = {
        "parsed": {
            "source": "cad",
            "walls": [{"x1": 0, "y1": 0, "x2": 100, "y2": 0}],
            "labels": [],
            "dimensions": [],
            "meta": {"layers": ["0"]},
        },
        "file_type": "cad",
        "filename": "plan.dxf",
    }
    u = heuristic_normalize(envelope)
    assert len(u.walls) == 1
    assert u.walls[0].x2 == 100
    assert u.meta.get("file_type") == "cad"
    assert u.meta.get("filename") == "plan.dxf"


def test_heuristic_normalize_flat_parser_output_unchanged():
    u = heuristic_normalize(
        {
            "walls": [{"x1": 1, "y1": 2, "x2": 3, "y2": 4}],
            "units": "mm",
        }
    )
    assert len(u.walls) == 1
    assert u.walls[0].x1 == 1


def test_unified_layout_roundtrip():
    data = {
        "units": "mm",
        "walls": [{"x1": 0, "y1": 0, "x2": 5000, "y2": 0}],
        "rooms": [
            {
                "name": "Retail Room",
                "area": 502,
                "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]],
            }
        ],
        "fixtures": [],
        "openings": [],
    }
    u = UnifiedLayout.model_validate(data)
    assert u.walls[0].x2 == 5000


def test_validation_polygon_closed():
    u = UnifiedLayout(
        units="mm",
        walls=[],
        rooms=[
            {
                "name": "A",
                "area": 100,
                "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]],
            }
        ],
    )
    issues = validate_layout(u)
    codes = [i.code for i in issues]
    assert "invalid_polygon" not in codes


def test_svg_generation_smoke():
    u = UnifiedLayout(
        units="mm",
        walls=[{"x1": 0, "y1": 0, "x2": 100, "y2": 0}],
        rooms=[
            {
                "name": "R",
                "area": None,
                "polygon": [[0, 0], [50, 0], [50, 50], [0, 50]],
            }
        ],
    )
    svg = layout_to_svg(u)
    assert "<svg" in svg and "</svg>" in svg
