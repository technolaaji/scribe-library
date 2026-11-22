#!/usr/bin/env python3
"""
Scribe Library engine — the local API behind the library UI.

It owns everything that needs Python: USB/MTP sync with the Kindle, decoding
notebooks and books (kfxlib), the SQLite registry (names, shelves, change
tracking) and annotation export. The UI is the Next.js app in ../app; in
production it is built to ../out and served from here, so one process is
enough. In development, `npm run dev` runs this engine and `next dev`
side by side and Next proxies the engine's routes.

Major sections of this file (in order):
  1. Imports & app setup
  2. Library configuration (paths, migration, registry)
  3. Discovery
  4. Conversion pipelines (notebook → PDF, book → PDF or raw)
  5. Flask routes (UI + API)
  6. CLI

Usage:
    python server.py                    # auto-detect, sync nothing, just serve
    python server.py --sync             # sync notebooks AND books before serving
    python server.py --sync-notebooks   # only notebooks
    python server.py --sync-books       # only books
"""

import argparse
import hashlib
import json
import logging
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

import annotations
import nbk_to_pdf
import kfx_to_pdf
import library
import registry

app = Flask(__name__)
log = logging.getLogger(__name__)

# Cap upload size at the same limit as MTP push (100MB)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024


# ── Library configuration ────────────────────────────────────────────────────

# Library lives next to the app so it's easy to find, back up, or remove.
# Users who prefer the old hidden-folder layout can pass --library to override.
APP_DIR = Path(__file__).resolve().parent
REPO_DIR = APP_DIR.parent
LIBRARY_ROOT = REPO_DIR / "library"

# The built UI (`npm run build` → static export). Served at / when present.
UI_DIR = REPO_DIR / "out"

# Legacy locations we'll auto-migrate from on first run, in order of priority.
# We move content from these into the new library/ folder if found.
LEGACY_PATHS = [
    Path.home() / ".scribe_library",      # previous version's location
    Path.home() / ".scribe_notebooks",    # the version before that
]

# Cache for converted files (PDFs, page JPEGs, extracted covers) — separate
# from the library so users can blow it away without losing original content.
CACHE_DIR = Path(tempfile.gettempdir()) / "scribe_reader_cache"
CACHE_DIR.mkdir(exist_ok=True)

# Populated at startup
PATHS: library.LibraryPaths | None = None
REGISTRY: registry.Registry | None = None


def init_library(root: Path = LIBRARY_ROOT) -> library.LibraryPaths:
    """Initialize on-disk layout. Migrates from older locations on first run."""
    paths = library.LibraryPaths(root)
    paths.ensure_exists()

    for legacy in LEGACY_PATHS:
        if legacy == paths.root:
            continue  # don't migrate from ourselves

        # If legacy is a *.scribe_library, it has the same internal layout —
        # migrate the whole tree (notebooks/ and books/ subdirs).
        if legacy.name == ".scribe_library" and legacy.is_dir():
            moved = _migrate_full_library(legacy, paths)
            if moved:
                log.info("Migrated %d file(s) from %s to %s",
                         moved, legacy, paths.root)
        # If it's the old flat .scribe_notebooks, just move the .nbk + .png files
        elif legacy.is_dir():
            moved = paths.migrate_legacy(legacy)
            if moved:
                log.info("Migrated %d notebook(s) from %s to %s",
                         moved, legacy, paths.notebooks_dir)
    return paths


def _migrate_full_library(legacy_root: Path, new_paths: library.LibraryPaths) -> int:
    """
    Move a complete legacy library (notebooks/ + books/ subdirs) into the new
    location. Idempotent — files already present at the destination are
    skipped, not overwritten.
    """
    moved = 0
    legacy_layout = library.LibraryPaths(legacy_root)
    for legacy_dir, new_dir in [
        (legacy_layout.notebooks_dir, new_paths.notebooks_dir),
        (legacy_layout.purchased_dir, new_paths.purchased_dir),
        (legacy_layout.sideloaded_dir, new_paths.sideloaded_dir),
    ]:
        if not legacy_dir.exists():
            continue
        new_dir.mkdir(parents=True, exist_ok=True)
        for src in legacy_dir.iterdir():
            if not src.is_file():
                continue
            dst = new_dir / src.name
            if dst.exists():
                continue
            try:
                src.rename(dst)
                moved += 1
            except OSError as e:
                log.warning("Could not migrate %s: %s", src, e)

    # Move meta.json too if it exists
    if legacy_layout.meta_path.exists() and not new_paths.meta_path.exists():
        try:
            legacy_layout.meta_path.rename(new_paths.meta_path)
        except OSError:
            pass

    return moved


def init_registry(paths: library.LibraryPaths) -> registry.Registry:
    reg = registry.Registry(paths.root / "scribe.db", paths.root)
    reg.reconcile(_discover_all(paths))
    return reg


# ── Discovery ────────────────────────────────────────────────────────────────

