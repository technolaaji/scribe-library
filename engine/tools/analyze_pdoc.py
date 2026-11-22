#!/usr/bin/env python3
"""
analyze_pdoc.py — Deep-dive analysis of files already pulled by inspect_pdoc.

Works entirely on local files in ./pdoc_inspection/, no Kindle needed.
Run inspect_pdoc.py first to get the data, then this to analyze it.

What it does:
  1. Opens every PDOC notebook's `nbk` file as SQLite, dumps actual
     contents (not just schema) — focused on KDF kvtable rows
  2. Reads every `actions.log` file (these are small, plain-text-ish, and
     should record what was drawn)
  3. Cross-references the sync DB more aggressively:
     - Lists every notebook_id in `notebook_state` (110 rows!)
     - Scans EVERY column in EVERY table (BLOB columns too)
     - Decodes any base64 / JSON-shaped values it finds
  4. Identifies which PDOC notebook(s) actually have stroke data
     (vs which are empty stubs)

Usage:
    python3 analyze_pdoc.py
    python3 analyze_pdoc.py --pdoc-dir pdoc_inspection
    python3 analyze_pdoc.py --target-pdf conversation.pdf
"""
from __future__ import annotations

import argparse
import base64
import json
import sqlite3
import sys
from pathlib import Path


def safe_blob_repr(v) -> str:
    """Render a value (possibly bytes) for the report. Truncate aggressively."""
    if isinstance(v, bytes):
        if len(v) > 64:
            head = v[:32].hex()
            tail = v[-16:].hex()
            return f"<BLOB {len(v)}B {head}…{tail}>"
        return f"<BLOB {len(v)}B {v.hex()}>"
    if isinstance(v, str):
        if len(v) > 200:
            return repr(v[:200] + "…")
        # Try JSON pretty-print if it looks like JSON
        s = v.strip()
        if s.startswith(("{", "[")):
            try:
                parsed = json.loads(s)
                pretty = json.dumps(parsed, indent=2, default=str)
                if len(pretty) < 800:
                    return f"\n```json\n{pretty}\n```"
            except Exception:
                pass
        return repr(v)
    return repr(v)


# ── Sync DB analysis ────────────────────────────────────────────────────────

def analyze_sync_db(db_path: Path, target_pdf: str) -> list[str]:
    """Return a list of Markdown lines describing what's in the sync DB."""
    lines = ["# Sync DB deep dive\n"]

    if not db_path.exists():
        lines.append(f"⚠ {db_path} not found. Run inspect_pdoc.py first.\n")
        return lines

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 1. List every notebook_state entry (110 rows!)
    lines.append("## All `notebook_state` entries\n")
    lines.append(f"Each `notebook_id` here is a real notebook the device knows about.")
    lines.append(f"Bold = matches a PDOC pattern; check these against the actual folders.\n")
    lines.append("| notebook_id | state |")
    lines.append("|-------------|-------|")
    cur = conn.execute("SELECT notebook_id, state FROM notebook_state ORDER BY notebook_id")
    pdoc_ids = set()
    ebok_ids = set()
    other_ids = set()
    for row in cur.fetchall():
        nid = row["notebook_id"]
        state = row["state"]
        if "!!PDOC!!" in nid:
            pdoc_ids.add(nid)
            lines.append(f"| **`{nid}`** | {state} |")
        elif "!!EBOK!!" in nid:
            ebok_ids.add(nid)
            lines.append(f"| `{nid}` | {state} |")
        else:
            other_ids.add(nid)
            lines.append(f"| `{nid}` | {state} |")
    lines.append("")
    lines.append(f"Total: **{len(pdoc_ids)}** PDOC, **{len(ebok_ids)}** EBOK, **{len(other_ids)}** other\n")

    # 2. Scan every column in every table for "conversation"-like strings
    # (more aggressive than before — also try column NAMES not just contents)
    lines.append("## Aggressive search for PDF references\n")
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]

    target_stem = Path(target_pdf).stem
    needles = [target_stem, target_stem.lower(), "conversation", ".pdf",
               "documents/", "PDOC"]
    found_any = False
    for table in tables:
        cur = conn.execute(f'PRAGMA table_info("{table}")')
        cols = [(r[1], r[2]) for r in cur.fetchall()]
        # For each column, sample a few values
        for col_name, col_type in cols:
            try:
                cur = conn.execute(
                    f'SELECT "{col_name}" FROM "{table}" LIMIT 5')
                samples = [r[0] for r in cur.fetchall()]
            except Exception:
                continue
            for sample in samples:
                if sample is None:
                    continue
                # Convert to text for searching
                if isinstance(sample, bytes):
                    text = sample.decode("utf-8", errors="ignore")
                else:
                    text = str(sample)
                for needle in needles:
                    if needle in text:
                        lines.append(
                            f"- **{table}.{col_name}** matches `{needle}`: "
                            f"{safe_blob_repr(sample)}"
                        )
                        found_any = True
                        break  # don't double-report
                else:
                    continue
                break
    if not found_any:
        lines.append("(no aggressive matches either)\n")
    lines.append("")

    # 3. Dump key_value_store (only 2 rows, might be metadata)
    lines.append("## `key_value_store` contents\n")
    cur = conn.execute("SELECT key, value FROM key_value_store")
    for row in cur.fetchall():
        lines.append(f"- **`{row['key']}`** = `{row['value']}`")
    lines.append("")

    # 4. Dump notebook_client_address (24 rows — one per real local notebook?)
    lines.append("## `notebook_client_address` (mapping notebook_id → client path)\n")
    cur = conn.execute(
        "SELECT notebook_id, client_address FROM notebook_client_address")
    lines.append("| notebook_id | client_address |")
    lines.append("|-------------|----------------|")
    for row in cur.fetchall():
        lines.append(f"| `{row['notebook_id']}` | `{row['client_address']}` |")
    lines.append("")

    # 5. Anything in deltasync_tokens for PDOC ids?
    lines.append("## `deltasync_tokens` for PDOC notebooks\n")
    cur = conn.execute(
        "SELECT notebook_id, sync_token FROM deltasync_tokens "
        "WHERE notebook_id LIKE '%!!PDOC!!%'"
    )
    rows = cur.fetchall()
    if rows:
        lines.append("| notebook_id | sync_token |")
        lines.append("|-------------|-----------|")
        for row in rows:
            lines.append(f"| `{row['notebook_id']}` | `{row['sync_token']}` |")
    else:
        lines.append("(no PDOC entries — tokens may be book-only)")
    lines.append("")

    conn.close()
    return lines


