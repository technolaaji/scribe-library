"""
mtp_sync.py — Pure Python MTP driver for Kindle Scribe notebooks.

Talks directly to the Kindle over USB using pyusb + libusb.
No system tools, no FUSE, no Android File Transfer needed.

What it pulls from the Kindle:
    Internal Storage/.notebooks/
        <UUID>/
            nbk                ← the actual handwritten notebook (KDF/SQLite)
            nbk-journal        ← (skipped — sync journal, usually empty)
        thumbnails/
            <UUID>.png         ← first-page preview, used as a fallback when
                                  KDF decoding fails for a notebook

What it writes locally:
    <dest>/<UUID>.nbk          ← the raw notebook database (NOT renamed to .kfx
                                  anymore — these are KDF files, not KFX)
    <dest>/<UUID>.png          ← optional thumbnail

Requirements:
    pip3 install pyusb
    brew install libusb          # macOS
    sudo apt install libusb-1-0  # Linux (usually already present)

Usage:
    python3 mtp_sync.py                    # sync to ~/.scribe_notebooks/
    python3 mtp_sync.py --dest ~/my-notes  # sync to custom folder
    python3 mtp_sync.py --detect           # just check if Kindle is connected
"""

import logging
import re
import os
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# ── Kindle USB identifiers ────────────────────────────────────────────────────

KINDLE_VID = 0x1949  # Amazon vendor ID

# All known Kindle product IDs (Scribe uses 0x0380-0x0384 range)
KINDLE_PIDS = {
    0x0001, 0x0002, 0x0003, 0x0004, 0x0005, 0x0006,
    0x0007, 0x0008, 0x0009, 0x000A, 0x000B, 0x000C,
    0x0300, 0x0301, 0x0302, 0x0303, 0x0304,
    0x0380, 0x0381, 0x0382, 0x0383, 0x0384,
    0x0400, 0x0401, 0x0402,
}

# ── MTP/PTP constants ─────────────────────────────────────────────────────────

CT_COMMAND  = 1
CT_DATA     = 2
CT_RESPONSE = 3
CT_EVENT    = 4

OP_GET_DEVICE_INFO    = 0x1001
OP_OPEN_SESSION       = 0x1002
OP_CLOSE_SESSION      = 0x1003
OP_GET_STORAGE_IDS    = 0x1004
OP_GET_STORAGE_INFO   = 0x1005
OP_GET_OBJECT_HANDLES = 0x1007
OP_GET_OBJECT_INFO    = 0x1008
OP_GET_OBJECT         = 0x1009
OP_DELETE_OBJECT      = 0x100B
OP_SEND_OBJECT_INFO   = 0x100C
OP_SEND_OBJECT        = 0x100D

RSP_OK              = 0x2001
RSP_SESSION_ALREADY = 0x201E

FMT_ASSOCIATION = 0x3001  # folder
FMT_UNDEFINED   = 0x3000  # generic file (we use this for uploads)
HANDLE_ROOT     = 0xFFFFFFFF
HANDLE_ALL      = 0x00000000

TIMEOUT_CMD  = 5_000
TIMEOUT_DATA = 60_000

# Notebook file/folder name patterns. The Kindle stores handwritten notebooks
# at .notebooks/<UUID>/nbk — i.e. a file LITERALLY named "nbk" with no
# extension. Older firmware also uses the !!EBOK!!notebook / !!PDOC!!notebook
# folders for pen annotations on books and PDFs (we skip those by default
# since they're per-document annotations, not standalone notebooks).
NB_RE = re.compile(r"^nbk$", re.IGNORECASE)
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
TARGET_DIRS = {".notebooks", "documents", "internal storage", "kindle"}

# Extensions we recognise as ebooks/documents on the device. Anything in
# documents/ with one of these extensions is a "book" we'll sync.
BOOK_EXTENSIONS = {
    ".pdf",          # sideloaded / Send-to-Kindle PDFs
    ".epub",         # sideloaded EPUBs
    ".kfx",          # Amazon's KFX format
    ".azw", ".azw3", ".azw8",  # legacy Amazon formats
    ".mobi", ".prc",  # very old Amazon formats
    ".txt",          # plain text documents
    ".kfx-zip",      # zipped KFX (rare)
}

# Folders inside documents/ that we DON'T treat as books. Each book on the
# Kindle has a sidecar folder named <Title>.sdr containing per-book metadata,
# annotations, and indexing — not user-readable content.
SKIP_DOC_FOLDER_SUFFIXES = (".sdr",)

# Upper bound on a single file we'll push to the device. 100MB is generous
# for a PDF; it stops accidental drag-and-drop of huge files.
MAX_PUSH_SIZE = 100 * 1024 * 1024


# ── MTP wire protocol helpers ─────────────────────────────────────────────────

def _pack_cmd(op_code, transaction_id, *params):
    n = len(params)
    length = 12 + 4 * n
    header = struct.pack('<IHHI', length, CT_COMMAND, op_code, transaction_id)
    return header + struct.pack(f'<{n}I', *params)


def _unpack_response(data):
    if len(data) < 12:
        raise ValueError(f"Response too short: {len(data)} bytes")
    length, ctype, code, txid = struct.unpack_from('<IHHI', data, 0)
    params = []
    offset = 12
    while offset + 4 <= len(data):
        params.append(struct.unpack_from('<I', data, offset)[0])
        offset += 4
    return code, params


def _read_mtp_string(data, offset):
    if offset >= len(data):
        return '', offset
    num_chars = data[offset]
    offset += 1
    if num_chars == 0:
        return '', offset
    byte_len = num_chars * 2
    raw = data[offset:offset + byte_len]
    offset += byte_len
    try:
        s = raw.decode('utf-16-le').rstrip('\x00')
    except Exception:
        s = ''
    return s, offset


def _parse_uint32_array(data):
    if len(data) < 16:
        return []
    payload = data[12:]
    if len(payload) < 4:
        return []
    count = struct.unpack_from('<I', payload, 0)[0]
    result = []
    for i in range(min(count, 10000)):
        off = 4 + i * 4
        if off + 4 > len(payload):
            break
        result.append(struct.unpack_from('<I', payload, off)[0])
    return result


def _parse_object_info(data):
    if len(data) < 12:
        return {}
    payload = data[12:]
    if len(payload) < 52:
        return {}
    try:
        storage_id, fmt, protection, size = struct.unpack_from('<IHHI', payload, 0)
        parent_id = struct.unpack_from('<I', payload, 28)[0]
        filename, off = _read_mtp_string(payload, 52)
        _captured, off = _read_mtp_string(payload, off)
        modified_raw, _ = _read_mtp_string(payload, off)
        return {
            'storage_id': storage_id,
            'format': fmt,
            'size': size,
            'parent_id': parent_id,
            'filename': filename,
            'modified': _parse_mtp_date(modified_raw),
        }
    except Exception as e:
        log.debug(f"parse_object_info error: {e}")
        return {}


def _parse_mtp_date(raw):
    """
    MTP dates look like '20261122T205849.' (optionally with tenths and a 'Z').
    The Kindle reports device-local time. Returns a unix timestamp or None.
    """
    if not raw or len(raw) < 15:
        return None
    try:
        from datetime import datetime as _dt
        return _dt.strptime(raw[:15], "%Y%m%dT%H%M%S").timestamp()
    except ValueError:
        return None


