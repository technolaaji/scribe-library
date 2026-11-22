"""
nbk_to_pdf.py — Convert a Kindle Scribe handwritten notebook (KDF format) to PDF.

A Scribe notebook ("nbk" file inside .notebooks/<UUID>/) is a SQLite database in
Amazon's KDF format containing:
  - Page templates (KDF blobs in Ion format with embedded SVG)
  - Stroke vector data (one record per pen stroke: positions, pressure, thickness)

This module uses the vendored kfxlib (John Howell's KFX Input plugin, GPL v3) to
parse KDF, decode the strokes into SVGs, then rasterizes each SVG page and
combines them into a single PDF.

Pipeline:
    nbk file
      → kfxlib.YJ_Book.convert_to_epub()        # KDF → EPUB with one SVG per page
      → extract SVGs from the EPUB ZIP
      → rasterize each SVG to PNG (cairosvg)
      → stitch PNGs into a PDF (Pillow)

Public entry point:
    convert(nbk_path, out_pdf_path) -> bool
"""

from __future__ import annotations

import io
import logging
import re
import sys
import zipfile
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Make sure the vendored kfxlib is importable. The folder lives next to this
# file, regardless of where the server is launched from.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))


# ── Dependency probe ─────────────────────────────────────────────────────────
# We do imports lazily so a missing dep produces a clear error message at
# conversion time rather than a confusing ImportError at server startup.

def _check_deps() -> Optional[str]:
    """Return None if everything is installed, else a human-readable hint."""
    missing: list[str] = []
    try:
        import lxml  # noqa: F401
    except ImportError:
        missing.append("lxml")
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        missing.append("Pillow")
    try:
        import cairosvg  # noqa: F401
    except ImportError:
        missing.append("cairosvg")
    if missing:
        return (
            "Missing required Python packages: " + ", ".join(missing) + "\n"
            "Install with: pip3 install " + " ".join(missing) + "\n"
            "Note: cairosvg requires libcairo (macOS: `brew install cairo`, "
            "Linux: `sudo apt install libcairo2`)."
        )
    return None


# ── Core conversion ──────────────────────────────────────────────────────────

# ── Render dimensions ────────────────────────────────────────────────────────
#
# The Kindle Scribe screen is 1872×2480 px at 300 dpi (≈ 6.24"×8.27"). The
# canvas dimensions kfxlib reports for each notebook page (in "NMDL units")
# vary by template — e.g. `narrow_rule_margin_1860x2480` literally encodes
# 1860×2480 NMDL units. Other templates use a higher-resolution unit grid.
#
# We don't try to guess a fixed PPI. Instead we render each page so its
# *longest* SVG dimension lands at TARGET_LONG_EDGE_PX. That gives consistent
# pixel sizes across notebooks regardless of template, and keeps strokes at
# the same physical scale they appear on the device.
#
# 2480 matches the Scribe's native vertical resolution. Bump it up if you
# want crisper output; down if PDFs are too big.
TARGET_LONG_EDGE_PX = 2480

# Legacy knobs kept for callers that still pass them. Prefer the long-edge
# constant above for consistent output.
RENDER_DPI = 300
NMDL_PPI = 300  # treat 1 NMDL unit ≈ 1 px @ 300dpi as the rough mapping


def _epub_pages(epub_bytes: bytes) -> list[tuple[str, "PageSpec"]]:
    """
    Pull out the per-page render specs from an EPUB produced by kfxlib's
    notebook converter.

    The actual layout (per yj_to_epub_notebook.py / yj_to_epub.py) is:

        OEBPS/
          content.opf                  ← manifest + spine
          <section>.xhtml              ← per-page wrapper, in spine order
          <section>.svg                ← per-page CONTENT (the strokes), referenced
                                          from the .xhtml via <image xlink:href="...">
          <template>.svg               ← shared page templates (rules/grids/etc.)

    Each page XHTML wraps an <svg> that references TWO image layers — first
    the page template (rules/margins), then the user's strokes. We need to
    composite both for the page to look like the original handwritten note.

    Returns a list of (page_id, PageSpec) in reading order.
    """
    pages: list[tuple[str, PageSpec]] = []
    with zipfile.ZipFile(io.BytesIO(epub_bytes), "r") as zf:
        names = zf.namelist()
        names_lower = {n.lower(): n for n in names}

        opf_name = _find_opf(zf)
        if not opf_name:
            log.warning("EPUB has no OPF — falling back to raw .svg scan")
            return _fallback_pages_from_svgs(zf)

        opf_dir = opf_name.rsplit("/", 1)[0] + "/" if "/" in opf_name else ""
        manifest = _opf_manifest_full(zf, opf_name)
        spine = _spine_ids_in_order(zf, opf_name)

        if not spine:
            log.warning("EPUB has empty spine — falling back to raw .svg scan")
            return _fallback_pages_from_svgs(zf)

        for idref in spine:
            entry = manifest.get(idref)
            if not entry:
                continue
            href, mtype = entry

            full_path = _resolve_href(opf_dir + href, names_lower) if href else None
            if not full_path:
                continue

            try:
                page_bytes = zf.read(full_path)
            except KeyError:
                continue

            spec = _build_page_spec(
                page_bytes, full_path, zf, names_lower, mtype)
            if spec:
                pages.append((idref, spec))

    if not pages:
        with zipfile.ZipFile(io.BytesIO(epub_bytes), "r") as zf:
            return _fallback_pages_from_svgs(zf)
    return pages


