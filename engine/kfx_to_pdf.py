"""
kfx_to_pdf.py — Book conversion and metadata extraction.

For each format we support:
  * .pdf, .epub, .mobi  → no conversion needed; just record metadata
  * .kfx, .azw3, .azw   → use kfxlib to extract metadata, and for image-based
                          books (manga, comics, illustrated) convert to PDF
                          via convert_to_pdf. For text books we currently
                          extract metadata only — opening the book downloads
                          the raw file. (Text KFX → PDF is a separate
                          conversion problem; not in scope for this iteration.)

Public entry points:
    extract_metadata(book_path)  → dict with title, authors, has_cover, cover_bytes
    convert_to_viewable(path, out_pdf_path)  → bool (True on success)
"""

from __future__ import annotations

import io
import logging
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Reuse the kfxlib next to this file
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))


# ── Metadata extraction ──────────────────────────────────────────────────────

def extract_metadata(book_path: Path) -> dict:
    """
    Extract whatever metadata we can from a book file.

    Returns a dict with keys: title, authors (list), has_cover, cover_bytes.
    Always returns a dict — fields are None / empty if extraction failed.
    """
    book_path = Path(book_path)
    ext = book_path.suffix.lower()
    result = {
        "title": None,
        "authors": [],
        "has_cover": False,
        "cover_bytes": None,
        "format": ext.lstrip(".").upper(),
    }

    if ext in (".kfx", ".azw", ".azw3", ".azw8", ".kfx-zip"):
        return _extract_kfx_metadata(book_path, result)
    if ext == ".pdf":
        return _extract_pdf_metadata(book_path, result)
    if ext == ".epub":
        return _extract_epub_metadata(book_path, result)

    # MOBI/PRC: we don't have a parser for these. Use filename only.
    return result


def _extract_kfx_metadata(book_path: Path, result: dict) -> dict:
    """Use kfxlib's get_metadata() to pull title/author/cover from a KFX book."""
    try:
        from kfxlib.yj_book import YJ_Book
        from kfxlib.message_logging import set_logger, JobLog
    except ImportError as e:
        log.warning("kfxlib not available: %s", e)
        return result

    base = logging.getLogger("kfxlib")
    if not base.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [kfxlib] %(message)s"))
        base.addHandler(h)
        base.setLevel(logging.WARNING)  # quieter for metadata-only paths
    set_logger(JobLog(base))

    try:
        book = YJ_Book(str(book_path))
        md = book.get_metadata()
    except Exception as e:
        log.warning("Could not extract KFX metadata for %s: %s", book_path, e)
        return result
    finally:
        set_logger(None)

    if md.title:
        result["title"] = md.title
    if md.authors:
        result["authors"] = list(md.authors)
    if md.cover_image_data:
        # cover_image_data is (extension, raw_bytes) per yj_metadata.py
        try:
            ext, data = md.cover_image_data
            result["has_cover"] = True
            result["cover_bytes"] = data
            result["cover_ext"] = ext or "jpg"
        except Exception:
            pass
    return result


def _extract_pdf_metadata(book_path: Path, result: dict) -> dict:
    """Pull title/author from PDF /Info dict and the first page as a cover."""
    try:
        import pypdf
    except ImportError:
        return result

    try:
        reader = pypdf.PdfReader(str(book_path))
        meta = reader.metadata or {}
        title = meta.get("/Title")
        author = meta.get("/Author")
        if title:
            result["title"] = str(title).strip()
        if author:
            authors = [a.strip() for a in str(author).replace(";", ",").split(",")
                       if a.strip()]
            result["authors"] = authors

        # Try to render the first page at low res as a cover thumbnail
        if len(reader.pages) > 0:
            try:
                page = reader.pages[0]
                images = list(page.images)
                if images:
                    biggest = max(images, key=lambda x: len(x.data))
                    result["cover_bytes"] = biggest.data
                    result["cover_ext"] = "jpg"
                    result["has_cover"] = True
            except Exception:
                pass
    except Exception as e:
        log.debug("PDF metadata extraction failed: %s", e)

    return result