def _write_mtp_string(s):
    """Encode a string in MTP wire format: <len-in-chars><utf16le data><nul>."""
    if not s:
        return b'\x00'
    s_with_nul = s + '\x00'
    encoded = s_with_nul.encode('utf-16-le')
    # num_chars includes the trailing nul terminator
    return bytes([len(s_with_nul)]) + encoded


def _build_object_info(storage_id, fmt, size, filename, parent_handle=0):
    """
    Build an MTP ObjectInfo dataset for SendObjectInfo. Most fields are
    zero/unset because the Kindle doesn't care about thumbnail metadata,
    image dimensions, etc. — it only really uses storage_id, format, size,
    parent_handle, and filename.
    """
    out = bytearray()
    # 0x00: storage_id (uint32)
    out += struct.pack('<I', storage_id)
    # 0x04: format (uint16)
    out += struct.pack('<H', fmt)
    # 0x06: protection_status (uint16) — 0 = no protection
    out += struct.pack('<H', 0)
    # 0x08: object_compressed_size (uint32)
    out += struct.pack('<I', size)
    # 0x0C: thumb_format (uint16)
    out += struct.pack('<H', 0)
    # 0x0E: thumb_compressed_size (uint32)
    out += struct.pack('<I', 0)
    # 0x12: thumb_pix_width (uint32)
    out += struct.pack('<I', 0)
    # 0x16: thumb_pix_height (uint32)
    out += struct.pack('<I', 0)
    # 0x1A: image_pix_width (uint32)
    out += struct.pack('<I', 0)
    # 0x1E: image_pix_height (uint32)
    out += struct.pack('<I', 0)
    # 0x22: image_bit_depth (uint32)
    out += struct.pack('<I', 0)
    # 0x26: parent_object_handle (uint32) — 0 since we pass parent in cmd params
    out += struct.pack('<I', parent_handle)
    # 0x2A: association_type (uint16) — 0 for normal files
    out += struct.pack('<H', 0)
    # 0x2C: association_description (uint32)
    out += struct.pack('<I', 0)
    # 0x30: sequence_number (uint32)
    out += struct.pack('<I', 0)
    # 0x34: filename (MTP string)
    out += _write_mtp_string(filename)
    # date_created (empty string)
    out += _write_mtp_string('')
    # date_modified (empty string)
    out += _write_mtp_string('')
    # keywords (empty string)
    out += _write_mtp_string('')
    return bytes(out)


# ── MTP device class ──────────────────────────────────────────────────────────

