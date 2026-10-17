#!/usr/bin/env python3
"""
comic_convert.py - turn comics (CBZ/CBR) and PDFs into EPUBs, with clearer lettering.

  Inputs:  CBZ, CBR, ZIP, RAR, PDF, or a folder of images
  Outputs: EPUB (fixed layout, one page image per page) - also CBZ/CBR if asked

The artwork is left exactly as it is: pages keep their original size, colour and
shading, and pages with no lettering changes are copied byte-for-byte. Only the
lettering inside speech bubbles and caption boxes is touched:

  Re-lettering (default)  Each bubble's text is read with OCR (Tesseract) and redrawn in a
                          clean comic font (Comic Neue) inside the same bubble, as large as the
                          bubble allows. The lettering's voice is kept: italic narration stays
                          italic, and words drawn heavier (emphasis) stay emphasised.
                          A wrong word on the page is worse than fuzzy lettering, so a bubble is
                          only re-lettered when the text is certain: it is read four ways (two
                          scales, as-is and with italics straightened) and every word must agree,
                          or one reading must be a clearly more common English word (HIM vs AIM);
                          every word must be a real word; and the letters must have the same
                          closed loops as the drawn lettering (D, O, A have them; V, H don't).
                          Otherwise - and whenever the new text wouldn't fit at least as big as
                          the old - the bubble keeps its original lettering, cleaned up instead.
  PDFs                    Pages that are just a scanned image are taken out of the PDF as they
                          are stored (no re-rendering); other pages are rendered at --pdf-dpi.
  Clean-up                Foggy, gray, JPEG-smeared lettering is redrawn in place as solid ink
                          on flat bubble paper, keeping the original letter shapes and ink colour.

The text read from each page is also stored as the page image's alt text, so screen
readers and text-to-speech can read the comic, and listed in a report next to the output
(--report) so you can check it.

Usage:
  python3 comic_convert.py issue.cbz                        # EPUB next to the input
  python3 comic_convert.py book.pdf                         # PDF -> EPUB
  python3 comic_convert.py ~/Comics/*.cbr -o ~/epubs         # choose output folder
  python3 comic_convert.py issue.cbr --lettering clean       # clean-up only, no re-lettering
  python3 comic_convert.py issue.cbr --lettering off         # plain format conversion
  python3 comic_convert.py issue.cbz --report                # also write the OCR text to a .txt
  python3 comic_convert.py manga.cbz --rtl                   # right-to-left reading order
  python3 comic_convert.py issue.cbz --format epub,cbz       # several outputs at once

Requirements:
  pip install pillow numpy opencv-python-headless wordfreq
  Re-lettering:  sudo apt install tesseract-ocr fonts-comic-neue
                 (without wordfreq, any disagreement between readings keeps the original)
                 (any other font: --font Bold.ttf --font-emphasis BoldItalic.ttf)
  Reading .cbr:  sudo apt install libarchive-tools
  Reading .pdf:  pip install pypdfium2   (or poppler's pdftoppm)
  Writing .cbr:  the `rar` program (shareware); CBZ is the better choice
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

try:
    import cv2
    import numpy as np
except ImportError:  # lettering features need these; plain conversion doesn't
    cv2 = np = None

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
DEFAULT_SKIP = r"(?i)^z"  # scan-group credit pages like "zWater.jpg" / "ZZZZZ.jpg"
FORMATS = ("epub", "cbz", "cbr")
TESSERACT = shutil.which("tesseract") or "tesseract"


# --------------------------------------------------------------------------- input

def natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def extract(src: Path, dest: Path) -> Path:
    """Extract an archive into dest (or use a folder as-is); return the root to scan."""
    if src.is_dir():
        return src
    if zipfile.is_zipfile(src):
        with zipfile.ZipFile(src) as z:
            for info in z.infolist():
                name = Path(info.filename)
                if info.is_dir() or name.is_absolute() or ".." in name.parts:
                    continue
                target = dest / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as fi, open(target, "wb") as fo:
                    shutil.copyfileobj(fi, fo)
        return dest
    tool = shutil.which("bsdtar") or shutil.which("7z") or shutil.which("unar")
    if not tool:
        sys.exit("Can't open RAR/CBR files: install one with  sudo apt install libarchive-tools")
    cmd = {"bsdtar": [tool, "-xf", str(src), "-C", str(dest)],
           "7z": [tool, "x", "-y", f"-o{dest}", str(src)],
           "unar": [tool, "-q", "-f", "-o", str(dest), str(src)]}[Path(tool).name]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
    return dest


def _extract_page_image(pdfium, page, stem: Path) -> bool:
    """Comic PDFs are usually one full-page image per page. Pull that image out as stored
    (same pixels, no re-rendering or resampling) when the page is nothing but that image."""
    import pypdfium2.raw as pdfraw
    try:
        objs = list(page.get_objects())
        imgs = [o for o in objs if o.type == pdfraw.FPDF_PAGEOBJ_IMAGE]
        if len(imgs) != 1 or any(o.type == pdfraw.FPDF_PAGEOBJ_TEXT for o in objs):
            return False
        img = imgs[0]
        l, b, r, t = img.get_bounds()
        pw, ph = page.get_size()
        m = img.get_matrix().get()
        if (r - l) * (t - b) < 0.9 * pw * ph or m[1] or m[2] or m[0] <= 0 or m[3] <= 0:
            return False  # not a plain, upright, full-page image: render instead
        img.extract(str(stem))
        return any(stem.parent.glob(stem.name + ".*"))
    except Exception:
        return False


def render_pdf(src: Path, dest: Path, dpi: int) -> dict:
    """Put every PDF page in dest as an image - the embedded scan itself for image-only pages,
    otherwise rendered at `dpi` - and return the PDF's title/author."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        pdfium = None
    if pdfium:
        pdf = pdfium.PdfDocument(str(src))
        meta = {}
        try:
            info = pdf.get_metadata_dict()
            meta = {"title": (info.get("Title") or "").strip(), "writer": (info.get("Author") or "").strip()}
        except Exception:
            pass
        n = len(pdf)
        for i in range(n):
            page = pdf[i]
            if not _extract_page_image(pdfium, page, dest / f"{i + 1:05d}"):
                img = page.render(scale=dpi / 72).to_pil()
                img.save(dest / f"{i + 1:05d}.png")
            print(f"\r  {src.name}: rendering PDF page {i + 1}/{n}", end="", flush=True)
            page.close()
        pdf.close()
        return {k: v for k, v in meta.items() if v}
    tool = shutil.which("pdftoppm")
    if not tool:
        sys.exit("Can't read PDFs: install a renderer with  pip install pypdfium2")
    subprocess.run([tool, "-png", "-r", str(dpi), str(src), str(dest / "p")],
                   check=True)
    return {}


def list_pages(root: Path, skip: re.Pattern | None) -> list[Path]:
    pages = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXT
             and not p.name.startswith(".") and "__MACOSX" not in p.parts]
    if skip:
        pages = [p for p in pages if not skip.search(p.name)]
    return sorted(pages, key=lambda p: [natural_key(x) for x in p.relative_to(root).parts])


def read_comicinfo(root: Path) -> dict:
    """Pull useful fields from ComicInfo.xml (the de-facto comic metadata standard), if present."""
    f = next((p for p in root.rglob("*") if p.name.lower() == "comicinfo.xml"), None)
    if not f:
        return {}
    try:
        tree = ET.parse(f).getroot()
    except ET.ParseError:
        return {}
    get = lambda tag: (tree.findtext(tag) or "").strip()
    return {k: v for k, v in {"title": get("Title"), "series": get("Series"), "number": get("Number"),
                              "writer": get("Writer"), "publisher": get("Publisher"),
                              "summary": get("Summary"), "language": get("LanguageISO"),
                              "manga": get("Manga")}.items() if v}


