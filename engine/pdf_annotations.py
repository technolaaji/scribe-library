"""
pdf_annotations.py — Decode Scribe stroke sidecars and overlay them on PDFs.

When you write notes on a PDF on a Kindle Scribe, the strokes are saved in a
`<basename>.sdr/` folder next to the PDF in `documents/`. The folder typically
contains one or more KDF (SQLite) blobs holding NMDL-format stroke data —
the same format kfxlib already decodes for `.nbk` notebooks.

Public entry points:

    find_sidecar(pdf_path) -> Path | None
        Look for the `<basename>.sdr/` folder next to a PDF.

    decode_strokes(sidecar_dir) -> AnnotationData
        Run the KDF blobs through kfxlib's notebook pipeline. Returns
        per-page rasterized stroke layers (transparent PNGs).

    overlay_on_pdf(pdf_path, sidecar_dir, out_pdf_path) -> bool
        End-to-end: read PDF, decode strokes, composite each page's strokes
        onto the corresponding PDF page, write a new annotated PDF. Returns
        True on success. Raises AnnotationError with a descriptive message
        otherwise.

If anything goes wrong — missing sidecar, decoder failure, page-count
mismatch — we record what happened in an `AnnotationDebug` object so the
caller can show diagnostics in the UI rather than silently fail.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


class AnnotationError(Exception):
    """Raised when annotation overlay can't be produced."""

    def __init__(self, msg: str, *, step: str = "", details: str = ""):
        super().__init__(msg)
        self.step = step
        self.details = details


@dataclass
class AnnotationDebug:
    """Diagnostic info exposed via the /api/debug/<id> endpoint."""

    sidecar_dir: str = ""
    sidecar_files: list = field(default_factory=list)  # list of (name, size)
    decoded_pages: int = 0
    pdf_pages: int = 0
    pages_with_strokes: list = field(default_factory=list)
    overlay_pages: int = 0
    error: str = ""
    step: str = ""
    log_lines: list = field(default_factory=list)


@dataclass
class AnnotationData:
    """Per-page rasterized stroke layers."""

    # page index (0-based) -> RGBA PIL.Image of strokes (transparent background)
    layers: dict = field(default_factory=dict)
    debug: AnnotationDebug = field(default_factory=AnnotationDebug)


# ── Discovery ────────────────────────────────────────────────────────────────

def find_sidecar(pdf_path: Path) -> Path | None:
    """
    Locate the `.sdr/` folder for a PDF, if it exists.

    Looks for `<basename>.sdr/` next to the PDF. Returns None if no sidecar
    exists (which is the common case for PDFs without annotations).
    """
    pdf_path = Path(pdf_path)
    candidate = pdf_path.with_name(pdf_path.stem + ".sdr")
    if candidate.is_dir():
        return candidate
    # Some firmware variants append the extension to the stem:
    #   "MyDoc.pdf"  → "MyDoc.pdf.sdr"  (rare but observed)
    candidate2 = pdf_path.parent / (pdf_path.name + ".sdr")
    if candidate2.is_dir():
        return candidate2
    return None


# ── Decoding ─────────────────────────────────────────────────────────────────

