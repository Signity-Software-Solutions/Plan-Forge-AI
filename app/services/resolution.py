"""
Deterministic resolution of values the layout does not carry.

``UnifiedLayout`` has no ceiling height, no wall thickness and no fixture
elevation, and fixture height is often absent. Those values have to come from
somewhere, and the honest answer is that some of them are assumptions. This
module is the single place where that happens, so that an assumption is always
recorded rather than buried in whichever builder happened to need it.

The ladder
    Every governed field resolves through one fixed precedence. First match
    wins, always:

        rank  source            origin                              confidence
        ----  ---------------   ---------------------------------   ----------
         1    user_supplied     layout.meta[<field>] override           1.00
         2    parsed            value present in normalized_json        0.90
         3    derived           computed from other known values        0.70
         4    type_default      per-fixture-type table                  0.40
         5    global_default    app/core/config.py                      0.20

Invariants
    * **Deterministic** — same input yields the same resolutions, which is what
      makes the GLB vertex hash (and therefore the response ETag) stable.
    * **Total** — every governed field always resolves. ``global_default`` is
      the floor; there is no unset state.
    * **Auditable** — every call appends to a ``ResolutionLog`` that is served
      verbatim alongside the model.
    * **Honest** — the worst link governs the model, not the average. A single
      *blind* (rank 5) default marks it ``not_for_construction``; rank 4 is an
      informed default and only lowers confidence. An assumed ceiling height is
      undetectable downstream, which is exactly why it must be visible here.
    * **Overridable** — rank 1 always wins, so correcting a value is a ``meta``
      edit rather than a code change.

Rules are data, not branches: each is a row keyed by ``(field, type)`` with an
id like ``R-CEIL-05``. Adding a default is a reviewable diff in a table.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field as dc_field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------

SOURCE_USER = "user_supplied"
SOURCE_PARSED = "parsed"
SOURCE_DERIVED = "derived"
SOURCE_TYPE_DEFAULT = "type_default"
SOURCE_GLOBAL_DEFAULT = "global_default"

# rank -> (source, confidence). Rank 1 is best.
_LADDER: dict[str, tuple[int, float]] = {
    SOURCE_USER: (1, 1.00),
    SOURCE_PARSED: (2, 0.90),
    SOURCE_DERIVED: (3, 0.70),
    SOURCE_TYPE_DEFAULT: (4, 0.40),
    SOURCE_GLOBAL_DEFAULT: (5, 0.20),
}

# At or below this rank the value is an assumption rather than a measurement.
ASSUMPTION_RANK = 4

# Rank 5 is a *blind* default: nothing about this layout informed it. Rank 4 is
# an informed default — the fixture's own type supplied it. Only the blind kind
# invalidates the model for construction, otherwise the badge would be lit on
# every model ever built and would stop meaning anything.
BLIND_RANK = 5


def rank_of(source: str) -> int:
    return _LADDER.get(source, (5, 0.20))[0]


def confidence_of(source: str) -> float:
    return _LADDER.get(source, (5, 0.20))[1]


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    """One resolved value plus the rule that produced it."""

    field: str
    value: Any
    source: str
    rule_id: str
    subject: str = ""  # e.g. "fixture[3]" — blank for layout-wide fields

    @property
    def rank(self) -> int:
        return rank_of(self.source)

    @property
    def confidence(self) -> float:
        return confidence_of(self.source)

    @property
    def is_assumption(self) -> bool:
        return self.rank >= ASSUMPTION_RANK

    @property
    def is_blind(self) -> bool:
        """Nothing about this layout informed the value — a pure guess."""
        return self.rank >= BLIND_RANK

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "value": self.value,
            "source": self.source,
            "rule_id": self.rule_id,
            "subject": self.subject,
            "confidence": self.confidence,
        }


@dataclass
class ResolutionLog:
    """Every resolution made while building one model."""

    entries: list[Resolution] = dc_field(default_factory=list)

    def record(self, r: Resolution) -> Resolution:
        self.entries.append(r)
        return r

    @property
    def model_confidence(self) -> float:
        """
        The **minimum** confidence across all resolutions, not the mean.

        One assumed ceiling height invalidates the model's Z axis however well
        everything else resolved, so averaging would be dishonest.
        """
        if not self.entries:
            return 1.0
        return min(e.confidence for e in self.entries)

    @property
    def has_assumptions(self) -> bool:
        return any(e.is_assumption for e in self.entries)

    @property
    def has_blind_defaults(self) -> bool:
        return any(e.is_blind for e in self.entries)

    def blind_fields(self) -> list[str]:
        return sorted({e.field for e in self.entries if e.is_blind})

    def worst(self) -> Resolution | None:
        if not self.entries:
            return None
        return min(self.entries, key=lambda e: (e.confidence, e.field))

    def by_source(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.entries:
            out[e.source] = out.get(e.source, 0) + 1
        return out

    def as_list(self) -> list[dict[str, Any]]:
        return [e.as_dict() for e in self.entries]

    def summary(self) -> dict[str, Any]:
        worst = self.worst()
        return {
            "model_confidence": self.model_confidence,
            "has_assumptions": self.has_assumptions,
            "has_blind_defaults": self.has_blind_defaults,
            "blind_fields": self.blind_fields(),
            "counts_by_source": self.by_source(),
            "weakest": worst.as_dict() if worst else None,
            "total": len(self.entries),
        }


# ---------------------------------------------------------------------------
# Helpers used by the rule tables
# ---------------------------------------------------------------------------


def positive_float(raw: Any) -> float | None:
    """Coerce to a positive float, or None. Never raises."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0 or value != value:  # NaN check
        return None
    return value


def non_negative_float(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value < 0 or value != value:
        return None
    return value


def meta_lookup(meta: dict[str, Any] | None, table: str, *keys: str) -> Any:
    """
    Look a value up in a ``meta`` sub-table by the first matching key.

    Used for per-fixture overrides such as
    ``meta.fixture_elevations_mm = {"<label>": 1200, "display": 900, "*": 0}``
    where precedence within the table is label, then kind, then wildcard.
    """
    sub = (meta or {}).get(table)
    if not isinstance(sub, dict):
        return None
    for key in keys:
        if key and key in sub:
            return sub[key]
    return None