# ------------------------------------------------------------------ lettering: find bubbles

@dataclass
class Bubble:
    x: int
    y: int
    w: int
    h: int
    interior: np.ndarray          # bool mask (h, w): inside the bubble outline
    ink: np.ndarray               # bool mask (h, w): lettering marks
    bg: tuple                     # median background colour (RGB)
    text_box: tuple = (0, 0, 0, 0)  # x0, y0, x1, y1 of the lettering, bubble-local
    letter_h: float = 0.0         # median letter height in px
    lines: int = 0
    text: str = ""
    conf: float = 0.0
    meta: dict = field(default_factory=dict)


def _letter_stats(holes: np.ndarray):
    n, lab, st, _ = cv2.connectedComponentsWithStats(holes.astype(np.uint8), connectivity=8)
    return n - 1, st[1:]


def find_bubbles(rgb: np.ndarray, scale: float = 0.5) -> list[Bubble]:
    """rgb: HxWx3 uint8 at full resolution. Detection runs at `scale` for speed; masks are
    returned at full resolution."""
    H, W = rgb.shape[:2]
    gray_full = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    small = cv2.resize(gray_full, (int(W * scale), int(H * scale)), interpolation=cv2.INTER_AREA)
    h, w = small.shape
    page = h * w
    found: list[tuple] = []
    for thr in (215, 180):
        bright = (small > thr).astype(np.uint8)
        n, lab, st, _ = cv2.connectedComponentsWithStats(bright, connectivity=4)
        for i in range(1, n):
            x, y, bw, bh, area = st[i]
            if bw < 12 or bh < 10 or bw > 0.7 * w or bh > 0.5 * h:
                continue
            comp = (lab[y:y + bh, x:x + bw] == i).astype(np.uint8)
            # fill holes: flood the outside from a padded border
            pad = cv2.copyMakeBorder(comp, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
            outside = pad.copy()
            cv2.floodFill(outside, None, (0, 0), 2)
            filled = (outside[1:-1, 1:-1] != 2)
            farea = filled.sum()
            if not (0.0006 * page < farea < 0.1 * page):
                continue
            if farea / (bw * bh) < 0.36:  # bubbles/captions are compact (0.36 lets chains of joined bubbles through)
                continue
            holes = filled & (comp == 0)
            hole_frac = holes.sum() / farea
            if not (0.025 < hole_frac < 0.5):
                continue
            nh, hst = _letter_stats(holes)
            if nh < 5:
                continue
            hh = hst[:, 3]
            hw = hst[:, 2]
            # letters: many small marks of similar height
            small_marks = (hh < 0.35 * bh) & (hw < 0.5 * bw)
            if small_marks.sum() < 5 or np.median(hst[:, 4]) > 0.02 * farea:
                continue
            # touching 2+ page edges = background, not a bubble
            if (x == 0) + (y == 0) + (x + bw == w) + (y + bh == h) >= 2:
                continue
            box = (x, y, bw, bh)
            if any(_iou(box, f[0]) > 0.3 for f in found):
                continue
            found.append((box, filled))
    out = []
    inv = 1 / scale
    for (x, y, bw, bh), filled in found:
        X, Y = int(x * inv), int(y * inv)
        Wd, Hd = min(W - X, int(np.ceil(bw * inv))), min(H - Y, int(np.ceil(bh * inv)))
        interior = cv2.resize(filled.astype(np.uint8), (Wd, Hd), interpolation=cv2.INTER_NEAREST).astype(bool)
        # shrink a little so the outline is never treated as interior
        interior = cv2.erode(interior.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=2).astype(bool)
        crop = rgb[Y:Y + Hd, X:X + Wd]
        g = gray_full[Y:Y + Hd, X:X + Wd]
        if not interior.any():
            continue
        bgsel = interior & (g >= np.percentile(g[interior], 60))
        bg = tuple(int(v) for v in np.median(crop[bgsel], axis=0))
        bg_l = float(np.median(g[bgsel]))
        ink = interior & (g < bg_l - 60)
        b = Bubble(X, Y, Wd, Hd, interior, ink, bg)
        _measure_text(b)
        if b.lines:
            out.append(b)
    return out


def _measure_text(b: Bubble):
    n, lab, st, _ = cv2.connectedComponentsWithStats(b.ink.astype(np.uint8), connectivity=8)
    if n <= 1:
        return
    st = st[1:]
    # ignore marks that are huge (tail/outline remnants) or tiny specks
    hts = st[:, 3]
    # letter height from letter-sized marks only: in short lines ("SIR...") dots and quote
    # marks would otherwise drag a plain median down
    big = hts[hts >= 0.6 * np.percentile(hts, 90)] if len(hts) else hts
    med = np.median(big) if len(big) else 0
    keep = (hts > 0.3 * med) & (hts < 3 * med) & (st[:, 4] > 3)
    if keep.sum() < 3:
        return
    s = st[keep]
    x0, y0 = s[:, 0].min(), s[:, 1].min()
    x1, y1 = (s[:, 0] + s[:, 2]).max(), (s[:, 1] + s[:, 3]).max()
    b.text_box = (int(x0), int(y0), int(x1), int(y1))
    sh = s[:, 3]
    b.letter_h = float(np.median(sh[sh >= 0.6 * np.percentile(sh, 90)]))
    # count lines by projecting mark centres
    rows = np.zeros(b.h, bool)
    for x, y, w, h, a in s:
        rows[y + h // 4: y + h - h // 4] = True
    b.lines = int(np.sum(np.diff(np.concatenate([[0], rows.astype(int)])) == 1))


def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    return inter / min(aw * ah, bw * bh)


# ------------------------------------------------------------------ text blocks + OCR


def text_blocks(b: Bubble) -> list[dict]:
    """Group the lettering marks of a bubble into separate text blocks (handles two bubbles
    joined into one shape). Each block: bool mask of its marks, bbox, letter height."""
    n, lab, st, _ = cv2.connectedComponentsWithStats(b.ink.astype(np.uint8), connectivity=8)
    if n <= 1 or not b.letter_h:
        return []
    keep = np.zeros(n, bool)
    hts = st[:, 3]
    keep[1:] = (hts[1:] > 0.25 * b.letter_h) & (hts[1:] < 2.5 * b.letter_h) & (st[1:, 4] > 2)
    marks = keep[lab]
    # small punctuation/dots near letters still belong: keep tiny marks within the text area
    tiny = (~keep[lab]) & (lab > 0) & (hts[lab] <= 0.25 * b.letter_h)
    # (a full stop can sit ~half a letter away from the word it ends)
    kt = max(3, int(b.letter_h * 1.3))
    grown = cv2.dilate(marks.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kt, kt)))
    marks |= tiny & grown.astype(bool)
    k = max(3, int(b.letter_h * 0.9))
    grown = cv2.dilate(marks.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, int(k * 1.4))))
    gn, glab, gst, _ = cv2.connectedComponentsWithStats(grown, connectivity=8)
    blocks = []
    for i in range(1, gn):
        m = marks & (glab == i)
        if m.sum() < 20:
            continue
        ys, xs = np.nonzero(m)
        blocks.append({"mask": m, "box": (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1)})
    return blocks


