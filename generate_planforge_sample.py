#!/usr/bin/env python3
"""
Run:  pip install openpyxl && python generate_planforge_sample.py
Output: planforge_sample.xlsx in cwd (POST /upload type=sheet).

Tracked CSV equivalent for pipeline tests: examples/planforge_sample.csv
(copy to samples/planforge_sample.csv if your .env points there.)
"""

from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# ── colour palette ──────────────────────────────────────────────────────────
BLACK = "FF000000"
WHITE = "FFFFFFFF"
LGRAY = "FFD9D9D9"
DGRAY = "FF595959"
YELLOW = "FFFFFF00"
CYAN = "FF00B0F0"
ORANGE = "FFFF6600"


def fill(hex_rgb):
    return PatternFill("solid", fgColor=hex_rgb)


def bold(size=9, color=BLACK, italic=False):
    return Font(name="Arial", size=size, bold=True, color=color, italic=italic)


def reg(size=8, color=BLACK):
    return Font(name="Arial", size=size, color=color)


def border(style="thin"):
    s = Side(style=style)
    return Border(left=s, right=s, top=s, bottom=s)


def center():
    return Alignment(horizontal="center", vertical="center", wrap_text=True)


def left():
    return Alignment(horizontal="left", vertical="center", wrap_text=True)


# ── workbook setup ───────────────────────────────────────────────────────────
wb = Workbook()

# ════════════════════════════════════════════════════════════════════════════
# SHEET 1 — PROJECT METADATA
# ════════════════════════════════════════════════════════════════════════════
ws_meta = wb.active
ws_meta.title = "ProjectMeta"

ws_meta.column_dimensions["A"].width = 28
ws_meta.column_dimensions["B"].width = 40

meta_rows = [
    ("FIELD", "VALUE"),
    ("project_name", "Demo Retail Store"),
    ("client", "Demo Retail Pvt. Ltd."),
    (
        "site_address",
        "UGS Shop No. 5, K Mall, Railway Station Main, Pali Rd, Near Ki Kothi, Heavy Industrial Area Phase 1, Basni, Jodhpur, Rajasthan 342003",
    ),
    ("interior_architect", "Demo Interior Studio"),
    ("drawing_title", "Furniture Layout"),
    ("drawing_number", "DP-01 / 101"),
    ("scale", "1:40"),
    ("units", "mm"),
    ("store_area_sft", 591),
    ("frontage_width", '11\'-0"'),
    ("existing_rcc_slab_ht_mm", 4450),
    ("proposed_retail_area_sft", 502),
    ("proposed_l2_svc_area", "N/A"),
    ("proposed_stock_room_sft", 83),
    ("proposed_staff_room", "N/A"),
    ("overall_width_mm", 12090),
    ("overall_depth_mm", 3740),
    ("revision", "A"),
    ("date", "16-05-2025"),
    ("status", "LAYOUT FOR APPROVAL"),
]

for r, (field, val) in enumerate(meta_rows, start=1):
    ws_meta.row_dimensions[r].height = 18
    c_a = ws_meta.cell(r, 1, field)
    c_b = ws_meta.cell(r, 2, val)
    if r == 1:
        for c in (c_a, c_b):
            c.fill = fill(DGRAY)
            c.font = bold(9, WHITE)
            c.alignment = center()
    else:
        c_a.font = bold(8)
        c_a.alignment = left()
        c_b.font = reg(8)
        c_b.alignment = left()
    for c in (c_a, c_b):
        c.border = border()


# ════════════════════════════════════════════════════════════════════════════
# SHEET 2 — ROOMS / ZONES
# ════════════════════════════════════════════════════════════════════════════
ws_rooms = wb.create_sheet("Rooms")

ws_rooms.column_dimensions["A"].width = 6
for col, w in zip("BCDEFGHIJ", [22, 10, 10, 10, 10, 10, 10, 10, 18]):
    ws_rooms.column_dimensions[col].width = w

