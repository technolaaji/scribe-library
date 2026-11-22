#!/usr/bin/env python3
"""
inspect_sdr.py — One-shot diagnostic tool to dump PDF annotation sidecars.

When you write notes on a sideloaded PDF on a Kindle Scribe, the strokes are
stored in a sidecar `<basename>.sdr/` folder next to the PDF in `documents/`.
We don't know the exact file layout across firmware versions, so this tool
pulls a sample and prints a detailed report so we can write a proper decoder.

Usage:
    python3 inspect_sdr.py
    python3 inspect_sdr.py --pdf-name "MyDocument.pdf"   # target a specific one
    python3 inspect_sdr.py --output ./sdr_samples/        # save full files

The output is a Markdown report you can paste back to me. Nothing on your
device is modified — this is read-only.

Privacy note: file CONTENTS are dumped (in hex preview + utf-8 attempt).
If you have annotations on a sensitive PDF, target a less-sensitive one
with --pdf-name, or redact before sharing.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import sys
from pathlib import Path

# Reuse the project's MTP transport.
_THIS_DIR = Path(__file__).resolve().parent.parent  # engine/
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from mtp_sync import (
    MTPDevice, _find_kindle_usb, FMT_ASSOCIATION, HANDLE_ROOT,
    SKIP_DOC_FOLDER_SUFFIXES,
)

log = logging.getLogger(__name__)


# ── Hex-and-text dump utilities ──────────────────────────────────────────────

def hex_preview(data: bytes, max_bytes: int = 512) -> str:
    """Return a side-by-side hex/ASCII dump of the first max_bytes bytes."""
    truncated = data[:max_bytes]
    lines = []
    for offset in range(0, len(truncated), 16):
        chunk = truncated[offset:offset + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        hex_part = hex_part.ljust(48)
        ascii_part = "".join(
            chr(b) if 0x20 <= b < 0x7f else "." for b in chunk
        )
        lines.append(f"  {offset:08x}  {hex_part}  {ascii_part}")
    if len(data) > max_bytes:
        lines.append(f"  ... ({len(data) - max_bytes} more bytes truncated)")
    return "\n".join(lines)


def detect_format(data: bytes) -> str:
    """Best-guess identification of the file format from magic bytes."""
    if not data:
        return "empty"
    if data.startswith(b"SQLite format 3\x00"):
        return "SQLite database (likely KDF)"
    if data.startswith(b"PK\x03\x04") or data.startswith(b"PK\x05\x06"):
        return "ZIP archive"
    if data.startswith(b"\x00\x00\x00") and len(data) > 8 and data[4:8] == b"ftyp":
        return "MP4 / ISO base media"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG image"
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG image"
    if data.startswith(b"%PDF"):
        return "PDF"
    if data.startswith(b"<?xml") or data.startswith(b"<svg") or data.startswith(b"<html"):
        return "XML/HTML"
    if data.startswith(b"{") or data.startswith(b"["):
        # Could be JSON
        try:
            json.loads(data.decode("utf-8", errors="strict"))
            return "JSON"
        except Exception:
            pass
    if all(0x09 <= b <= 0x7e or b in (0x0a, 0x0d) for b in data[:200]):
        return "ASCII text"
    return "binary (unknown)"


# ── MTP walk: find PDFs with adjacent .sdr/ folders ──────────────────────────

def find_pdf_with_annotations(
    mtp: MTPDevice,
    target_filename: str | None = None,
    log_fn=print,
) -> list[dict]:
    """
    Walk the device's documents/ folder collecting (PDF, sdr_folder) pairs.

    Each result dict:
        {
            'pdf_filename':   'MyDocument.pdf',
            'pdf_handle':     int,
            'pdf_size':       int,
            'sdr_handle':     int,           # the .sdr/ folder handle
            'sdr_filename':   'MyDocument.sdr',
            'storage_id':     int,
            'parent_path':    'Internal Storage/documents',
        }

    If target_filename is given, only PDFs matching that name are included.
    """
    results: list[dict] = []
    storage_ids = mtp.get_storage_ids()

    def walk(storage_id, parent_id, depth=0, path="", in_documents=False):
        if depth > 6:
            return
        try:
            handles = mtp.get_object_handles(storage_id, parent_id)
        except Exception as e:
            log_fn(f"  Warning: {path}: {e}")
            return

        # Two-pass: first pass collects PDFs in this folder, second pass
        # collects subdirs and looks for matching .sdr siblings.
        pdfs: dict[str, tuple[int, int]] = {}  # basename -> (handle, size)
        subdirs: list[tuple[int, str]] = []    # (handle, name)

        for handle in handles:
            try:
                info = mtp.get_object_info(handle)
            except Exception:
                continue
            name = info.get("filename", "")
            if not name:
                continue
            is_folder = info.get("format") == FMT_ASSOCIATION
            size = info.get("size", 0)
            child_path = f"{path}/{name}" if path else name

            if is_folder:
                # Skip nothing here — we WANT to see .sdr folders
                subdirs.append((handle, name))
                # Decide whether to recurse into this folder
                name_lower = name.lower()
                just_entered_docs = (
                    not in_documents and name_lower == "documents"
                )
                # Don't recurse into .sdr — we want to find them, not descend
                if name_lower.endswith(".sdr"):
                    continue
                should_recurse = (
                    depth == 0
                    or just_entered_docs
                    or in_documents
                    or (depth == 1 and name_lower == "documents")
                )
                if should_recurse:
                    walk(
                        storage_id, handle, depth + 1, child_path,
                        in_documents=in_documents or just_entered_docs,
                    )
            else:
                if in_documents and name.lower().endswith(".pdf"):
                    if (target_filename is None
                            or name.lower() == target_filename.lower()):
                        pdfs[name] = (handle, size)

        # Now match each PDF with a sibling .sdr/ folder
        for pdf_name, (pdf_handle, pdf_size) in pdfs.items():
            base = pdf_name.rsplit(".", 1)[0]
            sdr_candidates = [
                (h, n) for h, n in subdirs
                if n.lower() == f"{base.lower()}.sdr"
            ]
            if not sdr_candidates:
                continue
            sdr_handle, sdr_name = sdr_candidates[0]
            results.append({
                "pdf_filename": pdf_name,
                "pdf_handle": pdf_handle,
                "pdf_size": pdf_size,
                "sdr_handle": sdr_handle,
                "sdr_filename": sdr_name,
                "storage_id": storage_id,
                "parent_path": path or "(root)",
            })
            log_fn(f"  📄 {pdf_name}  📁 {sdr_name}")

    for sid in storage_ids:
        log_fn(f"Storage 0x{sid:08x}:")
        walk(sid, HANDLE_ROOT)

    return results


def collect_sdr_files(
    mtp: MTPDevice,
    sdr_handle: int,
    storage_id: int,
    log_fn=print,
) -> list[dict]:
    """
    Recursively walk a .sdr/ folder and return every file as:
        {'filename': str, 'size': int, 'data': bytes, 'rel_path': str}

    Files larger than 5MB are skipped (sample data dumps shouldn't be huge).
    """
    files: list[dict] = []

    def walk(parent_id, rel_path=""):
        try:
            handles = mtp.get_object_handles(storage_id, parent_id)
        except Exception as e:
            log_fn(f"  Warning: {rel_path}: {e}")
            return
        for h in handles:
            try:
                info = mtp.get_object_info(h)
            except Exception:
                continue
            name = info.get("filename", "")
            if not name:
                continue
            is_folder = info.get("format") == FMT_ASSOCIATION
            size = info.get("size", 0)
            entry_path = f"{rel_path}/{name}" if rel_path else name

            if is_folder:
                walk(h, entry_path)
            else:
                if size > 5 * 1024 * 1024:
                    log_fn(f"    ⚠ Skipping {entry_path} ({size / 1024 / 1024:.1f}MB > 5MB)")
                    files.append({
                        "filename": name, "size": size, "data": b"",
                        "rel_path": entry_path, "skipped": True,
                    })
                    continue
                # Pull the file bytes via a temp file (get_object expects
                # a Path, not a file-like object — it does a .tmp + rename)
                import tempfile as _tf
                tmp = _tf.NamedTemporaryFile(prefix="mtp_pull_",
                                             suffix=".bin", delete=False)
                tmp.close()
                tmp_path = Path(tmp.name)
                try:
                    mtp.get_object(h, tmp_path)
                    data = tmp_path.read_bytes()
                except Exception as e:
                    log_fn(f"    ✗ Failed to read {entry_path}: {e}")
                    tmp_path.unlink(missing_ok=True)
                    continue
                tmp_path.unlink(missing_ok=True)
                files.append({
                    "filename": name, "size": size, "data": data,
                    "rel_path": entry_path, "skipped": False,
                })

    walk(sdr_handle)
    return files


# ── Report generation ───────────────────────────────────────────────────────

def render_report(target: dict, files: list[dict]) -> str:
    """Render a Markdown report describing the .sdr/ contents."""
    lines = [
        f"# .sdr/ Inspection Report",
        "",
        f"## Target",
        "",
        f"- **PDF:** `{target['pdf_filename']}` ({target['pdf_size'] / 1024:.1f} KB)",
        f"- **Sidecar:** `{target['sdr_filename']}/`",
        f"- **Path:** `{target['parent_path']}/`",
        f"- **Files in .sdr/:** {len(files)}",
        "",
    ]

    if not files:
        lines.append("⚠ The .sdr/ folder is empty — this PDF may not have any annotations yet.")
        return "\n".join(lines)

    # Summary table
    lines.append("## File listing\n")
    lines.append("| File | Size | Detected format |")
    lines.append("|------|------|-----------------|")
    for f in files:
        if f.get("skipped"):
            fmt = "(skipped, too large)"
        else:
            fmt = detect_format(f["data"])
        size_str = (
            f"{f['size'] / 1024 / 1024:.1f} MB"
            if f["size"] >= 1024 * 1024
            else f"{f['size'] / 1024:.1f} KB"
            if f["size"] >= 1024
            else f"{f['size']} B"
        )
        lines.append(f"| `{f['rel_path']}` | {size_str} | {fmt} |")
    lines.append("")

    # Detail per file
    lines.append("## File details\n")
    for f in files:
        if f.get("skipped"):
            continue
        lines.append(f"### `{f['rel_path']}`")
        lines.append("")
        lines.append(f"- **Size:** {f['size']} bytes")
        lines.append(f"- **Format guess:** {detect_format(f['data'])}")
        lines.append("")

        # If it looks like SQLite, peek at table list
        if f["data"].startswith(b"SQLite format 3\x00"):
            tables = _peek_sqlite_tables(f["data"])
            if tables:
                lines.append("**Tables:**")
                for t in tables:
                    lines.append(f"- `{t}`")
                lines.append("")

        # If it looks like a ZIP, list entries
        if f["data"][:4] == b"PK\x03\x04":
            try:
                import zipfile as zf_mod
                z = zf_mod.ZipFile(io.BytesIO(f["data"]))
                lines.append("**Zip entries:**")
                for n in z.namelist()[:30]:
                    info = z.getinfo(n)
                    lines.append(f"- `{n}` ({info.file_size} B)")
                if len(z.namelist()) > 30:
                    lines.append(f"- ... ({len(z.namelist()) - 30} more)")
                lines.append("")
            except Exception:
                pass

        # Hex preview
        lines.append("**First 512 bytes (hex + ASCII):**")
        lines.append("```")
        lines.append(hex_preview(f["data"], max_bytes=512))
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


def _peek_sqlite_tables(data: bytes) -> list[str]:
    """Open a SQLite file from in-memory bytes and list table names."""
    import sqlite3
    import tempfile

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    try:
        tmp.write(data)
        tmp.close()
        conn = sqlite3.connect(tmp.name)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in cursor.fetchall()]
        conn.close()
        return tables
    except Exception:
        return []
    finally:
        Path(tmp.name).unlink(missing_ok=True)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Inspect .sdr/ annotation sidecars on a Kindle Scribe"
    )
    parser.add_argument(
        "--pdf-name",
        help="Only inspect the .sdr/ for this specific PDF "
             "(e.g. 'MyDoc.pdf'). Otherwise picks the first one found.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("sdr_inspection"),
        help="Local directory to save the full sidecar files (default: ./sdr_inspection/)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("sdr_report.md"),
        help="Where to write the Markdown report (default: ./sdr_report.md)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    print("─" * 60)
    print("  Kindle Scribe .sdr/ Sidecar Inspector")
    print("─" * 60)
    print()

    # Connect to device
    print("Connecting to Kindle…")
    usb_dev = _find_kindle_usb()
    if usb_dev is None:
        print("✗ No Kindle detected on USB. Plug in and unlock the device.")
        sys.exit(2)

    try:
        product = usb_dev.product or "Kindle"
    except Exception:
        product = "Kindle"
    print(f"✓ Found: {product}")

    mtp = MTPDevice(usb_dev)
    try:
        mtp.open()
        mtp._open_session()

        print("\nSearching for PDFs with .sdr/ sidecars…")
        candidates = find_pdf_with_annotations(
            mtp, target_filename=args.pdf_name)

        if not candidates:
            print()
            if args.pdf_name:
                print(f"✗ No .sdr/ folder found for '{args.pdf_name}'")
                print(
                    "   Either you haven't annotated that PDF yet, or it's "
                    "under a different name on the device."
                )
            else:
                print("✗ No annotated PDFs found in documents/")
                print(
                    "   Make sure you've actually written on at least one PDF "
                    "on your Scribe before running this tool."
                )
            sys.exit(1)

        print(f"\n✓ Found {len(candidates)} annotated PDF(s)")
        target = candidates[0]
        if len(candidates) > 1 and not args.pdf_name:
            print(
                f"  Multiple PDFs have annotations. Inspecting the first: "
                f"'{target['pdf_filename']}'"
            )
            print(
                f"  (use --pdf-name to target a specific one. Other "
                f"options: {', '.join(c['pdf_filename'] for c in candidates[1:5])}"
                f"{', ...' if len(candidates) > 5 else ''})"
            )

        print(f"\nInspecting {target['sdr_filename']}/…")
        files = collect_sdr_files(mtp, target["sdr_handle"], target["storage_id"])

    finally:
        try:
            mtp.close()
        except Exception:
            pass

    # Save the full files locally for follow-up analysis
    print()
    args.output.mkdir(parents=True, exist_ok=True)
    saved_count = 0
    for f in files:
        if f.get("skipped"):
            continue
        target_path = args.output / target["sdr_filename"] / f["rel_path"]
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(f["data"])
        saved_count += 1
    print(f"✓ Saved {saved_count} file(s) to {args.output}/{target['sdr_filename']}/")

    # Generate the report
    report = render_report(target, files)
    args.report.write_text(report)
    print(f"✓ Report written to {args.report}")

    print()
    print("─" * 60)
    print("Next step: paste the contents of the report back to Claude,")
    print("along with the saved files if any look interesting.")
    print("─" * 60)


if __name__ == "__main__":
    main()