def ocr_block(b: Bubble, blk: dict, gray: np.ndarray, target_h: float = 40.0, shear: float = 0.0,
              tesseract: str = TESSERACT):
    """OCR one text block from the real grayscale pixels (a thresholded mask thins strokes:
    D reads as V). Everything outside this block's lettering area is blanked to white so the
    bubble outline and neighbouring text can't leak in. Returns (lines, mean_conf) where
    lines = [[(word, conf, box)]], box in bubble-local full-resolution pixels."""
    pad = max(2, int(b.letter_h * 0.35))
    x0, y0, x1, y1 = blk["box"]
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(b.w, x1 + pad), min(b.h, y1 + pad)
    keep = cv2.dilate(blk["mask"].astype(np.uint8), np.ones((3, 3), np.uint8), iterations=pad).astype(bool)
    keep &= b.interior
    img = np.where(keep[y0:y1, x0:x1], gray[y0:y1, x0:x1], 255).astype(np.uint8)
    s = target_h / max(b.letter_h, 1)
    img = cv2.resize(img, (max(1, int(img.shape[1] * s)), max(1, int(img.shape[0] * s))), interpolation=cv2.INTER_CUBIC)
    P = 30
    img = cv2.copyMakeBorder(img, P, P, P, P, cv2.BORDER_CONSTANT, value=255)
    if shear:  # lean italic lettering back upright (x' = x + shear * (y - h/2))
        hh = img.shape[0]
        M = np.float32([[1, shear, -shear * hh / 2], [0, 1, 0]])
        img = cv2.warpAffine(img, M, (img.shape[1], hh), flags=cv2.INTER_CUBIC, borderValue=255)
    with tempfile.TemporaryDirectory() as td:
        f = os.path.join(td, "b.png")
        cv2.imwrite(f, img)
        r = subprocess.run([tesseract, f, "stdout", "--psm", "6", "-l", "eng", "tsv"],
                           capture_output=True, text=True)
    rows = list(csv.DictReader(io.StringIO(r.stdout), delimiter="\t", quoting=csv.QUOTE_NONE))
    lines, confs, cur = [], [], None
    for row in rows:
        if row.get("level") != "5" or not row.get("text", "").strip():
            continue
        key = (row["block_num"], row["par_num"], row["line_num"])
        if key != cur:
            lines.append([])
            cur = key
        L_, T_, W_, H_ = (int(row[k]) for k in ("left", "top", "width", "height"))
        if shear:  # undo the shear for the box (at its vertical centre)
            hh = img.shape[0]
            L_ = int(L_ - shear * (T_ + H_ / 2 - hh / 2))
        box = (x0 + (L_ - P) / s, y0 + (T_ - P) / s, x0 + (L_ - P + W_) / s, y0 + (T_ - P + H_) / s)
        lines[-1].append((row["text"], float(row["conf"]), box))
        confs.append(float(row["conf"]))
    return lines, (float(np.mean(confs)) if confs else 0.0)


# ------------------------------------------------------------------ text fixes

_OK_TOKEN = re.compile(r"^[\"'“”‘’(]*[A-Z0-9][A-Z0-9'’.,!?\-–—…*:;\")”’]*$|^[.…\-–—!?]+$")


def clean_ocr(text: str) -> str | None:
    """Fix common OCR slips on all-caps comic lettering; None if the text looks unreliable."""
    out_lines = []
    for line in text.splitlines():
        line = line.strip().strip("|").strip()
        if not line:
            continue
        line = line.upper()
        line = re.sub(r"(?<=[A-Z])/(?=[A-Z])|(?<=\s)/(?=[A-Z])|(?<=[A-Z])/(?=\s|$)", "I", line)  # italic I
        line = re.sub(r"(?<![A-Z'])T(?=('(VE|M|LL|D)\b|\s|$))", "I", line)   # T'VE / T HAVE -> I
        line = re.sub(r"\s+7(?=\s|$)", "?", line)                            # lone 7 -> ?
        line = re.sub(r"\s*,\s*\.\s*\.|\.\s*,\s*\.|\.\s*\.\s*,", "...", line)  # ,.. -> ...
        line = re.sub(r"\.\s+\.(\s*\.)?", "...", line)
        line = line.replace("|", "I")
        line = re.sub(r"\s+(\.{2,}|…)", r"\1", line)                # "SILENT .." -> "SILENT.."
        line = re.sub(r"(?<!\.)\.\.(?!\.)", "...", line)            # ".." is always a short "..."
        out_lines.append(line)
    words = " ".join(out_lines).split()
    if not words:
        return None
    if any(not _OK_TOKEN.match(w) for w in words):
        return None
    if any(re.search(r"[A-Z]", w) and re.search(r"\d", w) for w in words):  # 1S, 7-THANK
        return None
    digits_only = [w for w in words if re.fullmatch(r"\d[.,!?]*", w)]
    if digits_only and sum(bool(re.search(r"\d", w)) for w in words) == len(digits_only):
        return None  # a lone digit among words is a misread I ("THORNE, 1 HAVE")
    letters = sum(c.isalpha() for c in "".join(words))
    if letters < 2:
        return None
    return re.sub(r"^(\.{2,}|…)\s+", r"\1", " ".join(words))  # "... SOMETHING" -> "...SOMETHING"


# ------------------------------------------------------------------ typesetting

FONT_CANDIDATES = {
    "regular": ["/usr/share/fonts/opentype/comic-neue/ComicNeue-Bold.otf",
                "/usr/share/fonts/truetype/comic-neue/ComicNeue-Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "emph": ["/usr/share/fonts/opentype/comic-neue/ComicNeue-BoldItalic.otf",
             "/usr/share/fonts/truetype/comic-neue/ComicNeue-BoldItalic.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf"],
}


def find_font(kind):
    for p in FONT_CANDIDATES[kind]:
        if os.path.exists(p):
            return p
    return None


_font_cache = {}


def font(path, size):
    k = (path, size)
    if k not in _font_cache:
        _font_cache[k] = ImageFont.truetype(path, size)
    return _font_cache[k]


def _row_span(allowed, y0, y1, cx):
    """Widest horizontal span [l, r) usable for every row y0..y1, around x=cx."""
    band = allowed[max(0, y0):min(allowed.shape[0], y1)]
    if band.shape[0] == 0 or band.shape[0] < (y1 - y0):
        return None
    ok = band.all(axis=0)
    if not ok.any():
        return None
    cx = int(min(max(cx, 0), len(ok) - 1))
    if not ok[cx]:
        idx = np.nonzero(ok)[0]
        cx = int(idx[np.argmin(np.abs(idx - cx))])
    l = cx
    while l > 0 and ok[l - 1]:
        l -= 1
    r = cx
    while r < len(ok) and ok[r]:
        r += 1
    return l, r


WEIGHT = 0.04  # extra stroke as a fraction of font size (see layout)
CLEAN_GROW = 0.035  # thickening of cleaned (not re-lettered) strokes, as a fraction of letter height


def word_width(fnt, word, stroke):
    """Width of a word drawn letter by letter with `stroke`: letters are spaced apart by the
    stroke so thickened letters don't run into each other."""
    return sum(fnt.getlength(ch) for ch in word) + (len(word) - 1) * stroke + 2 * stroke


def draw_word(d, x, y, word, fnt, stroke):
    x += stroke
    for ch in word:
        d.text((x, y), ch, font=fnt, fill=(0, 0, 0), stroke_width=stroke, stroke_fill=(0, 0, 0))
        x += fnt.getlength(ch) + stroke