class MTPDevice:

    def __init__(self, usb_dev):
        self._dev = usb_dev
        self._ep_out = None
        self._ep_in  = None
        self._txid   = 1
        self._session_open = False
        self._intf_num = 0

    def open(self):
        import usb.util as util

        dev = self._dev

        # Detach any kernel driver (mainly needed on Linux)
        for intf_num in range(3):
            try:
                if dev.is_kernel_driver_active(intf_num):
                    dev.detach_kernel_driver(intf_num)
            except Exception:
                pass

        dev.set_configuration()
        cfg = dev.get_active_configuration()

        # cfg iterates Interface objects directly; each Interface iterates Endpoints.
        intf = None
        for candidate in cfg:
            if candidate.bInterfaceClass in (0x06, 0xFF):
                intf = candidate
                break

        if intf is None:
            intf = cfg[(0, 0)]

        self._intf_num = intf.bInterfaceNumber
        util.claim_interface(dev, self._intf_num)

        for ep in intf:
            ep_type = util.endpoint_type(ep.bmAttributes)
            ep_dir  = util.endpoint_direction(ep.bEndpointAddress)
            if ep_type == util.ENDPOINT_TYPE_BULK:
                if ep_dir == util.ENDPOINT_OUT:
                    self._ep_out = ep.bEndpointAddress
                elif ep_dir == util.ENDPOINT_IN:
                    self._ep_in = ep.bEndpointAddress

        if self._ep_out is None or self._ep_in is None:
            raise IOError("Could not find MTP bulk endpoints")

        log.debug(f"Endpoints: out=0x{self._ep_out:02x} in=0x{self._ep_in:02x}")

        # Clear any halt/stall condition on both endpoints
        try:
            dev.clear_halt(self._ep_out)
        except Exception:
            pass
        try:
            dev.clear_halt(self._ep_in)
        except Exception:
            pass

        # Drain any stale data sitting in the bulk-in buffer
        self._drain()

    def _drain(self, timeout=200):
        # Discard any stale bytes sitting in the bulk-in buffer
        try:
            while True:
                data = self._dev.read(self._ep_in, 512, timeout=timeout)
                if not data:
                    break
        except Exception:
            pass  # USBTimeoutError expected when buffer is empty

    def close(self):
        try:
            if self._session_open:
                self._close_session()
        except Exception:
            pass
        try:
            import usb.util
            usb.util.release_interface(self._dev, self._intf_num)
        except Exception:
            pass

    def _next_txid(self):
        tid = self._txid
        self._txid += 1
        return tid

    def _write(self, data, timeout=TIMEOUT_CMD):
        self._dev.write(self._ep_out, data, timeout=timeout)

    def _read(self, size=65536, timeout=TIMEOUT_CMD):
        try:
            return bytes(self._dev.read(self._ep_in, size, timeout=timeout))
        except Exception as e:
            raise IOError(f"USB read failed: {e}")

    def _read_all(self, timeout=TIMEOUT_DATA):
        """Read a complete MTP container, handling multi-packet responses."""
        chunk = self._read(65536, timeout=timeout)
        if len(chunk) < 4:
            raise IOError("Short USB read")

        total = struct.unpack_from('<I', chunk, 0)[0]
        buf = bytearray(chunk)

        while len(buf) < total:
            more = self._read(min(total - len(buf), 65536), timeout=timeout)
            if not more:
                break
            buf.extend(more)

        return bytes(buf)

    def _read_response(self, timeout=TIMEOUT_CMD):
        # Read one container and return it. If it is a DATA container,
        # discard it and read again to get the RESPONSE container.
        for _ in range(3):
            pkt = self._read_all(timeout=timeout)
            if len(pkt) < 6:
                continue
            ctype = struct.unpack_from('<H', pkt, 4)[0]
            if ctype == CT_RESPONSE:
                return pkt
            # DATA container - discard and read next
            log.debug(f"Discarding container type {ctype}, len={len(pkt)}")
        raise IOError("No RESPONSE container received")

    def _open_session(self, session_id=1):
        # GetDeviceInfo (txid=0 is valid before any session).
        # Confirms the device is responsive and flushes leftover packets.
        try:
            self._write(_pack_cmd(OP_GET_DEVICE_INFO, 0))
            pkt = self._read_all(timeout=TIMEOUT_CMD)
            ctype = struct.unpack_from('<H', pkt, 4)[0] if len(pkt) >= 6 else 0
            if ctype == CT_DATA:
                self._read_all(timeout=TIMEOUT_CMD)  # consume RESPONSE
            log.debug("GetDeviceInfo OK")
        except Exception as e:
            log.debug(f"GetDeviceInfo: {e}")
            self._drain()

        # OpenSession must use txid=0 (PTP spec — pre-session ops use txid=0).
        # After the session opens we reset _txid to 1 for subsequent ops.
        # 0x201E = already open — treat as success and reuse the session.
        self._write(_pack_cmd(OP_OPEN_SESSION, 0, session_id))
        resp = self._read_response(timeout=TIMEOUT_CMD)
        code, _ = _unpack_response(resp)
        if code not in (RSP_OK, RSP_SESSION_ALREADY):
            raise IOError(f"OpenSession failed: 0x{code:04x}")
        self._session_open = True
        self._txid = 1  # reset transaction counter for in-session ops
        log.debug(f"Session open (code=0x{code:04x})")

    def _close_session(self):
        tid = self._next_txid()
        self._write(_pack_cmd(OP_CLOSE_SESSION, tid))
        try:
            self._read_all(timeout=TIMEOUT_CMD)
        except Exception:
            pass
        self._session_open = False

    def _send_and_recv(self, op_code, *params):
        # Send command, then read DATA container (if any) + RESPONSE container.
        # Returns (data_bytes_or_None, response_code).
        tid = self._next_txid()
        self._write(_pack_cmd(op_code, tid, *params))

        pkt1 = self._read_all()
        if len(pkt1) < 6:
            raise IOError(f"Short response for op 0x{op_code:04x}")

        ctype1 = struct.unpack_from('<H', pkt1, 4)[0]

        if ctype1 == CT_DATA:
            # There's a response packet following the data
            data = pkt1
            resp_pkt = self._read_response()
        elif ctype1 == CT_RESPONSE:
            data = None
            resp_pkt = pkt1
        else:
            data = None
            resp_pkt = self._read_response()

        code, _ = _unpack_response(resp_pkt)
        return data, code

    def get_storage_ids(self):
        data, code = self._send_and_recv(OP_GET_STORAGE_IDS)
        if code != RSP_OK:
            raise IOError(f"GetStorageIDs failed: 0x{code:04x}")
        return _parse_uint32_array(data) if data else []

    def get_object_handles(self, storage_id, parent_id=HANDLE_ROOT):
        data, code = self._send_and_recv(
            OP_GET_OBJECT_HANDLES, storage_id, HANDLE_ALL, parent_id)
        if code != RSP_OK:
            raise IOError(f"GetObjectHandles failed: 0x{code:04x}")
        return _parse_uint32_array(data) if data else []

    def get_object_info(self, handle):
        data, code = self._send_and_recv(OP_GET_OBJECT_INFO, handle)
        if code != RSP_OK:
            return {}
        return _parse_object_info(data) if data else {}

    def get_object(self, handle, dest, progress_cb=None):
        tid = self._next_txid()
        self._write(_pack_cmd(OP_GET_OBJECT, tid, handle))

        header_chunk = self._read(65536, timeout=TIMEOUT_DATA)
        if len(header_chunk) < 12:
            raise IOError("Short data container")

        total_length = struct.unpack_from('<I', header_chunk, 0)[0]
        payload_length = total_length - 12

        written = 0
        tmp = dest.with_suffix(dest.suffix + '.tmp')

        with open(tmp, 'wb') as f:
            first_payload = header_chunk[12:]
            f.write(first_payload)
            written += len(first_payload)

            if progress_cb:
                progress_cb(written, payload_length)

            while written < payload_length:
                chunk = self._read(
                    min(payload_length - written, 65536),
                    timeout=TIMEOUT_DATA
                )
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
                if progress_cb:
                    progress_cb(written, payload_length)

        # Read response (use _read_response to skip any unexpected data packets)
        resp_data = self._read_response(timeout=TIMEOUT_CMD)
        code, _ = _unpack_response(resp_data)
        if code != RSP_OK:
            tmp.unlink(missing_ok=True)
            raise IOError(f"GetObject response: 0x{code:04x}")

        tmp.replace(dest)
        return written

    # ── Upload direction (push to device) ─────────────────────────────────

    def find_directory(self, storage_id, path_components):
        """
        Resolve a list of directory names (e.g. ["documents"]) starting from the
        storage root, returning the MTP handle of the deepest folder. Returns
        None if any component is missing.
        """
        parent = HANDLE_ROOT
        for name in path_components:
            target = name.lower()
            handles = self.get_object_handles(storage_id, parent)
            found = None
            for h in handles:
                info = self.get_object_info(h)
                if (info.get('format') == FMT_ASSOCIATION
                        and info.get('filename', '').lower() == target):
                    found = h
                    break
            if found is None:
                return None
            parent = found
        return parent

    def send_object(self, src_path, storage_id, parent_handle,
                    target_filename=None, fmt=FMT_UNDEFINED, progress_cb=None):
        """
        Push a local file to the device. Uses the MTP SendObjectInfo +
        SendObject sequence:
          1. SendObjectInfo: tells the device "I'm about to send a file with
             this name/size/format to this folder" — device replies with a
             new handle.
          2. SendObject: stream the bytes.

        Args:
            src_path: local file to upload
            storage_id: MTP storage ID (from get_storage_ids())
            parent_handle: handle of the destination folder (use 0xFFFFFFFF
                           for storage root)
            target_filename: name to give the file on-device. Defaults to
                             src_path.name. The Kindle's filesystem is
                             case-sensitive on FAT32-equivalent layers, so
                             we preserve the case the caller chose.
            fmt: MTP format code. FMT_UNDEFINED is fine for ebooks/PDFs.
            progress_cb: optional callable(bytes_written, total_bytes).

        Returns the handle of the newly created object on the device.
        """
        src_path = Path(src_path)
        if not src_path.exists():
            raise IOError(f"Source file does not exist: {src_path}")

        size = src_path.stat().st_size
        name = target_filename or src_path.name

        if size > MAX_PUSH_SIZE:
            raise IOError(
                f"File is {size / 1024 / 1024:.1f}MB; max push size is "
                f"{MAX_PUSH_SIZE / 1024 / 1024:.0f}MB. Split or compress."
            )

        # 1. Build the ObjectInfo dataset and send it via SendObjectInfo.
        oi = _build_object_info(
            storage_id=storage_id,
            fmt=fmt,
            size=size,
            filename=name,
        )

        tid = self._next_txid()
        # SendObjectInfo params: storage_id, parent_handle
        cmd = _pack_cmd(OP_SEND_OBJECT_INFO, tid, storage_id, parent_handle)
        self._write(cmd)

        # Send the ObjectInfo as a DATA container
        data_container = struct.pack(
            '<IHHI', 12 + len(oi), CT_DATA, OP_SEND_OBJECT_INFO, tid
        ) + oi
        self._write(data_container, timeout=TIMEOUT_DATA)

        # Read the RESPONSE — gives us back the new handle in params[2]
        resp = self._read_response(timeout=TIMEOUT_CMD)
        code, params = _unpack_response(resp)
        if code != RSP_OK:
            raise IOError(f"SendObjectInfo failed: 0x{code:04x}")

        if len(params) < 3:
            raise IOError("SendObjectInfo response missing object handle")
        new_handle = params[2]
        log.debug("New object handle on device: 0x%08x", new_handle)

        # 2. SendObject — stream the file bytes
        tid2 = self._next_txid()
        cmd2 = _pack_cmd(OP_SEND_OBJECT, tid2)
        self._write(cmd2)

        # First, the DATA container header announcing total length
        data_header = struct.pack(
            '<IHHI', 12 + size, CT_DATA, OP_SEND_OBJECT, tid2
        )
        self._write(data_header, timeout=TIMEOUT_DATA)

        # Stream the file in chunks
        written = 0
        with open(src_path, 'rb') as f:
            while written < size:
                chunk = f.read(min(65536, size - written))
                if not chunk:
                    break
                self._write(chunk, timeout=TIMEOUT_DATA)
                written += len(chunk)
                if progress_cb:
                    progress_cb(written, size)

        # Read the RESPONSE
        resp2 = self._read_response(timeout=TIMEOUT_DATA)
        code2, _ = _unpack_response(resp2)
        if code2 != RSP_OK:
            raise IOError(f"SendObject failed: 0x{code2:04x}")

        return new_handle

    # ── Discovery (download direction) ────────────────────────────────────



    def find_notebooks(self, log_fn=print):
        """
        Walk device storage looking for Scribe notebooks and their thumbnails.

        Returns two lists:
            notebooks  — list of (handle, "nbk", size, uuid_folder_name)
            thumbnails — list of (handle, filename, size, uuid_stem)

        The UUID folder name is the parent directory of the nbk file. The
        thumbnail stem is the filename without ".png".
        """
        notebooks: list[tuple[int, str, int, str | None, float | None]] = []
        thumbnails: list[tuple[int, str, int, str, float | None]] = []

        storage_ids = self.get_storage_ids()
        log_fn(f"  Storages: {len(storage_ids)}")

        # Track whether we're currently inside a thumbnails/ folder so we can
        # collect the per-notebook PNGs.
        def walk(storage_id, parent_id, depth=0, path='', in_thumbnails=False):
            if depth > 6:
                return
            try:
                handles = self.get_object_handles(storage_id, parent_id)
            except Exception as e:
                log_fn(f"  Warning: {path}: {e}")
                return

            log_fn(f"  {'  ' * depth}[{path or 'root'}] {len(handles)} items")

            for handle in handles:
                try:
                    info = self.get_object_info(handle)
                except Exception:
                    continue

                name = info.get('filename', '')
                if not name:
                    continue

                is_folder = info.get('format') == FMT_ASSOCIATION
                fmt = info.get('format', 0)
                size = info.get('size', 0)
                child_path = f"{path}/{name}" if path else name
                name_lower = name.lower().strip()

                if is_folder:
                    # Decide whether to recurse into this folder.
                    # We always descend at the top level. Below that we descend
                    # into:
                    #   - known notebook containers (.notebooks, documents, …)
                    #   - UUID-named subfolders (one per notebook)
                    #   - the thumbnails/ folder under .notebooks
                    # We skip the per-document annotation folders because they
                    # contain pen annotations *for books*, not standalone
                    # notebooks (different rendering pipeline, out of scope).
                    is_thumb_folder = name_lower == 'thumbnails'
                    is_uuid = bool(UUID_RE.match(name))
                    is_known_root = name_lower in TARGET_DIRS
                    # "<ASIN>!!EBOK!!notebook" / "<id>!!PDOC!!notebook" hold the
                    # handwriting written *inside a book* (margin notes,
                    # handwritten sticky notes). We pull them so the book
                    # viewer can show them at their position.
                    is_in_book = name_lower.endswith('!!notebook')

                    should_recurse = (
                        depth == 0
                        or is_known_root
                        or is_uuid
                        or is_thumb_folder
                        or is_in_book
                        or (depth == 1
                            and name_lower not in {'clipboard', 'page_cache',
                                                   '.backups', 'screenshots',
                                                   'audible', 'system'})
                    )

                    tag = '(recurse)' if should_recurse else '(skip)'
                    log_fn(f"  {'  ' * depth}📁 {name}/ {tag}")
                    if should_recurse:
                        walk(
                            storage_id, handle, depth + 1, child_path,
                            in_thumbnails=is_thumb_folder or in_thumbnails,
                        )

                else:
                    log_fn(f"  {'  ' * depth}📄 {name} fmt=0x{fmt:04x} size={size}")

                    # Thumbnail PNG inside .notebooks/thumbnails/
                    if in_thumbnails and name_lower.endswith('.png'):
                        stem = name[:-len('.png')]
                        if UUID_RE.match(stem):
                            log_fn(f"  {'  ' * depth}  ✓ thumbnail for {stem}")
                            thumbnails.append((handle, name, size, stem,
                                               info.get('modified')))
                        continue

                    # Notebook database file: literally named "nbk".
                    if NB_RE.search(name):
                        path_parts = child_path.split('/')
                        # The UUID folder is the nearest ancestor that matches
                        # the UUID pattern.
                        folder_name = next(
                            (p for p in reversed(path_parts[:-1])
                             if UUID_RE.match(p)
                             or p.lower().endswith('!!notebook')),
                            None,
                        )
                        display = folder_name or name
                        log_fn(f"  {'  ' * depth}  ✓ notebook (will save as {display}.nbk)")
                        notebooks.append((handle, name, size, folder_name,
                                          info.get('modified')))

        for sid in storage_ids:
            log_fn(f"  Storage 0x{sid:08x}:")
            walk(sid, HANDLE_ROOT)

        return notebooks, thumbnails

    def find_books(self, log_fn=print):
        """
        Walk the device's documents/ folder collecting every ebook/document.

        Returns a list of dicts with metadata about each book:
            {
                'handle': int,         # MTP handle for download
                'storage_id': int,     # which storage it lives on
                'filename': str,       # original on-device filename
                'size': int,           # bytes
                'subdir': str | None,  # subfolder under documents/, if any
            }

        We accept any file with a known book extension (.pdf, .epub, .kfx,
        .azw, .azw3, etc.) and skip the per-book .sdr metadata folders.
        """
        books: list[dict] = []
        storage_ids = self.get_storage_ids()
        log_fn(f"  Storages: {len(storage_ids)}")

        def walk(storage_id, parent_id, depth=0, path='', in_documents=False):
            if depth > 6:
                return
            try:
                handles = self.get_object_handles(storage_id, parent_id)
            except Exception as e:
                log_fn(f"  Warning: {path}: {e}")
                return

            for handle in handles:
                try:
                    info = self.get_object_info(handle)
                except Exception:
                    continue

                name = info.get('filename', '')
                if not name:
                    continue

                is_folder = info.get('format') == FMT_ASSOCIATION
                size = info.get('size', 0)
                child_path = f"{path}/{name}" if path else name
                name_lower = name.lower()

                if is_folder:
                    # Recursion strategy:
                    #   depth 0 → always (top-level storage)
                    #   depth 1 → only into "documents" or other top-level dirs
                    #   inside documents → recurse into ALL subfolders, including
                    #                      *.sdr/ (which holds PDF annotation
                    #                      sidecars — strokes the user wrote on
                    #                      the document on the device).
                    just_entered_docs = (
                        not in_documents and name_lower in TARGET_DIRS
                        and name_lower == 'documents'
                    )
                    nested_in_docs = in_documents

                    is_sdr = name_lower.endswith('.sdr')

                    should_recurse = (
                        depth == 0
                        or just_entered_docs
                        or nested_in_docs  # inside documents/, recurse into everything
                        or (depth == 1 and name_lower in TARGET_DIRS)
                    )

                    if should_recurse:
                        log_fn(f"  {'  ' * depth}📁 {name}/ (recurse)")
                        walk(
                            storage_id, handle, depth + 1, child_path,
                            in_documents=in_documents or just_entered_docs,
                        )
                    else:
                        log_fn(f"  {'  ' * depth}📁 {name}/ (skip)")

                else:
                    # Files only count if we're inside documents/.
                    if not in_documents:
                        continue

                    ext = ''
                    dot = name_lower.rfind('.')
                    if dot >= 0:
                        ext = name_lower[dot:]

                    # Detect "is this file inside a .sdr/ sidecar folder?"
                    parts = child_path.split('/')
                    in_sdr = any(p.lower().endswith('.sdr') for p in parts)

                    # Outside .sdr/, only accept known book formats.
                    # Inside .sdr/, accept everything — we don't know what
                    # files the firmware writes there and rejecting unknowns
                    # would defeat the point of pulling annotations.
                    if not in_sdr and ext not in BOOK_EXTENSIONS:
                        log_fn(f"  {'  ' * depth}📄 {name} (skip — non-book)")
                        continue

                    # Compute subdir relative to documents/. child_path looks
                    # like "Internal Storage/documents/Subfolder/Book.pdf";
                    # we want just "Subfolder" (or None for root).
                    try:
                        docs_idx = next(
                            i for i, p in enumerate(parts)
                            if p.lower() == 'documents'
                        )
                        sub_parts = parts[docs_idx + 1:-1]  # drop documents/ and the file
                        subdir = '/'.join(sub_parts) if sub_parts else None
                    except StopIteration:
                        subdir = None

                    log_fn(f"  {'  ' * depth}📄 {name} ({size / 1024:.0f}KB) "
                           f"{'📝' if in_sdr else '✓'}")
                    books.append({
                        'handle': handle,
                        'storage_id': storage_id,
                        'filename': name,
                        'size': size,
                        'modified': info.get('modified'),
                        'subdir': subdir,
                        'is_annotation': in_sdr,
                    })

        for sid in storage_ids:
            log_fn(f"  Storage 0x{sid:08x}:")
            walk(sid, HANDLE_ROOT)

        return books

    def find_screenshots(self, log_fn=print):
        """
        Walk the ROOT of each storage and pick up every PNG/JPG file. The
        Kindle drops screenshots directly at the drive root with filenames
        like 'Screenshot_2024-XX-XX-...png'. We accept any image-shaped file
        at the root rather than rely on the prefix exclusively (some
        firmwares use slightly different naming).

        Returns a list of dicts: {handle, storage_id, filename, size}.
        """
        screenshots: list[dict] = []
        storage_ids = self.get_storage_ids()

        IMAGE_EXTS = {'.png', '.jpg', '.jpeg'}

        for sid in storage_ids:
            try:
                handles = self._screenshot_handles(sid)
            except Exception as e:
                log_fn(f"  Warning: storage 0x{sid:08x}: {e}")
                continue

            for handle in handles:
                try:
                    info = self.get_object_info(handle)
                except Exception:
                    continue

                name = info.get('filename', '')
                if not name:
                    continue
                if info.get('format') == FMT_ASSOCIATION:
                    continue  # folder, skip

                ext = ''
                dot = name.lower().rfind('.')
                if dot >= 0:
                    ext = name.lower()[dot:]
                if ext not in IMAGE_EXTS:
                    continue

                size = info.get('size', 0)
                log_fn(f"  📸 {name} ({size / 1024:.0f}KB)")
                screenshots.append({
                    'handle': handle,
                    'storage_id': sid,
                    'filename': name,
                    'size': size,
                })

        return screenshots

    def _screenshot_handles(self, storage_id):
        """
        Handles of every object where screenshots can live: the drive root
        (older firmware) and a top-level screenshots/ folder (newer firmware).
        """
        handles = list(self.get_object_handles(storage_id, HANDLE_ROOT))
        for h in list(handles):
            info = self.get_object_info(h)
            if (info.get('format') == FMT_ASSOCIATION
                    and info.get('filename', '').lower() == 'screenshots'):
                handles += self.get_object_handles(storage_id, h)
        return handles

    def find_object_by_filename(self, filename, log_fn=print):
        """
        Re-scan every storage's root looking for a file with this exact
        filename. Used when we want to act on a file (e.g. delete) but only
        have its original on-device name — not its current MTP handle, which
        isn't guaranteed to persist across sessions.

        Returns (handle, storage_id) or (None, None) if not found.
        """
        storage_ids = self.get_storage_ids()
        for sid in storage_ids:
            try:
                handles = self._screenshot_handles(sid)
            except Exception as e:
                log_fn(f"  Warning: storage 0x{sid:08x}: {e}")
                continue
            for handle in handles:
                try:
                    info = self.get_object_info(handle)
                except Exception:
                    continue
                if info.get('filename') == filename:
                    return handle, sid
        return None, None

    def delete_object(self, handle):
        """
        Send MTP DeleteObject for a single handle. Raises RuntimeError on
        any non-OK response (file not found, permission denied, etc.)
        """
        # OP_DELETE_OBJECT params: (handle, format). Format = 0 means "any" —
        # we want to delete this exact object regardless of its type.
        _data, code = self._send_and_recv(OP_DELETE_OBJECT, handle, 0)
        if code != RSP_OK:
            raise RuntimeError(
                f"DeleteObject failed: response 0x{code:04x}"
            )

