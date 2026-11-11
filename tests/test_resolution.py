"""The resolution ladder: precedence, auditability and honesty."""

import pytest

from app.services.resolution import (
    ASSUMPTION_RANK,
    BLIND_RANK,
    SOURCE_DERIVED,
    SOURCE_GLOBAL_DEFAULT,
    SOURCE_PARSED,
    SOURCE_TYPE_DEFAULT,
    SOURCE_USER,
    Resolution,
    ResolutionLog,
    confidence_of,
    meta_lookup,
    non_negative_float,
    positive_float,
    rank_of,
)

_ORDER = [
    SOURCE_USER,
    SOURCE_PARSED,
    SOURCE_DERIVED,
    SOURCE_TYPE_DEFAULT,
    SOURCE_GLOBAL_DEFAULT,
]


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


def test_ranks_are_strictly_ordered_best_first():
    ranks = [rank_of(s) for s in _ORDER]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)


def test_confidence_decreases_monotonically_down_the_ladder():
    confidences = [confidence_of(s) for s in _ORDER]
    assert confidences == sorted(confidences, reverse=True)


def test_unknown_source_is_treated_as_the_worst_case():
    """An unrecognised source must never look better than a known bad one."""
    assert rank_of("something_new") == rank_of(SOURCE_GLOBAL_DEFAULT)
    assert confidence_of("something_new") == confidence_of(SOURCE_GLOBAL_DEFAULT)


def test_assumption_and_blind_thresholds_split_informed_from_guessed():
    informed = Resolution("h", 900, SOURCE_TYPE_DEFAULT, "R-X-04")
    guessed = Resolution("h", 900, SOURCE_GLOBAL_DEFAULT, "R-X-05")
    measured = Resolution("h", 900, SOURCE_PARSED, "R-X-02")

    assert informed.is_assumption and not informed.is_blind
    assert guessed.is_assumption and guessed.is_blind
    assert not measured.is_assumption and not measured.is_blind
    assert ASSUMPTION_RANK < BLIND_RANK


# ---------------------------------------------------------------------------
# Model confidence
# ---------------------------------------------------------------------------


def test_model_confidence_is_the_minimum_not_the_mean():
    """
    One blind guess invalidates the model regardless of how well everything else
    resolved. Averaging would hide exactly the case that matters.
    """
    log = ResolutionLog()
    for _ in range(9):
        log.record(Resolution("a", 1, SOURCE_USER, "R-A-01"))
    log.record(Resolution("b", 2, SOURCE_GLOBAL_DEFAULT, "R-B-05"))

    assert log.model_confidence == pytest.approx(confidence_of(SOURCE_GLOBAL_DEFAULT))
    assert log.model_confidence < 0.5  # a mean would have been ~0.92


def test_empty_log_is_fully_confident():
    assert ResolutionLog().model_confidence == 1.0
    assert ResolutionLog().has_assumptions is False
    assert ResolutionLog().worst() is None


def test_worst_reports_the_weakest_entry():
    log = ResolutionLog()
    log.record(Resolution("a", 1, SOURCE_USER, "R-A-01"))
    log.record(Resolution("ceiling_height_mm", 3000, SOURCE_GLOBAL_DEFAULT, "R-CEIL-05"))
    worst = log.worst()
    assert worst.field == "ceiling_height_mm"
    assert worst.rule_id == "R-CEIL-05"


def test_blind_fields_are_listed_and_deduplicated():
    log = ResolutionLog()
    log.record(Resolution("ceiling_height_mm", 3000, SOURCE_GLOBAL_DEFAULT, "R-CEIL-05"))
    log.record(Resolution("ceiling_height_mm", 3000, SOURCE_GLOBAL_DEFAULT, "R-CEIL-05"))
    log.record(Resolution("h", 900, SOURCE_TYPE_DEFAULT, "R-FXH-04"))
    assert log.blind_fields() == ["ceiling_height_mm"]
    assert log.has_blind_defaults is True


def test_summary_carries_everything_a_caller_needs():
    log = ResolutionLog()
    log.record(Resolution("a", 1, SOURCE_PARSED, "R-A-02"))
    log.record(Resolution("b", 2, SOURCE_TYPE_DEFAULT, "R-B-04"))
    s = log.summary()
    assert set(s) == {
        "model_confidence",
        "has_assumptions",
        "has_blind_defaults",
        "blind_fields",
        "counts_by_source",
        "weakest",
        "total",
    }
    assert s["total"] == 2
    assert s["counts_by_source"] == {SOURCE_PARSED: 1, SOURCE_TYPE_DEFAULT: 1}


def test_log_is_ordered_and_serialisable():
    log = ResolutionLog()
    log.record(Resolution("a", 1, SOURCE_USER, "R-A-01", "fixture[0]"))
    log.record(Resolution("b", 2, SOURCE_PARSED, "R-B-02"))
    rows = log.as_list()
    assert [r["field"] for r in rows] == ["a", "b"]
    assert rows[0]["subject"] == "fixture[0]"
    assert rows[0]["confidence"] == 1.0


# ---------------------------------------------------------------------------
# Coercion helpers — these sit on the hot path for untrusted spreadsheet values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (100, 100.0),
        ("250.5", 250.5),
        (0, None),
        (-5, None),
        (None, None),
        ("tall", None),
        ("", None),
        (float("nan"), None),
        (True, None),  # bools are ints in Python; never a dimension
    ],
)
def test_positive_float_coercion(raw, expected):
    assert positive_float(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [(0, 0.0), (1200, 1200.0), (-1, None), ("high", None), (None, None), (True, None)],
)
def test_non_negative_float_coercion(raw, expected):
    assert non_negative_float(raw) == expected


def test_meta_lookup_honours_key_order():
    meta = {"t": {"label": 1, "kind": 2, "*": 3}}
    assert meta_lookup(meta, "t", "label", "kind", "*") == 1
    assert meta_lookup(meta, "t", "missing", "kind", "*") == 2
    assert meta_lookup(meta, "t", "missing", "nope", "*") == 3


def test_meta_lookup_tolerates_missing_or_malformed_tables():
    assert meta_lookup(None, "t", "a") is None
    assert meta_lookup({}, "t", "a") is None
    assert meta_lookup({"t": "not a dict"}, "t", "a") is None
    assert meta_lookup({"t": {}}, "t", "") is None
