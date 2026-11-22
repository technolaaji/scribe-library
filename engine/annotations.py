"""
annotations.py — highlights, typed notes and handwritten notes made *inside*
a book, exported with their position in the book.

Where the Kindle Scribe keeps them (see docs/annotations.md for the full
write-up):

  documents/<Book>.sdr/<Book><hash>.yjr
      KRDS file ("Kindle Reader Data Store"). One record per annotation:
      start position, end position, created/modified time, template, and
      then either the typed note text or the id of a handwriting canvas.

  .notebooks/<ASIN>!!EBOK!!notebook/nbk   (purchased books)
  .notebooks/<ASIN>!!PDOC!!notebook/nbk   (sideloaded / Send-to-Kindle books)
      A KDF notebook. Each handwritten note is one canvas (a `$260` section)
      that is *not* in the notebook's reading order; the `.yjr` record points
      at it by id.

A position looks like 'AZUIAAAAAAAA:110879':
  * the base64 part decodes to 9 bytes: version (1), eid (uint32 LE),
    offset within that element (uint32 LE);
  * the number after ':' is the absolute KFX position id (pid).
kfxlib's content position map turns (eid, offset) into a pid and gives the
text at every pid, and its location map turns a pid into the "Location N"
the Kindle shows. Location numbers match My Clippings.txt exactly.

CLI:
    python3 engine/annotations.py dump  <file.yjr>
    python3 engine/annotations.py export <book.kfx> [--yjr X] [--nbk Y] [--out DIR]
"""

from __future__ import annotations

import base64
import bisect
import json
import logging
import re
import shutil
import struct
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

log = logging.getLogger(__name__)

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

# Bump when the cached JSON shape changes.
CACHE_VERSION = 5


# ── KRDS (.yjr / .yjf) ──────────────────────────────────────────────────────

KRDS_MAGIC = b"\x00\x00\x00\x00\x00\x1a\xb1\x26"

# Annotation type ids used inside annotation.cache.object
ANNOTATION_TYPES = {
    0: "bookmark",
    1: "highlight",
    2: "note",
    10: "handwritten_note",
}


class KRDSError(Exception):
    pass


class _Reader:
    def __init__(self, data: bytes):
        self.b = data
        self.i = 0

    def take(self, n: int) -> bytes:
        if self.i + n > len(self.b):
            raise KRDSError(f"truncated at byte {self.i}")
        v = self.b[self.i:self.i + n]
        self.i += n
        return v

    def u8(self) -> int:
        return self.take(1)[0]

    def string(self) -> str | None:
        if self.u8() == 1:  # 1 = null string
            return None
        n = struct.unpack(">H", self.take(2))[0]
        return self.take(n).decode("utf-8", "replace")

    def value(self):
        t = self.u8()
        if t == 0x00:
            return self.u8() != 0
        if t == 0x01:
            return struct.unpack(">i", self.take(4))[0]
        if t == 0x02:
            return struct.unpack(">q", self.take(8))[0]
        if t == 0x03:
            return self.string()
        if t == 0x04:
            return struct.unpack(">f", self.take(4))[0]
        if t == 0x05:
            return struct.unpack(">d", self.take(8))[0]
        if t == 0x07:
            return struct.unpack(">h", self.take(2))[0]
        if t == 0x08:
            return self.u8()
        if t == 0xFE:
            name = self.string()
            children = []
            while self.b[self.i] != 0xFF:
                children.append(self.value())
            self.i += 1
            return KRDSObject(name, children)
        raise KRDSError(f"unknown value type 0x{t:02x} at byte {self.i - 1}")


@dataclass
class KRDSObject:
    name: str | None
    children: list


def parse_krds(data: bytes) -> list:
    """Parse a KRDS file into a list of top-level values/objects."""
    if not data.startswith(KRDS_MAGIC):
        raise KRDSError("not a KRDS file (bad magic)")
    r = _Reader(data)
    r.i = len(KRDS_MAGIC)
    out = []
    while r.i < len(data):
        try:
            out.append(r.value())
        except KRDSError:
            # Unknown trailing records (newer firmware) must not lose the
            # annotations we already decoded.
            log.debug("KRDS: stopping at byte %d", r.i)
            break
    return out


