"""Unified layout JSON schema — all parsers normalize toward this shape."""

from typing import Any

from pydantic import BaseModel, Field


class WallSegment(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float


class Opening(BaseModel):
    kind: str = "door"
    x1: float = 0.0
    y1: float = 0.0
    x2: float = 0.0
    y2: float = 0.0
    label: str | None = None


class Fixture(BaseModel):
    kind: str = "generic"
    x: float = 0.0
    y: float = 0.0
    label: str | None = None
    # Footprint in layout units (mm when units=="mm"); center at (x, y).
    width_mm: float | None = None
    depth_mm: float | None = None
    height_mm: float | None = None
    rotation_deg: float = 0.0


class Room(BaseModel):
    name: str = ""
    area: float | None = None
    polygon: list[list[float]] = Field(default_factory=list)


class UnifiedLayout(BaseModel):
    units: str = "mm"
    walls: list[WallSegment] = Field(default_factory=list)
    rooms: list[Room] = Field(default_factory=list)
    fixtures: list[Fixture] = Field(default_factory=list)
    openings: list[Opening] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    def model_dump_json_schema_compatible(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
