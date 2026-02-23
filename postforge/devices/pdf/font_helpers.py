# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
Font utility functions for PDF builder.

Standalone helpers for encoding, widths, PFB conversion, cmap rewrite,
and ToUnicode mapping -- shared across Type 1, Type 42, and CFF font
embedding paths.
"""

import struct

from ...core import types as ps
from ...core.unicode_mapping import glyph_name_to_unicode
from .font_embedder import FontEmbedder
from .cid_font_embedder import CIDFontEmbedder

try:
    from pypdf.generic import (
        ArrayObject,
        DictionaryObject,
        NameObject,
        NumberObject,
        StreamObject,
    )
    from pypdf.generic import create_string_object
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False


def _charstrings_fingerprint(font_dict: ps.Dict) -> tuple:
    """Compute a content-based fingerprint for a font's CharStrings.

    DVIPS re-encoding creates new CharStrings dict objects per font instance,
    so Python id() differs even though the glyph programs are identical.
    Additionally, DVIPS subsets CharStrings per instance (only including
    needed glyphs), so the glyph name set varies even for the same base font.

    Uses (FontName, FontBBox) as a stable fingerprint -- both are constant
    across all subsets of the same font program.  This correctly groups
    same-font instances while keeping genuinely different fonts separate
    (e.g., CMR10 in the main document vs CMR10 in an embedded EPS from a
    different TeX installation).

    Args:
        font_dict: PostScript font dictionary.

    Returns:
        Tuple usable as a dict key, or empty tuple if identification fails.
    """
    font_name = font_dict.val.get(b'FontName')
    if not font_name:
        return ()
    bbox = font_dict.val.get(b'FontBBox')
    if bbox and bbox.TYPE in ps.ARRAY_TYPES and bbox.length >= 4:
        bbox_tuple = tuple(
            bbox.val[bbox.start + i].val for i in range(4))
    else:
        bbox_tuple = ()
    return (font_name.val, bbox_tuple)


def _get_font_bbox(font_dict: ps.Dict) -> tuple[float, float, float, float]:
    """Extract FontBBox from a PostScript font dictionary."""
    bbox = font_dict.val.get(b'FontBBox')
    if bbox and bbox.TYPE in ps.ARRAY_TYPES and bbox.length >= 4:
        vals = [bbox.val[bbox.start + i].val for i in range(4)]
        # Apply FontMatrix scaling (typically [0.001 0 0 0.001 0 0])
        fm = font_dict.val.get(b'FontMatrix')
        if fm and fm.TYPE in ps.ARRAY_TYPES:
            sx = fm.val[fm.start].val
            sy = fm.val[fm.start + 3].val
            # Scale to 1000-unit space (standard for PDF)
            if abs(sx) > 0 and abs(sx) < 0.01:
                scale = 1.0 / sx
                return (vals[0] * scale, vals[1] * scale,
                        vals[2] * scale, vals[3] * scale)
        return tuple(vals)
    return (0, -200, 1000, 800)


def _get_char_range(glyphs_used: set[int]) -> tuple[int, int]:
    """Get first and last character codes from glyph usage set."""
    if not glyphs_used:
        return (0, 0)
    return (min(glyphs_used), max(glyphs_used))


def _get_widths(font_dict: ps.Dict, glyphs_used: set[int],
                first_char: int, last_char: int,
                font_embedder: FontEmbedder) -> ArrayObject:
    """Get widths array for Type 1 font."""
    width_map = font_embedder.get_glyph_widths(font_dict, glyphs_used)
    default_width = 600
    return ArrayObject([
        NumberObject(width_map.get(cc, default_width))
        for cc in range(first_char, last_char + 1)
    ])


_STANDARD_ENCODINGS = {
    b'StandardEncoding', b'ISOLatin1Encoding',
    b'WinAnsiEncoding', b'MacRomanEncoding', b'MacExpertEncoding',
}


def _get_font_flags(font_dict: ps.Dict) -> int:
    """Compute PDF font descriptor Flags from PostScript font dictionary.

    Fonts with standard named encodings get NonSymbolic (32), which tells
    PDF viewers the encoding can be mapped through standard encoding tables.

    Fonts with custom array encodings (e.g., music fonts, TeX CM fonts) get
    Symbolic (4), which tells PDF viewers to use the encoding built into
    the font program without attempting standard encoding remapping.
    """
    encoding = font_dict.val.get(b'Encoding')
    if encoding is not None and encoding.TYPE == ps.T_NAME:
        if encoding.val in _STANDARD_ENCODINGS:
            return 32  # NonSymbolic -- standard encoding
    # Custom array encoding, unknown named encoding, or no encoding -> Symbolic
    return 4  # Symbolic -- encoding built into font program


def _to_pfb(font_data: bytes, length1: int, length2: int,
            length3: int) -> bytes:
    """Convert PFA-style font data to PFB format with segment markers."""
    parts = []

    # ASCII header segment
    header = font_data[:length1]
    parts.append(b'\x80\x01')
    parts.append(struct.pack('<I', len(header)))
    parts.append(header)

    # Binary eexec segment
    binary = font_data[length1:length1 + length2]
    parts.append(b'\x80\x02')
    parts.append(struct.pack('<I', len(binary)))
    parts.append(binary)

    # ASCII trailer segment
    trailer = font_data[length1 + length2:]
    if trailer:
        parts.append(b'\x80\x01')
        parts.append(struct.pack('<I', len(trailer)))
        parts.append(trailer)

    # EOF marker
    parts.append(b'\x80\x03')

    return b''.join(parts)


def _build_tounicode_map(font_dict: ps.Dict,
                         glyphs_used: set[int]) -> dict[int, str] | None:
    """Build ToUnicode mapping from font encoding."""
    encoding = font_dict.val.get(b'Encoding')
    if not encoding or encoding.TYPE not in ps.ARRAY_TYPES:
        return None

    tounicode: dict[int, str] = {}
    for cc in glyphs_used:
        if cc >= encoding.length:
            continue
        glyph_name_obj = encoding.val[encoding.start + cc]
        if not hasattr(glyph_name_obj, 'val'):
            continue
        glyph_name = glyph_name_obj.val
        if isinstance(glyph_name, bytes):
            glyph_name = glyph_name.decode('latin-1')
        if glyph_name == '.notdef':
            continue
        unicode_val = glyph_name_to_unicode(glyph_name)
        if unicode_val:
            tounicode[cc] = unicode_val

    return tounicode if tounicode else None


def _build_pdf_encoding(font_dict: ps.Dict, first_char: int,
                        last_char: int) -> DictionaryObject | None:
    """Build PDF Encoding dictionary from PostScript encoding."""
    encoding = font_dict.val.get(b'Encoding')
    if not encoding or encoding.TYPE not in ps.ARRAY_TYPES:
        return None

    differences = []
    last_code = -2  # Force first entry to emit code

    for cc in range(first_char, last_char + 1):
        if cc >= encoding.length:
            break
        glyph_name_obj = encoding.val[encoding.start + cc]
        if not hasattr(glyph_name_obj, 'val'):
            continue
        glyph_name = glyph_name_obj.val
        if isinstance(glyph_name, bytes):
            glyph_name = glyph_name.decode('latin-1')
        if glyph_name == '.notdef':
            continue

        if cc != last_code + 1:
            differences.append(NumberObject(cc))
        differences.append(NameObject('/' + glyph_name))
        last_code = cc

    if not differences:
        return None

    enc_dict = DictionaryObject()
    enc_dict[NameObject('/Type')] = NameObject('/Encoding')
    enc_dict[NameObject('/Differences')] = ArrayObject(differences)
    return enc_dict


def _get_glyph_name_for_code(encoding: object, char_code: int) -> bytes | None:
    """Get glyph name for a character code from an encoding."""
    if encoding is None:
        return None
    if encoding.TYPE == ps.T_NAME:
        if 32 <= char_code <= 126:
            return chr(char_code).encode('latin-1')
        return None
    if encoding.TYPE in ps.ARRAY_TYPES:
        if 0 <= char_code < encoding.length:
            elem = encoding.val[encoding.start + char_code]
            if elem.TYPE == ps.T_NAME:
                return elem.val
    return None


def _rewrite_type42_cmap(font_data: bytes, tables: dict,
                          font_dict: ps.Dict,
                          glyphs_used: set[int]) -> bytes:
    """Rewrite cmap table in TrueType font to match PostScript encoding.

    Symbolic TrueType fonts in PDF use cmap directly (ignoring /Encoding).
    We must rewrite the cmap so char codes map to the correct GIDs as
    specified by the PostScript Encoding + CharStrings.

    Builds a cmap with:
    - Platform (1,0) format 0: direct char_code -> GID (Mac/generic)
    - Platform (3,0) format 4: 0xF000+char_code -> GID (Windows Symbol)
    """
    encoding = font_dict.val.get(b'Encoding')
    char_strings = font_dict.val.get(b'CharStrings')
    if not char_strings or char_strings.TYPE != ps.T_DICT:
        return font_data

    # Build char_code -> GID mapping from Encoding + CharStrings
    code_to_gid: dict[int, int] = {}
    for char_code in range(256):
        glyph_name = _get_glyph_name_for_code(encoding, char_code)
        if not glyph_name:
            continue
        cs_entry = char_strings.val.get(glyph_name)
        if cs_entry is None:
            continue
        gid = cs_entry.val if hasattr(cs_entry, 'val') else int(cs_entry)
        code_to_gid[char_code] = gid

    if not code_to_gid:
        return font_data

    # Build format 0 subtable for platform (1, 0) - Macintosh Roman
    fmt0_glyph_array = bytearray(256)
    for cc, gid in code_to_gid.items():
        if 0 <= cc < 256 and gid <= 255:
            fmt0_glyph_array[cc] = gid

    fmt0_data = struct.pack('>HHH', 0, 262, 0) + bytes(fmt0_glyph_array)

    # Build format 4 subtable for platform (3, 0) - Windows Symbol
    segments = []
    for cc in sorted(code_to_gid.keys()):
        if cc not in glyphs_used:
            continue
        sym_code = 0xF000 + cc
        gid = code_to_gid[cc]
        segments.append((sym_code, sym_code, gid - sym_code, 0))

    # Add terminating segment
    segments.append((0xFFFF, 0xFFFF, 1, 0))

    seg_count = len(segments)
    search_range = 1
    entry_selector = 0
    while search_range * 2 <= seg_count:
        search_range *= 2
        entry_selector += 1
    search_range *= 2
    range_shift = seg_count * 2 - search_range

    fmt4 = bytearray()
    fmt4.extend(struct.pack('>H', 4))        # format
    length_pos = len(fmt4)
    fmt4.extend(struct.pack('>H', 0))         # length (fill later)
    fmt4.extend(struct.pack('>H', 0))         # language
    fmt4.extend(struct.pack('>H', seg_count * 2))
    fmt4.extend(struct.pack('>H', search_range))
    fmt4.extend(struct.pack('>H', entry_selector))
    fmt4.extend(struct.pack('>H', range_shift))

    for _, end, _, _ in segments:
        fmt4.extend(struct.pack('>H', end))
    fmt4.extend(struct.pack('>H', 0))  # reservedPad
    for start, _, _, _ in segments:
        fmt4.extend(struct.pack('>H', start))
    for _, _, delta, _ in segments:
        fmt4.extend(struct.pack('>H', delta & 0xFFFF))
    for _, _, _, ro in segments:
        fmt4.extend(struct.pack('>H', ro))

    fmt4_len = len(fmt4)
    struct.pack_into('>H', fmt4, length_pos, fmt4_len)
    fmt4_data = bytes(fmt4)

    # Assemble cmap table
    num_subtables = 2
    cmap_header = struct.pack('>HH', 0, num_subtables)

    records_size = 4 + num_subtables * 8
    fmt0_offset = records_size
    fmt4_offset = fmt0_offset + len(fmt0_data)

    cmap_header += struct.pack('>HHI', 1, 0, fmt0_offset)
    cmap_header += struct.pack('>HHI', 3, 0, fmt4_offset)

    new_cmap = cmap_header + fmt0_data + fmt4_data

    # Replace cmap table in font data and reassemble
    keep_tables: dict[bytes, bytes] = {}
    for tag, (tbl_offset, tbl_length) in tables.items():
        keep_tables[tag] = font_data[tbl_offset:tbl_offset + tbl_length]
    keep_tables[b'cmap'] = new_cmap

    assembler = CIDFontEmbedder()
    return assembler._assemble_truetype(keep_tables)


def _get_type42_bbox(font_data: bytes, tables: dict,
                      scale: float) -> list[int]:
    """Get font bounding box from TrueType head table."""
    head_info = tables.get(b'head')
    if head_info:
        ho = head_info[0]
        if ho + 54 <= len(font_data):
            x_min = struct.unpack('>h', font_data[ho + 36:ho + 38])[0]
            y_min = struct.unpack('>h', font_data[ho + 38:ho + 40])[0]
            x_max = struct.unpack('>h', font_data[ho + 40:ho + 42])[0]
            y_max = struct.unpack('>h', font_data[ho + 42:ho + 44])[0]
            return [
                int(round(x_min * scale)), int(round(y_min * scale)),
                int(round(x_max * scale)), int(round(y_max * scale)),
            ]
    return [0, -200, 1000, 800]


def _get_type42_hmtx_widths(font_dict: ps.Dict, font_data: bytes,
                             tables: dict, units_per_em: int,
                             glyphs_used: set[int]) -> dict[int, int]:
    """Get glyph widths for Type 42 font via Encoding -> CharStrings -> hmtx.

    Maps char_code -> glyph_name (Encoding) -> GID (CharStrings) -> width (hmtx).
    """
    hmtx_info = tables.get(b'hmtx')
    hhea_info = tables.get(b'hhea')
    if not hmtx_info or not hhea_info:
        return {}

    hmtx_offset = hmtx_info[0]
    hhea_offset = hhea_info[0]
    scale = 1000.0 / units_per_em if units_per_em > 0 else 1.0

    if hhea_offset + 36 > len(font_data):
        return {}
    num_hmetrics = int.from_bytes(
        font_data[hhea_offset + 34:hhea_offset + 36], 'big')

    encoding = font_dict.val.get(b'Encoding')
    char_strings = font_dict.val.get(b'CharStrings')
    if not char_strings or char_strings.TYPE != ps.T_DICT:
        return {}

    widths: dict[int, int] = {}
    for char_code in glyphs_used:
        glyph_name = _get_glyph_name_for_code(encoding, char_code)
        if not glyph_name:
            continue
        cs_entry = char_strings.val.get(glyph_name)
        if cs_entry is None:
            continue
        gid = cs_entry.val if hasattr(cs_entry, 'val') else int(cs_entry)

        if gid < num_hmetrics:
            offset = hmtx_offset + gid * 4
            if offset + 2 <= len(font_data):
                w = int.from_bytes(font_data[offset:offset + 2], 'big')
                widths[char_code] = int(round(w * scale))
        elif num_hmetrics > 0:
            offset = hmtx_offset + (num_hmetrics - 1) * 4
            if offset + 2 <= len(font_data):
                w = int.from_bytes(font_data[offset:offset + 2], 'big')
                widths[char_code] = int(round(w * scale))

    return widths


def _build_pdf_w_array(w_array_data: list) -> ArrayObject | None:
    """Convert width data list to PDF /W ArrayObject."""
    if not w_array_data:
        return None
    result = ArrayObject()
    for entry in w_array_data:
        if isinstance(entry, int):
            result.append(NumberObject(entry))
        elif isinstance(entry, list):
            inner = ArrayObject([NumberObject(w) for w in entry])
            result.append(inner)
    return result


def _make_pdf_string(value: str) -> StreamObject:
    """Create a PDF string object from a Python string."""
    return create_string_object(value)