@dataclass
class Position:
    eid: int
    offset: int
    pid: int | None
    raw: str

    @classmethod
    def parse(cls, raw: str) -> "Position":
        encoded, _, number = (raw or "").partition(":")
        eid = offset = 0
        try:
            b = base64.b64decode(encoded + "=" * (-len(encoded) % 4))
            if len(b) >= 9:
                eid = struct.unpack("<I", b[1:5])[0]
                offset = struct.unpack("<I", b[5:9])[0]
        except (ValueError, struct.error):
            pass
        return cls(eid, offset, int(number) if number.isdigit() else None, raw)


@dataclass
class Annotation:
    type: str
    start: Position
    end: Position
    created: str | None
    modified: str | None
    note: str | None = None          # typed note text
    canvas_id: str | None = None     # handwritten note → canvas in the in-book nbk
    # filled by resolve()
    text: str | None = None
    location: int | None = None
    end_location: int | None = None
    image: str | None = None         # file name of the rendered handwriting
    # True when the device didn't store this note's position and it was
    # inferred from where you were reading when you wrote it.
    approximate: bool = False


def _ms_to_iso(v) -> str | None:
    if not isinstance(v, int) or v <= 0:
        return None
    return datetime.fromtimestamp(v / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def read_annotations(yjr_path: Path) -> list[Annotation]:
    """All annotations stored in a .yjr file, in reading order."""
    tree = parse_krds(Path(yjr_path).read_bytes())
    found: list[Annotation] = []

    def walk(v):
        if not isinstance(v, KRDSObject):
            return
        if v.name and v.name.startswith("annotation.personal."):
            kind = v.name.rsplit(".", 1)[-1]
            vals = v.children
            strings_after = [x for x in vals[5:] if isinstance(x, str)]
            ann = Annotation(
                type=kind,
                start=Position.parse(vals[0] if len(vals) > 0 else ""),
                end=Position.parse(vals[1] if len(vals) > 1 else ""),
                created=_ms_to_iso(vals[2] if len(vals) > 2 else None),
                modified=_ms_to_iso(vals[3] if len(vals) > 3 else None),
            )
            if kind == "handwritten_note" and strings_after:
                ann.canvas_id = strings_after[0]
            elif kind == "note" and strings_after:
                ann.note = strings_after[0]
            found.append(ann)
            return
        for c in v.children:
            walk(c)

    for v in tree:
        walk(v)

    found.sort(key=lambda a: (a.start.pid if a.start.pid is not None else 1 << 62))
    return found


# ── Positions → text and locations (kfxlib) ─────────────────────────────────

def _quiet_kfxlib():
    from kfxlib.message_logging import set_logger, JobLog
    base = logging.getLogger("kfxlib.annotations")
    base.setLevel(logging.ERROR)
    set_logger(JobLog(base))


class BookPositions:
    """Text and location lookup for one KFX book."""

    def __init__(self, book_path: Path):
        from kfxlib.yj_book import YJ_Book
        _quiet_kfxlib()
        self.book = YJ_Book(str(book_path))
        self.book.decode_book()
        self.chunks = self.book.collect_content_position_info()
        self.chunk_pids = [c.pid for c in self.chunks]
        self.by_eid: dict[int, list] = {}
        for c in self.chunks:
            self.by_eid.setdefault(c.eid, []).append(c)
        loc = self.book.collect_location_map_info(self.chunks)
        if not loc:
            loc = self.book.generate_approximate_locations(self.chunks)
        self.location_pids = [l.pid for l in loc]

    def metadata(self, key: str):
        try:
            return self.book.get_metadata_value(key)
        except Exception:
            return None

    def pid(self, pos: Position) -> int | None:
        for c in self.by_eid.get(pos.eid, []):
            if c.eid_offset <= pos.offset < c.eid_offset + max(c.length, 1):
                return c.pid + (pos.offset - c.eid_offset)
        return pos.pid

    def location(self, pid: int | None) -> int | None:
        if pid is None or not self.location_pids:
            return None
        return max(1, bisect.bisect_right(self.location_pids, pid))

    def text(self, start: int, end: int) -> str:
        parts = []
        i = max(0, bisect.bisect_right(self.chunk_pids, start) - 1)
        while i < len(self.chunks):
            c = self.chunks[i]
            if c.pid > end:
                break
            if c.text and c.pid + c.length > start:
                a = max(0, start - c.pid)
                z = min(c.length, end - c.pid + 1)
                parts.append(c.text[a:z])
            i += 1
        return " ".join(" ".join(parts).split())


# ── Handwriting canvases (in-book notebook) ─────────────────────────────────

def canvas_ids(nbk_path: Path) -> list[str]:
    """Every handwriting canvas stored in an in-book notebook."""
    from kfxlib.yj_book import YJ_Book
    _quiet_kfxlib()
    tmp = Path(tempfile.mkdtemp(prefix="inbook_"))
    try:
        d = tmp / "nb"
        d.mkdir()
        shutil.copyfile(nbk_path, d / "nbk")
        b = YJ_Book(str(d))
        b.decode_book(pure=True)
        return [str(f.fid) for f in b.fragments.get_all("$260")]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def render_canvases(nbk_path: Path, out_dir: Path, width: int = 1200) -> dict[str, str]:
    """
    Render every handwriting canvas in an in-book notebook to PNG.

    kfxlib only renders pages that are in a notebook's reading order, and
    in-book canvases never are, so we put all of them into the reading order
    before converting. Returns {canvas_id: png file name}.
    """
    import cairosvg
    from kfxlib.yj_book import YJ_Book
    _quiet_kfxlib()

    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="inbook_"))
    rendered: dict[str, str] = {}
    try:
        d = tmp / "nb"
        d.mkdir()
        shutil.copyfile(nbk_path, d / "nbk")
        b = YJ_Book(str(d))
        b.decode_book(pure=True)
        sections = [f.fid for f in b.fragments.get_all("$260")]
        if not sections:
            return rendered
        for ro_type in ("$538", "$258"):
            for frag in b.fragments.get_all(ro_type):
                for ro in frag.value.get("$169", []):
                    ro["$170"] = list(sections)
        epub = b.convert_to_epub()
        zf = zipfile.ZipFile(BytesIO(epub))
        for name in zf.namelist():
            if not name.endswith(".svg"):
                continue
            cid = Path(name).stem
            png = out_dir / f"{cid}.png"
            cairosvg.svg2png(bytestring=zf.read(name), write_to=str(png),
                             output_width=width, background_color="white")
            rendered[cid] = png.name
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return rendered