def decode_strokes(sidecar_dir: Path, pdf_pages: int = 0) -> AnnotationData:
    """
    Run KDF blobs in the sidecar through kfxlib's notebook pipeline.

    `pdf_pages` lets us pre-allocate per-page layers (we only render strokes
    for pages 0 through pdf_pages-1; extras are dropped).

    Returns AnnotationData. Raises AnnotationError with a clear message if
    no decoded data could be produced.
    """
    sidecar_dir = Path(sidecar_dir)
    debug = AnnotationDebug(sidecar_dir=str(sidecar_dir))
    debug.pdf_pages = pdf_pages

    if not sidecar_dir.is_dir():
        raise AnnotationError(
            f"Sidecar folder not found: {sidecar_dir}",
            step="missing-sidecar",
        )

    # Inventory the folder so debug output is informative.
    candidates: list[Path] = []
    for child in sorted(sidecar_dir.iterdir()):
        if child.is_file():
            try:
                size = child.stat().st_size
            except OSError:
                size = 0
            debug.sidecar_files.append((child.name, size))
            candidates.append(child)

    if not candidates:
        raise AnnotationError(
            f"Sidecar folder {sidecar_dir.name} is empty",
            step="empty-sidecar",
        )

    debug.log_lines.append(
        f"Sidecar contents: {[(n, s) for n, s in debug.sidecar_files]}")

    # Try every plausible candidate in order of likelihood. KDF blobs are
    # SQLite under the hood, so the first 16 bytes are 'SQLite format 3\0'.
    # We look for that magic to identify decode candidates.
    SQLITE_MAGIC = b"SQLite format 3\x00"

    decoded_data = None
    last_error = None
    tried = []

    for candidate in candidates:
        try:
            with open(candidate, "rb") as f:
                head = f.read(16)
        except OSError as e:
            log.debug("Could not read %s: %s", candidate, e)
            continue
        if head != SQLITE_MAGIC:
            debug.log_lines.append(
                f"Skip {candidate.name}: not SQLite magic ({head[:4].hex()}…)")
            continue

        debug.log_lines.append(f"Trying {candidate.name} as KDF blob…")
        tried.append(candidate.name)
        try:
            decoded_data = _kfxlib_decode_kdf(candidate)
            debug.log_lines.append(
                f"  ✓ kfxlib produced EPUB ({len(decoded_data)} bytes)")
            break
        except Exception as e:
            last_error = str(e)
            debug.log_lines.append(f"  ✗ {e}")
            continue

    if decoded_data is None:
        raise AnnotationError(
            f"None of the {len(tried)} KDF candidate(s) in the sidecar could "
            f"be decoded. Last error: {last_error}",
            step="kdf-decode-failed",
            details="; ".join(debug.log_lines[-3:]),
        )

    # The decoded EPUB has per-page SVG layers. Walk the spine and pick up
    # each page's stroke layer, then rasterize them as transparent PNGs at
    # a resolution that matches the PDF page rendering.
    try:
        page_layers = _epub_to_stroke_layers(decoded_data, pdf_pages)
    except Exception as e:
        raise AnnotationError(
            f"Could not extract stroke layers from decoded EPUB: {e}",
            step="layer-extraction",
            details=str(e),
        )

    debug.decoded_pages = len(page_layers)
    debug.pages_with_strokes = sorted(page_layers.keys())
    debug.log_lines.append(
        f"Extracted strokes for {len(page_layers)} page(s): "
        f"{debug.pages_with_strokes[:20]}{'…' if len(page_layers) > 20 else ''}")

    return AnnotationData(layers=page_layers, debug=debug)


def _kfxlib_decode_kdf(kdf_path: Path) -> bytes:
    """
    Hand a KDF blob to kfxlib's notebook converter, return EPUB bytes.

    Reuses the same kfxlib path used for `.nbk` notebooks. The KDF format
    is identical — what differs is what's INSIDE (a notebook's strokes vs a
    PDF's annotation overlay).
    """
    # Stage the file in a directory named "nbk" (this is what kfxlib's
    # check_located_file logic looks for). Same trick we use in nbk_to_pdf.
    import shutil
    import tempfile

    from kfxlib.yj_book import YJ_Book
    from kfxlib.message_logging import set_logger, JobLog

    base = logging.getLogger("kfxlib")
    set_logger(JobLog(base))

    staging = Path(tempfile.mkdtemp(prefix="scribe_sdr_"))
    try:
        # kfxlib accepts a directory containing a file literally named "nbk"
        staged = staging / "nbk"
        shutil.copyfile(kdf_path, staged)

        book = YJ_Book(str(staging))
        try:
            epub_bytes = book.convert_to_epub()
        except Exception:
            # The canonical notebook path failed — try the generic path.
            # Some firmware writes annotation blobs that don't fit the
            # notebook mold cleanly. Fall through with the original error.
            raise
    finally:
        set_logger(None)
        shutil.rmtree(staging, ignore_errors=True)

    if not epub_bytes:
        raise RuntimeError("kfxlib returned empty EPUB")
    return epub_bytes