# A page is either a single SVG (single-layer) or a list of layer SVG bytes
# (multi-layer, composited at pixel level). PageSpec is the union.
class PageSpec:
    """Render specification for one notebook page."""

    def __init__(self, layers: list[bytes]):
        self.layers = layers

    @classmethod
    def single(cls, svg: bytes) -> "PageSpec":
        return cls([svg])

    @classmethod
    def layered(cls, svgs: list[bytes]) -> "PageSpec":
        return cls(svgs)

    @property
    def is_layered(self) -> bool:
        return len(self.layers) > 1


def _opf_manifest_full(zf: zipfile.ZipFile, opf_path: str) -> dict[str, tuple[str, str]]:
    """Like _opf_manifest but returns id -> (href, media-type) for ALL items."""
    try:
        opf = zf.read(opf_path).decode("utf-8", errors="replace")
    except KeyError:
        return {}
    result: dict[str, tuple[str, str]] = {}
    # Match in any attribute order (id, href, media-type) — there are at least
    # three orderings used in the wild, so we extract via individual lookups.
    for m in re.finditer(r"<item\b([^>]+)/>", opf):
        attrs = m.group(1)
        id_m = re.search(r'\bid="([^"]+)"', attrs)
        href_m = re.search(r'\bhref="([^"]+)"', attrs)
        mt_m = re.search(r'\bmedia-type="([^"]+)"', attrs)
        if id_m and href_m:
            result[id_m.group(1)] = (href_m.group(1),
                                     mt_m.group(1) if mt_m else "")
    return result


def _resolve_href(href: str, names_lower: dict[str, str]) -> str | None:
    """Find the actual zip entry name matching a relative href."""
    # Direct hit
    if href in names_lower.values():
        return href
    if href.lower() in names_lower:
        return names_lower[href.lower()]
    # Try basename match (covers cases where the OPF path prefix is wrong)
    base = href.rsplit("/", 1)[-1].lower()
    for low, real in names_lower.items():
        if low.endswith("/" + base) or low == base:
            return real
    return None


def _build_page_spec(
    page_bytes: bytes,
    page_path: str,
    zf: zipfile.ZipFile,
    names_lower: dict[str, str],
    media_type: str,
) -> "PageSpec | None":
    """
    Build a render spec for one page.

    A) page_bytes is already an SVG file → single-layer spec.

    B) page_bytes is XHTML containing <image xlink:href="X.svg"> references
       → multi-layer spec: each referenced SVG becomes one layer, rasterized
         separately, then alpha-composited on top of each other in PIL.
         (We composite at pixel level rather than at SVG level because
         template SVGs frequently contain nested <image> refs to JXR/PNG
         files that cairosvg cannot resolve from a zip.)

    C) page_bytes is XHTML with strokes inlined directly → single-layer spec
       extracted from the inline <svg>.
    """
    # Case A: already an SVG file
    if media_type == "image/svg+xml" or page_path.lower().endswith(".svg"):
        return PageSpec.single(page_bytes)

    # Otherwise it's XHTML/HTML wrapping the visual content.
    head = page_bytes[:512].lower()
    if b"<html" in head or b"<?xml" in head or b"<!doctype" in head:
        referenced_svgs = _find_referenced_svgs(page_bytes, page_path, names_lower)

        if referenced_svgs:
            # Read each layer from the zip. Skip layers that are missing or
            # empty rather than failing the whole page.
            layer_bytes: list[bytes] = []
            for svg_path in referenced_svgs:
                try:
                    data = zf.read(svg_path)
                    if data.strip():
                        layer_bytes.append(data)
                except KeyError:
                    log.debug("Layer %s referenced but not in zip", svg_path)
            if layer_bytes:
                return PageSpec.layered(layer_bytes)

        # No external refs → maybe strokes inlined directly
        inline = _isolate_svg(page_bytes)
        if inline:
            return PageSpec.single(inline)
        return None

    # Fall back: try to find an <svg> in whatever this is
    inline = _isolate_svg(page_bytes)
    return PageSpec.single(inline) if inline else None


