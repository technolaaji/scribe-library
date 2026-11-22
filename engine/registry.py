"""
registry.py — the local catalogue of everything in the library.

Files on disk are the source of truth for *content*; this SQLite database is
the source of truth for everything *you* add on top of it:

  * a display name (Scribe notebooks arrive as bare UUIDs; rename them here)
  * a shelf, for grouping (e.g. "Journal", "Work", "Reading notes")
  * a free-text note
  * when you last opened each item

Shelves are first-class: you can create one before putting anything on it,
rename it (its items follow), or remove it (its items become unshelved).

It also tracks what changed between syncs. Every time the library is listed
we reconcile the files we find against what the registry remembers:

  new file            → `added`   event, item shows as New until opened
  content changed     → `updated` event, item shows as Updated until opened
  file disappeared    → `removed` event, row kept (so names survive a resync)
  file came back      → `restored` event

"Content changed" is decided by a SHA-1 of the file, recomputed only when the
size or mtime moves, so listing stays fast.

The database lives at <library>/scribe.db. It's plain SQLite: open it with
any SQLite browser if you want to look inside.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id             TEXT PRIMARY KEY,      -- same id the API uses
    kind           TEXT NOT NULL,         -- notebook | book | screenshot
    rel_path       TEXT NOT NULL,         -- path relative to the library root
    original_name  TEXT,                  -- name from the device / metadata
    display_name   TEXT,                  -- your name for it (NULL = original)
    shelf          TEXT,                  -- your grouping (NULL = unshelved)
    note           TEXT,                  -- your note
    size           INTEGER,
    mtime          REAL,
    fingerprint    TEXT,                  -- sha1 of the file contents
    version        INTEGER NOT NULL DEFAULT 1,
    first_seen     TEXT NOT NULL,
    last_changed   TEXT,                  -- last time the content changed
    last_opened    TEXT,                  -- last time you opened it here
    removed_at     TEXT                   -- set while the file is missing
);
CREATE INDEX IF NOT EXISTS items_kind ON items(kind);

CREATE TABLE IF NOT EXISTS changes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id   TEXT NOT NULL REFERENCES items(id),
    change    TEXT NOT NULL,              -- added | updated | removed | restored | renamed
    at        TEXT NOT NULL,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS changes_at ON changes(at);
CREATE INDEX IF NOT EXISTS changes_item ON changes(item_id);

CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

CREATE TABLE IF NOT EXISTS shelves (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE COLLATE NOCASE,   -- items.shelf holds this name
    description  TEXT,
    created_at   TEXT NOT NULL
);

-- Where a handwritten note sits in its book, when the device didn't store it
-- where USB can read it (Active Canvas notes). Recorded once, then kept.
CREATE TABLE IF NOT EXISTS note_positions (
    canvas_id    TEXT PRIMARY KEY,
    item_id      TEXT NOT NULL,       -- the book
    position     TEXT NOT NULL,       -- KFX position string, e.g. 'AbIDAAAAAAAA:180432'
    source       TEXT NOT NULL,       -- 'last-read' = where you were reading when it was written
    recorded_at  TEXT NOT NULL
);
"""

# Columns the UI is allowed to edit.
EDITABLE = ("display_name", "shelf", "note")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def file_fingerprint(path: Path) -> str | None:
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