def _epub_to_stroke_layers(
    epub_bytes: bytes, max_pages: int = 0,
) -> dict:
    """
    From a kfxlib-produced EPUB, extract per-page stroke SVGs and rasterize
    them as transparent RGBA PIL.Images. Returns {page_index: PIL.Image}.

    Reuses the same logic as nbk_to_pdf._epub_pages, but renders only the
    STROKE layer (no template/background) — the PDF page itself will be the
    background. We rasterize at the same resolution downstream rendering
    uses so the overlay aligns precisely.
    """
    import nbk_to_pdf

    pages = nbk_to_pdf._epub_pages(epub_bytes)

    layers: dict = {}
    for i, (page_id, spec) in enumerate(pages):
        if max_pages and i >= max_pages:
            break

        # spec.layers[0] is the template (white page background + rules).
        # spec.layers[1:] are stroke layers (what the user wrote).
        # For PDF overlays we only want the strokes — the PDF supplies the
        # background. If there's only one layer (no strokes), skip.
        if len(spec.layers) < 2:
            continue

        # Composite all stroke layers (skip the template at index 0) onto
        # a transparent canvas. Each layer is rasterized with transparent
        # background so they stack cleanly.
        from PIL import Image
        composite = None
        for stroke_svg in spec.layers[1:]:
            try:
                img = nbk_to_pdf._rasterize_svg_with_bg(
                    stroke_svg, background=None)  # RGBA, transparent bg
            except Exception as e:
                log.warning("Stroke layer %d rasterize failed: %s", i, e)
                continue
            if composite is None:
                composite = img.convert("RGBA")
            else:
                rgba = img.convert("RGBA")
                if rgba.size != composite.size:
                    rgba = rgba.resize(composite.size, Image.LANCZOS)
                composite = Image.alpha_composite(composite, rgba)

        if composite is not None and _has_visible_strokes(composite):
            layers[i] = composite

    return layers


def _has_visible_strokes(rgba_image) -> bool:
    """
    Return True if any pixel has non-zero alpha. Used to skip empty pages
    (the user may have written on only some pages of a PDF).
    """
    # Check the alpha channel histogram. If everything is alpha=0, the
    # histogram bin at 0 holds all the pixels.
    if rgba_image.mode != "RGBA":
        return True  # can't tell, assume yes
    alpha = rgba_image.split()[3]
    hist = alpha.histogram()
    total = sum(hist)
    transparent = hist[0] if hist else total
    visible = total - transparent
    # Need at least ~10 visible pixels to count (anti-aliasing noise floor)
    return visible > 10


# ── Overlay onto PDF ─────────────────────────────────────────────────────────