def _find_referenced_svgs(
    xhtml_bytes: bytes,
    xhtml_path: str,
    names_lower: dict[str, str],
) -> list[str]:
    """
    Scan an XHTML/SVG page for <image> elements and return the resolved zip
    paths of each referenced .svg file, in document order.

    Uses lxml because lxml normalizes namespace prefixes — kfxlib's serialized
    output may use any of `xlink:href`, `{http://www.w3.org/1999/xlink}href`,
    or even bare `href`, depending on how the namespace map was set up. A
    regex misses some of these; the tree walk catches them all.
    """
    from lxml import etree

    refs: list[str] = []
    try:
        # Recover from minor malformations (BOMs, stray text, etc.). The XHTML
        # we get from kfxlib is well-formed, but let's not be brittle.
        parser = etree.XMLParser(recover=True, ns_clean=True)
        root = etree.fromstring(xhtml_bytes, parser=parser)
    except Exception as e:
        log.debug("XML parse for %s failed (%s) — falling back to regex",
                  xhtml_path, e)
        # Fall back to the old regex approach so a single malformed page
        # doesn't kill the whole notebook.
        text = xhtml_bytes.decode("utf-8", errors="replace")
        for m in re.finditer(
            r'<image\b[^>]*?\b(?:xlink:href|href)="([^"]+\.svg)"',
            text, re.IGNORECASE,
        ):
            refs.append(m.group(1))
    else:
        # Walk every descendant; pick up any <image> element regardless of
        # namespace, and grab its href from any of the candidate attributes.
        XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
        for elem in root.iter():
            tag = etree.QName(elem.tag).localname.lower() if elem.tag else ""
            if tag != "image":
                continue
            href = (
                elem.get(XLINK_HREF)
                or elem.get("href")
                or elem.get("xlink:href")  # some serializers don't expand
            )
            if href and href.lower().endswith(".svg"):
                refs.append(href)

    if not refs:
        return []

    base_dir = xhtml_path.rsplit("/", 1)[0] if "/" in xhtml_path else ""
    resolved: list[str] = []
    for ref in refs:
        if ref.startswith(("http://", "https://", "data:")):
            continue
        # Resolve relative to the XHTML's directory
        if ref.startswith("/"):
            candidate = ref.lstrip("/")
        elif base_dir:
            # naive join — collapse single ../
            if ref.startswith("../"):
                parent = base_dir.rsplit("/", 1)[0] if "/" in base_dir else ""
                candidate = (parent + "/" + ref[3:]) if parent else ref[3:]
            else:
                candidate = base_dir + "/" + ref
        else:
            candidate = ref

        real = _resolve_href(candidate, names_lower)
        if real and real not in resolved:
            resolved.append(real)
        elif not real:
            log.debug("XHTML %s references %s which is not in the EPUB zip",
                      xhtml_path, ref)
    return resolved


def _extract_svg_dimensions(svg_str: str) -> tuple[str | None, str | None, str | None]:
    """
    Pull (viewBox, width, height) attributes from the FIRST <svg> element.
    Returns None for any that are missing. Uses bounded regex anchored to
    the opening <svg ...> tag to avoid matching nested <svg> elements that
    kfxlib sometimes embeds inside notebook content.
    """
    open_tag_m = re.search(r"<svg\b[^>]*>", svg_str, re.IGNORECASE)
    if not open_tag_m:
        return None, None, None
    tag = open_tag_m.group(0)
    vb = re.search(r'\bviewBox="([^"]+)"', tag, re.IGNORECASE)
    w  = re.search(r'\bwidth="([^"]+)"', tag, re.IGNORECASE)
    h  = re.search(r'\bheight="([^"]+)"', tag, re.IGNORECASE)
    return (vb.group(1) if vb else None,
            w.group(1) if w else None,
            h.group(1) if h else None)