room_headers = [
    "room_id",
    "room_name",
    "area_sft",
    "area_mm2",
    "origin_x_mm",
    "origin_y_mm",
    "width_mm",
    "depth_mm",
    "notes",
]

# header row
for c, h in enumerate(room_headers, 2):
    cell = ws_rooms.cell(1, c, h)
    cell.fill = fill(DGRAY)
    cell.font = bold(9, WHITE)
    cell.alignment = center()
    cell.border = border()

rooms = [
    # id  name               sft  mm2        ox    oy    w     d     notes
    ("R01", "RETAIL ROOM", 502, 46_600_000, 280, 0, 9810, 3740, "Main sales floor"),
    ("R02", "STORE ROOM", 83, 7_700_000, 0, 870, 280, 2870, "Stock / back-of-house"),
    ("R03", "ENTRY LOBBY", 0, 0, 280, 3440, 280, 300, "Store entry zone"),
]

for r, row in enumerate(rooms, start=2):
    ws_rooms.row_dimensions[r].height = 18
    for c, val in enumerate(row, 2):
        cell = ws_rooms.cell(r, c, val)
        cell.font = reg(8)
        cell.alignment = center()
        cell.border = border()
        if c == 3:  # area_sft
            cell.font = bold(8, "FF0000FF")


# ════════════════════════════════════════════════════════════════════════════
# SHEET 3 — WALLS (line segments, origin = bottom-left)
# ════════════════════════════════════════════════════════════════════════════
ws_walls = wb.create_sheet("Walls")

wall_headers = [
    "wall_id",
    "label",
    "x1_mm",
    "y1_mm",
    "x2_mm",
    "y2_mm",
    "thickness_mm",
    "layer",
    "notes",
]
for letter, w in zip("ABCDEFGHI", [8, 22, 10, 10, 10, 10, 10, 14, 20]):
    ws_walls.column_dimensions[letter].width = w

for c, h in enumerate(wall_headers, 1):
    cell = ws_walls.cell(1, c, h)
    cell.fill = fill(DGRAY)
    cell.font = bold(9, WHITE)
    cell.alignment = center()
    cell.border = border()

walls = [
    # id     label           x1    y1    x2     y2    thk  layer          notes
    ("W01", "Perimeter-Top", 0, 0, 12090, 0, 150, "WALL_EXT", "Top boundary"),
    ("W02", "Perimeter-Bot", 0, 3740, 12090, 3740, 150, "WALL_EXT", "Bottom / frontage"),
    ("W03", "Perimeter-Left", 0, 0, 0, 3740, 150, "WALL_EXT", "Left boundary"),
    ("W04", "Perimeter-Right", 12090, 0, 12090, 3740, 150, "WALL_EXT", "Right boundary"),
    ("W05", "StoreRoom-Sep", 280, 870, 280, 3740, 100, "WALL_INT", "Divides store from retail"),
    ("W06", "BrandWall-75in", 280, 870, 280, 2200, 100, "WALL_INT", "Brand wall 75-inch LFD"),
    ("W07", "Rear-Fascia", 0, 0, 12090, 150, 150, "FASCIA", "Rear top fascia"),
]

for r, row in enumerate(walls, start=2):
    ws_walls.row_dimensions[r].height = 18
    for c, val in enumerate(row, 1):
        cell = ws_walls.cell(r, c, val)
        cell.font = reg(8)
        cell.alignment = center()
        cell.border = border()


# ════════════════════════════════════════════════════════════════════════════
# SHEET 4 — FIXTURES & FURNITURE
# ════════════════════════════════════════════════════════════════════════════
ws_fix = wb.create_sheet("Fixtures")