def layout(words, styles, allowed, size, fonts, center):
    """Try to fit words into the allowed mask at font size `size`. styles[i] = (font kind
    "regular"/"emph", heavy) - heavy adds a stroke for emphasis inside italic narration.
    Returns [(y, [(x, word, font, stroke, cap_top, cap)])] or None."""
    fnts = {k: font(fonts[k], size) for k in ("regular", "emph")}
    f_reg = fnts["regular"]
    # Heavy strokes on purpose: a page is usually shrunk 2-4x to fit a reader's screen, and
    # a normal-weight stroke ends up under a pixel wide there - gray fog. Every word gets an
    # outline in the ink colour that thickens it; emphasised words get a heavier one.
    base = max(1, round(size * WEIGHT))
    heavy_s = max(base + 1, round(size * WEIGHT * 1.6))
    strokes = [heavy_s if heavy else base for _, heavy in styles]
    space = f_reg.getlength(" ") + 2 * base
    widths = [word_width(fnts[k], w, st) for w, (k, _), st in zip(words, styles, strokes)]
    cap_top, cap_bot = f_reg.getbbox("H")[1], f_reg.getbbox("H")[3]
    cap = cap_bot - cap_top
    lh = int(round(cap * 1.28 + 2 * base))  # all caps: no descenders, tight comic leading
    cx, cy = center
    for n in range(1, len(words) + 1):
        top = int(cy - n * lh / 2)
        lines, i, ok = [], 0, True
        for li in range(n):
            y = top + li * lh
            span = _row_span(allowed, y, y + lh, cx)
            if span is None:
                ok = False
                break
            l, r = span
            avail = r - l
            line, wsum = [], 0.0
            while i < len(words):
                add = widths[i] + (space if line else 0)
                if wsum + add > avail:
                    break
                line.append(i)
                wsum += add
                i += 1
            if not line:
                ok = False
                break
            lines.append((y, l, r, line, wsum))
        if ok and i == len(words):
            placed = []
            for y, l, r, line, wsum in lines:
                x = (l + r - wsum) / 2
                items = []
                for j in line:
                    k, _ = styles[j]
                    items.append((x, words[j], fnts[k], strokes[j], cap_top, cap))
                    x += widths[j] + space
                placed.append((y, items))
            return placed
    return None


def word_emphasis(blk: dict, words, letter_h: float):
    """Flag words drawn with heavier strokes than normal lettering (bold / bold-italic
    emphasis). Stroke width = 2x the 90th-percentile distance from ink pixel to paper."""
    m = blk["mask"]
    dt = cv2.distanceTransform(m.astype(np.uint8), cv2.DIST_L2, 3)
    sw = []
    for _, _, (a, b_, c, d) in words:
        a, b_, c, d = int(max(a, 0)), int(max(b_, 0)), int(c), int(d)
        box = m[b_:d, a:c]
        sw.append(2 * float(np.percentile(dt[b_:d, a:c][box], 90)) if box.any() else 0.0)
    # normal strokes are ~0.17-0.21 of the letter height; the block median guards against
    # lettering that is heavier or lighter overall
    ref = 0.19 * letter_h
    if len(sw) >= 3:
        ref = min(ref, float(np.median(sw)))
    return [w >= 1.25 * ref for w in sw]


# ------------------------------------------------------------------ page pipeline
MIN_CONF = 80.0
WHITE_BG = 215


def _smoothstep_ink(crop_rgb, region, bg, ink, grow: int = 0):
    """In-place clean-up: map each pixel to a mix of background and solid ink based on how
    dark it is, with a steep curve, so fuzzy gray strokes become solid and the paper
    becomes flat."""
    g = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    bg_l = float(np.dot(bg, [0.299, 0.587, 0.114]))
    t = np.clip((bg_l - g) / max(bg_l - 40, 1), 0, 1)
    a = np.clip((t - 0.18) / 0.45, 0, 1)
    a = a * a * (3 - 2 * a)  # smoothstep keeps anti-aliased edges, kills fog
    if grow:  # thicken thin strokes so they survive being shrunk to screen size
        k = 2 * grow + 1
        a = np.maximum(a, cv2.dilate(a, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))) * 0.92)
    out = crop_rgb.astype(np.float32)
    mix = (np.array(bg, np.float32) * (1 - a[..., None]) + np.array(ink, np.float32) * a[..., None])
    out[region] = mix[region]
    return out.astype(np.uint8)


def process(rgb: np.ndarray, reletter=True, fonts=None, min_conf=MIN_CONF, log=None):
    """Return (new_rgb, stats). Only lettering regions are touched."""
    H, W = rgb.shape[:2]
    out = rgb.copy()
    stats = {"bubbles": 0, "relettered": 0, "cleaned": 0, "skipped": 0}
    fonts = fonts or {"regular": find_font("regular"), "emph": find_font("emph")}
    lh_min, lh_max = 0.0035 * H, 0.013 * H
    for b in find_bubbles(rgb):
        if not (lh_min <= b.letter_h <= lh_max):
            continue
        bg_l = float(np.dot(b.bg, [0.299, 0.587, 0.114]))
        if bg_l < 160:
            continue
        # only neutral, near-white paper counts: tan skin, sky or coloured art with fine
        # lines (wrinkles, hair) can look like lettering, and the art must never be touched
        if bg_l < WHITE_BG or max(b.bg) - min(b.bg) >= 40:
            continue
        if not _is_lettering_area(rgb, b, bg_l):
            continue
        blocks = [blk for blk in text_blocks(b) if _looks_like_text(blk, b.letter_h)]
        if not blocks:
            continue
        stats["bubbles"] += 1
        crop = out[b.y:b.y + b.h, b.x:b.x + b.w]
        gray0 = cv2.cvtColor(rgb[b.y:b.y + b.h, b.x:b.x + b.w], cv2.COLOR_RGB2GRAY)
        # share the interior between blocks: each pixel belongs to the nearest block
        if len(blocks) > 1:
            dists = [cv2.distanceTransform((~blk["mask"]).astype(np.uint8), cv2.DIST_L2, 3) for blk in blocks]
            owner = np.argmin(np.stack(dists), axis=0)
        else:
            owner = np.zeros(b.interior.shape, int)
        plain = bg_l >= WHITE_BG and max(b.bg) - min(b.bg) < 40
        for k, blk in enumerate(blocks):
            region = b.interior & (owner == k)
            done = False
            if reletter and plain and fonts["regular"]:
                got = read_block(b, blk, gray0, min_conf)
                if got:
                    text, words = got
                    toks = text.split()
                    emph = word_emphasis(blk, words, b.letter_h) if len(toks) == len(words) else [False] * len(toks)
                    # keep the lettering's voice: italic narration stays italic (emphasis there is
                    # shown with a heavier stroke); upright dialogue uses bold italic for emphasis
                    if block_slant(blk, b.letter_h) >= 0.2:
                        styles = [("emph", e) for e in emph]
                    else:
                        styles = [("emph" if e else "regular", False) for e in emph]
                    done = reletter_block(crop, b, blk, region, toks, styles, fonts)
                    if done:
                        stats["relettered"] += 1
                        if log is not None:
                            log.append(text)
            if not done:
                x0, y0, x1, y1 = blk["box"]
                pad = int(b.letter_h * 0.6)
                near = np.zeros_like(region)
                near[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad] = True
                g = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
                dark = blk["mask"] & (g <= np.percentile(g[blk["mask"]], 25))
                ink = np.median(crop[dark], axis=0) if dark.any() else np.zeros(3)
                ink = tuple(float(v) * 0.6 for v in ink)  # same hue as the original ink, but solid
                crop[:] = _smoothstep_ink(crop, region & near, b.bg, ink, grow=max(0, round(b.letter_h * CLEAN_GROW)))
                stats["cleaned"] += 1
        out[b.y:b.y + b.h, b.x:b.x + b.w] = crop
    return out, stats


try:
    from wordfreq import zipf_frequency
except ImportError:  # without it, any disagreement between OCR readings keeps the original lettering
    zipf_frequency = None

UNSLANT = 0.3  # shear that stands this kind of italic lettering upright


def _key(token: str) -> str:
    """Letters of a token, with the same slips clean_ocr fixes (italic I read as / or |)."""
    t = token.upper().replace("’", "'").replace("/", "I").replace("|", "I")
    return re.sub(r"[^A-Z0-9']", "", t)


def _commonness(token: str) -> float:
    k = _key(token).replace("'", "")
    if not k:
        return 0.0
    if k.isdigit():
        return 3.0
    return zipf_frequency(k.lower(), "en") if zipf_frequency else 0.0


_HOLES = {**{c: 1 for c in "ADOPQR4690"}, "B": 2, "8": 2}