# ── Public API ────────────────────────────────────────────────────────────────

@dataclass
class SyncReport:
    method: str = "pyusb (direct MTP)"
    device_name: str = ""
    notebooks_found: int = 0
    notebooks_copied: int = 0
    notebooks_skipped: int = 0
    thumbnails_copied: int = 0
    books_found: int = 0
    books_copied: int = 0
    books_skipped: int = 0
    screenshots_found: int = 0
    screenshots_copied: int = 0
    screenshots_skipped: int = 0
    errors: list = field(default_factory=list)
    local_paths: list = field(default_factory=list)
    log_lines: list = field(default_factory=list)


@dataclass
class DeleteReport:
    """Result of a delete-from-Kindle request."""
    method: str = "pyusb (direct MTP)"
    device_name: str = ""
    deleted: bool = False
    target_filename: str = ""
    errors: list = field(default_factory=list)
    log_lines: list = field(default_factory=list)


@dataclass
class PushReport:
    method: str = "pyusb (direct MTP)"
    device_name: str = ""
    file_pushed: bool = False
    target_path: str = ""
    bytes_sent: int = 0
    errors: list = field(default_factory=list)
    log_lines: list = field(default_factory=list)


def _find_kindle_usb():
    """Return a pyusb Device for the connected Kindle, or None."""
    try:
        import usb.core
    except ImportError:
        return None

    devices = list(usb.core.find(find_all=True, idVendor=KINDLE_VID))
    return devices[0] if devices else None


