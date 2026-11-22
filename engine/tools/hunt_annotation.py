#!/usr/bin/env python3
"""
hunt_annotation.py — Find where Kindle Scribe stored YOUR specific annotation.

Context: you drew a stroke on a sideloaded PDF (e.g. conversation.pdf). The
expected location `documents/conversation.sdr/` doesn't exist. So the
annotation either lives somewhere else, or it wasn't persisted.

This tool searches every folder on the device — much more thoroughly than
the previous diagnostic. For each candidate location, it reports filenames,
sizes, and modification times so we can correlate with WHEN you drew the stroke.

Usage:
    python3 hunt_annotation.py
    python3 hunt_annotation.py --pdf-name conversation.pdf
    python3 hunt_annotation.py --hours-back 24    # only show files
                                                   # changed in last 24h

The output is a Markdown report (annotation_hunt.md) — paste it back.
Privacy: only metadata (filenames, sizes, dates), no file contents.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent.parent  # engine/
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from mtp_sync import (
    MTPDevice, _find_kindle_usb, FMT_ASSOCIATION, HANDLE_ROOT,
)

log = logging.getLogger(__name__)


def walk_everything(mtp, storage_id, parent_id=HANDLE_ROOT, depth=0,
                    path="", max_depth=8, log_fn=lambda x: None):
    """Yield every file (not folder) anywhere under parent_id, no filtering."""
    if depth > max_depth:
        return
    try:
        handles = mtp.get_object_handles(storage_id, parent_id)
    except Exception as e:
        log_fn(f"  ! Could not list {path}: {e}")
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
        mtime = info.get("modification_date", "") or info.get("date_modified", "")
        full_path = f"{path}/{name}" if path else name

        if not is_folder:
            yield {
                "path": full_path,
                "name": name,
                "size": size,
                "mtime": mtime,
                "handle": h,
            }
        else:
            yield from walk_everything(
                mtp, storage_id, h, depth + 1, full_path,
                max_depth, log_fn)


def hunt(mtp, target_pdf=None, hours_back=None, log_fn=print):
    """
    Search the device for files relevant to a specific PDF's annotation.

    Returns dict with categorized matches:
      {
          'name_matches':  files whose name contains the PDF basename
          'recent_files':  files modified in the last N hours (if hours_back given)
          'notebook_files': files inside .notebooks/
          'sync_files':    files inside .sync/
          'all_top_dirs':  every top-level folder + size summary
      }
    """
    result = {
        "name_matches": [],
        "recent_files": [],
        "notebook_files": [],
        "sync_files": [],
        "all_top_dirs": [],
        "all_extensions": {},
    }

    pdf_basename = None
    if target_pdf:
        # Strip path + extension to get a search needle
        pdf_basename = Path(target_pdf).stem.lower()

    # Threshold for "recent" — convert hours into a UTC datetime
    recent_threshold = None
    if hours_back is not None:
        recent_threshold = datetime.utcnow().timestamp() - hours_back * 3600

    storage_ids = mtp.get_storage_ids()

    for sid in storage_ids:
        log_fn(f"\nStorage 0x{sid:08x}: walking everything (this may take a minute)…")

        # Top-level summary first
        try:
            top_handles = mtp.get_object_handles(sid, HANDLE_ROOT)
        except Exception as e:
            log_fn(f"  Could not list root: {e}")
            continue

        top_dirs = []
        for th in top_handles:
            try:
                info = mtp.get_object_info(th)
            except Exception:
                continue
            name = info.get("filename", "")
            if not name:
                continue
            is_folder = info.get("format") == FMT_ASSOCIATION
            top_dirs.append((name, th, is_folder))
            if not is_folder:
                # Also note loose files at root
                size = info.get("size", 0)
                result["all_top_dirs"].append({
                    "name": name, "type": "file", "files": 0, "size": size,
                })

        # Now walk each top-level folder, but treat .notebooks/ and .sync/
        # specially because those are the most likely candidates
        for top_name, top_handle, is_folder in top_dirs:
            if not is_folder:
                continue

            file_count = 0
            total_size = 0
            top_files = []

            for entry in walk_everything(
                    mtp, sid, top_handle, depth=1, path=top_name,
                    max_depth=8, log_fn=log_fn):
                file_count += 1
                total_size += entry["size"]
                top_files.append(entry)

                # Track by extension for top-level summary
                ext = Path(entry["name"]).suffix.lower() or "(none)"
                result["all_extensions"][ext] = result["all_extensions"].get(ext, 0) + 1

                # Did the filename mention our PDF?
                if pdf_basename and pdf_basename in entry["name"].lower():
                    result["name_matches"].append(entry)

                # Was it modified recently?
                if recent_threshold and entry["mtime"]:
                    try:
                        # MTP date format: "YYYYMMDDTHHMMSS"
                        mtime_str = str(entry["mtime"])
                        if "T" in mtime_str and len(mtime_str) >= 15:
                            dt = datetime.strptime(
                                mtime_str.split(".")[0][:15], "%Y%m%dT%H%M%S")
                            if dt.timestamp() >= recent_threshold:
                                result["recent_files"].append(entry)
                    except (ValueError, TypeError):
                        pass

            # Stash the file list under specific top-level folders
            if top_name.lower() == ".notebooks":
                result["notebook_files"] = top_files
            elif top_name.lower() == ".sync":
                result["sync_files"] = top_files

            result["all_top_dirs"].append({
                "name": top_name, "type": "folder",
                "files": file_count, "size": total_size,
            })

            log_fn(f"  {top_name}/: {file_count} files, "
                   f"{total_size / 1024 / 1024:.1f} MB")

    return result


def render_report(result, target_pdf, hours_back) -> str:
    lines = ["# Annotation hunt report", ""]
    if target_pdf:
        lines.append(f"**Target:** `{target_pdf}`")
    if hours_back is not None:
        lines.append(f"**Recent threshold:** files modified in the last {hours_back}h")
    lines.append("")

    # Top-level inventory
    lines.append("## Device top-level inventory")
    lines.append("")
    lines.append("| Folder | Type | File count | Total size |")
    lines.append("|--------|------|------------|------------|")
    for d in sorted(result["all_top_dirs"], key=lambda x: -x["size"]):
        size_str = (
            f"{d['size'] / 1024 / 1024:.1f} MB" if d['size'] > 1024 * 1024
            else f"{d['size'] / 1024:.1f} KB" if d['size'] > 1024
            else f"{d['size']} B"
        )
        lines.append(
            f"| `{d['name']}/` | {d['type']} | {d['files']} | {size_str} |"
        )
    lines.append("")

    # Name matches — most direct signal
    lines.append("## ★ Files mentioning the PDF basename")
    lines.append("")
    if result["name_matches"]:
        lines.append(f"Found **{len(result['name_matches'])}** file(s) with the basename in their path:")
        lines.append("")
        lines.append("| Path | Size | Modified |")
        lines.append("|------|------|----------|")
        for f in result["name_matches"][:100]:
            size_str = _fmt_size(f['size'])
            lines.append(f"| `{f['path']}` | {size_str} | {f.get('mtime','')} |")
    else:
        lines.append("**No files contain the PDF basename in their path.**")
        lines.append("")
        lines.append("This is the strongest evidence yet: there's no annotation data " +
                     "stored under any name related to this PDF anywhere on the device.")
    lines.append("")

    # Recent files
    if hours_back is not None:
        lines.append(f"## Recently modified (last {hours_back}h)")
        lines.append("")
        if result["recent_files"]:
            lines.append("| Path | Size | Modified |")
            lines.append("|------|------|----------|")
            sorted_recent = sorted(
                result["recent_files"],
                key=lambda f: str(f.get('mtime', '')),
                reverse=True,
            )
            for f in sorted_recent[:60]:
                lines.append(
                    f"| `{f['path']}` | {_fmt_size(f['size'])} | {f.get('mtime','')} |"
                )
            if len(sorted_recent) > 60:
                lines.append(f"| ... ({len(sorted_recent) - 60} more) | | |")
        else:
            lines.append("(no files modified in this window — try a wider one)")
        lines.append("")

    # .notebooks/ contents (the key folder)
    lines.append("## `.notebooks/` contents")
    lines.append("")
    nf = result["notebook_files"]
    if nf:
        # Group by extension to give an overview
        by_ext = {}
        for f in nf:
            ext = Path(f['name']).suffix.lower() or "(none)"
            by_ext.setdefault(ext, []).append(f)
        lines.append(f"Total: {len(nf)} files. By extension:")
        lines.append("")
        for ext, files in sorted(by_ext.items(), key=lambda x: -len(x[1])):
            total_size = sum(f['size'] for f in files)
            lines.append(f"- `{ext}`: {len(files)} files, "
                         f"{total_size / 1024 / 1024:.1f} MB")
        lines.append("")

        # Show recent + name-matched files specifically
        interesting = []
        for f in nf:
            tag = ""
            if target_pdf and Path(target_pdf).stem.lower() in f['name'].lower():
                tag = "name-match"
            elif hours_back is not None and result.get('recent_files') and \
                    f in result['recent_files']:
                tag = "recent"
            if tag:
                interesting.append((f, tag))

        if interesting:
            lines.append("### Interesting files in .notebooks/")
            lines.append("")
            lines.append("| Path | Size | Modified | Why |")
            lines.append("|------|------|----------|-----|")
            for f, tag in interesting[:50]:
                lines.append(
                    f"| `{f['path']}` | {_fmt_size(f['size'])} | "
                    f"{f.get('mtime','')} | {tag} |"
                )
        else:
            # Just show a sample of the recent ones to give shape
            lines.append("### Sample (first 30 files)")
            lines.append("")
            lines.append("| Path | Size | Modified |")
            lines.append("|------|------|----------|")
            for f in nf[:30]:
                lines.append(
                    f"| `{f['path']}` | {_fmt_size(f['size'])} | {f.get('mtime','')} |"
                )
    else:
        lines.append("(no files visible)")
    lines.append("")

    # .sync/ contents
    lines.append("## `.sync/` contents")
    lines.append("")
    if result["sync_files"]:
        lines.append("| Path | Size | Modified |")
        lines.append("|------|------|----------|")
        for f in result["sync_files"][:30]:
            lines.append(
                f"| `{f['path']}` | {_fmt_size(f['size'])} | {f.get('mtime','')} |"
            )
    else:
        lines.append("(empty or hidden from MTP)")
    lines.append("")

    # File extension distribution overall
    lines.append("## File extension distribution (entire device)")
    lines.append("")
    lines.append("| Extension | Count |")
    lines.append("|-----------|-------|")
    for ext, count in sorted(result["all_extensions"].items(),
                             key=lambda x: -x[1])[:30]:
        lines.append(f"| `{ext}` | {count} |")
    lines.append("")

    return "\n".join(lines)


def _fmt_size(n):
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def main():
    parser = argparse.ArgumentParser(
        description="Hunt for handwritten annotation data on a Kindle Scribe"
    )
    parser.add_argument(
        "--pdf-name", default="conversation.pdf",
        help="The PDF filename you wrote on (default: conversation.pdf)",
    )
    parser.add_argument(
        "--hours-back", type=float, default=None,
        help="Also list files modified in the last N hours (e.g. --hours-back 6)",
    )
    parser.add_argument(
        "--report", type=Path, default=Path("annotation_hunt.md"),
        help="Where to write the Markdown report",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    print("─" * 60)
    print("  Annotation hunt — searching the device")
    print("─" * 60)

    print(f"\nTarget PDF basename: '{Path(args.pdf_name).stem}'")
    if args.hours_back:
        print(f"Also flagging files modified in the last {args.hours_back}h")

    print("\nConnecting to Kindle…")
    usb_dev = _find_kindle_usb()
    if usb_dev is None:
        print("✗ No Kindle detected on USB. Plug in and unlock.")
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

        result = hunt(
            mtp,
            target_pdf=args.pdf_name,
            hours_back=args.hours_back,
            log_fn=print,
        )
    finally:
        try:
            mtp.close()
        except Exception:
            pass

    report = render_report(result, args.pdf_name, args.hours_back)
    args.report.write_text(report)

    print()
    print("─" * 60)
    print(f"✓ Report written to {args.report}")
    print(f"\nSummary:")
    print(f"  - Files mentioning '{Path(args.pdf_name).stem}': "
          f"{len(result['name_matches'])}")
    if args.hours_back:
        print(f"  - Files changed in last {args.hours_back}h: "
              f"{len(result['recent_files'])}")
    print(f"  - Files in .notebooks/: {len(result['notebook_files'])}")
    print(f"  - Files in .sync/: {len(result['sync_files'])}")
    print()
    print("Paste the report back to continue the investigation.")
    print("─" * 60)


if __name__ == "__main__":
    main()