class Registry:
    def __init__(self, db_path: Path, library_root: Path):
        self.db_path = Path(db_path)
        self.root = Path(library_root)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.executescript(SCHEMA)
            db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                       (str(SCHEMA_VERSION),))
            # Shelves used to exist only as names on items; give each a row.
            db.execute("""INSERT OR IGNORE INTO shelves(name, created_at)
                          SELECT DISTINCT shelf, ? FROM items
                          WHERE shelf IS NOT NULL AND shelf != ''""", (_now(),))

    # ── plumbing ────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _rel(self, path: str | Path) -> str:
        p = Path(path)
        try:
            return str(p.resolve().relative_to(self.root.resolve()))
        except ValueError:
            return str(p)

    def _meta(self, db, key: str) -> str | None:
        row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    # ── reconcile ───────────────────────────────────────────────────────────

    def reconcile(self, items: list[dict]) -> dict:
        """
        Bring the registry in line with the files that exist right now.
        `items` are the dicts produced by library.discover_*.
        Returns counts of what changed.
        """
        counts = {"added": 0, "updated": 0, "removed": 0, "restored": 0}
        now = _now()
        with self._lock, self._connect() as db:
            # The very first reconcile is the baseline: register everything
            # quietly so the library doesn't open with every item marked New.
            first_run = self._meta(db, "baseline_at") is None
            known = {r["id"]: r for r in db.execute("SELECT * FROM items")}
            seen: set[str] = set()

            for item in items:
                iid = item["id"]
                seen.add(iid)
                path = Path(item["path"])
                try:
                    st = path.stat()
                except OSError:
                    continue
                row = known.get(iid)

                if row is None:
                    db.execute(
                        """INSERT INTO items(id, kind, rel_path, original_name, size, mtime,
                                             fingerprint, first_seen, last_opened)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (iid, item["kind"], self._rel(path), _original_name(item),
                         st.st_size, st.st_mtime, file_fingerprint(path), now,
                         now if first_run else None),
                    )
                    if not first_run:
                        self._log(db, iid, "added", now)
                    counts["added"] += 1
                    continue

                if row["removed_at"]:
                    db.execute("UPDATE items SET removed_at = NULL WHERE id = ?", (iid,))
                    self._log(db, iid, "restored", now)
                    counts["restored"] += 1

                # Refresh the name the device/metadata gives it (books get
                # their real title once metadata is extracted).
                orig = _original_name(item)
                if orig and orig != row["original_name"]:
                    db.execute("UPDATE items SET original_name = ? WHERE id = ?", (orig, iid))

                if row["size"] == st.st_size and row["mtime"] == st.st_mtime:
                    continue

                fp = file_fingerprint(path)
                if fp and fp != row["fingerprint"]:
                    delta = st.st_size - (row["size"] or 0)
                    db.execute(
                        """UPDATE items SET size = ?, mtime = ?, fingerprint = ?,
                                  version = version + 1, last_changed = ?
                           WHERE id = ?""",
                        (st.st_size, st.st_mtime, fp, now, iid),
                    )
                    self._log(db, iid, "updated", now,
                              f"{'+' if delta >= 0 else ''}{delta / 1024:.0f} KB")
                    counts["updated"] += 1
                else:
                    # Same bytes, new timestamp (e.g. re-downloaded): no change.
                    db.execute("UPDATE items SET size = ?, mtime = ? WHERE id = ?",
                               (st.st_size, st.st_mtime, iid))

            for iid, row in known.items():
                if iid not in seen and not row["removed_at"]:
                    db.execute("UPDATE items SET removed_at = ? WHERE id = ?", (now, iid))
                    self._log(db, iid, "removed", now)
                    counts["removed"] += 1

            if first_run and items:
                db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('baseline_at', ?)", (now,))
            if any(counts[k] for k in ("updated", "removed", "restored")) or (
                    counts["added"] and not first_run):
                db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('last_change_at', ?)", (now,))
        return counts

    def _log(self, db, item_id: str, change: str, at: str, detail: str | None = None):
        db.execute("INSERT INTO changes(item_id, change, at, detail) VALUES (?, ?, ?, ?)",
                   (item_id, change, at, detail))

    # ── reads ───────────────────────────────────────────────────────────────

    def annotate(self, items: list[dict]) -> list[dict]:
        """Attach registry fields (names, shelf, status) to discovered items."""
        with self._connect() as db:
            rows = {r["id"]: r for r in db.execute("SELECT * FROM items")}
        out = []
        for item in items:
            row = rows.get(item["id"])
            out.append({**item, **_registry_fields(row, item)})
        return out

    def get(self, item_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None

    def shelves(self) -> list[dict]:
        """Every shelf, including empty ones, with how many items it holds."""
        with self._connect() as db:
            return [dict(r) for r in db.execute(
                """SELECT s.id, s.name, s.description, s.created_at,
                          COUNT(i.id) AS count,
                          COALESCE(SUM(i.kind = 'notebook'), 0) AS notebooks,
                          COALESCE(SUM(i.kind = 'book'), 0) AS books,
                          COALESCE(SUM(i.kind = 'screenshot'), 0) AS screenshots
                   FROM shelves s
                   LEFT JOIN items i ON i.shelf = s.name COLLATE NOCASE AND i.removed_at IS NULL
                   GROUP BY s.id ORDER BY s.name COLLATE NOCASE""")]

    def changes(self, limit: int = 200, item_id: str | None = None) -> list[dict]:
        q = """SELECT c.id, c.item_id, c.change, c.at, c.detail, i.kind,
                      COALESCE(i.display_name, i.original_name) AS name,
                      i.display_name IS NOT NULL AS renamed, i.rel_path
               FROM changes c JOIN items i ON i.id = c.item_id"""
        args: list = []
        if item_id:
            q += " WHERE c.item_id = ?"
            args.append(item_id)
        q += " ORDER BY c.at DESC, c.id DESC LIMIT ?"
        args.append(limit)
        with self._connect() as db:
            return [dict(r) for r in db.execute(q, args)]

    def summary(self) -> dict:
        with self._connect() as db:
            unseen = db.execute(
                """SELECT COUNT(*) FROM items WHERE removed_at IS NULL AND (
                       last_opened IS NULL OR (last_changed IS NOT NULL AND last_changed > last_opened))"""
            ).fetchone()[0]
            return {
                "unseen": unseen,
                "baseline_at": self._meta(db, "baseline_at"),
                "last_change_at": self._meta(db, "last_change_at"),
            }

    # ── writes ──────────────────────────────────────────────────────────────

    # ── shelves ─────────────────────────────────────────────────────────────

    def create_shelf(self, name: str, description: str | None = None) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("A shelf needs a name.")
        with self._lock, self._connect() as db:
            if db.execute("SELECT 1 FROM shelves WHERE name = ?", (name,)).fetchone():
                raise ValueError(f"There's already a shelf called {name}.")
            db.execute("INSERT INTO shelves(name, description, created_at) VALUES (?, ?, ?)",
                       (name, (description or "").strip() or None, _now()))
        return self._shelf_by_name(name)

    def update_shelf(self, shelf_id: int, name: str | None = None,
                     description: str | None = None) -> dict | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM shelves WHERE id = ?", (shelf_id,)).fetchone()
            if not row:
                return None
            new_name = (name or "").strip() or row["name"]
            if new_name.lower() != row["name"].lower() and db.execute(
                    "SELECT 1 FROM shelves WHERE name = ?", (new_name,)).fetchone():
                raise ValueError(f"There's already a shelf called {new_name}.")
            desc = row["description"] if description is None else (description.strip() or None)
            db.execute("UPDATE shelves SET name = ?, description = ? WHERE id = ?",
                       (new_name, desc, shelf_id))
            # items refer to shelves by name: carry them over
            db.execute("UPDATE items SET shelf = ? WHERE shelf = ? COLLATE NOCASE",
                       (new_name, row["name"]))
        return self._shelf_by_name(new_name)

    def delete_shelf(self, shelf_id: int) -> bool:
        """Remove a shelf. Its items stay in the library, unshelved."""
        with self._lock, self._connect() as db:
            row = db.execute("SELECT name FROM shelves WHERE id = ?", (shelf_id,)).fetchone()
            if not row:
                return False
            db.execute("UPDATE items SET shelf = NULL WHERE shelf = ? COLLATE NOCASE", (row["name"],))
            db.execute("DELETE FROM shelves WHERE id = ?", (shelf_id,))
        return True

    def set_shelf_items(self, shelf_id: int, add: list[str] = (), remove: list[str] = ()) -> dict | None:
        """Put items on a shelf (moving them off any other) or take them off it."""
        with self._lock, self._connect() as db:
            row = db.execute("SELECT name FROM shelves WHERE id = ?", (shelf_id,)).fetchone()
            if not row:
                return None
            for iid in add:
                db.execute("UPDATE items SET shelf = ? WHERE id = ?", (row["name"], iid))
            for iid in remove:
                db.execute("UPDATE items SET shelf = NULL WHERE id = ? AND shelf = ? COLLATE NOCASE",
                           (iid, row["name"]))
        return self._shelf_by_name(row["name"])

    def _shelf_by_name(self, name: str) -> dict | None:
        return next((s for s in self.shelves() if s["name"].lower() == name.lower()), None)

    # ── handwritten-note positions ──────────────────────────────────────────

    def note_positions(self, item_id: str) -> dict[str, str]:
        with self._connect() as db:
            return {r["canvas_id"]: r["position"] for r in db.execute(
                "SELECT canvas_id, position FROM note_positions WHERE item_id = ?", (item_id,))}

    def save_note_position(self, item_id: str, canvas_id: str, position: str,
                           source: str = "last-read"):
        with self._lock, self._connect() as db:
            db.execute("""INSERT OR IGNORE INTO note_positions
                          (canvas_id, item_id, position, source, recorded_at)
                          VALUES (?, ?, ?, ?, ?)""",
                       (canvas_id, item_id, position, source, _now()))

    # ── item edits ──────────────────────────────────────────────────────────

    def update(self, item_id: str, fields: dict) -> dict | None:
        clean = {}
        for k in EDITABLE:
            if k in fields:
                v = fields[k]
                v = v.strip() if isinstance(v, str) else v
                clean[k] = v or None
        if not clean:
            return self.get(item_id)
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if not row:
                return None
            if clean.get("shelf"):
                # typing a new shelf name on an item creates the shelf
                existing = db.execute("SELECT name FROM shelves WHERE name = ?",
                                      (clean["shelf"],)).fetchone()
                if existing:
                    clean["shelf"] = existing["name"]
                else:
                    db.execute("INSERT INTO shelves(name, created_at) VALUES (?, ?)",
                               (clean["shelf"], _now()))
            sets = ", ".join(f"{k} = ?" for k in clean)
            db.execute(f"UPDATE items SET {sets} WHERE id = ?", (*clean.values(), item_id))
            if "display_name" in clean and clean["display_name"] != row["display_name"]:
                self._log(db, item_id, "renamed", _now(),
                          clean["display_name"] or "(name cleared)")
        return self.get(item_id)

    def mark_opened(self, item_id: str):
        with self._lock, self._connect() as db:
            db.execute("UPDATE items SET last_opened = ? WHERE id = ?", (_now(), item_id))

    def mark_all_seen(self):
        with self._lock, self._connect() as db:
            db.execute("UPDATE items SET last_opened = ? WHERE removed_at IS NULL", (_now(),))


def _original_name(item: dict) -> str:
    if item["kind"] == "book":
        return item.get("title") or item.get("name") or ""
    return item.get("name") or ""


def _registry_fields(row, item: dict) -> dict:
    if row is None:
        return {"display_name": None, "shelf": None, "note": None,
                "status": "new", "version": 1, "first_seen": None,
                "last_changed": None, "last_opened": None,
                "label": _original_name(item)}
    if row["last_opened"] is None:
        status = "new"
    elif row["last_changed"] and row["last_changed"] > row["last_opened"]:
        status = "updated"
    else:
        status = "seen"
    return {
        "display_name": row["display_name"],
        "shelf": row["shelf"],
        "note": row["note"],
        "status": status,
        "version": row["version"],
        "first_seen": row["first_seen"],
        "last_changed": row["last_changed"],
        "last_opened": row["last_opened"],
        "label": row["display_name"] or _original_name(item),
    }