def _discover_all(paths: library.LibraryPaths) -> list[dict]:
    cache = library.load_metadata_cache(paths)
    return (
        library.discover_notebooks(paths)
        + library.discover_books(paths, cache, CACHE_DIR)
        + library.discover_screenshots(paths)
    )


def all_items(reconcile: bool = False) -> list[dict]:
    """
    Everything in the library — notebooks, books and screenshots — with the
    registry's fields (your names, shelves, New/Updated status) attached.
    """
    if not PATHS:
        return []
    items = _discover_all(PATHS)
    if REGISTRY:
        if reconcile:
            REGISTRY.reconcile(items)
        items = REGISTRY.annotate(items)
    return items


def find_item(item_id: str) -> dict | None:
    """Look up one item by its stable hash ID."""
    for item in _discover_all(PATHS) if PATHS else []:
        if item["id"] == item_id:
            return item
    return None


# ── Conversion pipelines ─────────────────────────────────────────────────────

def _ensure_notebook_pdf(item: dict) -> Path:
    """Convert (or reuse cached) PDF for one notebook. Raises on failure."""
    nb_cache = CACHE_DIR / item["id"]
    nb_cache.mkdir(parents=True, exist_ok=True)
    pdf_path = nb_cache / "out.pdf"
    meta_path = nb_cache / "meta.json"

    src = Path(item["path"])
    try:
        mtime = src.stat().st_mtime
    except OSError as e:
        raise RuntimeError(f"Cannot read notebook file: {e}")

    # Cache hit?
    if pdf_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            if meta.get("mtime") == mtime and meta.get("ok"):
                return pdf_path
        except Exception:
            pass

    log.info("Converting notebook %s → %s", src, pdf_path)
    try:
        result = nbk_to_pdf.convert_or_raise(src, pdf_path)
    except nbk_to_pdf.ConversionError as e:
        lines = [f"[step: {e.step or 'unknown'}] {e}"]
        if e.kfx_errors:
            lines.append("\nkfxlib errors:")
            lines.extend(f"  • {m}" for m in e.kfx_errors[:10])
        if e.kfx_warnings:
            lines.append("\nkfxlib warnings (first 5):")
            lines.extend(f"  • {m}" for m in e.kfx_warnings[:5])
        raise RuntimeError("\n".join(lines))

    # The notebook changed: page images from the previous version are stale.
    for stale in nb_cache.glob("page_*.jpg"):
        stale.unlink(missing_ok=True)
    meta_path.write_text(json.dumps({"mtime": mtime, "ok": True,
                                     "pages": result.get("pages", 0)}))
    return pdf_path


# Bump when book conversion changes, so cached results (including cached
# "can't convert" verdicts) are redone.
BOOK_CONVERTER_VERSION = 2


def _ensure_book_pdf(item: dict) -> tuple[Path, str]:
    """
    Convert a book to a PDF the in-browser reader can serve.

    Returns (pdf_path, kind) where kind is:
       'pdf'      → a real PDF, in-browser viewing works
       'raw'      → not convertable; serve the raw file as a download
    Raises RuntimeError with a user-readable message if conversion fails.
    """
    nb_cache = CACHE_DIR / item["id"]
    nb_cache.mkdir(parents=True, exist_ok=True)
    pdf_path = nb_cache / "out.pdf"
    meta_path = nb_cache / "meta.json"
    src = Path(item["path"])

    try:
        mtime = src.stat().st_mtime
    except OSError as e:
        raise RuntimeError(f"Cannot read book file: {e}")

    if pdf_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            if (meta.get("mtime") == mtime and meta.get("ok")
                    and meta.get("converter") == BOOK_CONVERTER_VERSION):
                return pdf_path, meta.get("kind", "pdf")
        except Exception:
            pass

    ext = item.get("extension", "").lower()

    log.info("Converting book %s (%s) → %s", src, ext, pdf_path)
    try:
        kfx_to_pdf.convert_to_viewable(src, pdf_path)
        kind = "pdf"
    except kfx_to_pdf.BookConvertError as e:
        # Don't write a bad PDF to cache — but DO record that this file isn't
        # convertable so we don't keep retrying. Mark it as 'raw' so the API
        # returns a download URL instead of pages.
        meta_path.write_text(json.dumps({
            "mtime": mtime, "ok": True, "kind": "raw",
            "reason": str(e), "step": e.step,
            "converter": BOOK_CONVERTER_VERSION,
        }))
        return src, "raw"

    # A fresh conversion means new pages: drop page images from older runs.
    for old in nb_cache.glob("page_*.jpg"):
        old.unlink(missing_ok=True)
    meta_path.write_text(json.dumps({"mtime": mtime, "ok": True, "kind": kind,
                                     "converter": BOOK_CONVERTER_VERSION}))
    return pdf_path, kind


