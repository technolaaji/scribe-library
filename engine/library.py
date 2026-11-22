"""
library.py — Library discovery, classification, and metadata caching.

A "library" is the local mirror of content from a Kindle Scribe, organized as:

    <library_root>/
        notebooks/
            <UUID>.nbk            ← handwritten notebooks (KDF/SQLite)
            <UUID>.png            ← per-notebook thumbnails
        books/
            purchased/
                <ASIN>.kfx        ← Amazon-purchased ebooks
                <ASIN>.azw3       ← (older Amazon format, also accepted)
            sideloaded/
                <slug>.pdf        ← user-pushed PDFs
                <slug>.epub       ← user-pushed EPUBs
                <slug>.mobi       ← (older sideloaded format)
            meta.json             ← cached book metadata (title/author/cover)

Classification rules:
  * extension == .nbk             → notebook
  * extension == .pdf|.epub|.mobi → sideloaded book
  * extension == .kfx|.azw3|.azw  → purchased book
  * anything else                 → ignore (samples, dictionaries, system files)

The meta.json cache stores title/author/cover hashes per book so we don't
re-extract metadata from KFX books on every UI refresh (KFX metadata
extraction is slow — ~1s per book).
"""

import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# Book covers are fetched in parallel, and each one may write metadata, so
# read-modify-write of meta.json has to be serialised.
_META_LOCK = threading.Lock()

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# What extensions go where, and how to display them
NOTEBOOK_EXT = {".nbk"}
PURCHASED_EXT = {".kfx", ".azw", ".azw3", ".azw8", ".kfx-zip"}
SIDELOADED_EXT = {".pdf", ".epub", ".mobi", ".prc", ".txt"}
KNOWN_EXT = NOTEBOOK_EXT | PURCHASED_EXT | SIDELOADED_EXT

# How extensions render to a friendly format string
FORMAT_NAMES = {
    ".nbk": "Notebook",
    ".kfx": "KFX",
    ".azw": "AZW",
    ".azw3": "AZW3",
    ".azw8": "AZW8",
    ".kfx-zip": "KFX",
    ".pdf": "PDF",
    ".epub": "EPUB",
    ".mobi": "MOBI",
    ".prc": "MOBI",
    ".txt": "TXT",
}


# ── Library paths ────────────────────────────────────────────────────────────

