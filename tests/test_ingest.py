"""OCR / PDF ingestion tests using generated images and PDFs."""
import shutil

import pytest
from PIL import Image, ImageDraw, ImageFont

from opencrawl.ingest import image_to_record, lines_to_markdown, pdf_to_record

needs_tesseract = pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")


def _font(size):
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _screenshot(lines, dark=False):
    bg, fg = ((20, 20, 20), (235, 235, 235)) if dark else ((255, 255, 255), (0, 0, 0))
    img = Image.new("RGB", (900, 60 + 50 * len(lines)), bg)
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        d.text((30, 30 + 50 * i), line, fill=fg, font=_font(28))
    return img


LINES = ["Sec. 6-12. Noise after midnight.", "No amplified sound is permitted", "in residential districts after 12 a.m."]


@needs_tesseract
@pytest.mark.parametrize("dark", [False, True])
def test_screenshot_ocr(tmp_path, dark):
    p = tmp_path / "shot.png"
    _screenshot(LINES, dark).save(p)
    rec = image_to_record(p, url="https://example.gov/x")
    assert "amplified sound" in rec.text
    assert rec.meta["mean_confidence"] > 60
    assert len(rec.meta["sha256"]) == 64


def test_markdown_shaping():
    lines = [
        {"block": 1, "par": 1, "text": "ARTICLE IV", "conf": 95, "top": 0},
        {"block": 1, "par": 2, "text": "Sec. 2-14. Quorum.", "conf": 95, "top": 0},
        {"block": 1, "par": 3, "text": "A majority of the coun-", "conf": 95, "top": 0},
        {"block": 1, "par": 3, "text": "cil is a quorum.", "conf": 95, "top": 0},
        {"block": 1, "par": 4, "text": "(a) Exceptions apply.", "conf": 95, "top": 0},
    ]
    text, md = lines_to_markdown(lines)
    assert "council is a quorum" in text
    assert "### ARTICLE IV" in md and "### Sec. 2-14. Quorum." in md and "- (a) Exceptions apply." in md


@needs_tesseract
def test_pdf_text_and_scan(tmp_path):
    # page 1: real text layer; page 2: image only (scan) -> OCR
    scan = _screenshot(LINES).convert("RGB")
    try:
        from reportlab.pdfgen import canvas  # optional; use if available
    except ImportError:
        canvas = None
    out = tmp_path / "mixed.pdf"
    if canvas:
        c = canvas.Canvas(str(out))
        c.drawString(72, 720, "ORDINANCE NO. 2026-07")
        c.drawString(72, 700, "An ordinance amending Chapter 6 regarding noise in residential areas.")
        c.showPage()
        scan_path = tmp_path / "scan.png"
        scan.save(scan_path)
        c.drawImage(str(scan_path), 50, 400, width=500, height=200)
        c.showPage()
        c.save()
        rec = pdf_to_record(out)
        assert rec.meta["pages"] == 2 and rec.meta["ocr_pages"] == [2]
        assert "ORDINANCE NO. 2026-07" in rec.markdown and "amplified sound" in rec.text
    else:
        scan.save(out, "PDF", resolution=150)
        rec = pdf_to_record(out)
        assert rec.meta["ocr_pages"] == [1] and "amplified sound" in rec.text