def _pdf_to_page_images(pdf_path: Path, out_dir: Path) -> list[Path]:
    """
    Extract per-page JPEGs from a PDF for inline viewing in the reader.

    For PDFs we built ourselves (via nbk_to_pdf or kfx_to_pdf), every page is
    a single embedded image, so we extract those images directly. For
    arbitrary PDFs (like sideloaded books) we fall back to PyMuPDF/pdf2image
    if available, otherwise we skip image extraction and let the browser's
    native PDF viewer handle display via the /pdf/ endpoint.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cached = sorted(out_dir.glob("page_*.jpg"))
    if cached:
        return cached

    # Strategy 1: pull embedded images out of the PDF (fast for our PDFs)
    page_files = _extract_embedded_page_images(pdf_path, out_dir)
    if page_files:
        return page_files

    # Strategy 2: rasterize the PDF page-by-page if a renderer is available
    page_files = _rasterize_pdf_pages(pdf_path, out_dir)
    return page_files


def _extract_embedded_page_images(pdf_path: Path, out_dir: Path) -> list[Path]:
    """For PDFs with one image per page, pull those out directly."""
    try:
        import pypdf
        from io import BytesIO
        from PIL import Image

        reader = pypdf.PdfReader(str(pdf_path))
        page_files: list[Path] = []
        for i, page in enumerate(reader.pages):
            imgs = list(page.images)
            if len(imgs) != 1:
                # Page has zero or multiple images — not our format, bail
                # to let the rasterizer take over.
                return []
            try:
                img = Image.open(BytesIO(imgs[0].data)).convert("RGB")
            except Exception as e:
                log.warning("Page %d image decode failed: %s", i, e)
                continue
            page_file = out_dir / f"page_{i:04d}.jpg"
            img.save(page_file, "JPEG", quality=88, optimize=True)
            img.close()
            page_files.append(page_file)
        return page_files
    except Exception as e:
        log.debug("Embedded-image extraction failed: %s", e)
        return []


def _rasterize_pdf_pages(pdf_path: Path, out_dir: Path) -> list[Path]:
    """
    Rasterize every page of a PDF using PyMuPDF (fitz) if available.
    PyMuPDF is the only renderer we attempt — it's a single pip install,
    bundles MuPDF, and works without external system dependencies.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        log.info("PyMuPDF not installed — skipping per-page rasterization. "
                 "The browser's built-in PDF viewer will be used instead.")
        return []

    page_files: list[Path] = []
    try:
        doc = fitz.open(str(pdf_path))
        # Render at ~150 DPI, which is enough for screen reading.
        zoom = 150 / 72
        matrix = fitz.Matrix(zoom, zoom)
        for i, page in enumerate(doc):
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            page_file = out_dir / f"page_{i:04d}.jpg"
            pix.pil_save(str(page_file), format="JPEG", quality=85, optimize=True)
            page_files.append(page_file)
        doc.close()
    except Exception as e:
        log.error("PDF rasterization failed: %s", e)
        return []

    return page_files


# ── Routes: UI ───────────────────────────────────────────────────────────────

@app.route("/")
@app.route("/<path:path>")
def ui(path: str = ""):
    """
    Serve the built UI (static export in ../out). `/books` resolves to
    books.html; anything unknown falls back to index.html.
    """
    if not UI_DIR.exists():
        return (
            "<h1>UI not built</h1><p>Run <code>npm run build</code> in the project "
            "folder, or use <code>npm run dev</code> during development.</p>",
            404,
        )
    clean = path.strip("/")
    for candidate in (clean, f"{clean}.html", f"{clean}/index.html"):
        if candidate and (UI_DIR / candidate).is_file():
            return send_from_directory(UI_DIR, candidate)
    return send_from_directory(UI_DIR, "index.html")


# ── Routes: discovery ────────────────────────────────────────────────────────

@app.route("/api/library")
def api_library():
    """
    Every item, with registry fields. Reconciles against the files on disk
    first, so anything added or changed since the last call is flagged.
    """
    items = all_items(reconcile=True)
    return jsonify({
        "items": [_public(i) for i in items],
        "shelves": REGISTRY.shelves() if REGISTRY else [],
        "summary": REGISTRY.summary() if REGISTRY else {},
    })


def _public(item: dict) -> dict:
    """Drop absolute paths from what we send to the browser."""
    return {k: v for k, v in item.items() if k != "path"}


# ── Routes: registry (names, shelves, changes) ───────────────────────────────

@app.route("/api/items/<item_id>", methods=["GET", "PATCH"])
def api_item(item_id):
    if not REGISTRY or not REGISTRY.get(item_id):
        abort(404)
    if request.method == "PATCH":
        REGISTRY.update(item_id, request.get_json(silent=True) or {})
    item = next((i for i in all_items() if i["id"] == item_id), None)
    if not item:
        abort(404)
    return jsonify({**_public(item), "history": REGISTRY.changes(50, item_id)})


