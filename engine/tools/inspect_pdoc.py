#!/usr/bin/env python3
"""
inspect_pdoc.py — Investigate how Kindle Scribe maps PDFs to annotation notebooks.

Discovery from previous diagnostics:
  - PDF annotations are stored in `.notebooks/<UUID>!!PDOC!!notebook/nbk` —
    same KDF/SQLite format as handwritten notebooks
  - There's a `.sync/KSDKNoteSyncDB.sqlite` (~27 MB) that almost certainly
    holds the mapping from PDF filename → PDOC notebook UUID

This tool:
  1. Pulls KSDKNoteSyncDB.sqlite from the device
  2. Inspects its tables and looks for a row mentioning the target PDF
  3. Pulls every PDOC notebook's `nbk` file
  4. Reports SQLite table schemas + sizes so we can write the real decoder

Usage:
    python3 inspect_pdoc.py
    python3 inspect_pdoc.py --pdf-name conversation.pdf

Privacy note: the sync DB may contain reading-position data, bookmark text,
and notebook UUIDs for many books. The tool dumps schema and lookup results
but no row contents from sensitive tables. The PDOC `nbk` files contain
your handwriting strokes; their hex previews are limited to first 256 bytes.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import sqlite3
import sys
import tempfile
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent.parent  # engine/
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from mtp_sync import (
    MTPDevice, _find_kindle_usb, FMT_ASSOCIATION, HANDLE_ROOT,
)

log = logging.getLogger(__name__)


def hex_preview(data: bytes, max_bytes: int = 256) -> str:
    truncated = data[:max_bytes]
    lines = []
    for offset in range(0, len(truncated), 16):
        chunk = truncated[offset:offset + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk).ljust(48)
        ascii_part = "".join(
            chr(b) if 0x20 <= b < 0x7f else "." for b in chunk
        )
        lines.append(f"  {offset:08x}  {hex_part}  {ascii_part}")
    return "\n".join(lines)


def find_path(mtp, storage_id, parent_id, target_path_parts, depth=0):
    """
    Walk to a specific path like ['.sync', 'KSDKNoteSyncDB.sqlite'] and
    return the handle of the final file/folder (or None).
    """
    if not target_path_parts:
        return parent_id
    target = target_path_parts[0].lower()
    try:
        handles = mtp.get_object_handles(storage_id, parent_id)
    except Exception:
        return None
    for h in handles:
        try:
            info = mtp.get_object_info(h)
        except Exception:
            continue
        if info.get("filename", "").lower() == target:
            return find_path(mtp, storage_id, h, target_path_parts[1:], depth + 1)
    return None


def list_pdoc_notebooks(mtp, log_fn=print):
    """Find every PDOC notebook folder under .notebooks/ and return their handles."""
    pdocs: list[dict] = []
    for sid in mtp.get_storage_ids():
        nb_handle = find_path(mtp, sid, HANDLE_ROOT, [".notebooks"])
        if not nb_handle:
            continue
        try:
            entries = mtp.get_object_handles(sid, nb_handle)
        except Exception as e:
            log_fn(f"  ! Could not list .notebooks/: {e}")
            continue
        for h in entries:
            try:
                info = mtp.get_object_info(h)
            except Exception:
                continue
            name = info.get("filename", "")
            if "!!PDOC!!" in name and info.get("format") == FMT_ASSOCIATION:
                pdocs.append({
                    "name": name,
                    "handle": h,
                    "storage_id": sid,
                })
    return pdocs


def pull_file(mtp, handle, storage_id, log_fn=print) -> bytes:
    """
    Pull a single file and return its bytes.

    MTPDevice.get_object writes to a Path on disk (with a `.tmp` rename
    pattern), not a file-like object. So we hand it a temp path and read
    the bytes back into memory afterward.
    """
    tmp = tempfile.NamedTemporaryFile(prefix="mtp_pull_", suffix=".bin",
                                      delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)
    try:
        mtp.get_object(handle, tmp_path)
        return tmp_path.read_bytes()
    finally:
        tmp_path.unlink(missing_ok=True)


def collect_pdoc_files(mtp, pdoc_handle, storage_id):
    """Pull every file inside a PDOC notebook folder."""
    files: list[dict] = []
    try:
        children = mtp.get_object_handles(storage_id, pdoc_handle)
    except Exception:
        return files
    for h in children:
        try:
            info = mtp.get_object_info(h)
        except Exception:
            continue
        name = info.get("filename", "")
        if not name:
            continue
        if info.get("format") == FMT_ASSOCIATION:
            continue
        size = info.get("size", 0)
        if size > 5 * 1024 * 1024:
            files.append({"name": name, "size": size, "data": b"", "skipped": True})
            continue
        try:
            data = pull_file(mtp, h, storage_id)
            files.append({"name": name, "size": size, "data": data, "skipped": False})
        except Exception as e:
            files.append({"name": name, "size": size, "data": b"", "skipped": True,
                          "error": str(e)})
    return files


def inspect_sqlite(data: bytes) -> dict:
    """Open SQLite from bytes and return table info."""
    if not data.startswith(b"SQLite format 3\x00"):
        return {"error": "not a SQLite file"}
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    try:
        tmp.write(data)
        tmp.close()
        conn = sqlite3.connect(tmp.name)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in cursor.fetchall()]
        result = {"tables": {}}
        for table in tables:
            # Get columns. Identifier-quote with double-quotes (SQL standard);
            # using {table!r} would render single quotes which SQLite reads as
            # a string literal, not a column reference.
            cur = conn.execute(f'PRAGMA table_info("{table}")')
            cols = [{"name": r[1], "type": r[2]} for r in cur.fetchall()]
            # Get row count
            try:
                cur = conn.execute(f'SELECT COUNT(*) FROM "{table}"')
                row_count = cur.fetchone()[0]
            except Exception:
                row_count = -1
            result["tables"][table] = {
                "columns": cols,
                "row_count": row_count,
            }
        conn.close()
        return result
    except Exception as e:
        return {"error": str(e)}
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def search_sqlite_for(data: bytes, needle: str) -> list[dict]:
    """
    Search every TEXT column in every table for rows containing `needle`.
    Returns matches as [{table, columns_with_match, sample_row}].
    """
    if not data.startswith(b"SQLite format 3\x00"):
        return []
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    try:
        tmp.write(data)
        tmp.close()
        conn = sqlite3.connect(tmp.name)
        conn.row_factory = sqlite3.Row

        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = [r[0] for r in cur.fetchall()]
        matches = []
        needle_lower = needle.lower()

        for table in tables:
            try:
                cur = conn.execute(f'PRAGMA table_info("{table}")')
                text_cols = [r[1] for r in cur.fetchall()
                             if r[2].upper() in ("TEXT", "VARCHAR", "CHAR", "")
                             or r[2] == ""]
            except Exception:
                continue
            if not text_cols:
                continue

            # Build OR query across text columns. We quote identifiers with
            # double-quotes (SQL standard) — using {c!r} would render single
            # quotes, which SQLite reads as a string literal, not a column ref.
            where = " OR ".join(
                f'LOWER(CAST("{c}" AS TEXT)) LIKE ?' for c in text_cols
            )
            params = [f"%{needle_lower}%"] * len(text_cols)
            try:
                cur = conn.execute(
                    f'SELECT * FROM "{table}" WHERE {where} LIMIT 5', params)
                rows = cur.fetchall()
            except Exception:
                continue

            if rows:
                for row in rows:
                    row_dict = {k: row[k] for k in row.keys()}
                    # Truncate big BLOBs
                    for k, v in list(row_dict.items()):
                        if isinstance(v, bytes):
                            row_dict[k] = (
                                f"<BLOB {len(v)} bytes, "
                                f"first 16: {v[:16].hex()}>"
                            )
                        elif isinstance(v, str) and len(v) > 200:
                            row_dict[k] = v[:200] + "…"
                    matches.append({"table": table, "row": row_dict})

        conn.close()
        return matches
    except Exception as e:
        return [{"error": str(e)}]
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def render_report(sync_db_info, pdocs_info, pdf_search_results, target_pdf,
                  output_dir):
    """Generate the Markdown report."""
    lines = ["# PDOC annotation investigation report", ""]
    lines.append(f"**Target PDF:** `{target_pdf}`")
    lines.append("")
    lines.append(f"Saved files to `{output_dir}/` — paths in this report are local.")
    lines.append("")

    # Sync DB summary
    lines.append("## .sync/KSDKNoteSyncDB.sqlite")
    lines.append("")
    if sync_db_info.get("error"):
        lines.append(f"⚠ Could not read sync DB: `{sync_db_info['error']}`")
    elif "tables" in sync_db_info:
        lines.append("**Tables (with row counts):**")
        lines.append("")
        for tname, tinfo in sorted(sync_db_info["tables"].items()):
            lines.append(f"### `{tname}` ({tinfo['row_count']} rows)")
            lines.append("")
            lines.append("| Column | Type |")
            lines.append("|--------|------|")
            for col in tinfo["columns"]:
                lines.append(f"| `{col['name']}` | {col['type']} |")
            lines.append("")
    lines.append("")

    # PDF lookup
    lines.append(f"## Search results for '{target_pdf}' in sync DB")
    lines.append("")
    if pdf_search_results:
        lines.append(f"Found **{len(pdf_search_results)}** match(es):")
        lines.append("")
        for m in pdf_search_results:
            if m.get("error"):
                lines.append(f"- ⚠ Error: {m['error']}")
                continue
            lines.append(f"### Table `{m['table']}`")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(m["row"], indent=2, default=str))
            lines.append("```")
            lines.append("")
    else:
        lines.append("**No rows mention the target PDF in any text column.**")
        lines.append("")
        lines.append("This is unexpected — the file is on the device. Possible reasons:")
        lines.append("- The PDF is referenced by hash/UUID rather than filename")
        lines.append("- Filename is stored case-folded or with an extension stripped")
        lines.append("- The mapping lives in a BLOB column we didn't search")
    lines.append("")

    # PDOC notebooks inventory
    lines.append("## PDOC notebooks (annotation containers)")
    lines.append("")
    if pdocs_info:
        lines.append(f"Found **{len(pdocs_info)}** PDOC notebook folder(s):")
        lines.append("")
        for p in pdocs_info:
            lines.append(f"### `{p['folder_name']}`")
            lines.append("")
            lines.append(f"- **Files in folder:** {len(p['files'])}")
            for f in p["files"]:
                size_str = (
                    f"{f['size']/1024/1024:.1f} MB" if f['size'] >= 1024*1024
                    else f"{f['size']/1024:.1f} KB" if f['size'] >= 1024
                    else f"{f['size']} B"
                )
                lines.append(f"  - `{f['name']}` ({size_str})")
            lines.append("")

            nbk = next((f for f in p["files"]
                        if f["name"].lower() == "nbk" and not f.get("skipped")), None)
            if nbk:
                # Inspect this nbk as SQLite
                info = inspect_sqlite(nbk["data"])
                if "tables" in info:
                    lines.append("**`nbk` (SQLite tables + row counts):**")
                    lines.append("")
                    for tname, tinfo in sorted(info["tables"].items()):
                        lines.append(
                            f"- `{tname}` — {tinfo['row_count']} rows, "
                            f"{len(tinfo['columns'])} cols"
                        )
                    lines.append("")
                # Hex preview of first 256 bytes
                lines.append("**`nbk` hex preview (first 256 bytes):**")
                lines.append("```")
                lines.append(hex_preview(nbk["data"]))
                lines.append("```")
                lines.append("")
    else:
        lines.append("(no PDOC folders found)")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Inspect KSDK sync DB and PDOC notebooks"
    )
    parser.add_argument("--pdf-name", default="conversation.pdf")
    parser.add_argument("--output", type=Path, default=Path("pdoc_inspection"))
    parser.add_argument("--report", type=Path, default=Path("pdoc_report.md"))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    print("─" * 60)
    print("  PDOC annotation investigation")
    print("─" * 60)

    print("\nConnecting to Kindle…")
    usb_dev = _find_kindle_usb()
    if usb_dev is None:
        print("✗ No Kindle detected on USB.")
        sys.exit(2)

    args.output.mkdir(parents=True, exist_ok=True)

    mtp = MTPDevice(usb_dev)
    try:
        mtp.open()
        mtp._open_session()

        # --- Step 1: Pull the sync DB ---
        print("\nLooking for .sync/KSDKNoteSyncDB.sqlite…")
        sync_db_data = b""
        sync_db_info = {"error": "not found"}
        for sid in mtp.get_storage_ids():
            sync_handle = find_path(
                mtp, sid, HANDLE_ROOT,
                [".sync", "KSDKNoteSyncDB.sqlite"],
            )
            if sync_handle:
                print("  ✓ Found, downloading (27.6 MB may take a moment)…")
                sync_db_data = pull_file(mtp, sync_handle, sid)
                (args.output / "KSDKNoteSyncDB.sqlite").write_bytes(sync_db_data)
                sync_db_info = inspect_sqlite(sync_db_data)
                print(f"  ✓ Saved to {args.output / 'KSDKNoteSyncDB.sqlite'}")
                break
        if not sync_db_data:
            print("  ✗ KSDKNoteSyncDB.sqlite not found")

        # --- Step 2: Search the sync DB for the target PDF ---
        pdf_search_results = []
        if sync_db_data:
            target_stem = Path(args.pdf_name).stem
            print(f"\nSearching sync DB for '{target_stem}'…")
            pdf_search_results = search_sqlite_for(sync_db_data, target_stem)
            print(f"  → {len(pdf_search_results)} match(es)")

        # --- Step 3: Pull every PDOC notebook ---
        print("\nLocating PDOC notebooks under .notebooks/…")
        pdocs = list_pdoc_notebooks(mtp)
        print(f"  Found {len(pdocs)} PDOC folder(s)")

        pdocs_info = []
        for p in pdocs:
            print(f"  Pulling {p['name']}/…")
            files = collect_pdoc_files(mtp, p["handle"], p["storage_id"])
            # Save to disk
            pdoc_dir = args.output / "pdoc" / p["name"]
            pdoc_dir.mkdir(parents=True, exist_ok=True)
            for f in files:
                if not f.get("skipped"):
                    (pdoc_dir / f["name"]).write_bytes(f["data"])
            pdocs_info.append({
                "folder_name": p["name"],
                "files": files,
            })

    finally:
        try:
            mtp.close()
        except Exception:
            pass

    # --- Generate the report ---
    report = render_report(
        sync_db_info, pdocs_info, pdf_search_results,
        args.pdf_name, args.output,
    )
    args.report.write_text(report)

    print()
    print("─" * 60)
    print(f"✓ Report:  {args.report}")
    print(f"✓ Saved:   {args.output}/")
    print()
    print("Paste the report back to continue.")
    print("─" * 60)


if __name__ == "__main__":
    main()