def _needs_copy(dest, size, modified=None):
    """
    True when the local copy is missing or differs from the device's file.

    Size alone misses edits that rewrite pages in place (a notebook's SQLite
    file often keeps its size), so we also compare the device's modification
    time, which we stamp onto the local copy after every download.
    """
    if not dest.exists():
        return True
    st = dest.stat()
    if st.st_size != size:
        return True
    if modified is not None and abs(st.st_mtime - modified) > 2:
        return True
    return False


def _stamp_mtime(dest, modified):
    """Give the local copy the device's modification time."""
    if modified is None:
        return
    try:
        os.utime(dest, (modified, modified))
    except OSError:
        pass


def sync_notebooks(dest, log=None, force=False):
    """
    Connect to Kindle via USB MTP and download all handwritten notebooks (and
    their thumbnails) to dest. Returns SyncReport.

    The notebook is saved as <UUID>.nbk (the raw KDF database). The matching
    thumbnail, if present, is saved as <UUID>.png next to it.
    """
    if log is None:
        log = print

    report = SyncReport()
    dest = Path(dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)

    def _log(msg):
        report.log_lines.append(msg)
        log(msg)

    try:
        import usb.core  # noqa: F401
    except ImportError:
        msg = (
            "pyusb is not installed.\n"
            "Fix: pip3 install pyusb && brew install libusb"
        )
        report.errors.append(msg)
        _log(f"✗ {msg}")
        return report

    _log("Scanning USB for Kindle…")
    usb_dev = _find_kindle_usb()

    if usb_dev is None:
        msg = (
            "No Kindle detected on USB.\n"
            "• Plug in your Kindle and unlock the screen.\n"
            "• Try a different cable — some USB-C cables are charge-only.\n"
            "• Make sure libusb is installed: brew install libusb"
        )
        report.errors.append(msg)
        _log(f"✗ {msg}")
        return report

    try:
        name = usb_dev.product or "Kindle"
    except Exception:
        name = "Kindle"

    report.device_name = name
    _log(
        f"Found: {name} "
        f"(VID=0x{usb_dev.idVendor:04x} PID=0x{usb_dev.idProduct:04x})"
    )

    mtp = MTPDevice(usb_dev)
    try:
        mtp.open()
        _log("USB interface claimed")
        mtp._open_session()
        _log("MTP session open")

        notebooks, thumbnails = mtp.find_notebooks(log_fn=_log)
        report.notebooks_found = len(notebooks)

        if not notebooks:
            _log("No notebooks found on device.")
            return report

        _log(f"\nDownloading {len(notebooks)} notebook(s) "
             f"and {len(thumbnails)} thumbnail(s)…")

        # 1. Notebooks: <UUID>.nbk (or filename if no UUID was found).
        in_book_dir = dest / "in-book"
        for handle, filename, size, folder_name, modified in notebooks:
            if folder_name and folder_name.lower().endswith('!!notebook'):
                # Handwriting inside a book: keyed by the book's ASIN/id.
                in_book_dir.mkdir(parents=True, exist_ok=True)
                save_name = f"{folder_name.split('!!')[0]}.nbk"
                dst = in_book_dir / save_name
            else:
                save_name = f"{folder_name}.nbk" if folder_name else filename
                dst = dest / save_name

            if not force and not _needs_copy(dst, size, modified):
                _log(f"  ✓ {save_name} — up to date")
                report.notebooks_skipped += 1
                report.local_paths.append(dst)
                continue

            _log(f"  ↓ {save_name} ({size / 1024 / 1024:.1f} MB)…")

            last_pct = [-1]
            def progress(done, total, _last=last_pct):
                if total > 0:
                    pct = done * 100 // total
                    if pct // 25 > _last[0] // 25:
                        _log(f"    {pct}%")
                        _last[0] = pct

            try:
                mtp.get_object(handle, dst, progress_cb=progress)
                _stamp_mtime(dst, modified)
                report.notebooks_copied += 1
                report.local_paths.append(dst)
                _log(f"  ✓ Saved → {dst}")
            except Exception as e:
                msg = f"Failed to download {save_name}: {e}"
                report.errors.append(msg)
                _log(f"  ✗ {msg}")

        # 2. Thumbnails: <UUID>.png. We're less strict here — if a thumbnail
        # fails to copy we just log and move on.
        for handle, filename, size, stem, modified in thumbnails:
            dst = dest / f"{stem}.png"
            if not force and not _needs_copy(dst, size, modified):
                continue
            try:
                mtp.get_object(handle, dst)
                _stamp_mtime(dst, modified)
                report.thumbnails_copied += 1
                _log(f"  ✓ thumbnail → {dst.name}")
            except Exception as e:
                _log(f"  ⚠ thumbnail {filename} failed: {e}")

    except Exception as e:
        msg = f"MTP error: {e}"
        report.errors.append(msg)
        _log(f"✗ {msg}")
    finally:
        try:
            mtp.close()
        except Exception:
            pass

    return report