class LibraryPaths:
    """Owns the local on-disk layout for the library."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser()
        self.notebooks_dir = self.root / "notebooks"
        self.books_dir = self.root / "books"
        self.purchased_dir = self.books_dir / "purchased"
        self.sideloaded_dir = self.books_dir / "sideloaded"
        self.screenshots_dir = self.root / "screenshots"
        self.meta_path = self.books_dir / "meta.json"

    def ensure_exists(self):
        for d in (self.notebooks_dir, self.purchased_dir,
                  self.sideloaded_dir, self.screenshots_dir):
            d.mkdir(parents=True, exist_ok=True)

    def migrate_legacy(self, legacy_notebooks_dir: Path):
        """
        First-run migration: if a previous version stored notebooks in
        ~/.scribe_notebooks, move them into ~/.scribe_library/notebooks/.
        Idempotent — does nothing if the legacy dir is missing.
        """
        legacy = Path(legacy_notebooks_dir).expanduser()
        if not legacy.exists() or legacy == self.notebooks_dir:
            return 0
        if not legacy.is_dir():
            return 0

        self.ensure_exists()
        moved = 0
        for src in legacy.iterdir():
            if src.is_file():
                dst = self.notebooks_dir / src.name
                if not dst.exists():
                    try:
                        src.rename(dst)
                        moved += 1
                    except OSError as e:
                        log.warning("Could not migrate %s: %s", src, e)
        if moved:
            log.info("Migrated %d notebook(s) from %s", moved, legacy)
        return moved


# ── Discovery ────────────────────────────────────────────────────────────────

def _stable_id(path: Path, root: Path | None = None) -> str:
    """
    Short stable hash of a path, used for URL-safe IDs. Hashed relative to the
    library root when given, so IDs (and the registry rows keyed on them)
    survive moving the library folder.
    """
    p = path.resolve()
    if root is not None:
        try:
            p = p.relative_to(Path(root).resolve())
        except ValueError:
            pass
    return hashlib.md5(str(p).encode()).hexdigest()[:12]


def discover_screenshots(paths: LibraryPaths) -> list[dict]:
    """
    Find every screenshot in the library.

    Screenshots are stored as PNGs (sometimes JPGs) at the root of the
    Kindle's mounted drive — we mirror them into library/screenshots/.
    Filenames typically look like "Screenshot_2024-XX-XX-HH-MM-SS.png".
    """
    items: list[dict] = []
    if not paths.screenshots_dir.exists():
        return items

    for img in sorted(paths.screenshots_dir.iterdir()):
        if not img.is_file():
            continue
        if img.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            continue
        try:
            stat = img.stat()
        except OSError:
            continue

        # Extract a friendly date from the filename if it follows the
        # device's typical pattern, otherwise fall back to mtime.
        captured = _parse_screenshot_timestamp(img.name) or stat.st_mtime
        items.append({
            "kind": "screenshot",
            "id": _stable_id(img, paths.root),
            "name": img.name,
            "filename": img.name,
            "path": str(img),
            "size_mb": round(stat.st_size / 1024 / 1024, 2),
            "size_kb": round(stat.st_size / 1024, 1),
            "modified": datetime.fromtimestamp(captured).isoformat(),
            "captured_at": datetime.fromtimestamp(captured).isoformat(),
            "extension": img.suffix.lower(),
        })

    items.sort(key=lambda n: n["modified"], reverse=True)
    return items


def _parse_screenshot_timestamp(filename: str) -> float | None:
    """
    Parse 'Screenshot_2024-04-29-10-30-45.png' style filenames into a unix
    timestamp. Returns None if the format doesn't match — the caller falls
    back to the file's mtime.
    """
    import re
    m = re.match(
        r"Screenshot_(\d{4})[-_]?(\d{2})[-_]?(\d{2})[-_]?"
        r"(\d{2})[-_]?(\d{2})[-_]?(\d{2})",
        filename,
    )
    if not m:
        return None
    try:
        dt = datetime(*map(int, m.groups()))
        return dt.timestamp()
    except (ValueError, OverflowError):
        return None


def discover_notebooks(paths: LibraryPaths) -> list[dict]:
    """
    Find every notebook in the library.

    Returns a list of dicts ready to JSON-serialize for the UI.
    """
    items: list[dict] = []
    if not paths.notebooks_dir.exists():
        return items

    for nbk in sorted(paths.notebooks_dir.glob("*.nbk")):
        try:
            stat = nbk.stat()
        except OSError:
            continue
        uuid = nbk.stem if UUID_RE.match(nbk.stem) else None
        thumb = paths.notebooks_dir / f"{nbk.stem}.png"
        items.append({
            "kind": "notebook",
            "id": _stable_id(nbk, paths.root),
            "name": uuid or nbk.stem,
            "uuid": uuid,
            "path": str(nbk),
            "format": "Notebook",
            "size_mb": round(stat.st_size / 1024 / 1024, 2),
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "has_thumbnail": thumb.exists(),
            "source": "scribe",
        })

    items.sort(key=lambda n: n["modified"], reverse=True)
    return items


def discover_books(
    paths: LibraryPaths,
    metadata_cache: dict | None = None,
    cache_dir: Path | None = None,
) -> list[dict]:
    """
    Find every book in the library, classify it as purchased or sideloaded,
    and attach cached metadata if present.

    cache_dir: optional path to the conversion cache. If provided, books
    without metadata covers are still marked has_cover=True when a converted
    first-page JPEG exists — the /cover route uses that as a fallback. This
    is what makes KFX books with no cover metadata still get a tile.
    """
    items: list[dict] = []
    cache = metadata_cache or {}

    for source_dir, source_label in (
        (paths.purchased_dir, "purchased"),
        (paths.sideloaded_dir, "sideloaded"),
    ):
        if not source_dir.exists():
            continue
        for f in sorted(source_dir.iterdir()):
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext not in PURCHASED_EXT and ext not in SIDELOADED_EXT:
                continue

            try:
                stat = f.stat()
            except OSError:
                continue

            book_id = _stable_id(f, paths.root)
            cached = cache.get(book_id, {})

            # Title fallback: use the filename without extension if no
            # metadata is available. For purchased books the filename is
            # often an ASIN like "B07XYZ123" — not pretty but recognizable.
            title = cached.get("title") or _filename_to_title(f)
            authors = cached.get("authors") or []

            # has_cover priority:
            #  1. Real metadata cover was extracted
            #  2. We have a converted PDF and its first page is cached as JPEG
            #     — the /cover route falls back to that
            has_cover = cached.get("has_cover", False)
            if not has_cover and cache_dir is not None:
                first_page = cache_dir / book_id / "page_0000.jpg"
                if first_page.exists():
                    has_cover = True

            items.append({
                "kind": "book",
                "id": book_id,
                "name": title,
                "title": title,
                "authors": authors,
                "path": str(f),
                "format": FORMAT_NAMES.get(ext, ext.lstrip(".").upper()),
                "extension": ext,
                "size_mb": round(stat.st_size / 1024 / 1024, 2),
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "source": source_label,
                "has_cover": has_cover,
                # Whether this book needs format conversion to render in-browser
                "needs_conversion": ext in PURCHASED_EXT,
            })

    items.sort(key=lambda n: n["modified"], reverse=True)
    return items


def _filename_to_title(path: Path) -> str:
    """Make a reasonable display title from a filename."""
    stem = path.stem
    # Replace underscores/dashes with spaces, collapse runs
    pretty = re.sub(r"[_\-]+", " ", stem).strip()
    # Don't over-prettify if it's clearly an ASIN or hash
    if re.match(r"^[A-Z0-9]{10}$", stem) or re.match(r"^[a-f0-9]{32}$", stem):
        return stem
    # Capitalize words if it looks lowercase-with-spaces
    if pretty.islower():
        pretty = pretty.title()
    return pretty


# ── Metadata cache ───────────────────────────────────────────────────────────

def load_metadata_cache(paths: LibraryPaths) -> dict:
    """Load the title/author/cover cache. Returns {} if missing or corrupt."""
    if not paths.meta_path.exists():
        return {}
    try:
        return json.loads(paths.meta_path.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("Corrupt metadata cache at %s — rebuilding", paths.meta_path)
        return {}


def save_metadata_cache(paths: LibraryPaths, cache: dict):
    """Atomically write the metadata cache."""
    paths.books_dir.mkdir(parents=True, exist_ok=True)
    tmp = paths.meta_path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(cache, indent=2, sort_keys=True))
    tmp.replace(paths.meta_path)


def update_book_metadata(paths: LibraryPaths, book_id: str,
                         title: str | None, authors: list[str] | None,
                         has_cover: bool):
    """Record metadata for one book in the cache."""
    with _META_LOCK:
        cache = load_metadata_cache(paths)
        cache[book_id] = {
            "title": title,
            "authors": authors or [],
            "has_cover": has_cover,
            "updated": datetime.utcnow().isoformat(),
        }
        save_metadata_cache(paths, cache)


# ── Classification helpers used by the MTP sync ──────────────────────────────

def classify_remote_file(filename: str, parent_path: str = "") -> str | None:
    """
    Decide where a file from the device should land in the local library.
    Returns one of: 'notebook', 'purchased', 'sideloaded', or None to skip.

    Heuristic for purchased vs sideloaded:
      Amazon-purchased books on a Kindle have an ASIN-shaped name
      (`B` + 9 alphanumerics, optionally with `_EBOK` / `_EBSP` / `_SMPL`
      / `_PDOC` suffix and a known extension). Anything else with a book
      extension is treated as sideloaded.

    If the parent folder name itself looks like an ASIN folder
    (e.g. `B07XYZ12345_EBOK/`), the file inside is also purchased.
    """
    lower = filename.lower()

    # Notebook database (file literally named "nbk", or .nbk extension)
    if lower == "nbk" or lower.endswith(".nbk"):
        return "notebook"

    suffix = Path(lower).suffix
    stem = Path(filename).stem

    # Helper: does `name` match an ASIN pattern?
    asin_re = re.compile(
        r"^B[0-9A-Z]{9}(?:_EBOK|_EBSP|_SMPL|_PDOC|_HMRK)?$",
        re.IGNORECASE,
    )

    parent_name = Path(parent_path).name

    if suffix in PURCHASED_EXT:
        if asin_re.match(stem) or asin_re.match(parent_name):
            return "purchased"
        return "sideloaded"

    if suffix in SIDELOADED_EXT:
        # PDFs and EPUBs can also be in ASIN folders if you bought them on
        # Amazon as fixed-layout (rare). Treat them as purchased then.
        if asin_re.match(stem) or asin_re.match(parent_name):
            return "purchased"
        return "sideloaded"

    return None


def safe_local_filename(remote_name: str) -> str:
    """
    Sanitize a filename from the device to be safe on the local filesystem.
    Strips path separators and other troublesome characters but otherwise
    preserves the original name for human readability.
    """
    name = remote_name.replace("/", "_").replace("\\", "_")
    # Remove control chars
    name = "".join(c for c in name if c == " " or c == "." or c.isalnum()
                   or c in "()[]{}_-,'!&+~@")
    name = name.strip().strip(".")
    return name or "untitled"