@app.route("/api/items/<item_id>/opened", methods=["POST"])
def api_item_opened(item_id):
    if REGISTRY:
        REGISTRY.mark_opened(item_id)
    return jsonify({"ok": True})


@app.route("/api/items/mark_all_seen", methods=["POST"])
def api_mark_all_seen():
    if REGISTRY:
        REGISTRY.mark_all_seen()
    return jsonify({"ok": True})


@app.route("/api/shelves", methods=["GET", "POST"])
def api_shelves():
    if not REGISTRY:
        abort(503)
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        try:
            shelf = REGISTRY.create_shelf(body.get("name", ""), body.get("description"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"shelf": shelf}), 201
    return jsonify({"shelves": REGISTRY.shelves()})


@app.route("/api/shelves/<int:shelf_id>", methods=["PATCH", "DELETE"])
def api_shelf(shelf_id):
    if not REGISTRY:
        abort(503)
    if request.method == "DELETE":
        return jsonify({"ok": REGISTRY.delete_shelf(shelf_id)})
    body = request.get_json(silent=True) or {}
    try:
        shelf = REGISTRY.update_shelf(shelf_id, body.get("name"), body.get("description"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not shelf:
        abort(404)
    return jsonify({"shelf": shelf})


@app.route("/api/shelves/<int:shelf_id>/items", methods=["POST"])
def api_shelf_items(shelf_id):
    if not REGISTRY:
        abort(503)
    body = request.get_json(silent=True) or {}
    shelf = REGISTRY.set_shelf_items(shelf_id, body.get("add") or [], body.get("remove") or [])
    if not shelf:
        abort(404)
    return jsonify({"shelf": shelf})


@app.route("/api/changes")
def api_changes():
    limit = min(int(request.args.get("limit", 200)), 1000)
    return jsonify({
        "changes": REGISTRY.changes(limit) if REGISTRY else [],
        "summary": REGISTRY.summary() if REGISTRY else {},
    })


@app.route("/api/status")
def api_status():
    try:
        from mtp_sync import detect_device
        device = detect_device()
    except Exception:
        device = ""

    nb_count = len(library.discover_notebooks(PATHS))
    book_count = len(library.discover_books(PATHS, library.load_metadata_cache(PATHS), CACHE_DIR))
    screenshot_count = len(library.discover_screenshots(PATHS))
    in_book = PATHS.notebooks_dir / "in-book"
    in_book_count = len(list(in_book.glob("*.nbk"))) if in_book.exists() else 0
    converter_msg = nbk_to_pdf._check_deps() or ""

    # Book reader status — distinct from the notebook-decoder check
    book_reader_ready = kfx_to_pdf._check_weasyprint_available()
    book_reader_msg = "" if book_reader_ready else (
        "WeasyPrint not installed — text-format books (KFX/AZW3/EPUB) will "
        "show as download-only. Install with: pip3 install weasyprint"
    )

    cache_size = 0
    if CACHE_DIR.exists():
        cache_size = sum(
            f.stat().st_size for f in CACHE_DIR.rglob("*") if f.is_file()
        )

    return jsonify({
        "device_connected": bool(device),
        "device_name": device or None,
        "library_root": str(LIBRARY_ROOT),
        "notebook_count": nb_count,
        "book_count": book_count,
        "screenshot_count": screenshot_count,
        "in_book_notebook_count": in_book_count,
        "registry_path": str(REGISTRY.db_path) if REGISTRY else None,
        "converter_ready": not converter_msg,
        "converter_message": converter_msg,
        "book_reader_ready": book_reader_ready,
        "book_reader_message": book_reader_msg,
        "cache_size_mb": round(cache_size / 1024 / 1024, 2),
    })


# ── Routes: notebook reader ──────────────────────────────────────────────────

@app.route("/api/notebook/<item_id>")
def api_notebook(item_id):
    item = find_item(item_id)
    if not item or item["kind"] != "notebook":
        abort(404)
    try:
        pdf_path = _ensure_notebook_pdf(item)
        nb_cache = CACHE_DIR / item_id
        page_files = _pdf_to_page_images(pdf_path, nb_cache)
    except Exception as e:
        return jsonify({"error": str(e), **item}), 500

    return jsonify({
        **item,
        "pages": [f"/page/{item_id}/{i:04d}" for i in range(len(page_files))],
        "pdf_url": f"/pdf/{item_id}",
    })


@app.route("/thumbnail/<item_id>")
def serve_thumbnail(item_id):
    """Serve the first-page thumbnail PNG for a notebook (sidebar previews)."""
    item = find_item(item_id)
    if not item or item["kind"] != "notebook":
        abort(404)
    nb_path = Path(item["path"])
    thumb = nb_path.with_suffix(".png")
    if not thumb.exists():
        abort(404)
    return send_file(str(thumb), mimetype="image/png")


# ── Routes: book reader ──────────────────────────────────────────────────────

@app.route("/api/book/<item_id>")
def api_book(item_id):
    item = find_item(item_id)
    if not item or item["kind"] != "book":
        abort(404)

    # Lazy-extract metadata on first open if we haven't already
    cache = library.load_metadata_cache(PATHS)
    if item_id not in cache:
        _try_extract_metadata(item)

    try:
        pdf_path, kind = _ensure_book_pdf(item)
    except Exception as e:
        return jsonify({"error": str(e), **item}), 500

    if kind == "raw":
        # Not viewable in-browser — only a download is offered
        return jsonify({
            **item,
            "pages": [],
            "viewable": False,
            "download_url": f"/raw/{item_id}",
            "message": (
                "This format isn't supported for in-browser reading yet. "
                "Use the Download button to save the file and open it in "
                "another reader."
            ),
        })

    nb_cache = CACHE_DIR / item_id
    page_files = _pdf_to_page_images(pdf_path, nb_cache)

    return jsonify({
        **item,
        "pages": [f"/page/{item_id}/{i:04d}" for i in range(len(page_files))],
        "pdf_url": f"/pdf/{item_id}",
        "viewable": True,
        # If page extraction didn't work, the UI will fall back to the
        # browser's native PDF viewer via the pdf_url.
        "fallback_pdf": len(page_files) == 0,
    })


@app.route("/cover/<item_id>")
def serve_cover(item_id):
    """
    Serve a cover image for a book or notebook. Resolution order:
      1. Cached extracted cover (cover.jpg/png from book metadata)
      2. Notebook thumbnail PNG (sibling .png file)
      3. First page of the converted PDF (page_0000.jpg) — the fallback
         that makes KFX/EPUB books with no metadata cover still get a tile
    """
    item_dir = CACHE_DIR / item_id

    # 1. Real cover from metadata
    cover_files = sorted(item_dir.glob("cover.*"))
    if cover_files:
        return send_file(str(cover_files[0]))

    item = find_item(item_id)
    if not item:
        abort(404)

    # 2. Notebook thumbnail — for notebooks this is the device-generated PNG
    if item["kind"] == "notebook":
        thumb = Path(item["path"]).with_suffix(".png")
        if thumb.exists():
            return send_file(str(thumb), mimetype="image/png")

    # 3. For books, try to extract real cover from metadata once
    if item["kind"] == "book":
        if _try_extract_metadata(item):
            cover_files = sorted(item_dir.glob("cover.*"))
            if cover_files:
                return send_file(str(cover_files[0]))

    # 4. Fallback: first page of the converted PDF
    first_page = item_dir / "page_0000.jpg"
    if first_page.exists():
        return send_file(str(first_page), mimetype="image/jpeg")

    abort(404)


def _try_extract_metadata(item: dict) -> bool:
    """Extract metadata for one book and persist title/author/cover."""
    src = Path(item["path"])
    try:
        md = kfx_to_pdf.extract_metadata(src)
    except Exception as e:
        log.debug("Metadata extraction failed for %s: %s", src, e)
        return False

    nb_cache = CACHE_DIR / item["id"]
    nb_cache.mkdir(parents=True, exist_ok=True)

    has_cover = False
    if md.get("cover_bytes"):
        ext = (md.get("cover_ext") or "jpg").lstrip(".").lower()
        if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
            ext = "jpg"
        cover_path = nb_cache / f"cover.{ext}"
        try:
            cover_path.write_bytes(md["cover_bytes"])
            has_cover = True
        except OSError:
            pass

    library.update_book_metadata(
        PATHS, item["id"],
        title=md.get("title"),
        authors=md.get("authors") or [],
        has_cover=has_cover,
    )
    return True


# ── Routes: shared (page images, raw downloads, PDFs) ────────────────────────

@app.route("/page/<item_id>/<page_num>")
def serve_page(item_id, page_num):
    page_file = CACHE_DIR / item_id / f"page_{page_num}.jpg"
    if not page_file.exists():
        abort(404)
    return send_file(str(page_file), mimetype="image/jpeg")


@app.route("/pdf/<item_id>")
def serve_pdf(item_id):
    """Serve the converted PDF (or original PDF for sideloaded books)."""
    pdf_file = CACHE_DIR / item_id / "out.pdf"
    if pdf_file.exists():
        return send_file(str(pdf_file), mimetype="application/pdf")
    # Fall back to the original file if it's already a PDF
    item = find_item(item_id)
    if item and Path(item["path"]).suffix.lower() == ".pdf":
        return send_file(item["path"], mimetype="application/pdf")
    abort(404)


@app.route("/raw/<item_id>")
def serve_raw(item_id):
    """Serve the original book file as a download (for non-PDF formats)."""
    item = find_item(item_id)
    if not item:
        abort(404)
    src = Path(item["path"])
    if not src.exists():
        abort(404)
    return send_file(
        str(src),
        as_attachment=True,
        download_name=src.name,
    )


# ── Routes: sync ─────────────────────────────────────────────────────────────

@app.route("/api/sync_notebooks", methods=["POST"])
def api_sync_notebooks():
    try:
        from mtp_sync import sync_notebooks
    except ImportError:
        return jsonify({"error": "mtp_sync.py not found"}), 500

    log_lines: list[str] = []
    report = sync_notebooks(dest=PATHS.notebooks_dir, log=log_lines.append)
    return jsonify({
        "ok": report.notebooks_found > 0,
        "kind": "notebooks",
        "changes": REGISTRY.reconcile(_discover_all(PATHS)) if REGISTRY else {},
        "device": report.device_name,
        "found": report.notebooks_found,
        "copied": report.notebooks_copied,
        "skipped": report.notebooks_skipped,
        "thumbnails": report.thumbnails_copied,
        "errors": report.errors,
        "log": log_lines,
    })


@app.route("/api/sync_books", methods=["POST"])
def api_sync_books():
    try:
        from mtp_sync import sync_books
    except ImportError:
        return jsonify({"error": "mtp_sync.py not found"}), 500

    log_lines: list[str] = []
    report = sync_books(
        dest=PATHS.books_dir,
        purchased_subdir=PATHS.purchased_dir.name,
        sideloaded_subdir=PATHS.sideloaded_dir.name,
        log=log_lines.append,
    )
    return jsonify({
        "ok": report.books_found > 0,
        "kind": "books",
        "changes": REGISTRY.reconcile(_discover_all(PATHS)) if REGISTRY else {},
        "device": report.device_name,
        "found": report.books_found,
        "copied": report.books_copied,
        "skipped": report.books_skipped,
        "errors": report.errors,
        "log": log_lines,
    })


@app.route("/api/sync_screenshots", methods=["POST"])
def api_sync_screenshots():
    try:
        from mtp_sync import sync_screenshots
    except ImportError:
        return jsonify({"error": "mtp_sync.py not found"}), 500

    log_lines: list[str] = []
    report = sync_screenshots(dest=PATHS.screenshots_dir, log=log_lines.append)
    return jsonify({
        "ok": report.screenshots_found > 0,
        "kind": "screenshots",
        "changes": REGISTRY.reconcile(_discover_all(PATHS)) if REGISTRY else {},
        "device": report.device_name,
        "found": report.screenshots_found,
        "copied": report.screenshots_copied,
        "skipped": report.screenshots_skipped,
        "errors": report.errors,
        "log": log_lines,
    })


# ── Routes: screenshots (view + delete) ──────────────────────────────────────

@app.route("/screenshot/<item_id>")
def serve_screenshot(item_id):
    """Serve a screenshot PNG/JPG full-size."""
    item = find_item(item_id)
    if not item or item.get("kind") != "screenshot":
        abort(404)
    src = Path(item["path"])
    if not src.exists():
        abort(404)
    mime = "image/png" if src.suffix.lower() == ".png" else "image/jpeg"
    return send_file(str(src), mimetype=mime)


@app.route("/api/screenshot/<item_id>/delete", methods=["POST"])
def api_delete_screenshot(item_id):
    """
    Delete a screenshot from the LOCAL library AND from the Kindle.

    Two-phase delete:
      1. Connect to the device (if attached) and DELETE_OBJECT it via MTP
      2. Remove the local file
    If step 1 fails (no device, file already gone, etc.) we still proceed
    with step 2 unless the request explicitly asks for atomic delete.
    """
    item = find_item(item_id)
    if not item or item.get("kind") != "screenshot":
        abort(404)

    src = Path(item["path"])
    filename = item["filename"]
    log_lines: list[str] = []
    device_errors: list[str] = []
    device_deleted = False
    device_name = ""

    try:
        from mtp_sync import delete_from_kindle
        report = delete_from_kindle(filename, log=log_lines.append)
        device_deleted = report.deleted
        device_errors = report.errors
        device_name = report.device_name
    except ImportError:
        device_errors = ["mtp_sync not available"]

    # Always remove the local file even if device delete failed.
    # If the user wanted to retry the device delete later, they can re-sync
    # to pull the file back, then delete again.
    local_deleted = False
    try:
        if src.exists():
            src.unlink()
            local_deleted = True
            log_lines.append(f"✓ Removed local copy: {src.name}")
    except OSError as e:
        log_lines.append(f"✗ Local delete failed: {e}")

    return jsonify({
        "ok": device_deleted and local_deleted,
        "local_deleted": local_deleted,
        "device_deleted": device_deleted,
        "device_name": device_name,
        "filename": filename,
        "errors": device_errors,
        "log": log_lines,
    })


# ── Routes: upload (push to Kindle) ──────────────────────────────────────────

@app.route("/api/upload", methods=["POST"])
def api_upload():
    """
    Receive a PDF (or other small file) from the browser and push it onto
    the Kindle's documents/ folder via MTP.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file in request"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    # Only allow safe extensions on push to keep the surface small.
    safe_extensions = {".pdf", ".epub", ".mobi", ".txt", ".docx", ".rtf"}
    ext = Path(f.filename).suffix.lower()
    if ext not in safe_extensions:
        return jsonify({
            "error": f"Refusing to push '{ext}' — only "
                     f"{', '.join(sorted(safe_extensions))} are allowed.",
        }), 400

    # Stage the upload to a temp file on disk; MTP send streams from a path
    tmp_dir = Path(tempfile.mkdtemp(prefix="scribe_upload_"))
    safe_name = library.safe_local_filename(f.filename)
    tmp_path = tmp_dir / safe_name
    try:
        f.save(str(tmp_path))
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": f"Could not stage upload: {e}"}), 500

    try:
        from mtp_sync import push_pdf_to_kindle
    except ImportError:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": "mtp_sync.py not found"}), 500

    log_lines: list[str] = []
    try:
        report = push_pdf_to_kindle(
            tmp_path, target_filename=safe_name, log=log_lines.append)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return jsonify({
        "ok": report.file_pushed,
        "device": report.device_name,
        "target": report.target_path,
        "bytes": report.bytes_sent,
        "errors": report.errors,
        "log": log_lines,
    })


# ── Routes: highlights & handwritten notes inside books ──────────────────────

def _book_annotations(item: dict) -> dict:
    return annotations.collect_cached(
        Path(item["path"]),
        sdr_dirs=[PATHS.sideloaded_dir, PATHS.purchased_dir],
        in_book_dir=PATHS.notebooks_dir / "in-book",
        cache_dir=CACHE_DIR / item["id"] / "annotations",
        # positions inferred for Active Canvas notes are kept in the registry
        hints=REGISTRY.note_positions(item["id"]) if REGISTRY else None,
        remember=(lambda cid, pos: REGISTRY.save_note_position(item["id"], cid, pos))
        if REGISTRY else None,
    )


@app.route("/api/book/<item_id>/annotations")
def api_book_annotations(item_id):
    item = find_item(item_id)
    if not item or item["kind"] != "book":
        abort(404)
    try:
        data = _book_annotations(item)
    except Exception as e:
        log.exception("Annotation export failed for %s", item["path"])
        return jsonify({"error": f"{type(e).__name__}: {e}", "annotations": []}), 500
    for a in data["annotations"]:
        if a.get("image"):
            a["image_url"] = f"/handwriting/{item_id}/{a['image']}"
    data.pop("book", None)
    data["yjr"] = Path(data["yjr"]).name if data.get("yjr") else None
    data["nbk"] = Path(data["nbk"]).name if data.get("nbk") else None
    return jsonify(data)


@app.route("/api/book/<item_id>/annotation_pages")
def api_annotation_pages(item_id):
    """
    Which reader page each annotation is on. Needs the book's rendered PDF,
    so the reader calls this once its pages have loaded.
    """
    item = find_item(item_id)
    if not item or item["kind"] != "book":
        abort(404)
    pdf = CACHE_DIR / item_id / "out.pdf"
    if not pdf.exists() and Path(item["path"]).suffix.lower() == ".pdf":
        pdf = Path(item["path"])
    if not pdf.exists():
        return jsonify({"pages": [], "ready": False})
    data = _book_annotations(item)
    cache = CACHE_DIR / item_id / "annotations" / "pages.json"
    stamp = [4, pdf.stat().st_mtime, len(data["annotations"]),
             [a["start"].get("raw") for a in data["annotations"]]]
    if cache.exists():
        try:
            cached = json.loads(cache.read_text())
            if cached.get("stamp") == stamp:
                return jsonify({"pages": cached["pages"], "ready": True})
        except (OSError, json.JSONDecodeError):
            pass
    try:
        pages = annotations.locate_pages(pdf, data)
    except Exception as e:
        log.warning("Could not place annotations on pages: %s", e)
        return jsonify({"pages": [], "ready": False, "error": str(e)})
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"stamp": stamp, "pages": pages}))
    return jsonify({"pages": pages, "ready": True})


@app.route("/api/book/<item_id>/annotations.md")
def api_book_annotations_md(item_id):
    item = find_item(item_id)
    if not item or item["kind"] != "book":
        abort(404)
    data = _book_annotations(item)
    meta = REGISTRY.get(item_id) if REGISTRY else None
    title = ((meta and (meta["display_name"] or meta["original_name"]))
             or data.get("title") or item["name"])
    md = annotations.to_markdown(
        data, title, image_prefix=f"{request.host_url.rstrip('/')}/handwriting/{item_id}/")
    filename = library.safe_local_filename(title)[:120] or "annotations"
    return app.response_class(md, mimetype="text/markdown", headers={
        "Content-Disposition": f'attachment; filename="{filename}.md"'})


@app.route("/handwriting/<item_id>/<name>")
def serve_handwriting(item_id, name):
    d = CACHE_DIR / item_id / "annotations" / "handwriting"
    if "/" in name or not (d / name).is_file():
        abort(404)
    return send_from_directory(d, name, mimetype="image/png")


# ── Routes: cache & metadata management ──────────────────────────────────────

@app.route("/api/clear_cache", methods=["POST", "GET"])
def clear_cache():
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    CACHE_DIR.mkdir(exist_ok=True)
    return jsonify({"ok": True})


@app.route("/api/extract_metadata/<item_id>", methods=["POST"])
def api_extract_metadata(item_id):
    """Extract title/author/cover for one book on demand."""
    item = find_item(item_id)
    if not item or item["kind"] != "book":
        abort(404)
    ok = _try_extract_metadata(item)
    return jsonify({"ok": ok, "item_id": item_id})


@app.route("/api/annotations/<item_id>")
def api_annotations(item_id):
    """
    Diagnostic endpoint: report whether a sideloaded PDF has an annotation
    sidecar and what we were able to decode from it. Returns:
        {
          has_sidecar: bool,
          sidecar_dir: str|null,
          sidecar_files: [(name, size), ...],
          decoded_pages: int,
          pages_with_strokes: [page_indices...],
          overlay_pages: int,
          error: str|null,
          log: [str, ...],
        }
    """
    import pdf_annotations
    item = find_item(item_id)
    if not item or item["kind"] != "book":
        abort(404)

    src = Path(item["path"])
    if src.suffix.lower() != ".pdf":
        return jsonify({
            "has_sidecar": False,
            "reason": "Annotations are only supported on sideloaded PDFs.",
        })

    sidecar = pdf_annotations.find_sidecar(src)
    if not sidecar:
        return jsonify({
            "has_sidecar": False,
            "pdf_path": str(src),
        })

    # Try to decode without writing the overlay (just diagnostics).
    try:
        data = pdf_annotations.decode_strokes(sidecar)
        debug = data.debug
        return jsonify({
            "has_sidecar": True,
            "sidecar_dir": str(sidecar),
            "sidecar_files": [
                {"name": n, "size": s} for n, s in debug.sidecar_files],
            "decoded_pages": debug.decoded_pages,
            "pages_with_strokes": debug.pages_with_strokes,
            "log": debug.log_lines,
        })
    except pdf_annotations.AnnotationError as e:
        return jsonify({
            "has_sidecar": True,
            "sidecar_dir": str(sidecar),
            "error": str(e),
            "step": e.step,
            "details": e.details,
        }), 500


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Kindle Scribe Library Reader")
    parser.add_argument("--library", default=str(LIBRARY_ROOT),
                        help="Library root directory (default: ./library)")
    parser.add_argument("--port", type=int, default=7070)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--sync", action="store_true",
                        help="Sync notebooks AND books before serving")
    parser.add_argument("--sync-notebooks", action="store_true",
                        help="Only sync notebooks before serving")
    parser.add_argument("--sync-books", action="store_true",
                        help="Only sync books before serving")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    LIBRARY_ROOT = Path(args.library).expanduser()
    PATHS = init_library(LIBRARY_ROOT)
    REGISTRY = init_registry(PATHS)

    if args.sync or args.sync_notebooks:
        try:
            from mtp_sync import sync_notebooks
            print("\n⇣ Syncing notebooks…")
            r = sync_notebooks(dest=PATHS.notebooks_dir)
            print(f"   ✓ {r.notebooks_copied} copied, {r.notebooks_skipped} up-to-date")
        except ImportError:
            print("   ✗ mtp_sync.py not found — skipping")

    if args.sync or args.sync_books:
        try:
            from mtp_sync import sync_books
            print("\n⇣ Syncing books…")
            r = sync_books(
                dest=PATHS.books_dir,
                purchased_subdir=PATHS.purchased_dir.name,
                sideloaded_subdir=PATHS.sideloaded_dir.name,
            )
            print(f"   ✓ {r.books_copied} copied, {r.books_skipped} up-to-date")
        except ImportError:
            print("   ✗ mtp_sync.py not found — skipping")

    dep_msg = nbk_to_pdf._check_deps()
    if dep_msg:
        print("\n⚠ Decoder dependencies missing:")
        for line in dep_msg.splitlines():
            print(f"   {line}")

    nb_count = len(library.discover_notebooks(PATHS))
    book_count = len(library.discover_books(PATHS, cache_dir=CACHE_DIR))
    ss_count = len(library.discover_screenshots(PATHS))
    print(f"\n📚 Scribe Library")
    print(f"   Library:  {PATHS.root}")
    print(f"   Found:    {nb_count} notebooks, {book_count} books, {ss_count} screenshots")
    print(f"   Registry: {REGISTRY.db_path}")
    if UI_DIR.exists():
        print(f"   Open:     http://{args.host}:{args.port}\n")
    else:
        print(f"   Engine:   http://{args.host}:{args.port}  (UI not built: use `npm run dev`)\n")

    app.run(host=args.host, port=args.port, debug=False)