# ── PDOC notebook content analysis ─────────────────────────────────────────

def analyze_pdoc_nbk(nbk_path: Path) -> dict:
    """
    Open a PDOC `nbk` file as SQLite and pull out actual content rows.

    Returns:
        {
            'tables': {tablename: row_count},
            'kvtable_keys': [list of keys] or None if no kvtable,
            'sample_values': {key: short_repr},
            'all_text': "everything readable concatenated",
            'has_strokes': bool,
            'error': str if any
        }
    """
    if not nbk_path.exists():
        return {"error": "missing"}
    if nbk_path.stat().st_size < 100:
        return {"error": "too small to be valid"}

    result = {"tables": {}, "kvtable_keys": [], "sample_values": {},
              "all_text": "", "has_strokes": False}

    try:
        conn = sqlite3.connect(nbk_path)
        conn.row_factory = sqlite3.Row

        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        for row in cur.fetchall():
            tname = row[0]
            try:
                cur2 = conn.execute(f'SELECT COUNT(*) FROM "{tname}"')
                count = cur2.fetchone()[0]
            except Exception:
                count = -1
            result["tables"][tname] = count

        # KDF format uses a `kvtable` with (key BLOB, value BLOB) layout
        # Try several possible names
        kv_candidates = ["kvtable", "kv_table", "key_value", "kvstore"]
        for kv_name in kv_candidates:
            if kv_name in result["tables"]:
                cur = conn.execute(f'SELECT * FROM "{kv_name}" LIMIT 100')
                rows = cur.fetchall()
                for row in rows:
                    keys = list(row.keys())
                    if len(keys) >= 2:
                        k = row[keys[0]]
                        v = row[keys[1]]
                        k_repr = (
                            k.hex() if isinstance(k, bytes) else str(k)
                        )[:100]
                        v_repr = safe_blob_repr(v)
                        result["kvtable_keys"].append(k_repr)
                        result["sample_values"][k_repr] = v_repr
                        # Concatenate readable strings for substring searching
                        if isinstance(v, bytes):
                            try:
                                result["all_text"] += v.decode("utf-8", errors="ignore") + "\n"
                            except Exception:
                                pass
                        elif isinstance(v, str):
                            result["all_text"] += v + "\n"

        # Heuristic: if total text is bigger than a few KB or contains
        # words like "stroke", "pen", "x", "y", "pressure", probably has strokes
        text_lower = result["all_text"].lower()
        if len(result["all_text"]) > 2000 or any(
            w in text_lower for w in ("stroke", "pen", "ink")
        ):
            result["has_strokes"] = True

        conn.close()
    except Exception as e:
        result["error"] = str(e)

    return result