def _extract_epub_metadata(book_path: Path, result: dict) -> dict:
    """Pull title/author/cover from an EPUB OPF + cover image."""
    import re
    import zipfile

    try:
        with zipfile.ZipFile(book_path, "r") as zf:
            # Locate OPF
            try:
                container = zf.read("META-INF/container.xml").decode(
                    "utf-8", errors="replace")
                m = re.search(r'full-path="([^"]+\.opf)"', container)
                opf_path = m.group(1) if m else None
            except KeyError:
                opf_path = None

            if not opf_path:
                for n in zf.namelist():
                    if n.endswith(".opf"):
                        opf_path = n
                        break
            if not opf_path:
                return result

            opf = zf.read(opf_path).decode("utf-8", errors="replace")
            t_m = re.search(r"<dc:title[^>]*>([^<]+)</dc:title>", opf)
            if t_m:
                result["title"] = t_m.group(1).strip()
            authors = re.findall(r"<dc:creator[^>]*>([^<]+)</dc:creator>", opf)
            if authors:
                result["authors"] = [a.strip() for a in authors]

            # Cover detection: look for <meta name="cover" content="X"/> then
            # find item id="X" in the manifest.
            cov_m = re.search(r'<meta\s+name="cover"\s+content="([^"]+)"', opf)
            cover_id = cov_m.group(1) if cov_m else None

            cover_href = None
            if cover_id:
                item_m = re.search(
                    rf'<item[^>]+id="{re.escape(cover_id)}"[^>]+href="([^"]+)"',
                    opf, re.IGNORECASE)
                if item_m:
                    cover_href = item_m.group(1)

            # Fall back: any image item with "cover" in id or href
            if not cover_href:
                fallback = re.search(
                    r'<item[^>]+id="[^"]*cover[^"]*"[^>]+href="([^"]+\.(?:jpg|jpeg|png|gif))"',
                    opf, re.IGNORECASE)
                if fallback:
                    cover_href = fallback.group(1)

            if cover_href:
                opf_dir = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
                full = opf_dir + cover_href
                try:
                    result["cover_bytes"] = zf.read(full)
                    result["cover_ext"] = full.rsplit(".", 1)[-1].lower()
                    result["has_cover"] = True
                except KeyError:
                    pass
    except (zipfile.BadZipFile, OSError) as e:
        log.debug("EPUB metadata extraction failed: %s", e)

    return result


# ── Conversion to viewable (PDF) ─────────────────────────────────────────────

class BookConvertError(Exception):
    """Raised when a book can't be converted to a viewable form."""
    def __init__(self, msg: str, *, step: str = "", reason: str = ""):
        super().__init__(msg)
        self.step = step
        self.reason = reason


def convert_to_viewable(book_path: Path, out_pdf_path: Path) -> bool:
    """
    Convert a book to a PDF the in-browser reader can display.

    Conversion strategy by format:
      .pdf                    → copy as-is
      .kfx / .azw / .azw3     → first try image-book PDF (works for
                                comics/manga); on failure, fall back to
                                text-book rendering (KFX → EPUB → WeasyPrint
                                → PDF) which works for novels & non-fiction.
      .epub                   → render directly via WeasyPrint
      .mobi / .prc            → not supported (no MOBI parser)

    Returns True on success. Raises BookConvertError with a user-readable
    message if conversion isn't possible — the UI then offers a raw download.
    """
    book_path = Path(book_path)
    out_pdf_path = Path(out_pdf_path)
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    ext = book_path.suffix.lower()

    # PDFs: serve as-is unless there's a `.sdr/` sidecar with strokes,
    # in which case render an annotated version.
    if ext == ".pdf":
        try:
            import pdf_annotations
            sidecar = pdf_annotations.find_sidecar(book_path)
        except ImportError:
            sidecar = None

        if sidecar:
            log.info("Found annotation sidecar at %s — applying overlay", sidecar)
            try:
                pdf_annotations.overlay_on_pdf(book_path, sidecar, out_pdf_path)
                return True
            except pdf_annotations.AnnotationError as e:
                # Fall back to plain PDF if overlay fails. We DON'T raise —
                # the user should still be able to read the PDF even if
                # annotation decoding broke. Diagnostics get logged.
                log.warning(
                    "Annotation overlay failed (step=%s): %s — serving PDF without overlay",
                    e.step, e,
                )

        # No sidecar (or overlay failed): plain copy
        import shutil
        shutil.copyfile(book_path, out_pdf_path)
        return True

    # KFX/AZW family: try image-book first (fastest), text-book second
    if ext in (".kfx", ".azw", ".azw3", ".azw8", ".kfx-zip"):
        try:
            return _convert_kfx_image_book(book_path, out_pdf_path)
        except BookConvertError as image_err:
            log.info("Image-book conversion didn't apply (%s) — "
                     "falling back to text-book rendering", image_err.step)
            try:
                return _convert_kfx_text_book(book_path, out_pdf_path)
            except BookConvertError as text_err:
                # Both paths failed; surface the more specific error
                raise text_err

    # Standalone EPUB
    if ext == ".epub":
        return _convert_epub_to_pdf(book_path, out_pdf_path)

    raise BookConvertError(
        f"In-browser viewing of {ext} books isn't supported yet — "
        "use the Download button to read this file in another app.",
        step="format-not-supported",
        reason=f"format {ext} requires an external reader",
    )