def _composite_svgs(svg_paths: list[str], zf: zipfile.ZipFile) -> bytes:
    """
    Read a sequence of .svg files from the zip and stack them into a single
    SVG document so the rasterizer paints layers in order.

    Critical: cairosvg requires the root <svg> to advertise an explicit size
    or it raises "SVG size is undefined". Some kfxlib-emitted layer SVGs
    have only a viewBox (no width/height), and the very first layer might
    even be missing the viewBox. So we look at every layer to find a usable
    viewBox, then synthesize matching pixel-equivalent width/height.
    """
    layers: list[tuple[str, bytes]] = []
    for sp in svg_paths:
        try:
            data = zf.read(sp)
        except KeyError:
            continue
        layers.append((sp, data))

    if not layers:
        return b""

    # Find a viewBox by scanning layers in order — first hit wins.
    viewbox = None
    src_w = src_h = None
    for path, data in layers:
        s = data.decode("utf-8", errors="replace")
        vb, w, h = _extract_svg_dimensions(s)
        if viewbox is None and vb:
            viewbox = vb
        if src_w is None and w:
            src_w = w
        if src_h is None and h:
            src_h = h
        if viewbox and src_w and src_h:
            break

    # Fall back to the standard Scribe canvas if NOTHING declared dimensions.
    if not viewbox:
        viewbox = "0 0 15624 20832"
        log.debug("No viewBox in any layer — defaulting to %s", viewbox)

    # If width/height weren't declared, derive them from the viewBox so the
    # SVG has a concrete pixel size cairosvg can use.
    if not (src_w and src_h):
        try:
            parts = viewbox.split()
            vb_w, vb_h = float(parts[2]), float(parts[3])
            src_w = src_w or f"{vb_w:.0f}"
            src_h = src_h or f"{vb_h:.0f}"
        except (IndexError, ValueError):
            src_w = src_w or "15624"
            src_h = src_h or "20832"

    out = []
    out.append('<?xml version="1.0" encoding="utf-8"?>')
    out.append(
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1" '
        f'viewBox="{viewbox}" width="{src_w}" height="{src_h}">'
    )
    # Force a white background so eraser strokes show as paper.
    out.append('<rect x="0" y="0" width="100%" height="100%" fill="white"/>')

    for path, data in layers:
        s = data.decode("utf-8", errors="replace")
        # Strip XML decl + DOCTYPE from inner content
        s = re.sub(r"<\?xml[^?]*\?>", "", s)
        s = re.sub(r"<!DOCTYPE[^>]*>", "", s)
        # Pull out everything between <svg ...> and </svg>
        m = re.search(r"<svg\b[^>]*>(.*)</svg>\s*$", s, re.DOTALL)
        if not m:
            continue
        inner = m.group(1)
        out.append(f'<g data-source="{path}">')
        out.append(inner)
        out.append("</g>")

    out.append("</svg>")
    return ("\n".join(out)).encode("utf-8")


def _resolve_svg_images(
    svg_bytes: bytes,
    svg_path: str,
    zf: zipfile.ZipFile,
    names_lower: dict[str, str],
) -> bytes:
    """
    If a standalone .svg references other .svg files via <image>, inline them.
    Otherwise return the SVG unchanged.
    """
    text = svg_bytes.decode("utf-8", errors="replace")
    if "xlink:href" not in text and 'href="' not in text:
        return svg_bytes
    # Find any <image> referencing another .svg
    refs = _find_referenced_svgs(svg_bytes, svg_path, names_lower)
    if not refs:
        return svg_bytes
    # Composite this svg + all the layers it references
    all_layers = [svg_path] + [r for r in refs if r != svg_path]
    return _composite_svgs(all_layers, zf)


def _fallback_pages_from_svgs(zf: zipfile.ZipFile) -> list[tuple[str, "PageSpec"]]:
    """
    Last-resort: every .svg file in the zip becomes a single-layer page,
    in zip order. Used when we can't read the spine for whatever reason.
    """
    pages = []
    for name in zf.namelist():
        if name.lower().endswith(".svg"):
            try:
                data = zf.read(name)
            except KeyError:
                continue
            pages.append((name, PageSpec.single(data)))
    return pages


def _find_opf(zf: zipfile.ZipFile) -> Optional[str]:
    """Locate the OPF (package) file — usually content.opf inside OEBPS/."""
    # First try META-INF/container.xml which is the EPUB-spec way.
    try:
        container = zf.read("META-INF/container.xml").decode("utf-8", errors="replace")
        m = re.search(r'full-path="([^"]+\.opf)"', container)
        if m:
            return m.group(1)
    except KeyError:
        pass
    # Fallback: any .opf in the archive.
    for name in zf.namelist():
        if name.endswith(".opf"):
            return name
    return None


def _spine_ids_in_order(zf: zipfile.ZipFile, opf_path: str) -> list[str]:
    """Return the spine itemref IDs in document order."""
    try:
        opf = zf.read(opf_path).decode("utf-8", errors="replace")
    except KeyError:
        return []
    spine_block = re.search(r"<spine[^>]*>(.*?)</spine>", opf, re.DOTALL)
    if not spine_block:
        return []
    return [m.group(1) for m in re.finditer(
        r'<itemref[^>]+idref="([^"]+)"', spine_block.group(1))]


def _isolate_svg(blob: bytes) -> Optional[bytes]:
    """Extract a single <svg>…</svg> block from a larger document."""
    start = blob.find(b"<svg")
    if start < 0:
        return None
    end = blob.rfind(b"</svg>")
    if end < 0:
        return None
    end += len(b"</svg>")
    svg = blob[start:end]
    if b"xmlns=" not in svg[:200]:
        svg = svg.replace(
            b"<svg",
            b'<svg xmlns="http://www.w3.org/2000/svg" '
            b'xmlns:xlink="http://www.w3.org/1999/xlink"',
            1,
        )
    return svg