def _safe_local_filename(name: str) -> str:
    """
    Make an on-device filename safe for the local filesystem. The Kindle
    accepts colons and other characters that some host filesystems reject
    (mainly Windows + macOS HFS+ legacy issues). We replace those with '_'
    and strip leading/trailing whitespace.
    """
    bad = '<>:"/\\|?*\x00'
    out = ''.join('_' if c in bad else c for c in name).strip()
    return out or 'untitled'


def sync_books(dest, log=None, force=False, purchased_subdir="purchased",
               sideloaded_subdir="sideloaded"):
    """
    Connect to Kindle via USB MTP and download every ebook/document from the
    device's documents/ folder. Each book is classified (purchased vs
    sideloaded) by `library.classify_remote_file` and saved into the
    matching subfolder under dest.

    Args:
        dest: parent directory; subdirectories `purchased/` and `sideloaded/`
              will be created underneath it.
        log: optional log callback
        force: re-download even if a file with matching size already exists
        purchased_subdir, sideloaded_subdir: names of the subdirectories.

    Returns SyncReport.

    Skips files already present locally with matching size unless force=True.
    """
    from library import classify_remote_file, safe_local_filename

    if log is None:
        log = print

    report = SyncReport()
    dest = Path(dest).expanduser()
    purchased_dir = dest / purchased_subdir
    sideloaded_dir = dest / sideloaded_subdir
    purchased_dir.mkdir(parents=True, exist_ok=True)
    sideloaded_dir.mkdir(parents=True, exist_ok=True)

    def _log(msg):
        report.log_lines.append(msg)
        log(msg)

    try:
        import usb.core  # noqa: F401
    except ImportError:
        msg = ("pyusb is not installed.\n"
               "Fix: pip3 install pyusb && brew install libusb")
        report.errors.append(msg)
        _log(f"✗ {msg}")
        return report

    _log("Scanning USB for Kindle…")
    usb_dev = _find_kindle_usb()
    if usb_dev is None:
        msg = "No Kindle detected on USB. Plug in and unlock the device."
        report.errors.append(msg)
        _log(f"✗ {msg}")
        return report

    try:
        report.device_name = usb_dev.product or "Kindle"
    except Exception:
        report.device_name = "Kindle"
    _log(f"Found: {report.device_name}")

    mtp = MTPDevice(usb_dev)
    try:
        mtp.open()
        _log("USB interface claimed")
        mtp._open_session()
        _log("MTP session open")

        books = mtp.find_books(log_fn=_log)
        report.books_found = len(books)

        if not books:
            _log("No books found in documents/ on the device.")
            return report

        _log(f"\nDownloading {len(books)} book(s)…")

        for book in books:
            is_annotation = book.get('is_annotation', False)
            subdir = book.get('subdir') or ''

            # Determine the target path. For ordinary books, classify and
            # drop into purchased/ or sideloaded/. For annotation files
            # (anything inside a `*.sdr/` folder under documents/), preserve
            # the folder structure so we can look them up later by base name.
            if is_annotation:
                # subdir for annotations looks like:
                #   "MyBook.sdr"           (annotation directly inside the .sdr)
                #   "Sub/MyBook.sdr"       (nested case)
                # We want to place under sideloaded/ (PDFs are sideloaded by
                # default; on the rare case of an annotated purchased book,
                # this is still findable from the basename).
                target_dir = sideloaded_dir / subdir
                target_dir.mkdir(parents=True, exist_ok=True)
                classification = 'annotation'
            else:
                classification = classify_remote_file(
                    book['filename'], subdir)
                if classification == 'purchased':
                    target_dir = purchased_dir
                else:
                    target_dir = sideloaded_dir

            safe_name = safe_local_filename(book['filename'])
            dst = target_dir / safe_name

            if not force and not _needs_copy(dst, book['size'],
                                             book.get('modified')):
                _log(f"  ✓ {classification:>10s}: {safe_name} — up to date")
                report.books_skipped += 1
                report.local_paths.append(dst)
                continue

            _log(f"  ↓ {classification:>10s}: {safe_name} "
                 f"({book['size'] / 1024 / 1024:.1f} MB)…")

            last_pct = [-1]
            def progress(done, total, _last=last_pct):
                if total > 0:
                    pct = done * 100 // total
                    if pct // 25 > _last[0] // 25:
                        _log(f"    {pct}%")
                        _last[0] = pct

            try:
                mtp.get_object(book['handle'], dst, progress_cb=progress)
                _stamp_mtime(dst, book.get('modified'))
                report.books_copied += 1
                report.local_paths.append(dst)
                _log(f"  ✓ Saved → {dst.relative_to(dest)}")
            except Exception as e:
                msg = f"Failed to download {safe_name}: {e}"
                report.errors.append(msg)
                _log(f"  ✗ {msg}")

    except Exception as e:
        msg = f"MTP error: {e}"
        report.errors.append(msg)
        _log(f"✗ {msg}")
    finally:
        try:
            mtp.close()
        except Exception:
            pass

    return report