# ── Finding the files for a book ────────────────────────────────────────────

def find_sidecar(book_path: Path, search_dirs: list[Path], ext: str) -> Path | None:
    """A book's sidecar file: <dir>/<book stem>.sdr/*<ext>, newest first."""
    stem = Path(book_path).stem
    candidates: list[Path] = []
    for d in search_dirs:
        sdr = Path(d) / f"{stem}.sdr"
        if sdr.is_dir():
            candidates += [p for p in sdr.glob(f"*{ext}") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def find_yjr(book_path: Path, search_dirs: list[Path]) -> Path | None:
    """The .yjr sidecar (annotations) for a book."""
    return find_sidecar(book_path, search_dirs, ".yjr")


def last_read_position(yjf_path: Path) -> tuple[str, float] | None:
    """
    (position, unix time) of where you last were in the book, from the .yjf
    reading-state file: its `lpr` object holds a byte, the position string
    and the time it was saved (ms). Parsed directly because .yjf files use
    KRDS types the annotation parser doesn't need.
    """
    try:
        b = Path(yjf_path).read_bytes()
    except OSError:
        return None
    i = b.find(b"\x00\x00\x03lpr")
    if i < 0:
        return None
    j = b.find(b"\x03\x00", i + 6)
    if j < 0 or j > i + 16:
        return None
    n = struct.unpack(">H", b[j + 2:j + 4])[0]
    pos = b[j + 4:j + 4 + n].decode("utf-8", "replace")
    k = j + 4 + n
    if b[k:k + 1] != b"\x02":
        return None
    ms = struct.unpack(">q", b[k + 1:k + 9])[0]
    return pos, ms / 1000


def find_in_book_notebook(asin: str | None, in_book_dir: Path,
                          wanted_canvases: set[str]) -> Path | None:
    """The in-book notebook for a book: by ASIN first, else by canvas ids."""
    if not in_book_dir.is_dir():
        return None
    if asin:
        p = in_book_dir / f"{asin}.nbk"
        if p.exists():
            return p
    if not wanted_canvases:
        return None
    for nbk in sorted(in_book_dir.glob("*.nbk")):
        try:
            data = nbk.read_bytes()
        except OSError:
            continue
        if any(c.encode() in data for c in wanted_canvases):
            return nbk
    return None


# ── High-level: everything for one book ─────────────────────────────────────

@dataclass
class BookAnnotations:
    book: str
    title: str | None = None
    asin: str | None = None
    yjr: str | None = None
    nbk: str | None = None
    annotations: list[Annotation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    total_positions: int | None = None   # length of the book in KFX positions

    def to_json(self) -> dict:
        d = asdict(self)
        d["counts"] = {}
        for a in self.annotations:
            d["counts"][a.type] = d["counts"].get(a.type, 0) + 1
        return d


# A reading position saved this soon after the in-book notebook changed is
# taken to be where that new note was written.
LAST_READ_WINDOW = 15 * 60


def collect(book_path: Path, sdr_dirs: list[Path], in_book_dir: Path,
            image_dir: Path, hints: dict[str, str] | None = None,
            remember=None) -> BookAnnotations:
    """
    Read, resolve and render all annotations for one book.

    hints: {canvas_id: position} for notes whose position was recorded
    earlier. remember(canvas_id, position) is called when a new one is
    inferred, so it can be kept (the reading position moves on).
    """
    hints = dict(hints or {})
    book_path = Path(book_path)
    result = BookAnnotations(book=str(book_path))

    yjr = find_yjr(book_path, sdr_dirs)
    if yjr:
        result.yjr = str(yjr)
        result.annotations = read_annotations(yjr)

    positions = None
    if book_path.suffix.lower() in (".kfx", ".azw8", ".kfx-zip"):
        try:
            positions = BookPositions(book_path)
            result.title = positions.metadata("title")
            result.asin = positions.metadata("ASIN")
            if positions.chunks:
                last = positions.chunks[-1]
                result.total_positions = last.pid + last.length
        except Exception as e:
            result.warnings.append(f"Could not open the book to resolve text: {e}")
    else:
        result.warnings.append(
            f"Text lookup only works for KFX books ({book_path.suffix} positions are kept raw).")

    def resolve(a: Annotation):
        if positions:
            p0 = positions.pid(a.start)
            p1 = positions.pid(a.end)
            a.location = positions.location(p0)
            a.end_location = positions.location(p1)
            if a.type == "highlight" and p0 is not None and p1 is not None:
                a.text = positions.text(p0, p1)
            elif a.type in ("note", "handwritten_note") and p0 is not None:
                # The passage the note is attached to (first ~200 chars).
                a.text = positions.text(p0, p0 + 200) or None

    for a in result.annotations:
        resolve(a)

    wanted = {a.canvas_id for a in result.annotations if a.canvas_id}
    nbk = find_in_book_notebook(result.asin, in_book_dir, wanted)
    if wanted and not nbk:
        result.warnings.append(
            "Handwritten notes found, but their in-book notebook isn't synced yet.")
    if nbk:
        result.nbk = str(nbk)
        try:
            images = render_canvases(nbk, image_dir)
        except Exception as e:
            images = {}
            result.warnings.append(f"Could not render handwriting: {e}")
        for a in result.annotations:
            if a.canvas_id:
                a.image = images.get(a.canvas_id)
                if not a.image:
                    result.warnings.append(f"Canvas {a.canvas_id} not found in {nbk.name}.")
        # Handwriting with no anchor in the .yjr. Active Canvas notes (written
        # into a space opened in the text) never get one on the USB-visible
        # storage, and books delivered by Amazon keep theirs internally. If
        # exactly one of them is new and the book's reading position was
        # saved right after the notebook changed, that's where it was written.
        orphans = [cid for cid in images if cid not in wanted]
        unplaced = [cid for cid in orphans if cid not in hints]
        yjf = find_sidecar(book_path, sdr_dirs, ".yjf")
        if len(unplaced) == 1 and yjf:
            last = last_read_position(yjf)
            nbk_time = nbk.stat().st_mtime
            if last and -60 <= last[1] - nbk_time <= LAST_READ_WINDOW:
                hints[unplaced[0]] = last[0]
                if remember:
                    remember(unplaced[0], last[0])
        missing = 0
        for cid in orphans:
            raw = hints.get(cid)
            a = Annotation(
                type="handwritten_note", start=Position.parse(raw or ""),
                end=Position.parse(raw or ""), created=None, modified=None,
                canvas_id=cid, image=images[cid], approximate=bool(raw))
            if raw:
                resolve(a)
            else:
                missing += 1
            result.annotations.append(a)
        if missing:
            result.warnings.append(
                f"{missing} handwritten note{'s' if missing != 1 else ''} without a position: "
                "the Scribe doesn't write where Active Canvas notes sit to its USB storage. "
                "Sync right after writing a note and its position is picked up from where you "
                "were reading.")
        result.annotations.sort(
            key=lambda a: (a.start.pid if a.start.pid is not None else 1 << 62))
    if not yjr and not nbk:
        result.warnings.append("No annotations synced for this book.")
    return result


def collect_cached(book_path: Path, sdr_dirs: list[Path], in_book_dir: Path,
                   cache_dir: Path, hints: dict[str, str] | None = None,
                   remember=None) -> dict:
    """collect(), cached on disk until the book, sidecar or notebook change."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    image_dir = cache_dir / "handwriting"
    cache_file = cache_dir / "annotations.json"

    yjr = find_yjr(book_path, sdr_dirs)
    stamp = {
        "v": CACHE_VERSION,
        "book": _mtime(book_path),
        "yjr": _mtime(yjr) if yjr else None,
        "yjf": _mtime(find_sidecar(book_path, sdr_dirs, ".yjf")),
        "hints": sorted((hints or {}).items()),
        "inbook": max((_mtime(p) for p in in_book_dir.glob("*.nbk")), default=None)
        if in_book_dir.is_dir() else None,
    }
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
            if cached.get("stamp") == stamp:
                return cached["data"]
        except (OSError, json.JSONDecodeError):
            pass

    data = collect(book_path, sdr_dirs, in_book_dir, image_dir,
                   hints=hints, remember=remember).to_json()
    cache_file.write_text(json.dumps({"stamp": stamp, "data": data}))
    return data


def _mtime(p: Path | None) -> float | None:
    try:
        return Path(p).stat().st_mtime if p else None
    except OSError:
        return None


# ── Positions → pages of the rendered PDF ───────────────────────────────────

def _norm(text: str) -> str:
    """Lowercase letters and digits only, single-spaced, line-break hyphens joined."""
    text = text.replace("\xad", "").replace("-\n", "").lower()
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text).split())


def _page_tokens(page) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Normalised word tokens on a PDF page, each with its box (x0, y0, x1, y1)."""
    out = []
    for w in page.get_text("words"):
        for tok in _norm(w[4]).split():
            out.append((tok, (w[0], w[1], w[2], w[3])))
    return out


def _find_tokens(tokens, needle: list[str]) -> int | None:
    """Index in `tokens` where the word sequence `needle` starts, or None."""
    if not needle:
        return None
    words = [t for t, _ in tokens]
    for i in range(len(words) - len(needle) + 1):
        if words[i:i + len(needle)] == needle:
            return i
    return None


def _line_rects(boxes, page_w, page_h) -> list[list[float]]:
    """Merge word boxes into one rectangle per text line, as page fractions."""
    lines: list[list[float]] = []
    for x0, y0, x1, y1 in sorted(boxes, key=lambda b: (round(b[1]), b[0])):
        if lines and abs(lines[-1][1] - y0) < (y1 - y0) * 0.5:
            line = lines[-1]
            line[0], line[1] = min(line[0], x0), min(line[1], y0)
            line[2], line[3] = max(line[2], x1), max(line[3], y1)
        else:
            lines.append([x0, y0, x1, y1])
    return [[l[0] / page_w, l[1] / page_h, l[2] / page_w, l[3] / page_h] for l in lines]


def locate_pages(pdf_path: Path, data: dict, words: int = 10) -> list[dict]:
    """
    Where each annotation falls in the rendered PDF: page, and spot on it.

    The reader shows books as a PDF we typeset ourselves, so Kindle positions
    don't map to its pages directly. Each annotation's passage is looked up in
    the PDF's text (its first `words` words); when it appears more than once,
    the match nearest the annotation's relative place in the book wins. With
    no text match, the page is estimated from that relative place.

    Returns, in the order of data["annotations"]:
        {page, exact, x, y, left, end_page, end_y, rects, highlight}
    x/y: start of the passage as fractions of the page (None if only the page
    is known); left: where that page's text column starts; end_page/end_y:
    end of a highlight; rects: for highlights, one [page, x0, y0, x1, y1] per
    highlighted line; highlight: for notes, the index of the highlight the
    note was written inside, if any.
    """
    try:
        import pymupdf as fitz
    except ImportError:  # older PyMuPDF
        import fitz
    doc = fitz.open(str(pdf_path))
    page_text = [_norm(pg.get_text()) for pg in doc]
    starts, offset = [], 0
    for t in page_text:
        starts.append(offset)
        offset += len(t) + 1
    full = " ".join(page_text)
    n_pages = len(page_text)
    total = data.get("total_positions") or 0
    token_cache: dict[int, list] = {}

    def tokens(i):
        if i not in token_cache:
            token_cache[i] = _page_tokens(doc[i])
        return token_cache[i]

    def find(page_i, text_words, from_end=False):
        """Token index of a passage's first (or last) word on a page."""
        toks = tokens(page_i)
        for k in range(min(len(text_words), 8), 2, -1):
            seq = text_words[-k:] if from_end else text_words[:k]
            i = _find_tokens(toks, seq)
            if i is not None:
                return i + k - 1 if from_end else i
        return None

    anns = data.get("annotations", [])
    out = []
    for a in anns:
        pid = (a.get("start") or {}).get("pid")
        rel = (pid / total) if (pid is not None and total) else None
        res = {"page": None, "exact": False, "x": None, "y": None, "left": None,
               "end_page": None, "end_y": None, "rects": [], "highlight": None}
        text_words = _norm(a.get("text") or "").split()
        needle = " ".join(text_words[:words])
        if len(needle) >= 12:
            hits, i = [], full.find(needle)
            while i >= 0:
                hits.append(i)
                i = full.find(needle, i + 1)
            if hits:
                target = rel * len(full) if rel is not None else hits[0]
                best = min(hits, key=lambda h: abs(h - target))
                res["page"], res["exact"] = bisect.bisect_right(starts, best) - 1, True
        if res["page"] is None and rel is not None and n_pages:
            res["page"] = min(n_pages - 1, max(0, round(rel * (n_pages - 1))))

        if res["exact"]:
            pg = res["page"]
            rect = doc[pg].rect
            toks = tokens(pg)
            si = find(pg, text_words)
            if si is not None:
                box = toks[si][1]
                res["x"], res["y"] = box[0] / rect.width, box[1] / rect.height
            if toks:
                res["left"] = min(b[0] for _, b in toks) / rect.width
            if a.get("type") == "highlight" and si is not None and len(text_words) > 3:
                for j in range(pg, min(n_pages, pg + 6)):
                    ei = find(j, text_words, from_end=True)
                    if ei is not None and (j > pg or ei >= si):
                        end_rect = doc[j].rect
                        res["end_page"] = j
                        res["end_y"] = tokens(j)[ei][1][3] / end_rect.height
                        for q in range(pg, j + 1):
                            qt = tokens(q)
                            lo = si if q == pg else 0
                            hi = ei if q == j else len(qt) - 1
                            r = doc[q].rect
                            for line in _line_rects([b for _, b in qt[lo:hi + 1]], r.width, r.height):
                                res["rects"].append([q] + line)
                        break
        out.append(res)
    doc.close()

    # a handwritten note written inside a highlighted passage belongs to it
    spans = [(i, a["start"].get("pid"), a["end"].get("pid")) for i, a in enumerate(anns)
             if a.get("type") == "highlight" and a["start"].get("pid") is not None
             and a["end"].get("pid") is not None]
    for i, a in enumerate(anns):
        if a.get("type") != "handwritten_note":
            continue
        p = (a.get("start") or {}).get("pid")
        if p is None:
            continue
        inside = [j for j, s0, s1 in spans if s0 - 5 <= p <= s1 + 5]
        if inside:
            out[i]["highlight"] = min(inside, key=lambda j: anns[j]["end"]["pid"] - anns[j]["start"]["pid"])
    return out


# ── Markdown export ─────────────────────────────────────────────────────────

LABELS = {"highlight": "Highlight", "note": "Note",
          "handwritten_note": "Handwritten note", "bookmark": "Bookmark"}


def to_markdown(data: dict, title: str, image_prefix: str = "") -> str:
    lines = [f"# {title}", ""]
    counts = data.get("counts") or {}
    if counts:
        lines.append(" · ".join(f"{n} {LABELS.get(k, k).lower()}{'s' if n != 1 else ''}"
                                for k, n in counts.items()))
        lines.append("")
    for a in data.get("annotations", []):
        where = f"Location {a['location']}" if a.get("location") else f"Position {a['start'].get('pid')}"
        if a.get("end_location") and a["end_location"] != a.get("location"):
            where += f"–{a['end_location']}"
        when = (a.get("created") or "")[:10]
        lines.append(f"## {LABELS.get(a['type'], a['type'])} · {where}" + (f" · {when}" if when else ""))
        lines.append("")
        if a.get("text"):
            lines += [f"> {a['text']}", ""]
        if a.get("note"):
            lines += [a["note"], ""]
        if a.get("image"):
            lines += [f"![Handwritten note at {where}]({image_prefix}{a['image']})", ""]
    if data.get("warnings"):
        lines += ["---", ""] + [f"- {w}" for w in data["warnings"]] + [""]
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────────────

def _main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump", help="print the annotations in a .yjr file")
    d.add_argument("yjr", type=Path)
    e = sub.add_parser("export", help="export a book's annotations to Markdown + PNGs")
    e.add_argument("book", type=Path)
    e.add_argument("--yjr", type=Path, help="sidecar .yjr (default: <book>.sdr/*.yjr next to it)")
    e.add_argument("--nbk", type=Path, help="in-book notebook (default: library/notebooks/in-book/<ASIN>.nbk)")
    e.add_argument("--out", type=Path, default=Path("annotations-export"))
    args = ap.parse_args(argv)

    if args.cmd == "dump":
        for a in read_annotations(args.yjr):
            print(json.dumps(asdict(a)))
        return 0

    root = _THIS_DIR.parent / "library"
    sdr_dirs = [args.book.parent, root / "books" / "sideloaded"]
    in_book = root / "notebooks" / "in-book"
    if args.yjr:
        tmp = Path(tempfile.mkdtemp()) / f"{args.book.stem}.sdr"
        tmp.mkdir()
        shutil.copyfile(args.yjr, tmp / args.yjr.name)
        sdr_dirs.insert(0, tmp.parent)
    if args.nbk:
        tmp_nb = Path(tempfile.mkdtemp())
        shutil.copyfile(args.nbk, tmp_nb / args.nbk.name)
        in_book = tmp_nb
    args.out.mkdir(parents=True, exist_ok=True)
    data = collect(args.book, sdr_dirs, in_book, args.out / "images").to_json()
    md = to_markdown(data, data.get("title") or args.book.stem, image_prefix="images/")
    (args.out / "annotations.md").write_text(md)
    (args.out / "annotations.json").write_text(json.dumps(data, indent=2))
    print(f"Wrote {args.out / 'annotations.md'} ({len(data['annotations'])} annotations)")
    for w in data["warnings"]:
        print(f"  ⚠ {w}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