def _convert_kfx_image_book(book_path: Path, out_pdf_path: Path) -> bool:
    """
    Try kfxlib's image-book PDF converter. Designed for manga/comics where
    each page is a pre-rendered image. For text books it raises an exception
    that we surface as a clean BookConvertError so the caller can fall back
    to the text-book rendering path.
    """
    try:
        from kfxlib.yj_book import YJ_Book
        from kfxlib.message_logging import set_logger, JobLog
    except ImportError as e:
        raise BookConvertError(
            f"kfxlib is not available: {e}", step="import")

    base = logging.getLogger("kfxlib")
    set_logger(JobLog(base))

    try:
        book = YJ_Book(str(book_path))
        try:
            pdf_bytes = book.convert_to_pdf()
        except Exception as e:
            err_str = str(e)
            raise BookConvertError(
                f"Not an image book: {err_str}",
                step="not-image-book",
                reason=err_str,
            )
    finally:
        set_logger(None)

    if not pdf_bytes:
        raise BookConvertError(
            "Image-book conversion returned an empty PDF.",
            step="image-book-empty",
        )

    out_pdf_path.write_bytes(pdf_bytes)
    return True


# ── Text-book / EPUB → PDF rendering ─────────────────────────────────────────

def _convert_kfx_text_book(book_path: Path, out_pdf_path: Path) -> bool:
    """
    Render a text-format KFX book by going KFX → EPUB → PDF via WeasyPrint.

    This works for novels and most non-fiction — the heavy lifting is done by
    kfxlib's existing convert_to_epub path (well-tested) and WeasyPrint
    (mature HTML/CSS renderer).
    """
    try:
        from kfxlib.yj_book import YJ_Book
        from kfxlib.message_logging import set_logger, JobLog
    except ImportError as e:
        raise BookConvertError(
            f"kfxlib is not available: {e}", step="import")

    if not _check_weasyprint_available():
        raise BookConvertError(
            "WeasyPrint isn't installed. Install with:\n"
            "    pip3 install weasyprint\n"
            "WeasyPrint is required for converting text-format books "
            "(KFX, AZW3, EPUB) to readable PDFs.",
            step="weasyprint-missing",
        )

    log.info("Rendering %s as text book (KFX → EPUB → PDF)", book_path.name)

    base = logging.getLogger("kfxlib")
    set_logger(JobLog(base))
    try:
        book = YJ_Book(str(book_path))
        try:
            epub_bytes = book.convert_to_epub()
        except Exception as e:
            raise BookConvertError(
                f"kfxlib could not produce EPUB from this book: {e}",
                step="kfx-to-epub",
                reason=str(e),
            )
    finally:
        set_logger(None)

    if not epub_bytes:
        raise BookConvertError(
            "kfxlib produced an empty EPUB — book may be DRM-protected.",
            step="kfx-empty-epub",
        )

    return _epub_bytes_to_pdf(epub_bytes, out_pdf_path)