def overlay_on_pdf(
    pdf_path: Path, sidecar_dir: Path, out_pdf_path: Path,
) -> AnnotationDebug:
    """
    Read a PDF, decode its sidecar strokes, render each page with its strokes
    composited on top, and write a new annotated PDF.

    Returns AnnotationDebug for diagnostic display. Raises AnnotationError
    if the pipeline fails before any page is overlaid.
    """
    pdf_path = Path(pdf_path)
    sidecar_dir = Path(sidecar_dir)
    out_pdf_path = Path(out_pdf_path)
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Open the PDF and figure out how many pages it has
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise AnnotationError(
            "PyMuPDF (fitz) is required for PDF annotation overlay. "
            "Install with: pip3 install PyMuPDF",
            step="missing-pymupdf",
        )

    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        raise AnnotationError(
            f"Could not open PDF {pdf_path.name}: {e}",
            step="pdf-open",
        )

    pdf_pages = len(doc)

    # 2. Decode the strokes
    annotation = decode_strokes(sidecar_dir, pdf_pages=pdf_pages)
    debug = annotation.debug

    if not annotation.layers:
        doc.close()
        debug.error = "No pages had decodable strokes"
        debug.step = "no-strokes"
        raise AnnotationError(
            "Decoded the sidecar but no pages had visible strokes — "
            "annotations may be in a format we don't recognize.",
            step="no-strokes",
            details="\n".join(debug.log_lines[-5:]),
        )

    # 3. For each PDF page that has annotations, composite the stroke layer
    #    on top of the PDF page. We do this by:
    #      a) rasterizing the PDF page at high DPI
    #      b) resizing the stroke layer to match
    #      c) alpha-compositing
    #      d) replacing the page in the document with the composited image
    from PIL import Image

    overlay_count = 0
    for page_idx, stroke_layer in annotation.layers.items():
        if page_idx >= pdf_pages:
            debug.log_lines.append(
                f"  ⚠ Skipping page {page_idx} — beyond PDF page count ({pdf_pages})")
            continue

        try:
            page = doc[page_idx]
            # Rasterize the PDF page at 150 DPI (good balance of quality vs size)
            zoom = 150 / 72
            matrix = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            page_img = Image.frombytes(
                "RGB", (pix.width, pix.height), pix.samples
            ).convert("RGBA")

            # Resize the stroke layer to match the PDF page size
            if stroke_layer.size != page_img.size:
                stroke_resized = stroke_layer.resize(
                    page_img.size, Image.LANCZOS)
            else:
                stroke_resized = stroke_layer

            # Composite
            composited = Image.alpha_composite(page_img, stroke_resized)
            composited_rgb = Image.new("RGB", composited.size, "white")
            composited_rgb.paste(composited, mask=composited.split()[3])

            # Replace the page in the PDF doc with the composited image.
            # We do this by deleting the old page and inserting a new image
            # page at the same index. Cleaner than trying to inject the
            # composited bitmap as a content stream.
            buf = io.BytesIO()
            composited_rgb.save(buf, "JPEG", quality=85, optimize=True)
            buf.seek(0)
            page_rect = page.rect
            doc.delete_page(page_idx)
            new_page = doc.new_page(
                pno=page_idx, width=page_rect.width, height=page_rect.height)
            new_page.insert_image(page_rect, stream=buf.getvalue())

            overlay_count += 1
            debug.log_lines.append(f"  ✓ Page {page_idx + 1}: overlay applied")
        except Exception as e:
            debug.log_lines.append(
                f"  ✗ Page {page_idx + 1}: {type(e).__name__}: {e}")
            continue

    debug.overlay_pages = overlay_count

    if overlay_count == 0:
        doc.close()
        debug.error = "No pages were successfully overlaid"
        raise AnnotationError(
            "All overlay attempts failed",
            step="overlay-all-failed",
            details="\n".join(debug.log_lines[-10:]),
        )

    # 4. Write the modified PDF
    try:
        doc.save(str(out_pdf_path), garbage=4, deflate=True)
    finally:
        doc.close()

    debug.log_lines.append(
        f"✓ Wrote annotated PDF: {overlay_count} page(s) with strokes")
    return debug


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        description="Decode and overlay Scribe PDF annotations")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_inspect = sub.add_parser(
        "inspect", help="Look at a sidecar folder; print what's there")
    p_inspect.add_argument("pdf", help="PDF file (sidecar is alongside)")

    p_decode = sub.add_parser("decode", help="Try to decode strokes; emit JSON debug")
    p_decode.add_argument("pdf")

    p_overlay = sub.add_parser("overlay", help="Render annotated PDF")
    p_overlay.add_argument("pdf")
    p_overlay.add_argument("out_pdf")

    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    pdf_path = Path(args.pdf)
    sidecar = find_sidecar(pdf_path)
    if not sidecar:
        print(f"No .sdr/ sidecar found next to {pdf_path}")
        sys.exit(1)

    if args.cmd == "inspect":
        print(f"Sidecar: {sidecar}")
        for child in sorted(sidecar.iterdir()):
            if child.is_file():
                head = b""
                try:
                    head = child.read_bytes()[:16]
                except OSError:
                    pass
                size = child.stat().st_size
                print(f"  {child.name:40s} {size:>10d} bytes  "
                      f"head={head[:8].hex()}")
        sys.exit(0)

    if args.cmd == "decode":
        try:
            data = decode_strokes(sidecar)
            print(json.dumps({
                "sidecar": str(sidecar),
                "decoded_pages": len(data.layers),
                "pages_with_strokes": sorted(data.layers.keys()),
                "debug_log": data.debug.log_lines,
            }, indent=2))
        except AnnotationError as e:
            print(json.dumps({
                "error": str(e),
                "step": e.step,
                "details": e.details,
            }, indent=2))
            sys.exit(2)

    if args.cmd == "overlay":
        try:
            debug = overlay_on_pdf(pdf_path, sidecar, Path(args.out_pdf))
            print(f"OK: wrote {args.out_pdf}")
            print(f"  Overlaid pages: {debug.overlay_pages}")
            print(f"  Debug log:")
            for line in debug.log_lines:
                print(f"    {line}")
        except AnnotationError as e:
            print(f"Error [{e.step}]: {e}", file=sys.stderr)
            if e.details:
                print(f"\nDetails:\n{e.details}", file=sys.stderr)
            sys.exit(2)
