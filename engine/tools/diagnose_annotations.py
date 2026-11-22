#!/usr/bin/env python3
"""
diagnose_annotations.py — Find where Kindle Scribe stores PDF annotation strokes.

Our `pdf_annotations.py` looks in `documents/<basename>.sdr/`, but if that
returns empty, annotations are either:
  (a) in the .sdr/ folder but our sync didn't pull them
  (b) in a different folder on the device entirely
  (c) only on Amazon's cloud, not locally on the device
  (d) not actually being saved (firmware bug or unconfirmed gesture)

This tool:
  1. Walks the entire device looking for ANY .sdr/ folders
  2. Reports their exact contents (filename + size, no contents)
  3. Searches other plausible locations for annotation files
     (.notebooks/, system/, userannotlog/, etc.)
  4. Cross-references against your annotated PDFs

Usage:
    python3 diagnose_annotations.py
    python3 diagnose_annotations.py --pdf-name "MyDoc.pdf"

The output is a Markdown report — paste it back to figure out where to
look next.

Privacy: only filenames + sizes + folder structure are reported. NO file
contents are dumped (unlike inspect_sdr.py).
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent.parent  # engine/
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from mtp_sync import (
    MTPDevice, _find_kindle_usb, FMT_ASSOCIATION, HANDLE_ROOT,
)

log = logging.getLogger(__name__)


def walk_filtered(mtp, storage_id, parent_id, depth=0, path="",
                  max_depth=4, log_fn=lambda x: None):
    """
    Yield (full_path, name, size, is_folder) for every entry under parent_id,
    up to max_depth levels deep.
    """
    if depth > max_depth:
        return
    try:
        handles = mtp.get_object_handles(storage_id, parent_id)
    except Exception as e:
        log_fn(f"  Warning: {path}: {e}")
        return
    for h in handles:
        try:
            info = mtp.get_object_info(h)
        except Exception:
            continue
        name = info.get("filename", "")
        if not name:
            continue
        size = info.get("size", 0)
        is_folder = info.get("format") == FMT_ASSOCIATION
        full_path = f"{path}/{name}" if path else name
        yield (full_path, name, size, is_folder, h)
        if is_folder:
            yield from walk_filtered(
                mtp, storage_id, h, depth + 1, full_path,
                max_depth, log_fn)


def find_annotation_evidence(mtp, target_pdf=None, log_fn=print):
    """
    Comprehensive search for annotation files anywhere on the device.

    Returns a dict:
        {
            'pdfs_found': [(path, size), ...],
            'sdr_folders': [(path, contents:[(name,size)])],
            'suspicious_paths': [(path, name, size, why)],
            'top_level_dirs': [(name, file_count, total_size)],
        }
    """
    result = {
        "pdfs_found": [],
        "sdr_folders": [],
        "suspicious_paths": [],
        "top_level_dirs": [],
    }

    # Suspicious filename patterns that might hold annotations
    SUSPICIOUS_BASENAMES = {
        "annotations", "annotation", "notes", "note",
        "userannotlog.db", "user_annotations.db",
        "highlights", "writing", "scribbles",
    }
    SUSPICIOUS_EXTS = {".pds", ".pdt", ".azw.res", ".azw3.res", ".phl"}

    storage_ids = mtp.get_storage_ids()

    for sid in storage_ids:
        log_fn(f"\nStorage 0x{sid:08x}:")

        # First — top-level directory inventory
        try:
            top_handles = mtp.get_object_handles(sid, HANDLE_ROOT)
        except Exception as e:
            log_fn(f"  Could not list root: {e}")
            continue

        for th in top_handles:
            try:
                info = mtp.get_object_info(th)
            except Exception:
                continue
            name = info.get("filename", "")
            if not name:
                continue
            is_folder = info.get("format") == FMT_ASSOCIATION
            if is_folder:
                # Descend a bit and count
                count = 0
                size = 0
                try:
                    for (_p, _n, sz, isf, _h) in walk_filtered(
                            mtp, sid, th, max_depth=2,
                            log_fn=lambda x: None):
                        if not isf:
                            count += 1
                            size += sz
                except Exception:
                    pass
                result["top_level_dirs"].append((name, count, size))
                log_fn(f"  📁 {name}/  ({count} files, {size / 1024 / 1024:.1f} MB)")
            else:
                size = info.get("size", 0)
                log_fn(f"  📄 {name}  ({size / 1024:.1f} KB)")

        # Now do a deeper walk looking for annotation evidence
        log_fn(f"\n  Searching for .sdr folders, PDFs, and suspicious files…")
        for (full_path, name, size, is_folder, handle) in walk_filtered(
                mtp, sid, HANDLE_ROOT, max_depth=4,
                log_fn=lambda x: None):

            name_lower = name.lower()

            # PDFs — note them
            if not is_folder and name_lower.endswith(".pdf"):
                if target_pdf is None or name_lower == target_pdf.lower():
                    result["pdfs_found"].append((full_path, size))

            # .sdr/ folders — capture their contents (just listings, no data)
            if is_folder and name_lower.endswith(".sdr"):
                contents = []
                try:
                    for (_, child_name, child_size, child_is_folder, _) in walk_filtered(
                            mtp, sid, handle, max_depth=3,
                            log_fn=lambda x: None):
                        contents.append({
                            "name": child_name,
                            "size": child_size,
                            "is_folder": child_is_folder,
                        })
                except Exception:
                    pass
                base = name[:-4] if name_lower.endswith(".sdr") else name
                result["sdr_folders"].append({
                    "path": full_path,
                    "basename": base,
                    "contents": contents,
                })

            # Suspicious filenames anywhere
            if not is_folder:
                ext = ""
                if "." in name_lower:
                    ext = "." + name_lower.rsplit(".", 1)[-1]
                stem = name_lower.split(".")[0]

                why = None
                if stem in SUSPICIOUS_BASENAMES:
                    why = f"name contains '{stem}'"
                elif ext in SUSPICIOUS_EXTS:
                    why = f"unusual extension {ext}"
                elif "annot" in name_lower:
                    why = "filename contains 'annot'"
                elif "writing" in name_lower or "stroke" in name_lower:
                    why = "filename contains writing-related word"

                if why:
                    result["suspicious_paths"].append({
                        "path": full_path,
                        "name": name,
                        "size": size,
                        "why": why,
                    })

    return result


def render_report(result: dict, target_pdf: str | None) -> str:
    lines = ["# Annotation diagnostic report", ""]

    if target_pdf:
        lines.append(f"Looking specifically for: `{target_pdf}`")
        lines.append("")

    # Top-level inventory
    lines.append("## Top-level directories")
    lines.append("")
    if result["top_level_dirs"]:
        lines.append("| Folder | File count | Total size |")
        lines.append("|--------|------------|------------|")
        for name, count, size in sorted(result["top_level_dirs"]):
            size_str = (
                f"{size / 1024 / 1024:.1f} MB" if size > 1024 * 1024
                else f"{size / 1024:.1f} KB" if size > 1024
                else f"{size} B"
            )
            lines.append(f"| `{name}/` | {count} | {size_str} |")
    else:
        lines.append("(none found)")
    lines.append("")

    # PDFs
    lines.append("## PDFs on device")
    lines.append("")
    if result["pdfs_found"]:
        lines.append("| Path | Size |")
        lines.append("|------|------|")
        for path, size in result["pdfs_found"][:50]:
            size_str = (
                f"{size / 1024 / 1024:.1f} MB" if size > 1024 * 1024
                else f"{size / 1024:.1f} KB"
            )
            lines.append(f"| `{path}` | {size_str} |")
        if len(result["pdfs_found"]) > 50:
            lines.append(f"| ... ({len(result['pdfs_found']) - 50} more) | |")
    else:
        lines.append("(no PDFs found)")
    lines.append("")

    # .sdr/ folders — the main attraction
    lines.append("## `.sdr/` sidecar folders")
    lines.append("")
    if result["sdr_folders"]:
        lines.append(f"Found **{len(result['sdr_folders'])}** `.sdr/` folder(s).")
        lines.append("")
        for sdr in result["sdr_folders"]:
            lines.append(f"### `{sdr['path']}`")
            lines.append("")
            if not sdr["contents"]:
                lines.append("⚠ **EMPTY** — nothing inside this `.sdr/` folder")
            else:
                lines.append(f"Contains **{len(sdr['contents'])}** entries:")
                lines.append("")
                lines.append("| Name | Size | Type |")
                lines.append("|------|------|------|")
                for entry in sdr["contents"]:
                    size_str = (
                        f"{entry['size'] / 1024 / 1024:.1f} MB" if entry['size'] > 1024 * 1024
                        else f"{entry['size'] / 1024:.1f} KB" if entry['size'] > 1024
                        else f"{entry['size']} B"
                    )
                    typ = "folder" if entry["is_folder"] else "file"
                    lines.append(f"| `{entry['name']}` | {size_str} | {typ} |")
            lines.append("")
    else:
        lines.append("⚠ **NO `.sdr/` folders found anywhere on the device.**")
        lines.append("")
        lines.append(
            "This is significant — it means either (a) your PDFs aren't actually "
            "saving annotations as separate files, or (b) the annotations are "
            "stored somewhere we haven't searched yet. See the suspicious files "
            "section below for clues."
        )
    lines.append("")

    # Suspicious files
    lines.append("## Suspicious files (potential annotation storage)")
    lines.append("")
    if result["suspicious_paths"]:
        lines.append("| Path | Size | Why suspicious |")
        lines.append("|------|------|----------------|")
        for entry in result["suspicious_paths"][:80]:
            size_str = (
                f"{entry['size'] / 1024 / 1024:.1f} MB" if entry['size'] > 1024 * 1024
                else f"{entry['size'] / 1024:.1f} KB"
            )
            lines.append(
                f"| `{entry['path']}` | {size_str} | {entry['why']} |"
            )
        if len(result["suspicious_paths"]) > 80:
            lines.append(f"| ... ({len(result['suspicious_paths']) - 80} more) | | |")
    else:
        lines.append("(none found)")
    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Diagnose where Kindle Scribe stores PDF annotations"
    )
    parser.add_argument(
        "--pdf-name",
        help="Only report on this specific PDF",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("annotation_diagnostic.md"),
        help="Where to write the Markdown report",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    print("─" * 60)
    print("  Kindle Scribe annotation diagnostic")
    print("─" * 60)

    print("\nConnecting to Kindle…")
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

        result = find_annotation_evidence(
            mtp, target_pdf=args.pdf_name, log_fn=print)

    finally:
        try:
            mtp.close()
        except Exception:
            pass

    report = render_report(result, args.pdf_name)
    args.report.write_text(report)

    print()
    print("─" * 60)
    print(f"✓ Report written to {args.report}")

    # Quick summary on stdout
    print()
    print(f"Found:")
    print(f"  - {len(result['pdfs_found'])} PDFs")
    print(f"  - {len(result['sdr_folders'])} .sdr/ folders")
    if result["sdr_folders"]:
        empties = sum(1 for s in result["sdr_folders"] if not s["contents"])
        non_empties = len(result["sdr_folders"]) - empties
        print(f"      ({empties} empty, {non_empties} with contents)")
    print(f"  - {len(result['suspicious_paths'])} suspicious files")
    print()
    print("Paste the contents of the report file back to continue.")
    print("─" * 60)


if __name__ == "__main__":
    main()