def _holes_on_page(m: np.ndarray, letter_h: float) -> int:
    """Closed loops in a word's ink (the counters of O, D, A, B, R...)."""
    m = m.astype(np.uint8)
    if m.size == 0:
        return 0
    inv = cv2.copyMakeBorder(1 - m, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=1)
    n, lab, st, _ = cv2.connectedComponentsWithStats(inv, connectivity=4)
    min_area = max(2.0, (letter_h * 0.09) ** 2)
    H, W = inv.shape
    return sum(1 for i in range(1, n) if st[i, 4] >= min_area and st[i, 0] > 0 and st[i, 1] > 0
               and st[i, 0] + st[i, 2] < W and st[i, 1] + st[i, 3] < H)


REJECTS = None  # set to a list to collect why blocks were left alone (for --report / debugging)


def _reject(reason, detail=""):
    if REJECTS is not None:
        REJECTS.append((reason, detail))
    return None


def _fix_junk(line, first: bool):
    """All-caps lettering never contains lowercase letters, so a token with lowercase or no
    letters at all, read with low confidence, is OCR noise. At the start of a block it's
    almost always a drawn ellipsis ("...SOMETHING" read as "-»e SOMETHING"): restore it."""
    out = []
    for i, (t, c, box) in enumerate(line):
        junk = c < 70 and (re.search(r"[a-z»«·¥]", t) or not re.search(r"[A-Za-z0-9]", t))
        if junk and len(re.sub(r"[^A-Za-z]", "", t)) <= 2:
            if first and i == 0:
                out.append(("...", 99.0, box))
                continue
            return None  # noise mid-text: something there wasn't read
        out.append((t, c, box))
    return out


def read_block(b: Bubble, blk: dict, gray: np.ndarray, min_conf: float):
    """Read a text block as reliably as possible, or return None to leave it alone.

    The block is read at two scales in its own orientation (italic narration with the slant
    straightened out, upright dialogue as-is); both reads must agree word for word. Where they
    don't, two reads in the other orientation get a vote and one reading must be a clearly
    more common English word (NEEVED vs NEEDED; needs the `wordfreq` package). Every word must
    be a real word and have the closed loops the drawn letters have. Anything less certain
    returns None, and the bubble keeps its original lettering.
    Returns (text, words) where words carry boxes for emphasis detection."""
    # Italic blocks (narration) are read with the slant straightened, upright ones as-is.
    # The other orientation only supplies alternatives for a word the main reads disagree on:
    # straightening upright text garbles it (HELL -> WELL), so it must never outvote.
    italic = block_slant(blk, b.letter_h) >= 0.2
    sh = UNSLANT if italic else 0.0
    alt_sh = 0.0 if italic else UNSLANT
    lines, _ = ocr_block(b, blk, gray, shear=sh)
    lines = [_fix_junk(line, first=(i == 0)) for i, line in enumerate(lines)]
    if any(line is None for line in lines):
        return _reject("unreadable mark mid-text")
    words = [w for line in lines for w in line]
    real = [w for w in words if re.search(r"[A-Za-z0-9]", w[0])]
    if not real:
        return _reject("confidence")
    conf = float(np.mean([w[1] for w in real]))
    if conf < min_conf or min(w[1] for w in real) < 30:
        return _reject("confidence")
    others = [[w for l in ocr_block(b, blk, gray, target_h=56.0, shear=sh)[0] for w in l],
              [w for l in ocr_block(b, blk, gray, shear=alt_sh)[0] for w in l],
              [w for l in ocr_block(b, blk, gray, target_h=56.0, shear=alt_sh)[0] for w in l]]
    fixed = []
    for t, c, box in words:
        if not re.search(r"[A-Za-z0-9]", t):  # punctuation only ("...", "--"): nothing to verify
            fixed.append((t, c, box))
            continue
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        cands, confs = [t], [c]  # [main 40px, main 56px, other orientation 40px, 56px]
        for n_, cand in enumerate(others):
            near = [w for w in cand if w[2][1] - 4 <= cy <= w[2][3] + 4]
            if near:
                w = min(near, key=lambda w: abs((w[2][0] + w[2][2]) / 2 - cx))
                cands.append(w[0])
                confs.append(w[1])
            elif n_ == 0:
                return _reject("word missing in 2nd read", t)
        # The two main readings must agree on the word's length (IN vs IIN = unsure).
        if len(_key(cands[1])) != len(_key(t)):
            return _reject("main reads differ in length", f"{t}/{cands[1]}")
        # all-caps lettering read as lowercase is a warning sign (CAPABLE -> "wigs", conf 36);
        # harmless case slips ("You.") are confident and repeat in the second read
        if re.search(r"[a-z]", t) and (c < 60 or _key(cands[1]) != _key(t)):
            return _reject("lowercase misread", t)
        main_ok = _key(cands[1]) == _key(t)
        alt_ok = (len(cands) == 4 and _key(cands[2]) == _key(cands[3])
                  and len(_key(cands[2])) == len(_key(t)) and not re.search(r"[a-z]", cands[2]))
        if main_ok and alt_ok and _key(cands[2]) != _key(t):
            # both orientations are self-consistent but disagree (italic HE read as WE when
            # straightened): trust the clearly more confident one, else give up
            m_conf, a_conf = min(confs[:2]), min(confs[2:])
            if a_conf >= m_conf + 15 and a_conf >= 80:  # must be confident in its own right
                tail = re.match(r".*?([.,!?…\-–—\"”’']*)$", t).group(1)
                t = re.sub(r"[.,!?…\-–—\"”’']*$", "", cands[2]) + tail
            elif m_conf < a_conf + 15 and m_conf < 80:
                return _reject("orientations disagree", f"{t}/{cands[2]}")
            fixed.append((t, c, box))
            continue
        if main_ok:
            if min(confs[:2]) < 40:
                return _reject("low-confidence word", t)
            fixed.append((t, c, box))
            continue
        # main reads disagree: same-length readings from all four vote (NEEVED/NEEDED)
        cands = [x for x in cands if len(_key(x)) == len(_key(t))]
        keys = {_key(x) for x in cands}
        if len(keys) > 1:
            if zipf_frequency is None:
                return _reject("no wordfreq")
            votes = {}
            for x in cands:
                votes.setdefault(_key(x), []).append(x)
            ranked = sorted(votes.values(), key=lambda v: _commonness(v[0]), reverse=True)
            best, runner = ranked[0][0], ranked[1][0]
            if (len(ranked[0]) < 2 or _commonness(best) < 2.5
                    or _commonness(best) - _commonness(runner) < 1.0):
                return _reject("ambiguous readings", "/".join(cands))
            # keep the first reading's punctuation if only the letters differed
            tail = re.match(r".*?([.,!?…\-–—\"”’']*)$", t).group(1)
            best = re.sub(r"[.,!?…\-–—\"”’']*$", "", best) + tail if _key(best) else best
            t = best
        fixed.append((t, c, box))
    # Shape check: the letters we'd print must have the same closed loops as the lettering
    # on the page. Catches consistent misreads of real words: CODE drawn, COVE read (D has a
    # loop, V doesn't); HIM drawn, AIM read; NEEDED drawn, NEEVED read.
    # For italic blocks, check on the straightened lettering (the boxes come from there; an
    # upright box would slice through slanted letters and miscount their loops).
    mask, dx, yc = blk["mask"], 0, 0.0
    if sh:
        pad = max(2, int(b.letter_h * 0.35))
        yc = (max(0, blk["box"][1] - pad) + min(b.h, blk["box"][3] + pad)) / 2
        dx = int(sh * b.h) + 2
        M = np.float32([[1, sh, dx - sh * yc], [0, 1, 0]])
        mask = cv2.warpAffine(blk["mask"].astype(np.uint8), M, (b.w + 2 * dx, b.h),
                              flags=cv2.INTER_NEAREST).astype(bool)
    covered = np.zeros(mask.shape, bool)
    for t, _, (a, b_, c, d) in fixed:
        shift = dx + sh * ((b_ + d) / 2 - yc) if sh else 0
        a, b_, c, d = int(max(a + shift, 0)), int(max(b_, 0)), int(c + shift), int(d)
        m = max(2, int(b.letter_h * 0.15))
        covered[max(0, b_ - m):d + m, max(0, a - m):c + m] = True
        if not re.search(r"[A-Za-z0-9]", t):
            continue
        want = sum(_HOLES.get(ch, 0) for ch in _key(t))
        if want != _holes_on_page(mask[b_:d, a:c], b.letter_h):
            return _reject("loop check", t)
    # Coverage check: every letter on the page must sit inside a word that was read. A word
    # OCR skipped (WE... dropped) or a misread spanning only part of a word fails this.
    n, lab, st, cen = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    for i in range(1, n):
        if st[i, 3] >= 0.5 * b.letter_h and st[i, 4] >= 0.02 * b.letter_h ** 2:
            cx_, cy_ = int(cen[i][0]), int(cen[i][1])
            if not covered[min(cy_, covered.shape[0] - 1), min(cx_, covered.shape[1] - 1)]:
                return _reject("letters not read", f"mark at {cx_},{cy_}")
    # Punctuation OCR drops: a full stop after a bold word often vanishes ("TOO EASY. THAT"
    # read as "TOO EASY THAT"). A small mark sitting on the baseline right at the end of a
    # word with no punctuation of its own is put back: "." on the line, "," if it dips below.
    lh = b.letter_h
    small = [(st[i, 0], st[i, 1], st[i, 0] + st[i, 2], st[i, 1] + st[i, 3]) for i in range(1, n)
             if st[i, 3] < 0.4 * lh and st[i, 2] < 0.4 * lh and st[i, 4] >= 2]
    for j, (t, c, (a, b_, c2, d)) in enumerate(fixed):
        if not re.search(r"[A-Za-z0-9]$", t):
            continue
        shift = dx + sh * ((b_ + d) / 2 - yc) if sh else 0
        right, bottom = c2 + shift, d
        for x0, y0, x1, y1 in small:
            if right - 0.45 * lh <= (x0 + x1) / 2 <= right + 0.7 * lh and y1 >= bottom - 0.3 * lh \
                    and y0 >= bottom - 0.45 * lh and y1 <= bottom + 0.6 * lh:
                fixed[j] = (t + ("," if y1 > bottom + 0.15 * lh else "."), c, (a, b_, c2, d))
                break
    out, k = [], 0
    for line in lines:
        out.append(" ".join(w[0] for w in fixed[k:k + len(line)]))
        k += len(line)
    text = clean_ocr("\n".join(out))
    if not text:
        return _reject("cleanup refused", " ".join(out))
    if zipf_frequency:
        # every word must exist (names are rare but known; a misread like BOMBEING isn't).
        # Sound effects (GGUCK) fail this too, and keep their hand lettering - which suits them.
        for part in re.split(r"[^A-Z']+", text.replace("’", "'")):
            part = part.strip("'")
            if len(part) > 1 and zipf_frequency(part.lower(), "en") == 0:
                return _reject("unknown word", part)
            if len(part) == 2 and zipf_frequency(part.lower(), "en") < 4.0:
                return _reject("unlikely short word", part)  # TN for I'M
    return text, fixed