def _rasterize_svg(svg_bytes: bytes, dpi: int = RENDER_DPI):
    """
    Rasterize one SVG page to a PIL.Image (RGB, white background).

    We render so the longer SVG dimension equals TARGET_LONG_EDGE_PX. This
    gives consistent output sizes across notebook templates that use
    different NMDL unit scales (1860×2480, 15624×20832, etc.).

    cairosvg refuses an SVG without explicit width/height — even if a
    viewBox is present — raising "SVG size is undefined". We pre-rewrite the
    root <svg> tag to ensure all three sizing attributes are present, and
    pass output_width/output_height explicitly to lock in our target size.

    The `dpi` parameter is kept for backward compatibility but only affects
    output if the SVG is too small for TARGET_LONG_EDGE_PX scaling to be
    meaningful — see the fallback below.
    """
    import cairosvg
    from PIL import Image

    text = svg_bytes.decode("utf-8", errors="replace")
    viewbox, w_attr, h_attr = _extract_svg_dimensions(text)

    # Determine the source canvas in user units. Priority:
    #   1) third/fourth components of viewBox (most reliable for kfxlib)
    #   2) explicit width/height on the root <svg>
    #   3) the Scribe-screen-native fallback
    canvas_w = canvas_h = None
    if viewbox:
        try:
            parts = viewbox.split()
            canvas_w = float(parts[2])
            canvas_h = float(parts[3])
        except (IndexError, ValueError):
            pass

    if (canvas_w is None or canvas_h is None) and w_attr and h_attr:
        try:
            canvas_w = float(re.match(r"[\d.]+", w_attr).group(0))
            canvas_h = float(re.match(r"[\d.]+", h_attr).group(0))
        except (AttributeError, ValueError):
            canvas_w = canvas_h = None

    if canvas_w is None or canvas_h is None:
        # Default to Scribe portrait dimensions in pixels
        canvas_w, canvas_h = 1860.0, 2480.0
        log.debug("No size info in SVG — defaulting to %dx%d", canvas_w, canvas_h)

    # Scale so the longer edge hits TARGET_LONG_EDGE_PX. Aspect ratio preserved.
    long_edge = max(canvas_w, canvas_h)
    scale = TARGET_LONG_EDGE_PX / long_edge if long_edge > 0 else 1.0
    output_width = max(1, int(round(canvas_w * scale)))
    output_height = max(1, int(round(canvas_h * scale)))

    log.debug("Rasterize: viewBox=%s canvas=%.0fx%.0f → %dx%d px (scale=%.3f)",
              viewbox, canvas_w, canvas_h, output_width, output_height, scale)

    # Belt-and-braces: ensure the SVG has explicit width/height/viewBox so
    # cairosvg never raises "SVG size is undefined" before our output sizing
    # arguments take effect.
    svg_for_cairo = _ensure_root_svg_has_size(
        svg_bytes,
        viewbox=viewbox or f"0 0 {canvas_w:.0f} {canvas_h:.0f}",
        width=f"{canvas_w:.0f}",
        height=f"{canvas_h:.0f}",
    )

    try:
        png_bytes = cairosvg.svg2png(
            bytestring=svg_for_cairo,
            output_width=output_width,
            output_height=output_height,
            background_color="white",
            unsafe=True,  # allow <image href="..."> to inline resources
        )
    except Exception as e:
        # Save the failing SVG so we can inspect it.
        debug_path = _DEBUG_DIR / f"failed_{abs(hash(svg_bytes)):x}.svg"
        try:
            _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            debug_path.write_bytes(svg_for_cairo)
            log.error("cairosvg failed; failing SVG saved at %s", debug_path)
        except Exception:
            pass
        log.error("cairosvg failed (canvas %.0fx%.0f → %dx%d px): %s",
                  canvas_w, canvas_h, output_width, output_height, e)
        raise

    return Image.open(io.BytesIO(png_bytes)).convert("RGB")


