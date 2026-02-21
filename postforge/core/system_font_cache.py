# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
System font cache — scans platform font directories for installed fonts,
extracts PostScript names, and persists the mapping in a JSON cache file.

Supported font formats:
  .pfa / .t1   — Type 1 ASCII (regex on /FontName)
  .pfb         — Type 1 binary (decode ASCII segments, then regex)
  .otf (CFF)   — OpenType with CFF outlines (parse CFF Name INDEX)
  .otf / .ttf  — OpenType/TrueType (parse name table, nameID 6)
"""

import json
import logging
import os
import re
import struct
import sys

logger = logging.getLogger(__name__)

# Platform-specific font directories
_FONT_DIRS = {
    "linux": [
        "/usr/share/fonts",
        "/usr/local/share/fonts",
        os.path.expanduser("~/.local/share/fonts"),
        os.path.expanduser("~/.fonts"),
    ],
    "darwin": [
        "/System/Library/Fonts",
        "/Library/Fonts",
        os.path.expanduser("~/Library/Fonts"),
    ],
    "win32": [
        os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
    ],
}

# Supported file extensions (lowercase, with dot)
_SUPPORTED_EXTENSIONS = frozenset({".pfa", ".t1", ".pfb", ".otf", ".ttf"})

# Cache file location
_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "postforge")
_CACHE_FILE = os.path.join(_CACHE_DIR, "system_fonts.json")

_CACHE_VERSION = 1

# /FontName regex for Type 1 fonts — matches /FontName /SomeName
_FONTNAME_RE = re.compile(rb"/FontName\s+/(\S+)")


class SystemFontCache:
    """Singleton cache mapping PostScript font names to system font file paths."""

    _instance = None

    def __init__(self) -> None:
        self._fonts: dict[str, str] = {}        # {ps_name: file_path}
        self._dir_mtimes: dict[str, float] = {}   # {dir_path: mtime}
        self._loaded: bool = False

    @classmethod
    def get_instance(cls) -> SystemFontCache:
        """Return the singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def get_font_path(self, font_name: str) -> str | None:
        """Look up a system font by PostScript name.

        Rebuilds the cache automatically if stale or not yet loaded.

        Args:
            font_name: PostScript font name (str).

        Returns:
            Absolute file path (str) or None.
        """
        if not self._loaded:
            self._load_or_rebuild()
        elif not self._is_fresh():
            self.rebuild()

        return self._fonts.get(font_name)

    def rebuild(self) -> None:
        """Force a full rescan of system font directories and persist the cache."""
        self._fonts.clear()
        self._dir_mtimes.clear()

        dirs = _get_platform_font_dirs()
        for d in dirs:
            if os.path.isdir(d):
                try:
                    self._dir_mtimes[d] = os.stat(d).st_mtime
                except OSError:
                    continue
                self._scan_directory(d)

        self._persist()
        self._loaded = True
        logger.info("System font cache rebuilt: %d fonts found", len(self._fonts))

    def font_count(self) -> int:
        """Return the number of cached fonts."""
        return len(self._fonts)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_or_rebuild(self) -> None:
        """Load cache from disk if fresh, otherwise rebuild."""
        if os.path.exists(_CACHE_FILE):
            try:
                with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("version") != _CACHE_VERSION:
                    self.rebuild()
                    return
                self._dir_mtimes = data.get("dir_mtimes", {})
                self._fonts = data.get("fonts", {})
                self._loaded = True
                if not self._is_fresh():
                    self.rebuild()
                return
            except (json.JSONDecodeError, KeyError, TypeError, OSError):
                pass  # Corrupt cache — rebuild
        self.rebuild()

    def _is_fresh(self) -> bool:
        """Check whether cached directory mtimes match current filesystem."""
        dirs = _get_platform_font_dirs()
        existing_dirs = {d for d in dirs if os.path.isdir(d)}

        # Check for new or removed directories
        cached_dirs = set(self._dir_mtimes.keys())
        if existing_dirs != cached_dirs:
            return False

        # Check mtimes
        for d in existing_dirs:
            try:
                current_mtime = os.stat(d).st_mtime
            except OSError:
                return False
            if self._dir_mtimes.get(d) != current_mtime:
                return False

        return True

    def _persist(self) -> None:
        """Write the cache to disk as JSON."""
        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
            data = {
                "version": _CACHE_VERSION,
                "dir_mtimes": self._dir_mtimes,
                "fonts": self._fonts,
            }
            with open(_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError as exc:
            logger.warning("Could not write system font cache: %s", exc)

    def _scan_directory(self, root: str) -> None:
        """Recursively scan *root* for font files and extract PS names."""
        for dirpath, _dirnames, filenames in os.walk(root):
            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in _SUPPORTED_EXTENSIONS:
                    continue
                full_path = os.path.join(dirpath, fname)
                try:
                    name = _extract_font_name(full_path, ext)
                except Exception:
                    continue
                if name:
                    # First font found wins (avoids overwriting with duplicates)
                    if name not in self._fonts:
                        self._fonts[name] = full_path


# ------------------------------------------------------------------
# Platform helper
# ------------------------------------------------------------------

def _get_platform_font_dirs() -> list[str]:
    """Return the list of font directories for the current platform."""
    if sys.platform.startswith("linux"):
        key = "linux"
    elif sys.platform == "darwin":
        key = "darwin"
    elif sys.platform == "win32":
        key = "win32"
    else:
        key = "linux"  # best guess
    return _FONT_DIRS.get(key, [])


# ------------------------------------------------------------------
# Font name extraction by format
# ------------------------------------------------------------------

def _extract_font_name(path: str, ext: str) -> str | None:
    """Extract the PostScript font name from a font file.

    Returns the name as a str, or None on failure.
    """
    if ext in (".pfa", ".t1"):
        return _extract_name_type1_ascii(path)
    elif ext == ".pfb":
        return _extract_name_pfb(path)
    elif ext == ".otf":
        return _extract_name_otf(path)
    elif ext == ".ttf":
        return _extract_name_ttf(path)
    return None


def _extract_name_type1_ascii(path: str) -> str | None:
    """Extract /FontName from a PFA or .t1 file (first ~4KB of text)."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return None
    m = _FONTNAME_RE.search(head)
    if m:
        return m.group(1).decode("latin-1")
    return None


def _extract_name_pfb(path: str) -> str | None:
    """Extract /FontName from a PFB (binary Type 1) file.

    PFB files consist of segments: each begins with 0x80, a type byte,
    then a 4-byte little-endian length.  Type 1 = ASCII text, Type 2 = binary.
    We decode ASCII segments and search for /FontName.
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None

    offset = 0
    text_chunks = []
    while offset + 6 <= len(data):
        if data[offset] != 0x80:
            break
        seg_type = data[offset + 1]
        seg_len = struct.unpack_from("<I", data, offset + 2)[0]
        offset += 6
        if seg_type == 1:  # ASCII segment
            text_chunks.append(data[offset : offset + seg_len])
        elif seg_type == 3:  # EOF marker
            break
        offset += seg_len

    combined = b"".join(text_chunks)
    m = _FONTNAME_RE.search(combined[:4096])
    if m:
        return m.group(1).decode("latin-1")
    return None


def _extract_name_otf(path: str) -> str | None:
    """Extract PS name from an OTF file.

    If the file has CFF outlines (magic OTTO), parse the CFF Name INDEX.
    Otherwise fall back to the name table (nameID 6).
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None

    if len(data) < 12:
        return None

    # Check for OTTO magic (CFF-based OTF)
    if data[:4] == b"OTTO":
        name = _extract_name_cff(data)
        if name:
            return name

    # Fall back to name table
    return _extract_name_from_name_table(data)


def _extract_name_ttf(path: str) -> str | None:
    """Extract PS name from a TrueType (.ttf) file via the name table."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None

    if len(data) < 12:
        return None

    return _extract_name_from_name_table(data)


# ------------------------------------------------------------------
# CFF Name INDEX extraction (for OTTO / CFF-based OTF)
# ------------------------------------------------------------------

def _extract_name_cff(data: bytes) -> str | None:
    """Parse the CFF Name INDEX from an OTF file with CFF outlines.

    Finds the CFF table, then reads the Name INDEX (first INDEX after
    the CFF header) to get the PostScript font name.
    """
    # Find CFF table offset from the OTF table directory
    cff_offset, cff_length = _find_table(data, b"CFF ")
    if cff_offset is None:
        return None

    cff_data = data[cff_offset : cff_offset + cff_length]
    if len(cff_data) < 4:
        return None

    # CFF header: major(1), minor(1), hdrSize(1), offSize(1)
    hdr_size = cff_data[2]

    # Name INDEX immediately follows the header
    names, _ = _parse_cff_index(cff_data, hdr_size)
    if names:
        return names[0].decode("latin-1", errors="replace")
    return None


def _parse_cff_index(data: bytes, offset: int) -> tuple[list[bytes], int]:
    """Parse a CFF INDEX structure at *offset* within *data*.

    Returns (list_of_bytes, end_offset).
    """
    if offset + 2 > len(data):
        return [], offset

    count = struct.unpack_from(">H", data, offset)[0]
    offset += 2
    if count == 0:
        return [], offset

    if offset >= len(data):
        return [], offset
    off_size = data[offset]
    offset += 1

    # Read count+1 offsets
    offsets = []
    for _ in range(count + 1):
        if offset + off_size > len(data):
            return [], offset
        val = int.from_bytes(data[offset : offset + off_size], "big")
        offsets.append(val)
        offset += off_size

    # Data region starts at current position - 1 (offsets are 1-based)
    data_start = offset - 1
    items = []
    for i in range(count):
        start = data_start + offsets[i]
        end = data_start + offsets[i + 1]
        if start < 0 or end > len(data):
            return [], offset
        items.append(data[start:end])

    end_offset = data_start + offsets[-1]
    return items, end_offset


# ------------------------------------------------------------------
# OpenType/TrueType name table parsing (nameID 6 = PostScript name)
# ------------------------------------------------------------------

def _extract_name_from_name_table(data: bytes) -> str | None:
    """Parse the OpenType/TrueType name table for nameID 6 (PostScript name).

    Prefers platformID 3 (Windows) encodingID 1 (Unicode BMP) decoded as
    UTF-16BE.  Falls back to platformID 1 (Mac) encodingID 0 (Roman)
    decoded as latin-1.
    """
    name_offset, name_length = _find_table(data, b"name")
    if name_offset is None:
        return None

    tbl = data[name_offset : name_offset + name_length]
    if len(tbl) < 6:
        return None

    # name table header: format(2), count(2), stringOffset(2)
    _fmt, count, string_offset = struct.unpack_from(">HHH", tbl, 0)

    mac_name = None

    for i in range(count):
        rec_offset = 6 + i * 12
        if rec_offset + 12 > len(tbl):
            break
        platform_id, encoding_id, _lang_id, name_id, str_length, str_offset = (
            struct.unpack_from(">HHHHHH", tbl, rec_offset)
        )
        if name_id != 6:
            continue

        start = string_offset + str_offset
        end = start + str_length
        if end > len(tbl):
            continue
        raw = tbl[start:end]

        if platform_id == 3 and encoding_id == 1:
            # Windows Unicode BMP — best option, return immediately
            try:
                return raw.decode("utf-16-be")
            except UnicodeDecodeError:
                continue
        elif platform_id == 1 and encoding_id == 0 and mac_name is None:
            # Mac Roman — keep as fallback
            try:
                mac_name = raw.decode("latin-1")
            except UnicodeDecodeError:
                continue

    return mac_name


# ------------------------------------------------------------------
# OTF/TTF table directory helper
# ------------------------------------------------------------------

def _find_table(data: bytes, tag: bytes) -> tuple[int | None, int | None]:
    """Find a table in the OTF/TTF table directory.

    Returns (offset, length) or (None, None) if not found.
    """
    if len(data) < 12:
        return None, None

    num_tables = struct.unpack_from(">H", data, 4)[0]
    for i in range(num_tables):
        rec_offset = 12 + i * 16
        if rec_offset + 16 > len(data):
            break
        tbl_tag = data[rec_offset : rec_offset + 4]
        if tbl_tag == tag:
            tbl_offset = struct.unpack_from(">I", data, rec_offset + 8)[0]
            tbl_length = struct.unpack_from(">I", data, rec_offset + 12)[0]
            return tbl_offset, tbl_length

    return None, None
