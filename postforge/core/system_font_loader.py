# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
System Font Loader — loads binary OTF/TTF system fonts into PostForge.

Two loading paths:
  - OTF with CFF outlines:  Extract CFF table → cff_parser → _register_cff_font
  - TTF / OTF with glyf:    Build Type 42 font dict with sfnts, Encoding, CharStrings
"""

import logging
import struct

from . import types as ps
from .cff_parser import parse_cff, CFFError
from .system_font_cache import _find_table
from .unicode_mapping import GLYPH_TO_UNICODE
from ..operators import cff_ops

logger = logging.getLogger(__name__)

# Reverse map: Unicode codepoint (int) → glyph name (bytes)
# Built lazily on first use from GLYPH_TO_UNICODE.
_unicode_to_glyph = None


def _get_unicode_to_glyph():
    """Return the reverse AGL mapping: Unicode codepoint → glyph name bytes."""
    global _unicode_to_glyph
    if _unicode_to_glyph is None:
        _unicode_to_glyph = {}
        for gname, uchar in GLYPH_TO_UNICODE.items():
            if gname == b'.notdef':
                continue
            cp = ord(uchar)
            # First mapping wins (avoid overwriting)
            if cp not in _unicode_to_glyph:
                _unicode_to_glyph[cp] = gname
    return _unicode_to_glyph


def load_otf_cff(ctxt, file_path):
    """Load an OTF font with CFF outlines.

    Extracts the CFF table, parses it, and registers each font found
    as a Type 2 resource via the existing CFF pipeline.

    Args:
        ctxt: PostScript context
        file_path: Path to the .otf file

    Returns:
        True on success, False on any error
    """
    try:
        with open(file_path, 'rb') as f:
            data = f.read()
    except OSError:
        return False

    if len(data) < 12 or data[:4] != b'OTTO':
        return False

    # Find CFF table
    cff_offset, cff_length = _find_table(data, b'CFF ')
    if cff_offset is None:
        return False

    cff_bytes = data[cff_offset:cff_offset + cff_length]

    try:
        cff_fonts = parse_cff(cff_bytes)
    except (CFFError, Exception):
        return False

    if not cff_fonts:
        return False

    # Register in global VM (per PLRM: Type 1/2 fonts always go to global VM)
    saved_vm_mode = ctxt.vm_alloc_mode
    ctxt.vm_alloc_mode = True
    try:
        for cff_font in cff_fonts:
            cff_ops._register_cff_font(ctxt, cff_font, cff_bytes)
    except Exception:
        return False
    finally:
        ctxt.vm_alloc_mode = saved_vm_mode

    return True


def load_ttf(ctxt, file_path):
    """Load a TTF or OTF font with TrueType outlines as a Type 42 font.

    Builds a Type 42 font dictionary with sfnts, Encoding, and CharStrings,
    then registers it as a Font resource.

    Args:
        ctxt: PostScript context
        file_path: Path to the .ttf or .otf file

    Returns:
        True on success, False on any error
    """
    try:
        with open(file_path, 'rb') as f:
            data = f.read()
    except OSError:
        return False

    if len(data) < 12:
        return False

    # Verify magic: TrueType (0x00010000) or TTC (ttcf) or OTF with glyf
    magic = data[:4]
    if magic not in (b'\x00\x01\x00\x00', b'true', b'OTTO'):
        return False

    # Extract PostScript name from name table
    font_name = _extract_ps_name(data)
    if font_name is None:
        return False

    # Parse head table for unitsPerEm and bbox
    head_offset, head_length = _find_table(data, b'head')
    if head_offset is None:
        return False
    if head_offset + 54 > len(data):
        return False

    units_per_em = struct.unpack_from('>H', data, head_offset + 18)[0]
    if units_per_em == 0:
        units_per_em = 1000

    x_min = struct.unpack_from('>h', data, head_offset + 36)[0]
    y_min = struct.unpack_from('>h', data, head_offset + 38)[0]
    x_max = struct.unpack_from('>h', data, head_offset + 40)[0]
    y_max = struct.unpack_from('>h', data, head_offset + 42)[0]

    # Scale bbox by 1/unitsPerEm (Type 42 convention: FontMatrix is identity)
    em_scale = 1.0 / units_per_em
    bbox = [x_min * em_scale, y_min * em_scale,
            x_max * em_scale, y_max * em_scale]

    # Parse cmap to build Encoding and CharStrings
    encoding_arr, charstrings_dict = _build_encoding_and_charstrings(data, units_per_em)

    # Build the font dictionary
    ctxt_id = ctxt.current_ctxt_id if hasattr(ctxt, 'current_ctxt_id') else 0

    saved_vm_mode = ctxt.vm_alloc_mode
    ctxt.vm_alloc_mode = True
    try:
        font_dict = ps.Dict(ctxt_id, is_global=True)

        font_dict.val[b'FontType'] = ps.Int(42)

        font_name_bytes = font_name.encode('latin-1', errors='replace')
        font_dict.val[b'FontName'] = ps.Name(font_name_bytes)

        # Type 42 FontMatrix is identity [1 0 0 1 0 0]
        font_dict.val[b'FontMatrix'] = cff_ops._make_ps_array(
            ctxt_id, [ps.Real(1.0), ps.Real(0.0), ps.Real(0.0),
                       ps.Real(1.0), ps.Real(0.0), ps.Real(0.0)])

        font_dict.val[b'FontBBox'] = cff_ops._make_ps_array(
            ctxt_id, [ps.Real(v) for v in bbox])

        # Encoding: 256-element array of glyph names
        enc_elements = [ps.Name(name) for name in encoding_arr]
        font_dict.val[b'Encoding'] = cff_ops._make_ps_array(ctxt_id, enc_elements)

        # CharStrings: glyph name → GID (as ps.Int)
        cs_dict = ps.Dict(ctxt_id, is_global=True)
        for glyph_name_bytes, gid in charstrings_dict.items():
            cs_dict.val[glyph_name_bytes] = ps.Int(gid)
        font_dict.val[b'CharStrings'] = cs_dict

        # sfnts: single-element array containing the entire font file
        sfnts_string = cff_ops._make_ps_string(ctxt, data)
        font_dict.val[b'sfnts'] = cff_ops._make_ps_array(ctxt_id, [sfnts_string])

        font_dict.val[b'PaintType'] = ps.Int(0)

        # Store unitsPerEm for rendering
        font_dict.val[b'_unitsPerEm'] = ps.Int(units_per_em)

        # Register the font
        cff_ops._define_font_resource(ctxt, font_name_bytes, font_dict)
    except Exception:
        return False
    finally:
        ctxt.vm_alloc_mode = saved_vm_mode

    return True


def _extract_ps_name(data):
    """Extract PostScript name (nameID 6) from the name table.

    Returns str or None.
    """
    name_offset, name_length = _find_table(data, b'name')
    if name_offset is None:
        return None

    tbl = data[name_offset:name_offset + name_length]
    if len(tbl) < 6:
        return None

    _fmt, count, string_offset = struct.unpack_from('>HHH', tbl, 0)
    mac_name = None

    for i in range(count):
        rec_offset = 6 + i * 12
        if rec_offset + 12 > len(tbl):
            break
        platform_id, encoding_id, _lang_id, name_id, str_length, str_offset = (
            struct.unpack_from('>HHHHHH', tbl, rec_offset)
        )
        if name_id != 6:
            continue

        start = string_offset + str_offset
        end = start + str_length
        if end > len(tbl):
            continue
        raw = tbl[start:end]

        if platform_id == 3 and encoding_id == 1:
            try:
                return raw.decode('utf-16-be')
            except UnicodeDecodeError:
                continue
        elif platform_id == 1 and encoding_id == 0 and mac_name is None:
            try:
                mac_name = raw.decode('latin-1')
            except UnicodeDecodeError:
                continue

    return mac_name


def _build_encoding_and_charstrings(data, units_per_em):
    """Build Encoding array and CharStrings dict from cmap table.

    Returns:
        (encoding_arr, charstrings_dict)
        encoding_arr: list of 256 glyph name bytes (e.g., [b'.notdef', b'space', ...])
        charstrings_dict: dict mapping glyph_name_bytes → GID (int)
    """
    # Parse cmap table
    cmap_offset, cmap_length = _find_table(data, b'cmap')
    unicode_to_gid = {}
    if cmap_offset is not None:
        unicode_to_gid = _parse_cmap(data, cmap_offset, cmap_length)

    # Parse post table for GID → glyph name mapping
    gid_to_name = _parse_post_table(data)

    # Build reverse AGL: Unicode codepoint → glyph name
    reverse_agl = _get_unicode_to_glyph()

    # Build Encoding (256 entries) and CharStrings
    encoding_arr = []
    charstrings_dict = {}

    # Always include .notdef at GID 0
    charstrings_dict[b'.notdef'] = 0

    for char_code in range(256):
        gid = unicode_to_gid.get(char_code)
        if gid is None or gid == 0:
            encoding_arr.append(b'.notdef')
            continue

        # Determine glyph name: try post table, then AGL reverse, then fallback
        glyph_name = gid_to_name.get(gid)
        if glyph_name is None:
            glyph_name = reverse_agl.get(char_code)
        if glyph_name is None:
            glyph_name = f'uni{char_code:04X}'.encode('ascii')

        encoding_arr.append(glyph_name)
        charstrings_dict[glyph_name] = gid

    # Also add glyph names for GIDs beyond the 0-255 range that appear
    # in the cmap (for glyphshow/charpath by name)
    for unicode_cp, gid in unicode_to_gid.items():
        if gid == 0:
            continue
        glyph_name = gid_to_name.get(gid)
        if glyph_name is None:
            glyph_name = reverse_agl.get(unicode_cp)
        if glyph_name is None:
            glyph_name = f'uni{unicode_cp:04X}'.encode('ascii')
        if glyph_name not in charstrings_dict:
            charstrings_dict[glyph_name] = gid

    return encoding_arr, charstrings_dict


def _parse_cmap(data, cmap_offset, cmap_length):
    """Parse cmap table to build Unicode codepoint → GID mapping.

    Supports format 4 (BMP) and format 12 (full Unicode).
    Prefers platform 3/encoding 1 (Windows BMP), then platform 0 (Unicode).

    Returns dict: unicode_codepoint (int) → GID (int)
    """
    tbl = data[cmap_offset:cmap_offset + cmap_length]
    if len(tbl) < 4:
        return {}

    _version, num_records = struct.unpack_from('>HH', tbl, 0)

    # Find best subtable: prefer format 12, then format 4
    best_offset = None
    best_format = 0

    for i in range(num_records):
        rec_off = 4 + i * 8
        if rec_off + 8 > len(tbl):
            break
        plat_id, enc_id, sub_offset = struct.unpack_from('>HHI', tbl, rec_off)

        # Only consider Unicode-compatible subtables
        if plat_id == 3 and enc_id == 1:
            # Windows Unicode BMP
            if sub_offset + 2 <= len(tbl):
                fmt = struct.unpack_from('>H', tbl, sub_offset)[0]
                if fmt == 12 and fmt > best_format:
                    best_format = 12
                    best_offset = sub_offset
                elif fmt == 4 and best_format < 12:
                    best_format = 4
                    best_offset = sub_offset
        elif plat_id == 3 and enc_id == 10:
            # Windows Unicode full
            if sub_offset + 2 <= len(tbl):
                fmt = struct.unpack_from('>H', tbl, sub_offset)[0]
                if fmt == 12:
                    best_format = 12
                    best_offset = sub_offset
        elif plat_id == 0 and best_offset is None:
            # Unicode platform fallback
            if sub_offset + 2 <= len(tbl):
                fmt = struct.unpack_from('>H', tbl, sub_offset)[0]
                if fmt in (4, 12):
                    best_format = fmt
                    best_offset = sub_offset

    if best_offset is None:
        return {}

    if best_format == 4:
        return _parse_cmap_format4(tbl, best_offset)
    elif best_format == 12:
        return _parse_cmap_format12(tbl, best_offset)

    return {}


def _parse_cmap_format4(tbl, offset):
    """Parse cmap format 4 (segment mapping to delta values)."""
    if offset + 14 > len(tbl):
        return {}

    _fmt, length, _lang, seg_count_x2 = struct.unpack_from('>HHHH', tbl, offset)
    seg_count = seg_count_x2 // 2

    # Skip search range, entry selector, range shift
    seg_start = offset + 14

    end_codes_off = seg_start
    start_codes_off = seg_start + seg_count * 2 + 2  # +2 for reservedPad
    delta_off = start_codes_off + seg_count * 2
    range_off = delta_off + seg_count * 2

    result = {}
    for i in range(seg_count):
        end_code = struct.unpack_from('>H', tbl, end_codes_off + i * 2)[0]
        start_code = struct.unpack_from('>H', tbl, start_codes_off + i * 2)[0]
        id_delta = struct.unpack_from('>h', tbl, delta_off + i * 2)[0]
        id_range_offset = struct.unpack_from('>H', tbl, range_off + i * 2)[0]

        if start_code == 0xFFFF:
            break

        for code in range(start_code, end_code + 1):
            if id_range_offset == 0:
                gid = (code + id_delta) & 0xFFFF
            else:
                idx = id_range_offset // 2 + (code - start_code) + i
                glyph_off = range_off + idx * 2
                if glyph_off + 2 > len(tbl):
                    continue
                gid = struct.unpack_from('>H', tbl, glyph_off)[0]
                if gid != 0:
                    gid = (gid + id_delta) & 0xFFFF
            if gid != 0:
                result[code] = gid

    return result


def _parse_cmap_format12(tbl, offset):
    """Parse cmap format 12 (segmented coverage)."""
    if offset + 16 > len(tbl):
        return {}

    # Format 12: fixed32 format, uint32 length, uint32 language, uint32 nGroups
    _fmt = struct.unpack_from('>H', tbl, offset)[0]
    if offset + 4 > len(tbl):
        return {}
    _reserved, length, _lang, n_groups = struct.unpack_from('>HIII', tbl, offset + 2)

    result = {}
    group_off = offset + 16
    for i in range(n_groups):
        if group_off + 12 > len(tbl):
            break
        start_char, end_char, start_gid = struct.unpack_from('>III', tbl, group_off)
        group_off += 12
        for code in range(start_char, min(end_char + 1, start_char + 65536)):
            gid = start_gid + (code - start_char)
            if gid != 0:
                result[code] = gid

    return result


def _parse_post_table(data):
    """Parse post table to get GID → glyph name mapping.

    Returns dict: GID (int) → glyph name (bytes)
    """
    post_offset, post_length = _find_table(data, b'post')
    if post_offset is None:
        return {}

    tbl = data[post_offset:post_offset + post_length]
    if len(tbl) < 32:
        return {}

    # post table header: version (fixed 32), italicAngle, etc.
    version = struct.unpack_from('>I', tbl, 0)[0]

    if version == 0x00020000:
        return _parse_post_format2(tbl)

    # Format 1.0: standard Macintosh ordering (258 standard glyph names)
    # Format 3.0: no glyph names provided
    # For format 1.0, we could return the standard Mac glyph names but
    # it's simpler to fall back to AGL reverse lookup
    return {}


# Standard Macintosh glyph ordering (first 258 glyphs) - subset for common glyphs
_MAC_GLYPH_NAMES = [
    b'.notdef', b'.null', b'nonmarkingreturn', b'space', b'exclam',
    b'quotedbl', b'numbersign', b'dollar', b'percent', b'ampersand',
    b'quotesingle', b'parenleft', b'parenright', b'asterisk', b'plus',
    b'comma', b'hyphen', b'period', b'slash', b'zero', b'one', b'two',
    b'three', b'four', b'five', b'six', b'seven', b'eight', b'nine',
    b'colon', b'semicolon', b'less', b'equal', b'greater', b'question',
    b'at', b'A', b'B', b'C', b'D', b'E', b'F', b'G', b'H', b'I', b'J',
    b'K', b'L', b'M', b'N', b'O', b'P', b'Q', b'R', b'S', b'T', b'U',
    b'V', b'W', b'X', b'Y', b'Z', b'bracketleft', b'backslash',
    b'bracketright', b'asciicircum', b'underscore', b'grave', b'a', b'b',
    b'c', b'd', b'e', b'f', b'g', b'h', b'i', b'j', b'k', b'l', b'm',
    b'n', b'o', b'p', b'q', b'r', b's', b't', b'u', b'v', b'w', b'x',
    b'y', b'z', b'braceleft', b'bar', b'braceright', b'asciitilde',
    b'Adieresis', b'Aring', b'Ccedilla', b'Eacute', b'Ntilde',
    b'Odieresis', b'Udieresis', b'aacute', b'agrave', b'acircumflex',
    b'adieresis', b'atilde', b'aring', b'ccedilla', b'eacute', b'egrave',
    b'ecircumflex', b'edieresis', b'iacute', b'igrave', b'icircumflex',
    b'idieresis', b'ntilde', b'oacute', b'ograve', b'ocircumflex',
    b'odieresis', b'otilde', b'uacute', b'ugrave', b'ucircumflex',
    b'udieresis', b'dagger', b'degree', b'cent', b'sterling', b'section',
    b'bullet', b'paragraph', b'germandbls', b'registered', b'copyright',
    b'trademark', b'acute', b'dieresis', b'notequal', b'AE', b'Oslash',
    b'infinity', b'plusminus', b'lessequal', b'greaterequal', b'yen',
    b'mu', b'partialdiff', b'summation', b'product', b'pi', b'integral',
    b'ordfeminine', b'ordmasculine', b'Omega', b'ae', b'oslash',
    b'questiondown', b'exclamdown', b'logicalnot', b'radical', b'florin',
    b'approxequal', b'Delta', b'guillemotleft', b'guillemotright',
    b'ellipsis', b'nonbreakingspace', b'Agrave', b'Atilde', b'Otilde',
    b'OE', b'oe', b'endash', b'emdash', b'quotedblleft',
    b'quotedblright', b'quoteleft', b'quoteright', b'divide', b'lozenge',
    b'ydieresis', b'Ydieresis', b'fraction', b'currency',
    b'guilsinglleft', b'guilsinglright', b'fi', b'fl', b'daggerdbl',
    b'periodcentered', b'quotesinglbase', b'quotedblbase',
    b'perthousand', b'Acircumflex', b'Ecircumflex', b'Aacute',
    b'Edieresis', b'Egrave', b'Iacute', b'Icircumflex', b'Idieresis',
    b'Igrave', b'Oacute', b'Ocircumflex', b'apple', b'Ograve', b'Uacute',
    b'Ucircumflex', b'Ugrave', b'dotlessi', b'circumflex', b'tilde',
    b'macron', b'breve', b'dotaccent', b'ring', b'cedilla',
    b'hungarumlaut', b'ogonek', b'caron', b'Lslash', b'lslash',
    b'Scaron', b'scaron', b'Zcaron', b'zcaron', b'brokenbar', b'Eth',
    b'eth', b'Yacute', b'yacute', b'Thorn', b'thorn', b'minus',
    b'multiply', b'onesuperior', b'twosuperior', b'threesuperior',
    b'onehalf', b'onequarter', b'threequarters', b'franc', b'Gbreve',
    b'gbreve', b'Idotaccent', b'Scedilla', b'scedilla', b'Cacute',
    b'cacute', b'Ccaron', b'ccaron', b'dcroat',
]


def _parse_post_format2(tbl):
    """Parse post table format 2.0 for GID → glyph name mapping."""
    if len(tbl) < 34:
        return {}

    num_glyphs = struct.unpack_from('>H', tbl, 32)[0]

    offset = 34
    glyph_name_indices = []
    for _ in range(num_glyphs):
        if offset + 2 > len(tbl):
            break
        idx = struct.unpack_from('>H', tbl, offset)[0]
        glyph_name_indices.append(idx)
        offset += 2

    # Read extra name strings (indices >= 258)
    extra_names = []
    while offset < len(tbl):
        name_len = tbl[offset]
        offset += 1
        if offset + name_len > len(tbl):
            break
        extra_names.append(tbl[offset:offset + name_len])
        offset += name_len

    result = {}
    for gid, idx in enumerate(glyph_name_indices):
        if idx < 258:
            if idx < len(_MAC_GLYPH_NAMES):
                result[gid] = _MAC_GLYPH_NAMES[idx]
        else:
            extra_idx = idx - 258
            if extra_idx < len(extra_names):
                result[gid] = extra_names[extra_idx]

    return result