fix_headers = [
    "fixture_id",
    "kind",
    "label",
    "model_ref",
    "cx_mm",
    "cy_mm",
    "width_mm",
    "depth_mm",
    "height_mm",
    "rotation_deg",
    "zone",
    "notes",
]
for col_i, w in enumerate([8, 14, 22, 22, 10, 10, 10, 10, 10, 8, 14, 22], 1):
    ws_fix.column_dimensions[get_column_letter(col_i)].width = w

for c, h in enumerate(fix_headers, 1):
    cell = ws_fix.cell(1, c, h)
    cell.fill = fill(DGRAY)
    cell.font = bold(9, WHITE)
    cell.alignment = center()
    cell.border = border()

fixtures = [
    # id      kind         label                   model_ref           cx     cy    w     d    ht   rot  zone           notes
    ("F01", "display", '65" Smart Signage', "VW-65-STD", 1821, 150, 1040, 600, 150, 0, "REAR_WALL", "Top-wall display"),
    ("F02", "display", "Tablets Demo Bar", "—", 3124, 150, 1040, 500, 900, 0, "REAR_WALL", "Tablet countertop"),
    ("F03", "display", '55" Neo OLED', "—", 5334, 150, 1040, 500, 900, 0, "REAR_WALL", ""),
    ("F04", "display", "Smart Thing Hub", "—", 8644, 150, 800, 500, 900, 0, "REAR_WALL", ""),
    ("F05", "display", '55" LFD (top-right)', "VW-55-STD", 11360, 150, 730, 300, 150, 0, "REAR_WALL", "Corner LFD"),
    ("F06", "display", '55" LFD (mid-right)', "VW-55-STD", 11360, 1600, 730, 300, 150, 0, "REAR_WALL", ""),
    ("F07", "display", '55" Smart Signage', "VW-55-STD", 7660, 3200, 730, 300, 150, 0, "FRONT_WALL", "Lower front signage"),
    ("F08", "table", "NAR-ST-1.8M (Centre)", "NAR-ST-1.8M", 5580, 1870, 1800, 900, 750, 0, "RETAIL", "Central demo table"),
    ("F09", "table", "NAR-ST-1.8M (Right)", "NAR-ST-1.8M", 8790, 1870, 1800, 900, 750, 0, "RETAIL", ""),
    ("F10", "counter", "CS-2P Cashier", "CS-2P", 430, 1200, 400, 600, 900, 0, "STORE_ROOM", "Cashier counter"),
    ("F11", "counter", "TT-2P Trial Table", "TT-2P", 430, 3100, 400, 600, 900, 0, "RETAIL", ""),
    ("F12", "shelving", "Accessories Bar", "—", 5120, 3580, 2210, 300, 900, 0, "FRONT_WALL", "Red-lit accessories"),
    ("F13", "shelving", "Watch Wall", "—", 8430, 3580, 1870, 300, 900, 0, "FRONT_WALL", "Watch display"),
    ("F14", "display", 'Brand Wall 75" LFD', "VW-75-STD", 280, 1535, 300, 75, 1650, 0, "BRAND_WALL", "Vertical brand LFD"),
    ("F15", "chair", "Chair (Cashier area)", "—", 340, 960, 450, 450, 450, 0, "STORE_ROOM", ""),
    ("F16", "chair", "Chair (Trial area)", "—", 340, 2760, 450, 450, 450, 0, "RETAIL", ""),
]

for r, row in enumerate(fixtures, start=2):
    ws_fix.row_dimensions[r].height = 18
    for c, val in enumerate(row, 1):
        cell = ws_fix.cell(r, c, val)
        cell.font = reg(8)
        cell.alignment = center()
        cell.border = border()
        if c == 3:  # label highlight
            cell.font = bold(8, "FF0000AA")


# ════════════════════════════════════════════════════════════════════════════
# SHEET 5 — DIMENSION STRINGS (matches red callouts in drawing)
# ════════════════════════════════════════════════════════════════════════════
ws_dim = wb.create_sheet("Dimensions")