def push_pdf_to_kindle(src_path, target_filename=None, log=None):
    """
    Upload a single file (typically a PDF) to the Kindle's documents/ folder.

    Args:
        src_path: local file to upload (Path or str)
        target_filename: name to give the file on the device. Defaults to the
                         basename of src_path. Should end in .pdf for PDFs;
                         the Kindle uses the extension to decide how to render.
        log: optional log callback

    Returns PushReport.

    NOTE: After uploading, the file may not appear in your Kindle's home
    screen until the device's content indexer runs. A reboot or a few minutes
    of idle time usually triggers re-indexing. Until then the file IS on the
    device — you can verify by re-running sync_books.
    """
    if log is None:
        log = print

    report = PushReport()
    src_path = Path(src_path).expanduser()

    def _log(msg):
        report.log_lines.append(msg)
        log(msg)

    if not src_path.exists():
        msg = f"Source file not found: {src_path}"
        report.errors.append(msg)
        _log(f"✗ {msg}")
        return report

    target_filename = target_filename or src_path.name
    target_filename = _safe_local_filename(target_filename)
    report.target_path = f"documents/{target_filename}"

    try:
        import usb.core  # noqa: F401
    except ImportError:
        msg = "pyusb not installed. Run: pip3 install pyusb"
        report.errors.append(msg)
        _log(f"✗ {msg}")
        return report

    usb_dev = _find_kindle_usb()
    if usb_dev is None:
        msg = "No Kindle detected on USB."
        report.errors.append(msg)
        _log(f"✗ {msg}")
        return report

    try:
        report.device_name = usb_dev.product or "Kindle"
    except Exception:
        report.device_name = "Kindle"
    _log(f"Found: {report.device_name}")
    _log(f"Pushing {src_path.name} ({src_path.stat().st_size / 1024 / 1024:.1f} MB)…")

    mtp = MTPDevice(usb_dev)
    try:
        mtp.open()
        mtp._open_session()

        storage_ids = mtp.get_storage_ids()
        if not storage_ids:
            raise IOError("Kindle reports no MTP storage")
        storage_id = storage_ids[0]

        # Find the documents/ folder. On most Kindles there's a single
        # "Internal Storage" container with documents/ at depth 1.
        documents_handle = None
        candidate_paths = [
            ['documents'],
            ['Internal Storage', 'documents'],
            ['internal storage', 'documents'],
        ]
        for path_components in candidate_paths:
            handle = mtp.find_directory(storage_id, path_components)
            if handle is not None:
                documents_handle = handle
                _log(f"Found documents folder via {'/'.join(path_components)} "
                     f"(handle 0x{handle:08x})")
                break

        if documents_handle is None:
            raise IOError("Could not locate documents/ folder on the device")

        last_pct = [-1]
        def progress(done, total, _last=last_pct):
            if total > 0:
                pct = done * 100 // total
                if pct // 25 > _last[0] // 25:
                    _log(f"  {pct}%")
                    _last[0] = pct

        new_handle = mtp.send_object(
            src_path,
            storage_id=storage_id,
            parent_handle=documents_handle,
            target_filename=target_filename,
            progress_cb=progress,
        )

        report.file_pushed = True
        report.bytes_sent = src_path.stat().st_size
        _log(f"✓ Pushed to documents/{target_filename} (handle 0x{new_handle:08x})")
        _log(
            "Note: the file is on the device, but may not appear in the "
            "home screen until the Kindle re-indexes (usually after a "
            "reboot or a few minutes of idle time)."
        )

    except Exception as e:
        msg = f"Push failed: {e}"
        report.errors.append(msg)
        _log(f"✗ {msg}")
    finally:
        try:
            mtp.close()
        except Exception:
            pass

    return report