def _convert_epub_to_pdf(book_path: Path, out_pdf_path: Path) -> bool:
    """Render a standalone EPUB file via WeasyPrint."""
    if not _check_weasyprint_available():
        raise BookConvertError(
            "WeasyPrint isn't installed. Install with:\n"
            "    pip3 install weasyprint",
            step="weasyprint-missing",
        )

    try:
        epub_bytes = book_path.read_bytes()
    except OSError as e:
        raise BookConvertError(f"Cannot read EPUB: {e}", step="read")

    return _epub_bytes_to_pdf(epub_bytes, out_pdf_path)


def _check_weasyprint_available() -> bool:
    try:
        import weasyprint  # noqa: F401
        return True
    except ImportError:
        return False


def _epub_bytes_to_pdf(epub_bytes: bytes, out_pdf_path: Path) -> bool:
    """
    Core EPUB → PDF pipeline:

      1. Open the EPUB zip in memory.
      2. Parse OPF to get the spine in reading order.
      3. Concatenate all spine chapter HTMLs into ONE big HTML document,
         with page-break markers between chapters so chapter starts on a
         fresh page.
      4. Inline a default reading-friendly stylesheet (the EPUB's own CSS
         is also kept via WeasyPrint's url_fetcher).
      5. Hand to WeasyPrint with a custom url_fetcher that resolves any
         epub-internal: URL against the in-memory zip.

    The url_fetcher trick lets WeasyPrint pull CSS files, images, and fonts
    from inside the EPUB zip without us having to extract everything to disk.
    """
    import io
    import re
    import zipfile

    import weasyprint

    zf = zipfile.ZipFile(io.BytesIO(epub_bytes), "r")

    # 1. Find the OPF
    opf_path = _find_opf(zf)
    if not opf_path:
        zf.close()
        raise BookConvertError(
            "EPUB has no OPF — malformed file.", step="epub-no-opf")

    opf_dir = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
    opf_text = zf.read(opf_path).decode("utf-8", errors="replace")

    # 2. Manifest + spine
    manifest = _parse_opf_manifest(opf_text)
    spine_ids = _parse_opf_spine(opf_text)

    if not spine_ids:
        zf.close()
        raise BookConvertError(
            "EPUB has empty spine — nothing to render.", step="epub-empty-spine")

    # 3. Pull out chapter HTMLs in spine order, rewrite their URLs, concatenate
    chapters_html = []
    for idref in spine_ids:
        item = manifest.get(idref)
        if not item:
            continue
        href, mtype = item
        if mtype not in ("application/xhtml+xml", "text/html"):
            continue

        chapter_path = opf_dir + href
        try:
            chapter_bytes = zf.read(chapter_path)
        except KeyError:
            log.debug("Spine entry %s not in zip", chapter_path)
            continue

        chapter_text = chapter_bytes.decode("utf-8", errors="replace")
        # Extract just the <body> content; we'll wrap everything in a single
        # outer <html> document below.
        body = _extract_html_body(chapter_text)
        if not body.strip():
            continue

        chapter_dir = chapter_path.rsplit("/", 1)[0] if "/" in chapter_path else ""
        body = _rewrite_resource_urls(body, chapter_dir)
        chapters_html.append(body)

    if not chapters_html:
        zf.close()
        raise BookConvertError(
            "Spine had entries but no chapters had renderable content.",
            step="epub-no-chapters")

    # Also pull in any stylesheets referenced by the OPF — we'll @import them
    # in our combined doc so the original look-and-feel is preserved.
    css_imports = []
    for item_id, (href, mtype) in manifest.items():
        if mtype == "text/css":
            css_path = opf_dir + href
            css_imports.append(f'@import url("epub-internal:{css_path}");')

    # 4. Build the master HTML document
    chapter_separator = (
        '<div style="page-break-before: always; '
        'break-before: page; height:0;"></div>'
    )
    combined = chapter_separator.join(chapters_html)

    css_block = (
        "\n".join(css_imports) + "\n"
        # Reasonable defaults so books without their own CSS still look nice
        "@page { size: A5; margin: 1.5cm 1.8cm; }\n"
        "body { font-family: 'Georgia', 'Times New Roman', serif; "
        "font-size: 11pt; line-height: 1.5; color: #1a1a1a; }\n"
        "h1, h2, h3 { page-break-after: avoid; break-after: avoid; "
        "font-family: 'Georgia', serif; }\n"
        "h1 { page-break-before: always; break-before: page; }\n"
        "img { max-width: 100%; height: auto; }\n"
        "p { orphans: 3; widows: 3; }\n"
    )

    master_html = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<style>{css_block}</style>'
        f'</head><body>{combined}</body></html>'
    )

    # 5. Custom url_fetcher: resolves epub-internal: URLs against the zip
    def epub_url_fetcher(url, *_args, **_kwargs):
        return _fetcher_result(url, *_resolve_epub_url(url))

    def _resolve_epub_url(url):
        """(bytes, mime type) for an epub-internal: URL; empty for anything else."""
        if not url.startswith("epub-internal:"):
            # External URL — refuse to fetch (security: don't let books make
            # network requests). Return empty so WeasyPrint silently skips it.
            log.debug("Refusing external URL: %s", url)
            return b"", "application/octet-stream"

        path = url[len("epub-internal:"):]
        # URLs may have query strings or fragments
        path = path.split("?", 1)[0].split("#", 1)[0]

        # Resolve against zip names case-insensitively
        names_lower = {n.lower(): n for n in zf.namelist()}
        resolved = names_lower.get(path.lower())
        if not resolved:
            # Try basename match as a last resort
            base = path.rsplit("/", 1)[-1].lower()
            for low, real in names_lower.items():
                if low.endswith("/" + base):
                    resolved = real
                    break
        if not resolved:
            log.debug("EPUB resource not found: %s", path)
            return b"", "application/octet-stream"

        try:
            data = zf.read(resolved)
        except KeyError:
            return b"", "application/octet-stream"

        mime = _guess_mime(resolved)
        return data, mime

    log.info("Rendering %d chapters via WeasyPrint", len(chapters_html))

    try:
        pdf_bytes = weasyprint.HTML(
            string=master_html,
            url_fetcher=epub_url_fetcher,
        ).write_pdf()
    except Exception as e:
        zf.close()
        raise BookConvertError(
            f"WeasyPrint failed to render the book: {e}",
            step="weasyprint-render",
            reason=str(e),
        )
    finally:
        zf.close()

    if not pdf_bytes:
        raise BookConvertError(
            "WeasyPrint returned an empty PDF.",
            step="weasyprint-empty",
        )

    out_pdf_path.write_bytes(pdf_bytes)
    log.info("Wrote %s (%d bytes)", out_pdf_path, len(pdf_bytes))
    return True