def _rasterize_page(spec: "PageSpec"):
    """
    Rasterize a page (which may be one or more SVG layers) into a single
    PIL.Image. Multi-layer pages are alpha-composited in PIL: each layer is
    rendered onto a transparent canvas, then stacked. Layer ordering follows
    the order kfxlib emitted the references — typically [template, strokes],
    so strokes paint on top of the rules.
    """
    from PIL import Image

    if not spec.layers:
        # Empty page — return a blank Scribe-sized white image
        return Image.new("RGB", (1860, 2480), color="white")

    # Single layer: just rasterize it
    if not spec.is_layered:
        return _rasterize_svg(spec.layers[0])

    # Multi-layer: render each layer with TRANSPARENT background so we can
    # stack them. The base layer (template/rules) gets a white background;
    # subsequent layers (strokes) are drawn on transparent so the underlying
    # template remains visible where there are no strokes.
    layer_images = []
    target_size = None
    for i, layer_svg in enumerate(spec.layers):
        # Layer 0 = template/background; subsequent layers = strokes/overlays.
        # Background-color="white" only on the base; transparent on top.
        bg = "white" if i == 0 else None
        try:
            img = _rasterize_svg_with_bg(layer_svg, background=bg)
        except Exception as e:
            log.warning("Layer %d failed to rasterize (skipping): %s", i, e)
            continue

        if target_size is None:
            target_size = img.size
        elif img.size != target_size:
            # Layers may have different intrinsic sizes (template vs content).
            # Resize all of them to match the first layer's size.
            img = img.resize(target_size, Image.LANCZOS)

        layer_images.append(img)

    if not layer_images:
        # All layers failed — fail the page
        raise RuntimeError("All layers failed to rasterize")

    # Composite: start from the first (base) image, paste each subsequent
    # layer onto it using its alpha channel as a mask.
    base = layer_images[0].convert("RGBA")
    for overlay in layer_images[1:]:
        rgba = overlay.convert("RGBA")
        base = Image.alpha_composite(base, rgba)

    # Final flatten to white-backed RGB for PDF.
    final = Image.new("RGB", base.size, color="white")
    final.paste(base, mask=base.split()[3])  # use alpha as mask
    return final


def _rasterize_svg_with_bg(svg_bytes: bytes, background: str | None = "white"):
    """
    Like _rasterize_svg but allows a transparent background (background=None)
    so multi-layer pages can be alpha-composited.

    Returns an RGBA image when background=None, RGB otherwise.
    """
    import cairosvg
    from PIL import Image

    text = svg_bytes.decode("utf-8", errors="replace")
    viewbox, w_attr, h_attr = _extract_svg_dimensions(text)

    canvas_w = canvas_h = None
    if viewbox:
        try:
            parts = viewbox.split()
            canvas_w = float(parts[2])
            canvas_h = float(parts[3])
        except (IndexError, ValueError):
            pass
    if (canvas_w is None or canvas_h is None) and w_attr and h_attr:
        try:
            canvas_w = float(re.match(r"[\d.]+", w_attr).group(0))
            canvas_h = float(re.match(r"[\d.]+", h_attr).group(0))
        except (AttributeError, ValueError):
            canvas_w = canvas_h = None

    if canvas_w is None or canvas_h is None:
        canvas_w, canvas_h = 1860.0, 2480.0

    long_edge = max(canvas_w, canvas_h)
    scale = TARGET_LONG_EDGE_PX / long_edge if long_edge > 0 else 1.0
    output_width = max(1, int(round(canvas_w * scale)))
    output_height = max(1, int(round(canvas_h * scale)))

    svg_for_cairo = _ensure_root_svg_has_size(
        svg_bytes,
        viewbox=viewbox or f"0 0 {canvas_w:.0f} {canvas_h:.0f}",
        width=f"{canvas_w:.0f}",
        height=f"{canvas_h:.0f}",
    )

    # cairosvg's background_color=None means transparent.
    kwargs = dict(
        bytestring=svg_for_cairo,
        output_width=output_width,
        output_height=output_height,
        unsafe=True,
    )
    if background is not None:
        kwargs["background_color"] = background

    png_bytes = cairosvg.svg2png(**kwargs)
    img = Image.open(io.BytesIO(png_bytes))

    if background is None:
        return img.convert("RGBA")
    return img.convert("RGB")


def _ensure_root_svg_has_size(
    svg_bytes: bytes,
    viewbox: str,
    width: str,
    height: str,
) -> bytes:
    """
    Rewrite the root <svg ...> opening tag so it has explicit width, height,
    AND viewBox attributes. Existing values on the root are kept; only
    missing attributes are added. This makes cairosvg happy regardless of
    what the source emitted.
    """
    text = svg_bytes.decode("utf-8", errors="replace")
    open_m = re.search(r"<svg\b([^>]*)>", text, re.IGNORECASE)
    if not open_m:
        return svg_bytes

    attrs = open_m.group(1)
    additions = []
    if not re.search(r'\bviewBox=', attrs, re.IGNORECASE):
        additions.append(f'viewBox="{viewbox}"')
    if not re.search(r'\bwidth=', attrs, re.IGNORECASE):
        additions.append(f'width="{width}"')
    if not re.search(r'\bheight=', attrs, re.IGNORECASE):
        additions.append(f'height="{height}"')
    if not re.search(r'\bxmlns=', attrs, re.IGNORECASE):
        additions.append('xmlns="http://www.w3.org/2000/svg"')
    if not re.search(r'\bxmlns:xlink=', attrs, re.IGNORECASE):
        additions.append('xmlns:xlink="http://www.w3.org/1999/xlink"')

    if not additions:
        return svg_bytes

    new_open = f"<svg {' '.join(additions)}{attrs}>"
    return (text[:open_m.start()] + new_open + text[open_m.end():]).encode("utf-8")