def sync_screenshots(dest, log=None, force=False):
    """
    Connect to Kindle via USB MTP and download every PNG/JPG at the root of
    the Kindle's storage. Screenshots saved on the device with names like
    'Screenshot_2024-XX-XX-...png' get mirrored to dest/.

    Skips files already present locally with matching size unless force=True.
    """
    if log is None:
        log = print

    report = SyncReport()
    dest = Path(dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)

    def _log(msg):
        report.log_lines.append(msg)
        log(msg)

    try:
        import usb.core  # noqa: F401
    except ImportError:
        msg = ("pyusb is not installed.\n"
               "Fix: pip3 install pyusb && brew install libusb")
        report.errors.append(msg)
        _log(f"✗ {msg}")
        return report

    _log("Scanning USB for Kindle…")
    usb_dev = _find_kindle_usb()
    if usb_dev is None:
        msg = "No Kindle detected on USB. Plug in and unlock the device."
        report.errors.append(msg)
        _log(f"✗ {msg}")
        return report

    try:
        report.device_name = usb_dev.product or "Kindle"
    except Exception:
        report.device_name = "Kindle"
    _log(f"Found: {report.device_name}")

    mtp = MTPDevice(usb_dev)
    try:
        mtp.open()
        mtp._open_session()

        screenshots = mtp.find_screenshots(log_fn=_log)
        report.screenshots_found = len(screenshots)

        if not screenshots:
            _log("No screenshots found at the root of the device.")
            return report

        _log(f"\nDownloading {len(screenshots)} screenshot(s)…")
        for shot in screenshots:
            from library import safe_local_filename
            safe_name = safe_local_filename(shot['filename'])
            dst = dest / safe_name

            if not force and not _needs_copy(dst, shot['size']):
                report.screenshots_skipped += 1
                report.local_paths.append(dst)
                continue

            try:
                mtp.get_object(shot['handle'], dst)
                report.screenshots_copied += 1
                report.local_paths.append(dst)
                _log(f"  ✓ {safe_name}")
            except Exception as e:
                msg = f"Failed to download {safe_name}: {e}"
                report.errors.append(msg)
                _log(f"  ✗ {msg}")

    except Exception as e:
        msg = f"MTP error: {e}"
        report.errors.append(msg)
        _log(f"✗ {msg}")
    finally:
        try:
            mtp.close()
        except Exception:
            pass

    return report


def delete_from_kindle(filename, log=None):
    """
    Delete a single file from the Kindle's root storage by its filename.

    Re-scans the device to find the current MTP handle for this filename
    (handles may not be stable across sessions, so we don't trust persisted
    handles). Returns DeleteReport with success/failure info.
    """
    if log is None:
        log = print

    report = DeleteReport()
    report.target_filename = filename

    def _log(msg):
        report.log_lines.append(msg)
        log(msg)

    try:
        import usb.core  # noqa: F401
    except ImportError:
        report.errors.append("pyusb not installed")
        _log("✗ pyusb not installed")
        return report

    usb_dev = _find_kindle_usb()
    if usb_dev is None:
        report.errors.append("No Kindle detected on USB")
        _log("✗ No Kindle detected — plug in and unlock")
        return report

    try:
        report.device_name = usb_dev.product or "Kindle"
    except Exception:
        report.device_name = "Kindle"

    mtp = MTPDevice(usb_dev)
    try:
        mtp.open()
        mtp._open_session()
        _log(f"Looking for {filename} on device…")

        handle, sid = mtp.find_object_by_filename(filename, log_fn=_log)
        if handle is None:
            report.errors.append(
                f"{filename} not found on device (already deleted?)")
            _log(f"✗ {filename} not found on device")
            return report

        _log(f"Deleting handle 0x{handle:08x}…")
        try:
            mtp.delete_object(handle)
            report.deleted = True
            _log(f"✓ Deleted {filename} from Kindle")
        except Exception as e:
            report.errors.append(f"DeleteObject failed: {e}")
            _log(f"✗ {e}")

    except Exception as e:
        report.errors.append(f"MTP error: {e}")
        _log(f"✗ MTP error: {e}")
    finally:
        try:
            mtp.close()
        except Exception:
            pass

    return report


def detect_device():
    """Return device name string if a Kindle is connected, else empty string."""
    try:
        dev = _find_kindle_usb()
        if dev is None:
            return ""
        try:
            return dev.product or "Kindle"
        except Exception:
            return "Kindle"
    except Exception:
        return ""


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.WARNING)

    parser = argparse.ArgumentParser(
        description="Sync Kindle Scribe notebooks/books via USB MTP — no drivers needed"
    )
    sub = parser.add_subparsers(dest="cmd")

    # Default: sync notebooks (preserves backward-compat with old usage)
    parser.add_argument(
        "--dest", default=str(Path.home() / ".scribe_notebooks"),
        help="(notebooks) Where to save (default: ~/.scribe_notebooks)"
    )
    parser.add_argument(
        "--detect", action="store_true",
        help="Just check if a Kindle is connected, don't sync"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-download even if file already exists"
    )
    parser.add_argument("--verbose", "-v", action="store_true")

    # Subcommand: books
    p_books = sub.add_parser(
        "books", help="Sync ebooks/documents from documents/ folder"
    )
    p_books.add_argument(
        "--dest", default=str(Path.home() / ".scribe_books"),
        help="Where to save books (default: ~/.scribe_books)"
    )
    p_books.add_argument("--force", action="store_true")
    p_books.add_argument("--verbose", "-v", action="store_true")

    # Subcommand: push
    p_push = sub.add_parser(
        "push", help="Upload a PDF (or other supported file) to the Kindle"
    )
    p_push.add_argument("file", help="Local file to push")
    p_push.add_argument(
        "--name",
        help="Filename to use on-device (default: basename of source)",
    )
    p_push.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()

    if getattr(args, 'verbose', False):
        logging.getLogger().setLevel(logging.DEBUG)

    if args.detect and not args.cmd:
        name = detect_device()
        if name:
            print(f"✓ Kindle connected: {name}")
            sys.exit(0)
        else:
            print("✗ No Kindle detected")
            sys.exit(1)

    if args.cmd == "books":
        report = sync_books(dest=Path(args.dest), force=args.force)
        print()
        print(f"Device:  {report.device_name or '—'}")
        print(f"Found:   {report.books_found} books")
        print(f"Copied:  {report.books_copied}")
        print(f"Skipped: {report.books_skipped} (up to date)")
        if report.errors:
            print("Errors:")
            for e in report.errors:
                print(f"  ✗ {e}")
        sys.exit(0 if report.books_found and not report.errors else 1)

    if args.cmd == "push":
        report = push_pdf_to_kindle(
            src_path=args.file,
            target_filename=args.name,
        )
        print()
        print(f"Device: {report.device_name or '—'}")
        print(f"Target: {report.target_path}")
        if report.file_pushed:
            print(f"✓ Pushed {report.bytes_sent / 1024 / 1024:.1f} MB")
        if report.errors:
            print("Errors:")
            for e in report.errors:
                print(f"  ✗ {e}")
        sys.exit(0 if report.file_pushed else 1)

    # Default: sync notebooks
    report = sync_notebooks(dest=Path(args.dest), force=args.force)

    print()
    print(f"Device:     {report.device_name or '—'}")
    print(f"Found:      {report.notebooks_found} notebooks")
    print(f"Copied:     {report.notebooks_copied}")
    print(f"Skipped:    {report.notebooks_skipped} (up to date)")
    print(f"Thumbnails: {report.thumbnails_copied}")
    if report.errors:
        print("Errors:")
        for e in report.errors:
            print(f"  ✗ {e}")
    if report.local_paths:
        print(f"\nSaved to: {args.dest}")
        for p in report.local_paths:
            print(f"  {p.name}")