def block_slant(blk: dict, letter_h: float) -> float:
    """Shear that makes the block's strokes most vertical: ~0 upright, ~0.3-0.45 italic.
    Measured over the whole block, so a few diagonal letters (M, W, V) don't fool it."""
    x0, y0, x1, y1 = blk["box"]
    m = blk["mask"][y0:y1, x0:x1].astype(np.uint8)
    h = m.shape[0]
    m = cv2.copyMakeBorder(m, 0, 0, h, h, cv2.BORDER_CONSTANT, value=0)
    # score each text line separately so lines don't smear each other's columns
    rows = max(1, int(round(h / (letter_h * 1.3))))
    best, best_s = 0.0, -1.0
    for sh in np.linspace(-0.1, 0.6, 29):
        M = np.float32([[1, sh, -sh * h / 2], [0, 1, 0]])
        t = cv2.warpAffine(m, M, (m.shape[1], h), flags=cv2.INTER_NEAREST)
        sc = sum(float((band.sum(axis=0).astype(np.float64) ** 2).sum()) for band in np.array_split(t, rows))
        if sc > best_s:
            best_s, best = sc, float(sh)
    return best


def _is_lettering_area(rgb, b: Bubble, bg_l: float) -> bool:
    """Real bubbles/captions have flat paper behind the letters. Bright patches of artwork
    (hair highlights, tears, textured walls) don't: their non-ink pixels vary a lot."""
    g = cv2.cvtColor(rgb[b.y:b.y + b.h, b.x:b.x + b.w], cv2.COLOR_RGB2GRAY).astype(np.float32)
    ink_zone = cv2.dilate(b.ink.astype(np.uint8), np.ones((5, 5), np.uint8), iterations=2).astype(bool)
    paper = b.interior & ~ink_zone
    if paper.sum() < 50:
        return False
    flat = (np.abs(g[paper] - bg_l) < 22).mean()
    return flat >= 0.88


def _looks_like_text(blk: dict, letter_h: float) -> bool:
    """At least a short word: several letter-sized marks spread along a line."""
    x0, y0, x1, y1 = blk["box"]
    n = cv2.connectedComponents(blk["mask"].astype(np.uint8), connectivity=8)[0] - 1
    return n >= 4 and (x1 - x0) >= 2.0 * letter_h