dim_headers = ["dim_id", "axis", "from_ref", "to_ref", "value_mm", "color_in_dwg", "notes"]
for col_i, w in enumerate([8, 8, 22, 22, 12, 14, 28], 1):
    ws_dim.column_dimensions[get_column_letter(col_i)].width = w

for c, h in enumerate(dim_headers, 1):
    cell = ws_dim.cell(1, c, h)
    cell.fill = fill(DGRAY)
    cell.font = bold(9, WHITE)
    cell.alignment = center()
    cell.border = border()

dims = [
    # id      ax  from                  to                 val    color    notes
    ("D01", "X", "Left wall", "Col-1 grid", 1821, "BLACK", "Top dim string"),
    ("D02", "X", "Col-1", "Col-2", 3124, "BLACK", ""),
    ("D03", "X", "Col-2", "Smart Sig L", 150, "BLACK", "Gap"),
    ("D04", "X", "Smart Sig centre", "Smart Sig R", 2210, "RED", "Red = key module dim"),
    ("D05", "X", "Smart Sig R", "Gap", 150, "BLACK", ""),
    ("D06", "X", "Gap", "Watch Wall L", 3310, "RED", ""),
    ("D07", "X", "Watch Wall R", "Gap", 150, "BLACK", ""),
    ("D08", "X", "Gap", "Right wall", 1325, "BLACK", ""),
    ("D09", "X", "Overall width top", "—", 12090, "BLACK", "Total overall"),
    ("D10", "X", "Bottom: Left wall", "Col-1 bot", 1046, "BLACK", "Bottom dim string"),
    ("D11", "X", "—", "—", 97, "BLACK", ""),
    ("D12", "X", "—", "—", 26, "BLACK", ""),
    ("D13", "X", "—", "—", 1102, "BLACK", ""),
    ("D14", "X", "—", "—", 800, "BLACK", ""),
    ("D15", "X", "—", "—", 1100, "BLACK", ""),
    ("D16", "X", "Gap", "—", 150, "BLACK", ""),
    ("D17", "X", "Module", "—", 2210, "RED", ""),
    ("D18", "X", "Gap", "—", 150, "BLACK", ""),
    ("D19", "X", "Module", "—", 3310, "RED", ""),
    ("D20", "X", "Gap", "—", 150, "BLACK", ""),
    ("D21", "X", "End", "Right wall", 1350, "BLACK", ""),
    ("D22", "X", "Overall width bot", "—", 10140, "BLACK", "Inner clear width"),
    ("D23", "Y", "Top wall", "Retail floor", 3740, "BLACK", "Overall depth"),
    ("D24", "Y", "Top wall", "Rear display ht", 3200, "BLACK", "Right side"),
    ("D25", "Y", "Store room top", "Store room bot", 870, "BLACK", ""),
    ("D26", "Y", "Entry zone", "—", 300, "BLACK", "Implied entry depth"),
]

for r, row in enumerate(dims, start=2):
    ws_dim.row_dimensions[r].height = 18
    for c, val in enumerate(row, 1):
        cell = ws_dim.cell(r, c, val)
        cell.font = reg(8)
        cell.alignment = center()
        cell.border = border()
        if c == 6 and val == "RED":
            cell.font = bold(8, "FFFF0000")


# ════════════════════════════════════════════════════════════════════════════
# SHEET 6 — LFD MODEL SPEC TABLE (vendor callout box at bottom of drawing)
# ════════════════════════════════════════════════════════════════════════════
ws_lfd = wb.create_sheet("LFD_Models")

lfd_headers = [
    "type",
    "diagonal_inch",
    "model_number",
    "set_W_mm",
    "set_H_mm",
    "set_D_mm",
    "niche_W_mm",
    "niche_H_mm",
    "niche_D_mm",
]
for col_i, w in enumerate([14, 10, 24, 10, 10, 10, 10, 10, 10], 1):
    ws_lfd.column_dimensions[get_column_letter(col_i)].width = w

