"""Floor-plan images → merged wall segments (Hough + Phase 1), contour rooms (Phase 2), OCR labels."""

from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytesseract
from PIL import Image

from app.core.config import get_settings
from app.services.image_processing_utils import detect_rooms_from_contours, merge_hough_segments


def parse_image(path: Path | str, *, max_lines: int = 400) -> dict[str, Any]:
    settings = get_settings()
    if settings.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd

    p = Path(path)
    img_bgr = cv2.imread(str(p))
    if img_bgr is None:
        raise ValueError(f"Could not read image: {p}")

    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blur, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(
        edges,
        rho=1,
        theta=np.pi / 180,
        threshold=80,
        minLineLength=30,
        maxLineGap=12,
    )

    walls_raw: list[dict[str, float]] = []
    if lines is not None:
        for ln in lines[:max_lines]:
            x1, y1, x2, y2 = ln[0]
            walls_raw.append({"x1": float(x1), "y1": float(y1), "x2": float(x2), "y2": float(y2)})

    walls = merge_hough_segments(walls_raw)

    pil = Image.open(p)
    try:
        ocr_text = pytesseract.image_to_string(pil)
    except Exception as exc:
        ocr_text = ""
        ocr_meta = {"error": str(exc)}
    else:
        ocr_meta = {}

    data = pytesseract.image_to_data(pil, output_type=pytesseract.Output.DICT)
    labels: list[dict[str, Any]] = []
    n = len(data.get("text", []))
    for i in range(n):
        t = (data["text"][i] or "").strip()
        if not t:
            continue
        try:
            conf = float(data["conf"][i])
        except (ValueError, KeyError):
            conf = -1.0
        if conf < 30:
            continue
        labels.append(
            {
                "text": t,
                "x": float(data["left"][i]),
                "y": float(data["top"][i]),
                "w": float(data["width"][i]),
                "h": float(data["height"][i]),
                "conf": conf,
            }
        )

    rooms: list[dict[str, Any]] = []
    try:
        rooms = detect_rooms_from_contours(blur, labels)
    except Exception:
        rooms = []

    ih, iw = img_bgr.shape[:2]
    return {
        "source": "image",
        "units": "px",
        "walls": walls,
        "rooms": rooms,
        "labels": labels,
        "ocr_full_text": ocr_text,
        "meta": {
            "shape": list(img_bgr.shape),
            "image_width": iw,
            "image_height": ih,
            **ocr_meta,
        },
    }
