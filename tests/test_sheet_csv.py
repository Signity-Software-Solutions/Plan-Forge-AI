"""CSV / ZIP bundle parsing for type=sheet uploads."""

import zipfile
from pathlib import Path

import pytest

from app.services.sheet_parser import parse_csv, parse_csv_zip_bundle, parse_sheet


def test_parse_sheet_accepts_csv_unified(tmp_path: Path):
    csv_text = """sheet,FIELD,VALUE,room_id,room_name,area_sft,origin_x_mm,origin_y_mm,width_mm,depth_mm
ProjectMeta,project_name,X,,,,,,,
Rooms,,,R01,ROOM1,100,0,0,50,40
"""
    p = tmp_path / "bundle.csv"
    p.write_text(csv_text, encoding="utf-8")
    out = parse_csv(p)
    assert out["meta"]["sheet_bundle_format"] == "csv_unified"
    assert len(out["rooms"]) == 1
    assert out["meta"]["projectmeta"].get("project_name") == "X"


def test_parse_sheet_accepts_zip_of_csvs(tmp_path: Path):
    zpath = tmp_path / "plan.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr(
            "Fixtures.csv",
            "fixture_id,kind,label,cx_mm,cy_mm,width_mm,depth_mm\nF1,generic,L,10,20,4,4\n",
        )
    out = parse_csv_zip_bundle(zpath)
    assert out["meta"]["sheet_bundle_format"] == "csv_zip_bundle"
    assert len(out["fixtures"]) == 1
    assert out["fixtures"][0]["label"] == "L"


@pytest.mark.skipif(
    not Path("samples/planforge_sample.xlsx").is_file(),
    reason="sample workbook optional",
)
def test_xlsx_still_parses():
    out = parse_sheet(Path("samples/planforge_sample.xlsx"))
    assert out["meta"].get("sheet_bundle_format") == "excel"
    assert len(out["rooms"]) >= 1
