"""Tests for Phase 1 wall merging."""

from app.services.image_processing_utils import merge_hough_segments


def test_merge_collinear_chain():
    # Two horizontal segments that touch with same angle → merged cluster path
    raw = [
        {"x1": 0.0, "y1": 10.0, "x2": 50.0, "y2": 10.0},
        {"x1": 52.0, "y1": 10.0, "x2": 100.0, "y2": 10.0},
    ]
    out = merge_hough_segments(raw, endpoint_cluster_px=15.0)
    assert len(out) >= 1
    # Should reduce fragmentation along same line
    assert len(out) <= len(raw)


def test_merge_empty():
    assert merge_hough_segments([]) == []