def reletter_block(crop, b, blk, region, toks, styles, fonts):
    margin = max(2, int(b.letter_h * 0.3))
    allowed = cv2.erode(region.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=margin).astype(bool)
    x0, y0, x1, y1 = blk["box"]
    center = ((x0 + x1) / 2, (y0 + y1) / 2)
    # cap height of Comic Neue ~0.7 em: start big (up to 1.6x the original letters), never
    # go below the original size -- if it won't fit at least as big, keep the original
    fr = font(fonts["regular"], 100)
    cap_ratio = (fr.getbbox("H")[3] - fr.getbbox("H")[1]) / 100
    orig = b.letter_h / cap_ratio  # font size whose capitals match the original lettering
    best = None
    # down to 82%: at reading size, bold letters a little smaller than the original still
    # read far better than the thin original strokes
    for size in range(int(orig * 1.8), int(orig * 0.82) - 1, -1):
        best = layout(toks, styles, allowed, size, fonts, center)
        if best:
            break
        # also try centring on the free area itself
        ys, xs = np.nonzero(allowed)
        if len(ys):
            best = layout(toks, styles, allowed, size, fonts, (xs.mean(), ys.mean()))
            if best:
                break
    if not best:
        return False
    # erase the old lettering (and its anti-aliased halo) inside this block's region
    ink = cv2.dilate(blk["mask"].astype(np.uint8), np.ones((3, 3), np.uint8), iterations=2).astype(bool)
    erase = region & (ink | (cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY) < 200))
    erase &= cv2.dilate(blk["mask"].astype(np.uint8), np.ones((5, 5), np.uint8), iterations=int(b.letter_h // 3) + 1).astype(bool)
    crop[erase] = b.bg
    im = Image.fromarray(crop)
    d = ImageDraw.Draw(im)
    for y, items in best:
        for x, word, fnt, stroke, cap_top, cap in items:
            # centre the capitals in the line box
            draw_word(d, x, y + (cap * 0.14) + stroke - cap_top, word, fnt, stroke)
    crop[:] = np.array(im)
    return True


# --------------------------------------------------------------------------- pages

def process_page(args):
    """One source image -> list of (jpeg_bytes, page_text, stats) - two entries for a split spread."""
    path, split, rtl, quality, lettering, fonts, min_conf, max_h = args
    raw = Path(path).read_bytes()
    with Image.open(io.BytesIO(raw)) as im:
        is_jpeg = im.format == "JPEG"
        im = im.convert("RGB")
    parts = [im]
    if split and im.width > im.height:  # two-page spread
        left = im.crop((0, 0, im.width // 2, im.height))
        right = im.crop((im.width // 2, 0, im.width, im.height))
        parts = [right, left] if rtl else [left, right]
    out = []
    for p in parts:
        texts, stats = [], {}
        if lettering != "off":
            arr, stats = process(np.array(p), reletter=(lettering == "reletter"), fonts=fonts,
                                 min_conf=min_conf, log=texts)
            p = Image.fromarray(arr) if stats.get("relettered") or stats.get("cleaned") else p
        changed = bool(stats.get("relettered") or stats.get("cleaned"))
        if max_h and p.height > max_h:
            p = p.resize((round(p.width * max_h / p.height), max_h), Image.LANCZOS)
            changed = True
        if not changed and len(parts) == 1 and is_jpeg:
            data = raw  # untouched page: keep the original file exactly
        else:
            buf = io.BytesIO()
            # baseline JPEG with full-resolution colour (subsampling=0): the default halves colour
            # resolution, which visibly smears saturated edges like red-on-black lettering
            p.save(buf, "JPEG", quality=quality, subsampling=0, optimize=True, progressive=False)
            data = buf.getvalue()
        out.append((data, " / ".join(t.strip() for t in texts), stats))
    return out


# --------------------------------------------------------------------------- EPUB

def write_epub(pages: list[bytes], cover: bytes, out: Path, meta: dict, rtl: bool,
               alts: list[str] | None = None):
    n = len(pages)
    digits = max(3, len(str(n)))
    sizes = [Image.open(io.BytesIO(p)).size for p in pages]
    W, H = sizes[0]
    e = lambda s: html.escape(str(s), quote=True)
    title = e(meta["title"])
    book_id = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, 'comic-to-epub:' + meta['title'])}"
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    extra = []
    if meta.get("writer"):
        extra.append(f"<dc:creator>{e(meta['writer'])}</dc:creator>")
    if meta.get("publisher"):
        extra.append(f"<dc:publisher>{e(meta['publisher'])}</dc:publisher>")
    if meta.get("summary"):
        extra.append(f"<dc:description>{e(meta['summary'])}</dc:description>")
    if meta.get("series"):
        extra.append(f'<meta property="belongs-to-collection" id="series">{e(meta["series"])}</meta>')
        extra.append('<meta refines="#series" property="collection-type">series</meta>')
        if meta.get("number"):
            extra.append(f'<meta refines="#series" property="group-position">{e(meta["number"])}</meta>')

    manifest = ['<item id="cover-img" href="images/cover.jpg" media-type="image/jpeg" properties="cover-image"/>']
    spine, nav, ncx = [], [], []
    for i in range(n):
        num = f"{i + 1:0{digits}d}"
        manifest.append(f'<item id="i{num}" href="images/{num}.jpg" media-type="image/jpeg"/>')
        manifest.append(f'<item id="p{num}" href="pages/{num}.xhtml" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="p{num}"/>')
    for i in [0] + list(range(10, n, 10)):  # short TOC: start + every 10th page
        num = f"{i + 1:0{digits}d}"
        label = "Start" if i == 0 else f"Page {i + 1}"
        nav.append(f'<li><a href="pages/{num}.xhtml">{label}</a></li>')
        ncx.append(f'<navPoint id="n{num}" playOrder="{len(ncx) + 1}"><navLabel><text>{label}</text></navLabel>'
                   f'<content src="pages/{num}.xhtml"/></navPoint>')

    nl = "\n    "
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" prefix="rendition: http://www.idpf.org/vocab/rendition/#">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{book_id}</dc:identifier>
    <dc:title>{title}</dc:title>
    <dc:language>{e(meta.get("language") or "en")}</dc:language>
    {nl.join(extra)}
    <meta property="dcterms:modified">{modified}</meta>
    <meta property="rendition:layout">pre-paginated</meta>
    <meta property="rendition:orientation">portrait</meta>
    <meta property="rendition:spread">none</meta>
    <meta name="cover" content="cover-img"/>
    <meta name="fixed-layout" content="true"/>
    <meta name="original-resolution" content="{W}x{H}"/>
    <meta name="book-type" content="comic"/>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
    {nl.join(manifest)}
  </manifest>
  <spine toc="ncx" page-progression-direction="{'rtl' if rtl else 'ltr'}">
    {nl.join(spine)}
  </spine>
</package>
"""
    nav_doc = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>{title}</title></head>
<body><nav epub:type="toc" id="toc"><h1>{title}</h1><ol>
{chr(10).join(nav)}
</ol></nav></body>
</html>
"""
    ncx_doc = f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head><meta name="dtb:uid" content="{book_id}"/><meta name="dtb:depth" content="1"/>
<meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/></head>
<docTitle><text>{title}</text></docTitle>
<navMap>
{chr(10).join(ncx)}
</navMap>
</ncx>
"""
    css = ("@page { margin: 0; }\n"
           "html, body { margin: 0; padding: 0; width: 100%; height: 100%; background: #fff; }\n"
           "div.page { margin: 0; padding: 0; width: 100%; height: 100%; text-align: center; }\n"
           "img { display: block; margin: 0 auto; width: 100%; height: 100%; object-fit: contain; }\n")
    container = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""
    dz = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(out, "w") as z:
        # the mimetype entry must be first and stored uncompressed
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container, compress_type=dz)
        z.writestr("OEBPS/content.opf", opf, compress_type=dz)
        z.writestr("OEBPS/nav.xhtml", nav_doc, compress_type=dz)
        z.writestr("OEBPS/toc.ncx", ncx_doc, compress_type=dz)
        z.writestr("OEBPS/style.css", css, compress_type=dz)
        z.writestr("OEBPS/images/cover.jpg", cover, compress_type=zipfile.ZIP_STORED)
        for i, (data, (w, h)) in enumerate(zip(pages, sizes)):
            num = f"{i + 1:0{digits}d}"
            page = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>{title} - {i + 1}</title><meta name="viewport" content="width={w}, height={h}"/>
<link rel="stylesheet" type="text/css" href="../style.css"/></head>
<body><div class="page"><img src="../images/{num}.jpg" alt="{e(alts[i]) if alts and alts[i] else f'Page {i + 1}'}" width="{w}" height="{h}"/></div></body>
</html>
"""
            z.writestr(f"OEBPS/pages/{num}.xhtml", page, compress_type=dz)
            z.writestr(f"OEBPS/images/{num}.jpg", data, compress_type=zipfile.ZIP_STORED)  # already compressed


# ---------------------------------------------------------------------- CBZ / CBR

def comicinfo_xml(meta: dict, n: int, rtl: bool) -> str:
    e = lambda s: html.escape(str(s), quote=False)
    fields = [("Title", meta.get("title")), ("Series", meta.get("series")), ("Number", meta.get("number")),
              ("Summary", meta.get("summary")), ("Writer", meta.get("writer")),
              ("Publisher", meta.get("publisher")), ("PageCount", n),
              ("LanguageISO", meta.get("language")), ("Manga", "YesAndRightToLeft" if rtl else None)]
    body = "".join(f"  <{k}>{e(v)}</{k}>\n" for k, v in fields if v)
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<ComicInfo xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n' + body + "</ComicInfo>\n")


def _page_names(n: int) -> list[str]:
    digits = max(3, len(str(n)))
    return [f"{i + 1:0{digits}d}.jpg" for i in range(n)]


def write_cbz(pages: list[bytes], out: Path, meta: dict, rtl: bool):
    """CBZ = a zip of page images in name order (stored, since JPEGs don't compress further)."""
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        for name, data in zip(_page_names(len(pages)), pages):
            z.writestr(name, data)
        z.writestr("ComicInfo.xml", comicinfo_xml(meta, len(pages), rtl), compress_type=zipfile.ZIP_DEFLATED)


def write_cbr(pages: list[bytes], out: Path, meta: dict, rtl: bool):
    """CBR = a RAR of page images. Only RARLAB's `rar` can create RAR archives."""
    rar = shutil.which("rar")
    if not rar:
        sys.exit("\nCan't write CBR: it needs the `rar` program (sudo apt install rar). "
                 "CBZ works in every comic reader, so --format cbz is the usual choice.")
    with tempfile.TemporaryDirectory() as tmp:
        names = _page_names(len(pages))
        for name, data in zip(names, pages):
            (Path(tmp) / name).write_bytes(data)
        (Path(tmp) / "ComicInfo.xml").write_text(comicinfo_xml(meta, len(pages), rtl), encoding="utf-8")
        out.unlink(missing_ok=True)
        # a = add, -ep = no folder paths, -m0 = store (JPEGs are already compressed), -idq = quiet
        subprocess.run([rar, "a", "-ep", "-m0", "-idq", str(out.resolve())] + names + ["ComicInfo.xml"],
                       cwd=tmp, check=True)


# --------------------------------------------------------------------------- main

def convert(src: Path, out_dir: Path, opts, fonts) -> list[Path]:
    skip = re.compile(opts.skip) if opts.skip else None
    with tempfile.TemporaryDirectory() as tmp:
        if src.is_file() and src.suffix.lower() == ".pdf":
            meta = render_pdf(src, Path(tmp), opts.pdf_dpi)
            root, skip = Path(tmp), None  # PDF pages have no scan-credit filenames to skip
        else:
            root = extract(src, Path(tmp))
            meta = read_comicinfo(root)
        pages = list_pages(root, skip)
        if not pages:
            raise SystemExit(f"{src.name}: no page images found")
        rtl = opts.rtl or meta.get("manga", "").lower() == "yesandrighttoleft"
        jobs = [(str(p), not opts.no_split, rtl, opts.quality, opts.lettering, fonts, opts.min_confidence,
                 opts.max_height) for p in pages]
        results = []
        with ProcessPoolExecutor(max_workers=opts.jobs) as ex:
            for i, res in enumerate(ex.map(process_page, jobs), 1):
                results += res
                print(f"\r  {src.name}: page {i}/{len(pages)}", end="", flush=True)
        cover = results[0][0]

    out_pages = [r[0] for r in results]
    alts = [r[1] for r in results]
    tot = {k: sum(r[2].get(k, 0) for r in results) for k in ("relettered", "cleaned")}

    raw = src.stem if src.is_file() else src.name
    if not meta.get("title"):
        meta["title"] = re.sub(r"_+", " ", raw).strip() or "Comic"
        if meta.get("series"):
            meta["title"] = f"{meta['series']} #{meta['number']}" if meta.get("number") else meta["series"]
    stem = re.sub(r"[^\w\-. ]+", "", raw).strip() or "comic"
    outs = []
    for fmt in dict.fromkeys(opts.format):  # keep order, drop duplicates
        out = out_dir / f"{stem}.{fmt}"
        if src.is_file() and out.resolve() == src.resolve():  # never overwrite the input
            out = out_dir / f"{stem}_converted.{fmt}"
        if fmt == "epub":
            write_epub(out_pages, cover, out, meta, rtl, alts)
        elif fmt == "cbz":
            write_cbz(out_pages, out, meta, rtl)
        else:
            write_cbr(out_pages, out, meta, rtl)
        outs.append(out)
    if opts.report:
        rep = out_dir / f"{stem}_lettering.txt"
        lines = [f"Lettering report for {src.name}", ""]
        for i, (_, text, st) in enumerate(results, 1):
            lines.append(f"Page {i}: {st.get('relettered', 0)} re-lettered, {st.get('cleaned', 0)} cleaned")
            lines += [f"    {t}" for t in text.split(" / ") if t]
        rep.write_text("\n".join(lines) + "\n", encoding="utf-8")
        outs.append(rep)
    done = ", ".join(f"{o} ({o.stat().st_size / 1e6:.1f} MB)" for o in outs)
    letter = (f"; {tot['relettered']} text blocks re-lettered, {tot['cleaned']} cleaned"
              if opts.lettering != "off" else "")
    print(f"\r  {src.name}: {len(pages)} images -> {len(out_pages)} pages{letter} -> {done}")
    return outs


def _formats(value: str) -> list[str]:
    fmts = [f.strip().lower().lstrip(".") for f in value.split(",") if f.strip()]
    bad = [f for f in fmts if f not in FORMATS]
    if bad or not fmts:
        raise argparse.ArgumentTypeError(f"unknown format {', '.join(bad) or value!r}; choose from {', '.join(FORMATS)}")
    return fmts


def main():
    ap = argparse.ArgumentParser(description="Convert CBZ/CBR comics and PDFs to EPUB, with clearer lettering.")
    ap.add_argument("inputs", nargs="+", type=Path, help="CBZ/CBR/ZIP/RAR/PDF files or folders of images")
    ap.add_argument("-o", "--out", type=Path, help="output folder (default: next to each input)")
    ap.add_argument("-f", "--format", type=_formats, default=["epub"],
                    help="output format(s), comma-separated: epub (default), cbz, cbr  e.g. --format epub,cbz")
    ap.add_argument("--lettering", choices=["reletter", "clean", "off"], default="reletter",
                    help="reletter = OCR + redraw text in a clean font (default); clean = sharpen the "
                         "existing lettering in place; off = plain format conversion")
    ap.add_argument("--min-confidence", type=float, default=80.0,
                    help="OCR confidence (0-100) needed to re-letter a bubble; below it the bubble is "
                         "only cleaned (default 80)")
    ap.add_argument("--font", help="font file for re-lettering (default: Comic Neue Bold)")
    ap.add_argument("--font-emphasis", help="font file for emphasised words (default: Comic Neue Bold Italic)")
    ap.add_argument("--report", action="store_true", help="also write the text read from each page to a .txt")
    ap.add_argument("--rtl", action="store_true", help="right-to-left reading order (manga)")
    ap.add_argument("--no-split", action="store_true", help="keep two-page spreads as one page")
    ap.add_argument("--max-height", type=int, help="shrink pages taller than this many pixels (default: keep size)")
    ap.add_argument("--pdf-dpi", type=int, default=200, help="resolution for rendering PDF pages (default 200)")
    ap.add_argument("--quality", type=int, default=95, help="JPEG quality for changed pages (default 95)")
    ap.add_argument("--skip", default=DEFAULT_SKIP,
                    help="regex of image filenames to drop (default skips credit pages starting with 'z'; '' keeps all)")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count(), help="parallel workers")
    opts = ap.parse_args()

    fonts = None
    if opts.lettering != "off":
        if cv2 is None:
            sys.exit("Lettering needs OpenCV and NumPy:  pip install numpy opencv-python-headless\n"
                     "(or use --lettering off for a plain conversion)")
        if opts.lettering == "reletter":
            fonts = {"regular": opts.font or find_font("regular"),
                     "emph": opts.font_emphasis or opts.font or find_font("emph")}
            if not shutil.which(TESSERACT):
                print("note: Tesseract OCR not found (sudo apt install tesseract-ocr) - cleaning lettering instead")
                opts.lettering = "clean"
            elif not fonts["regular"]:
                print("note: no comic font found (sudo apt install fonts-comic-neue, or --font) - cleaning instead")
                opts.lettering = "clean"

    for src in opts.inputs:
        if not src.exists():
            print(f"skipping {src}: not found")
            continue
        out_dir = opts.out or src.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        convert(src, out_dir, opts, fonts)


if __name__ == "__main__":
    main()