def analyze_pdoc_dir(pdoc_dir: Path, target_pdf: str) -> list[str]:
    """Walk pdoc_inspection/pdoc/ and analyze each PDOC notebook."""
    lines = ["# PDOC notebook contents\n"]

    if not pdoc_dir.exists():
        lines.append(f"⚠ {pdoc_dir} not found.\n")
        return lines

    # Collect every PDOC subfolder
    pdocs = sorted([d for d in pdoc_dir.iterdir() if d.is_dir()])
    if not pdocs:
        lines.append("(no PDOC folders)")
        return lines

    lines.append(f"Found **{len(pdocs)}** PDOC folders.\n")

    # Sort by file size of nbk (biggest = most likely real content)
    sized = []
    for p in pdocs:
        nbk = p / "nbk"
        sz = nbk.stat().st_size if nbk.exists() else 0
        actions = p / "actions.log"
        actions_sz = actions.stat().st_size if actions.exists() else 0
        sized.append((p, sz, actions_sz))
    sized.sort(key=lambda x: -x[1])

    lines.append("## Summary table\n")
    lines.append("| PDOC folder | nbk size | actions.log size | tables | likely real? |")
    lines.append("|-------------|----------|------------------|--------|--------------|")
    for pdir, nbk_sz, actions_sz in sized:
        nbk = pdir / "nbk"
        info = analyze_pdoc_nbk(nbk) if nbk.exists() else {"error": "no nbk"}
        tables_str = ", ".join(f"{t}({c})" for t, c in info.get("tables", {}).items())
        likely = "✓" if info.get("has_strokes") else (
            "?" if nbk_sz > 38000 else "✗ (empty stub)"
        )
        size_str = f"{nbk_sz/1024:.1f}KB"
        actions_str = f"{actions_sz}B" if actions_sz else "0B"
        lines.append(
            f"| `{pdir.name}` | {size_str} | {actions_str} | "
            f"{tables_str or '?'} | {likely} |"
        )
    lines.append("")

    # Show actions.log contents for any non-empty ones
    lines.append("## Non-empty `actions.log` contents\n")
    found_actions = False
    for pdir, nbk_sz, actions_sz in sized:
        actions = pdir / "actions.log"
        if actions.exists() and actions_sz > 0:
            found_actions = True
            lines.append(f"### `{pdir.name}/actions.log` ({actions_sz} bytes)\n")
            try:
                content = actions.read_bytes()
                # Try as text
                try:
                    text = content.decode("utf-8")
                    lines.append("```")
                    lines.append(text)
                    lines.append("```")
                except UnicodeDecodeError:
                    lines.append(f"```\n(binary, {actions_sz} bytes)\n")
                    lines.append("first 256 bytes:")
                    for offset in range(0, min(256, len(content)), 16):
                        chunk = content[offset:offset + 16]
                        hex_part = " ".join(f"{b:02x}" for b in chunk).ljust(48)
                        ascii_part = "".join(
                            chr(b) if 0x20 <= b < 0x7f else "." for b in chunk
                        )
                        lines.append(f"  {offset:08x}  {hex_part}  {ascii_part}")
                    lines.append("```")
            except Exception as e:
                lines.append(f"(error reading: {e})")
            lines.append("")
    if not found_actions:
        lines.append("(none have non-empty actions.log)\n")

    # Detail the largest few PDOC nbk files
    lines.append("## Detailed contents of largest PDOC nbk files\n")
    for pdir, nbk_sz, _ in sized[:5]:
        nbk = pdir / "nbk"
        if not nbk.exists():
            continue
        info = analyze_pdoc_nbk(nbk)
        lines.append(f"### `{pdir.name}` ({nbk_sz/1024:.1f} KB)\n")
        if info.get("error"):
            lines.append(f"⚠ {info['error']}\n")
            continue
        lines.append(f"**Tables:** {info['tables']}\n")
        if info["kvtable_keys"]:
            lines.append(f"**kvtable rows ({len(info['kvtable_keys'])}):**\n")
            for k in info["kvtable_keys"][:30]:
                v_repr = info["sample_values"].get(k, "")
                lines.append(f"- `{k}` → {v_repr}")
            if len(info["kvtable_keys"]) > 30:
                lines.append(f"- ... ({len(info['kvtable_keys']) - 30} more)")
        else:
            lines.append("(no kvtable rows — empty stub or different schema)")
        lines.append("")

        # Search the all_text for the target PDF
        if target_pdf and Path(target_pdf).stem.lower() in info["all_text"].lower():
            lines.append(f"⭐ **This nbk's content mentions '{target_pdf}'!**\n")

    return lines


def main():
    parser = argparse.ArgumentParser(description="Analyze pulled PDOC data locally")
    parser.add_argument("--pdoc-dir", type=Path, default=Path("pdoc_inspection"))
    parser.add_argument("--target-pdf", default="conversation.pdf")
    parser.add_argument("--report", type=Path, default=Path("pdoc_analysis.md"))
    args = parser.parse_args()

    sync_db = args.pdoc_dir / "KSDKNoteSyncDB.sqlite"
    pdoc_subdir = args.pdoc_dir / "pdoc"

    print(f"Analyzing {args.pdoc_dir}…")
    print(f"  sync DB: {sync_db.exists()}")
    print(f"  pdoc dir: {pdoc_subdir.exists()}")

    output_lines = []
    output_lines += analyze_sync_db(sync_db, args.target_pdf)
    output_lines.append("")
    output_lines += analyze_pdoc_dir(pdoc_subdir, args.target_pdf)

    args.report.write_text("\n".join(output_lines))
    print(f"\n✓ Report written to {args.report}")
    print("\nPaste it back to continue.")


if __name__ == "__main__":
    main()