# Where failing SVGs go for inspection. Scoped to the module so server.py can
# wire it to a per-notebook directory at conversion time.
_DEBUG_DIR = Path("/tmp/scribe_reader_debug")


class ConversionError(Exception):
    """Raised by convert() when a step fails. Carries the step that failed and
    the kfxlib error/warning summary so the server can show useful messages."""

    def __init__(self, message: str, *, step: str = "", kfx_errors=None,
                 kfx_warnings=None, traceback_str: str = ""):
        super().__init__(message)
        self.step = step
        self.kfx_errors = list(kfx_errors or [])
        self.kfx_warnings = list(kfx_warnings or [])
        self.traceback = traceback_str


def _convert_with_kfxlib(nbk_path: Path):
    """
    Run kfxlib on the notebook file. Returns (epub_bytes, errors, warnings).

    kfxlib's file-extension dispatch only accepts {.kpf, .kfx, .azw8, .ion,
    .kfx-zip, .zip}. Renaming our notebook to ".kpf" doesn't work either,
    because that triggers kfxlib's zip-or-KDF detection — and `.kpf` files are
    presumed to be ZIP archives unless proven otherwise. (A real `.kpf` is a
    zip containing a `.kdf` inside.)

    The clean fix is to use kfxlib's directory-input mode: when YJ_Book is
    given a directory, it scans for files matching specific names — including
    the literal name "nbk" (see check_located_file in yj_book.py), which is
    the on-device layout for Scribe notebooks.

    So we stage the .nbk file into a temp directory as a file named "nbk"
    and hand kfxlib the directory.
    """
    import shutil
    import tempfile
    import traceback

    from kfxlib.yj_book import YJ_Book
    from kfxlib.message_logging import set_logger, JobLog

    # Wire kfxlib's logging through Python's logging module so we see its
    # messages on the server console AND collect them for the response.
    base = logging.getLogger("kfxlib")
    if not base.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [kfxlib] %(message)s"))
        base.addHandler(h)
        base.setLevel(logging.INFO)
    job_log = JobLog(base)
    set_logger(job_log)

    # Stage the notebook as a directory containing a file literally named
    # "nbk". This matches the on-device layout (.notebooks/<UUID>/nbk) and
    # is the input shape kfxlib's check_located_file() recognises.
    tmpdir = Path(tempfile.mkdtemp(prefix="nbk_kfxlib_"))
    try:
        staged_dir = tmpdir / "notebook"
        staged_dir.mkdir()
        staged = staged_dir / "nbk"
        shutil.copyfile(nbk_path, staged)
        log.debug("Staged notebook directory: %s", staged_dir)

        try:
            book = YJ_Book(str(staged_dir))
            epub_bytes = book.convert_to_epub()
        except Exception as e:
            tb = traceback.format_exc()
            raise ConversionError(
                f"kfxlib raised: {type(e).__name__}: {e}",
                step="kfxlib",
                kfx_errors=job_log.errors,
                kfx_warnings=job_log.warnings,
                traceback_str=tb,
            ) from e

        return epub_bytes, list(job_log.errors), list(job_log.warnings)
    finally:
        set_logger(None)
        shutil.rmtree(tmpdir, ignore_errors=True)


def convert(nbk_path: Path, out_pdf_path: Path) -> bool:
    """
    Convert one Scribe notebook (KDF) to PDF. Returns True on success.

    On failure, logs detailed diagnostics and re-raises a ConversionError
    only when called via convert_or_raise(). The boolean-returning form here
    swallows exceptions for backward compatibility with the original API.
    """
    try:
        convert_or_raise(nbk_path, out_pdf_path)
        return True
    except ConversionError as e:
        log.error("Conversion failed at step '%s': %s", e.step, e)
        if e.kfx_errors:
            log.error("kfxlib errors:")
            for msg in e.kfx_errors:
                log.error("  %s", msg)
        if e.kfx_warnings:
            log.warning("kfxlib warnings (first 5):")
            for msg in e.kfx_warnings[:5]:
                log.warning("  %s", msg)
        if e.traceback:
            log.debug("Traceback:\n%s", e.traceback)
        return False


