# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
CFF Font Operators

Implements .cff_startdata — the internal operator that backs the FontSetInit
ProcSet's StartData procedure.  Reads binary CFF data from the current file,
parses it with core.cff_parser, and registers each font via the resource system.
"""

from ..core import types as ps
from ..core import error as ps_error
from ..core.cff_parser import parse_cff, CFFError

# Registry mapping id(CharStrings.val) -> raw CFF binary bytes.
# Used by PDF embedding to retrieve the original CFF data for /FontFile3.
# Keyed by CharStrings identity so it survives scalefont/makefont copies
# (which share the same CharStrings dict).
_cff_registry = {}


def _make_ps_string(ctxt, raw_bytes):
    """Create a ps.String from raw bytes by writing into VM string storage."""
    strings = ps.global_resources.global_strings if ctxt.vm_alloc_mode else ctxt.local_strings
    offset = len(strings)
    length = len(raw_bytes)
    strings += raw_bytes
    return ps.String(ctxt.id, offset, length, is_global=ctxt.vm_alloc_mode)


def _make_ps_array(ctxt_id, elements, is_global=True):
    """Create a ps.Array with proper length tracking from a list of PS objects."""
    arr = ps.Array(ctxt_id, is_global=is_global)
    arr.setval(elements)
    return arr


def cff_startdata(ctxt, ostack):
    """
    byte_count .cff_startdata -

    Read byte_count bytes of CFF binary data from the current file,
    parse the CFF data, build PostScript font dictionaries for each
    font found, and register them as Font resources.

    Called via FontSetInit ProcSet's StartData procedure.
    The PS wrapper typically pushes: fontsetname byte_count StartData
    We consume byte_count; the fontsetname (if present) was already
    consumed by the PS-level wrapper or is left on the stack.

    PLRM Error conditions: stackunderflow, typecheck, invalidfont
    """
    # Validate stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, ".cff_startdata")

    # Validate operand type
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, ".cff_startdata")

    byte_count = int(ostack[-1].val)

    # Pop byte_count operand after validation
    ostack.pop()

    # Find topmost file on execution stack (same pattern as currentfile)
    current_file = None
    for i in range(len(ctxt.e_stack) - 1, -1, -1):
        if isinstance(ctxt.e_stack[i], (ps.File, ps.Run)):
            current_file = ctxt.e_stack[i]
            break

    if current_file is None:
        return ps_error.e(ctxt, ps_error.INVALIDFONT, ".cff_startdata")

    # Read exactly byte_count bytes of binary CFF data
    # Note: The PostScript tokenizer already consumed the whitespace delimiter
    # after the byte_count operand, so the file position is at the CFF data.
    cff_data = bytearray()
    if hasattr(current_file, 'read_bulk'):
        remaining = byte_count
        while remaining > 0:
            chunk = current_file.read_bulk(ctxt, min(remaining, 65536))
            if not chunk:
                break
            cff_data.extend(chunk)
            remaining -= len(chunk)
    else:
        for _ in range(byte_count):
            b = current_file.read(ctxt)
            if b is None:
                break
            if isinstance(b, int):
                cff_data.append(b)
            else:
                cff_data.append(ord(b) if isinstance(b, str) else b)

    cff_bytes = bytes(cff_data)

    # Parse CFF data
    try:
        cff_fonts = parse_cff(cff_bytes)
    except (CFFError, Exception):
        return ps_error.e(ctxt, ps_error.INVALIDFONT, ".cff_startdata")

    # Build and register PostScript font dictionaries for each CFF font
    # Force global VM mode (per PLRM: Type 1/2 fonts always go to global VM)
    saved_vm_mode = ctxt.vm_alloc_mode
    ctxt.vm_alloc_mode = True
    try:
        for cff_font in cff_fonts:
            _register_cff_font(ctxt, cff_font, cff_bytes)
    finally:
        ctxt.vm_alloc_mode = saved_vm_mode


def _register_cff_font(ctxt, cff_font, cff_bytes):
    """Build a PostScript font dictionary from a parsed CFFFont and register it.

    Must be called with ctxt.vm_alloc_mode = True (global VM) so that
    gcheck returns True and DefineResource stores in globalresourcedict.
    """
    ctxt_id = ctxt.current_ctxt_id if hasattr(ctxt, 'current_ctxt_id') else 0

    font_name_bytes = cff_font.name.encode('latin-1')

    # Create font dictionary in global VM
    font_dict = ps.Dict(ctxt_id, is_global=True)

    # FontType 2 (CFF)
    font_dict.val[b'FontType'] = ps.Int(2)

    # FontName
    font_dict.val[b'FontName'] = ps.Name(font_name_bytes)

    # FontMatrix
    font_dict.val[b'FontMatrix'] = _make_ps_array(
        ctxt_id, [ps.Real(v) for v in cff_font.font_matrix]
    )

    # FontBBox
    font_dict.val[b'FontBBox'] = _make_ps_array(
        ctxt_id, [ps.Real(float(v)) for v in cff_font.font_bbox]
    )

    # Encoding — 256-element array: code -> Name
    enc_elements = []
    for gid in cff_font.encoding:
        if 0 <= gid < len(cff_font.charset):
            glyph_name = cff_font.charset[gid]
        else:
            glyph_name = '.notdef'
        enc_elements.append(ps.Name(glyph_name.encode('latin-1')))
    font_dict.val[b'Encoding'] = _make_ps_array(ctxt_id, enc_elements)

    # CharStrings — dict mapping glyph_name_bytes -> ps.String(raw charstring bytes)
    cs_dict = ps.Dict(ctxt_id, is_global=True)
    for gid, cs_data in enumerate(cff_font.char_strings):
        if gid < len(cff_font.charset):
            glyph_name = cff_font.charset[gid]
        else:
            glyph_name = f'.gid{gid}'
        cs_dict.val[glyph_name.encode('latin-1')] = _make_ps_string(ctxt, cs_data)
    font_dict.val[b'CharStrings'] = cs_dict

    # Private dictionary
    priv_dict = ps.Dict(ctxt_id, is_global=True)
    priv_dict.val[b'defaultWidthX'] = ps.Real(cff_font.default_width_x)
    priv_dict.val[b'nominalWidthX'] = ps.Real(cff_font.nominal_width_x)

    # Local Subrs as array of ps.String
    if cff_font.local_subrs:
        priv_dict.val[b'Subrs'] = _make_ps_array(
            ctxt_id, [_make_ps_string(ctxt, s) for s in cff_font.local_subrs]
        )

    font_dict.val[b'Private'] = priv_dict

    # Store global subrs as internal attribute
    font_dict.val[b'_cff_global_subrs'] = _make_ps_array(
        ctxt_id, [_make_ps_string(ctxt, s) for s in cff_font.global_subrs]
    )

    # Register CFF binary for PDF embedding, keyed by CharStrings identity.
    # This survives scalefont/makefont copies since they share CharStrings.
    _cff_registry[id(cs_dict.val)] = cff_bytes

    # CID-specific data
    if cff_font.is_cid:
        font_dict.val[b'CIDFontType'] = ps.Int(0)
        if cff_font.ros:
            font_dict.val[b'_cff_ros'] = _make_ps_array(ctxt_id, [
                _make_ps_string(ctxt, cff_font.ros[0].encode('latin-1')),
                _make_ps_string(ctxt, cff_font.ros[1].encode('latin-1')),
                ps.Int(cff_font.ros[2]),
            ])

        # Store FD array data for CID per-glyph private dict selection
        if cff_font.fd_array:
            fd_elements = []
            for fd_entry in cff_font.fd_array:
                fd_dict = ps.Dict(ctxt_id, is_global=True)
                fd_priv = ps.Dict(ctxt_id, is_global=True)
                fd_priv.val[b'defaultWidthX'] = ps.Real(fd_entry.get('default_width_x', 0.0))
                fd_priv.val[b'nominalWidthX'] = ps.Real(fd_entry.get('nominal_width_x', 0.0))
                if fd_entry.get('local_subrs'):
                    fd_priv.val[b'Subrs'] = _make_ps_array(
                        ctxt_id, [_make_ps_string(ctxt, s) for s in fd_entry['local_subrs']]
                    )
                fd_dict.val[b'Private'] = fd_priv
                fd_elements.append(fd_dict)
            font_dict.val[b'_cff_fd_array'] = _make_ps_array(ctxt_id, fd_elements)

        if cff_font.fd_select:
            font_dict.val[b'_cff_fd_select'] = _make_ps_array(
                ctxt_id, [ps.Int(fd_idx) for fd_idx in cff_font.fd_select]
            )

    # Register font via definefont logic
    # Push key and font_dict, then call definefont equivalent
    _define_font_resource(ctxt, font_name_bytes, font_dict)



def _define_font_resource(ctxt, font_name_bytes, font_dict):
    """Register a font dictionary using defineresource logic.

    Mirrors the behavior of: fontname fontdict /Font defineresource
    """
    from . import resource as ps_resource

    # Push key and instance on operand stack, then call defineresource
    # defineresource pops key+instance+category and pushes instance back
    font_name = ps.Name(font_name_bytes)
    ctxt.o_stack.append(font_name)
    ctxt.o_stack.append(font_dict)
    ctxt.o_stack.append(ps.Name(b'Font'))
    ps_resource.defineresource(ctxt, ctxt.o_stack)
    # Pop the instance that defineresource leaves on the stack
    if ctxt.o_stack and ctxt.o_stack[-1].TYPE == ps.T_DICT:
        ctxt.o_stack.pop()