# ── EPUB parsing helpers ─────────────────────────────────────────────────────

def _find_opf(zf) -> str | None:
    """Locate the OPF (package) file in an EPUB zip."""
    import re
    try:
        container = zf.read("META-INF/container.xml").decode(
            "utf-8", errors="replace")
        m = re.search(r'full-path="([^"]+\.opf)"', container)
        if m:
            return m.group(1)
    except KeyError:
        pass
    for name in zf.namelist():
        if name.endswith(".opf"):
            return name
    return None


def _parse_opf_manifest(opf_text: str) -> dict[str, tuple[str, str]]:
    """Return id -> (href, media-type) for every <item> in the manifest."""
    import re
    result: dict[str, tuple[str, str]] = {}
    for m in re.finditer(r"<item\b([^>]+)/>", opf_text):
        attrs = m.group(1)
        id_m = re.search(r'\bid="([^"]+)"', attrs)
        href_m = re.search(r'\bhref="([^"]+)"', attrs)
        mt_m = re.search(r'\bmedia-type="([^"]+)"', attrs)
        if id_m and href_m:
            result[id_m.group(1)] = (
                href_m.group(1),
                mt_m.group(1) if mt_m else "",
            )
    return result


def _parse_opf_spine(opf_text: str) -> list[str]:
    """Return ordered list of itemref idrefs from the spine."""
    import re
    spine_block = re.search(r"<spine[^>]*>(.*?)</spine>", opf_text, re.DOTALL)
    if not spine_block:
        return []
    return [
        m.group(1)
        for m in re.finditer(
            r'<itemref[^>]+idref="([^"]+)"', spine_block.group(1)
        )
    ]