def convert_or_raise(nbk_path: Path, out_pdf_path: Path) -> dict:
    """
    Convert one Scribe notebook (KDF) to PDF.

    On success: returns a dict with stats (pages, errors, warnings).
    On failure: raises ConversionError with structured diagnostics so callers
                (e.g. the Flask server) can present them to the user.
    """
    nbk_path = Path(nbk_path)
    out_pdf_path = Path(out_pdf_path)
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Dependency check.
    err = _check_deps()
    if err:
        raise ConversionError(err, step="deps")

    if not nbk_path.exists():
        raise ConversionError(
            f"Notebook file does not exist: {nbk_path}", step="input")

    # 2. Quick magic-byte sanity check before we hand off to kfxlib.
    with open(nbk_path, "rb") as f:
        head = f.read(16)
    if not head.startswith(b"SQLite format 3"):
        raise ConversionError(
            f"This file does not look like a Scribe notebook (KDF/SQLite). "
            f"Expected SQLite header, got: {head!r}",
            step="magic",
        )

    log.info("Reading notebook: %s (%.1f MB)",
             nbk_path, nbk_path.stat().st_size / 1024 / 1024)

    # 3. KDF → EPUB via kfxlib.
    try:
        from kfxlib.yj_book import YJ_Book  # noqa: F401  (validate import)
    except ImportError as e:
        raise ConversionError(
            f"kfxlib is not importable next to nbk_to_pdf.py: {e}",
            step="import",
        )

    epub_bytes, kfx_errors, kfx_warnings = _convert_with_kfxlib(nbk_path)

    if not epub_bytes:
        raise ConversionError(
            "kfxlib returned empty EPUB — notebook may be empty or use an "
            "unsupported format. See server log for kfxlib diagnostics.",
            step="kfxlib", kfx_errors=kfx_errors, kfx_warnings=kfx_warnings,
        )

    # 4. Extract per-page SVGs.
    try:
        pages = _epub_pages(epub_bytes)
    except Exception as e:
        raise ConversionError(
            f"Failed to extract SVG pages from kfxlib's EPUB: {e}",
            step="epub-extract", kfx_errors=kfx_errors, kfx_warnings=kfx_warnings,
        )

    if not pages:
        # Save the EPUB next to the requested PDF so the user can inspect it
        # and tell us what's inside.
        debug_epub = out_pdf_path.with_suffix(".debug.epub")
        try:
            debug_epub.write_bytes(epub_bytes)
        except Exception:
            pass
        raise ConversionError(
            "kfxlib produced an EPUB but no per-page SVGs were found. "
            f"The EPUB has been saved to {debug_epub} for inspection. "
            "This notebook may be empty (no strokes drawn yet).",
            step="epub-extract", kfx_errors=kfx_errors, kfx_warnings=kfx_warnings,
        )

    log.info("Extracted %d page(s) — rendering to %dpx long edge",
             len(pages), TARGET_LONG_EDGE_PX)

    # 5. Rasterize each page (single-layer or multi-layer) and assemble PDF.
    page_images = []
    rasterize_errors: list[str] = []
    for i, (page_id, spec) in enumerate(pages):
        try:
            img = _rasterize_page(spec)
            page_images.append(img)
        except Exception as e:
            msg = f"Page {i + 1} ({page_id}) failed to rasterize: {e}"
            rasterize_errors.append(msg)
            log.error(msg)

    if not page_images:
        raise ConversionError(
            "All pages failed to rasterize. " + " | ".join(rasterize_errors[:3]),
            step="rasterize", kfx_errors=kfx_errors, kfx_warnings=kfx_warnings,
        )

    try:
        page_images[0].save(
            str(out_pdf_path), "PDF",
            resolution=float(RENDER_DPI),
            save_all=True,
            append_images=page_images[1:],
        )
    except Exception as e:
        raise ConversionError(
            f"Failed to write PDF: {e}", step="pdf-write")
    finally:
        for img in page_images:
            try:
                img.close()
            except Exception:
                pass

    log.info("Wrote %s (%.1f MB)", out_pdf_path,
             out_pdf_path.stat().st_size / 1024 / 1024)

    return {
        "pages": len(page_images),
        "kfx_errors": kfx_errors,
        "kfx_warnings": kfx_warnings,
        "rasterize_errors": rasterize_errors,
    }


# ── CLI for ad-hoc testing ───────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Convert a Scribe notebook (nbk) to PDF")
    parser.add_argument("nbk", help="Path to the nbk file (or a copy renamed)")
    parser.add_argument("pdf", help="Output PDF path")
    parser.add_argument("--dpi", type=int, default=RENDER_DPI,
                        help=f"Rasterization DPI (default {RENDER_DPI})")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )

    # Allow CLI override of DPI without touching module state in library mode.
    RENDER_DPI = args.dpi  # type: ignore[assignment]

    ok = convert(Path(args.nbk), Path(args.pdf))
    sys.exit(0 if ok else 1)