for c, h in enumerate(lfd_headers, 1):
    cell = ws_lfd.cell(1, c, h)
    cell.fill = fill(DGRAY)
    cell.font = bold(9, WHITE)
    cell.alignment = center()
    cell.border = border()

lfd_data = [
    # type          diag  model                set_W   set_H  set_D  niche_W niche_H niche_D
    ("Video Wall-LFD", 46, "VW-46-STD", 1022, 577, 93, 1040, 600, 150),
    ("Video Wall-LFD", 55, "VW-55-STD", 1213, 684, 74, 1235, 705, 150),
    ("Video Wall-LFD", 55, "VW-55-STD", 1213, 684, 74, 1235, 705, 150),
    ("SSB Box", 28, "SSB-G080NL1XL", None, None, None, None, None, None),
    ("Slim-LFD", 55, "SL-55-STD", 788, 1237, 29, 730, 1260, 110),
    ("Slim-LFD", 65, "SL-65-STD", 831, 1456, 30, 860, 1475, 110),
    ("Slim-LFD", 75, "SL-75-STD", 960, 1662, 30, 980, 1705, 110),
    ("Slim-LFD", 85, "SL-85-STD", 1085, 1904, 29, 1110, 1925, 110),
    ("Slim-LFD", 98, "SL-98-STD", 1250, 2195, 96, 1270, 2220, 175),
]

for r, row in enumerate(lfd_data, start=2):
    ws_lfd.row_dimensions[r].height = 18
    for c, val in enumerate(row, 1):
        display = "" if val is None else val
        cell = ws_lfd.cell(r, c, display)
        cell.font = reg(8)
        cell.alignment = center()
        cell.border = border()
    # colour-code by type
    t = row[0]
    color = "FFD9EAD3" if "Video" in t else ("FFCFE2F3" if "Slim" in t else LGRAY)
    for c in range(1, 10):
        ws_lfd.cell(r, c).fill = fill(color)


# ════════════════════════════════════════════════════════════════════════════
# SHEET 7 — OPENINGS (doors, pass-throughs)
# ════════════════════════════════════════════════════════════════════════════
ws_open = wb.create_sheet("Openings")

open_headers = ["opening_id", "type", "label", "x1_mm", "y1_mm", "x2_mm", "y2_mm", "width_mm", "notes"]
for col_i, w in enumerate([8, 10, 20, 10, 10, 10, 10, 10, 24], 1):
    ws_open.column_dimensions[get_column_letter(col_i)].width = w

for c, h in enumerate(open_headers, 1):
    cell = ws_open.cell(1, c, h)
    cell.fill = fill(DGRAY)
    cell.font = bold(9, WHITE)
    cell.alignment = center()
    cell.border = border()

openings = [
    ("O01", "entry", "STORE ENTRY", 4800, 3740, 6000, 3740, 1200, "Main frontage entry"),
    ("O02", "door", "Store Room Door", 280, 870, 280, 1170, 300, "Internal door to stock room"),
    ("O03", "opening", "110W Aisle (right)", 12090, 870, 12090, 3740, 110, "Side service aisle"),
]

for r, row in enumerate(openings, start=2):
    ws_open.row_dimensions[r].height = 18
    for c, val in enumerate(row, 1):
        cell = ws_open.cell(r, c, val)
        cell.font = reg(8)
        cell.alignment = center()
        cell.border = border()


# ════════════════════════════════════════════════════════════════════════════
# Save
# ════════════════════════════════════════════════════════════════════════════
out = "planforge_sample.xlsx"
wb.save(out)
print(f"✅  Saved → {out}")
print("Sheets:", [s.title for s in wb.worksheets])
print(
    """
Next steps
──────────
1. pip install openpyxl
2. python generate_planforge_sample.py
3. curl -X POST http://localhost:8000/upload \\
        -F "file=@planforge_sample.xlsx" \\
        -F "type=sheet" \\
        -F "project_name=Demo Retail Store"
"""
)