def _extract_html_body(html_text: str) -> str:
    """Return everything between <body> and </body>; if no <body>, return all."""
    import re
    m = re.search(r"<body\b[^>]*>(.*?)</body>", html_text, re.DOTALL | re.IGNORECASE)
    return m.group(1) if m else html_text


def _rewrite_resource_urls(html: str, chapter_dir: str) -> str:
    """
    Rewrite local src/href URLs in HTML to use our `epub-internal:` scheme
    so WeasyPrint's url_fetcher can resolve them against the zip.

    Skips URLs that are already absolute (http://, https://, data:, etc.)
    or that already use our scheme.
    """
    import re

    def _resolve_relative(url: str) -> str:
        if url.startswith("/"):
            return url.lstrip("/")
        if not chapter_dir:
            return url
        # Naive but correct enough: collapse `..` segments
        parts = (chapter_dir.split("/") if chapter_dir else []) + url.split("/")
        out: list[str] = []
        for p in parts:
            if p == "..":
                if out:
                    out.pop()
            elif p and p != ".":
                out.append(p)
        return "/".join(out)

    def _rewrite(match):
        attr = match.group(1)
        url = match.group(2)
        if url.startswith(("http://", "https://", "data:", "epub-internal:",
                           "mailto:", "javascript:", "#")):
            return match.group(0)
        resolved = _resolve_relative(url)
        return f'{attr}="epub-internal:{resolved}"'

    return re.sub(
        r'\b(src|href|xlink:href)="([^"]+)"',
        _rewrite,
        html,
    )


def _fetcher_result(url: str, data: bytes, mime: str):
    """
    What a WeasyPrint url_fetcher must return. WeasyPrint 70+ only accepts a
    URLFetcherResponse; older versions take a {'string', 'mime_type'} dict.
    """
    try:
        from weasyprint.urls import URLFetcherResponse
    except ImportError:
        return {"string": data, "mime_type": mime}
    return URLFetcherResponse(url, body=data, headers={"Content-Type": mime})


def _guess_mime(path: str) -> str:
    """Best-effort MIME type from filename extension."""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {
        "css": "text/css",
        "html": "text/html",
        "xhtml": "application/xhtml+xml",
        "xml": "application/xml",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "svg": "image/svg+xml",
        "webp": "image/webp",
        "ttf": "font/ttf",
        "otf": "font/otf",
        "woff": "font/woff",
        "woff2": "font/woff2",
        "js": "application/javascript",
    }.get(ext, "application/octet-stream")


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(description="Inspect or convert a book file")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_meta = sub.add_parser("meta", help="Extract metadata as JSON")
    p_meta.add_argument("book")

    p_conv = sub.add_parser("convert", help="Convert to viewable PDF")
    p_conv.add_argument("book")
    p_conv.add_argument("out_pdf")

    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )

    if args.cmd == "meta":
        md = extract_metadata(Path(args.book))
        # Don't dump the cover bytes — too noisy
        printable = {k: v for k, v in md.items() if k != "cover_bytes"}
        if md.get("cover_bytes"):
            printable["cover_bytes_len"] = len(md["cover_bytes"])
        print(_json.dumps(printable, indent=2, default=str))
    elif args.cmd == "convert":
        try:
            ok = convert_to_viewable(Path(args.book), Path(args.out_pdf))
            print(f"OK: wrote {args.out_pdf}" if ok else "Conversion failed")
            sys.exit(0 if ok else 1)
        except BookConvertError as e:
            print(f"Error [{e.step}]: {e}", file=sys.stderr)
            sys.exit(2)
