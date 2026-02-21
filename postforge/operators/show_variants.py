# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Show Variant Operators: kshow, glyphshow

These operators import rendering helpers from the refactored font modules:
font_ops.py, text_show.py, and font_rendering.py.
"""

import copy

from ..core import error as ps_error
from ..core import types as ps
from .matrix import _transform_delta


def _render_and_advance_single_glyph(ctxt, font_dict, char_code, font_type):
    """Render one glyph at **currentpoint** and advance **currentpoint** by its width.

    Based on **show**()'s inner **loop** pattern. Handles Type 1 and Type 3 fonts.
    Type 0 (composite) fonts are NOT supported here.

    Args:
        ctxt: PostScript context
        font_dict: Current font dictionary
        char_code: Character code (0-255)
        font_type: Font type (1 or 3)

    Returns:
        True if glyph rendered successfully, False otherwise
    """
    # Lazy imports to avoid circular import (font modules import control)
    from .font_rendering import (
        _render_type1_character, _render_type3_character,
    )
    from .text_show import _advance_current_point

    # Save current path, set up temp path with moveto at currentpoint
    saved_path = ctxt.gstate.path
    ctxt.gstate.path = ps.Path()
    ctxt.gstate.path.append(ps.SubPath())
    ctxt.gstate.path[-1].append(
        ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))

    try:
        currentpoint = copy.copy(ctxt.gstate.currentpoint)

        if font_type == 3:
            char_width = _render_type3_character(ctxt, font_dict, char_code)
            if char_width is not None:
                font_matrix = font_dict.val.get(b'FontMatrix')
                if font_matrix and font_matrix.TYPE in ps.ARRAY_TYPES:
                    user_width_x, _ = _transform_delta(font_matrix, char_width[0], char_width[1])
                else:
                    user_width_x = char_width[0]
                _advance_current_point(ctxt, currentpoint, user_width_x, font_dict)
            else:
                return False
        else:
            char_width = _render_type1_character(ctxt, font_dict, char_code)
            if char_width is not None:
                _advance_current_point(ctxt, currentpoint, char_width, font_dict)
            else:
                return False
    except Exception:
        ctxt.gstate.path = saved_path
        return False

    # Restore original path
    ctxt.gstate.path = saved_path
    return True


def kshow(ctxt, ostack):
    """
    proc string **kshow** -

    PLRM Section 8.2: Like **show**, but calls proc between each pair of
    adjacent characters. Before each call, **kshow** pushes the character codes
    of the two adjacent characters (the one just shown and the next one)
    on the operand stack.

    **kshow** does not work with composite (Type 0) fonts.

    Stack: proc string **kshow** -
    **Errors**: **invalidaccess**, **invalidfont**, **nocurrentpoint**,
                           stackunderflow, typecheck
    """
    # 1. STACKUNDERFLOW
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, kshow.__name__)

    # 2. TYPECHECK - string must be a string
    if ostack[-1].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, kshow.__name__)

    # 3. TYPECHECK - proc must be an executable array
    if ostack[-2].TYPE not in ps.ARRAY_TYPES or ostack[-2].attrib != ps.ATTRIB_EXEC:
        return ps_error.e(ctxt, ps_error.TYPECHECK, kshow.__name__)

    # 4. INVALIDACCESS
    if ostack[-1].access < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, kshow.__name__)
    if ostack[-2].access < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, kshow.__name__)

    # 5. INVALIDFONT
    current_font = ctxt.gstate.font
    if current_font is None:
        return ps_error.e(ctxt, ps_error.INVALIDFONT, kshow.__name__)

    # 6. kshow-specific: font must NOT be Type 0
    font_type = current_font.val.get(b'FontType', ps.Int(1)).val
    if font_type == 0:
        return ps_error.e(ctxt, ps_error.INVALIDFONT, kshow.__name__)

    # 7. NOCURRENTPOINT
    if ctxt.gstate.currentpoint is None:
        return ps_error.e(ctxt, ps_error.NOCURRENTPOINT, kshow.__name__)

    string_obj = ostack[-1]
    proc_obj = ostack[-2]

    # Only set up loop if string is non-empty
    if string_obj.length > 0:
        kshow_loop = ps.Loop(ps.LT_KSHOW)
        kshow_loop.proc = proc_obj
        kshow_loop.obj = copy.copy(string_obj)
        kshow_loop.control = 0  # character index tracker
        ctxt.e_stack.append(kshow_loop)

    ostack.pop()
    ostack.pop()


def glyphshow(ctxt, ostack):
    """
    name **glyphshow** -    (base fonts)
    cid **glyphshow** -     (CIDFonts)

    PLRM Section 8.2: Paints the single glyph identified by name (for base
    fonts) or cid (for CIDFonts), using the current font. Unlike **show**, the
    glyph is addressed directly by name or CID rather than through the
    font's Encoding array.

    **glyphshow** cannot be used with composite (Type 0) fonts.

    Stack: name|cid **glyphshow** -
    **Errors**: **invalidfont**, **nocurrentpoint**, **stackunderflow**, **typecheck**
    """
    from .font_rendering import (
        _render_type1_character, _render_type3_character,
        _render_cidfont_glyph,
    )
    from .text_show import _advance_current_point
    from .font_ops import _get_glyph_name, _get_charstring
    from ..core.charstring_interpreter import charstring_to_width

    # 1. STACKUNDERFLOW
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, glyphshow.__name__)

    # 2. INVALIDFONT
    current_font = ctxt.gstate.font
    if current_font is None:
        return ps_error.e(ctxt, ps_error.INVALIDFONT, glyphshow.__name__)

    # 3. NOCURRENTPOINT
    if ctxt.gstate.currentpoint is None:
        return ps_error.e(ctxt, ps_error.NOCURRENTPOINT, glyphshow.__name__)

    font_type = current_font.val.get(b'FontType', ps.Int(1)).val

    # Type 0 composite: invalidfont
    if font_type == 0:
        return ps_error.e(ctxt, ps_error.INVALIDFONT, glyphshow.__name__)

    # CIDFont (FontType 9 or 11): operand must be integer CID
    if font_type in (9, 11):
        if ostack[-1].TYPE != ps.T_INT:
            return ps_error.e(ctxt, ps_error.TYPECHECK, glyphshow.__name__)
        cid = ostack.pop().val

        # Save and setup temp path
        saved_path = ctxt.gstate.path
        ctxt.gstate.path = ps.Path()
        ctxt.gstate.path.append(ps.SubPath())
        ctxt.gstate.path[-1].append(
            ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))

        try:
            currentpoint = copy.copy(ctxt.gstate.currentpoint)
            # glyphshow with bare CIDFont: pass type0_font=None
            char_width = _render_cidfont_glyph(ctxt, current_font, cid, type0_font=None)
            if char_width is not None:
                _advance_current_point(ctxt, currentpoint, char_width, current_font)
        except Exception:
            pass

        ctxt.gstate.path = saved_path
        return

    # Base fonts (Type 1, Type 3): operand must be a name
    if ostack[-1].TYPE != ps.T_NAME:
        return ps_error.e(ctxt, ps_error.TYPECHECK, glyphshow.__name__)

    glyph_name = ostack.pop().val

    # Save and setup temp path
    saved_path = ctxt.gstate.path
    ctxt.gstate.path = ps.Path()
    ctxt.gstate.path.append(ps.SubPath())
    ctxt.gstate.path[-1].append(
        ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))

    try:
        currentpoint = copy.copy(ctxt.gstate.currentpoint)

        if font_type == 1:
            # Type 1: look up glyph name directly in CharStrings (bypass Encoding)
            encrypted_charstring = _get_charstring(current_font, glyph_name)
            if encrypted_charstring is None:
                # Try .notdef
                encrypted_charstring = _get_charstring(current_font, b'.notdef')
            if encrypted_charstring is not None:
                private_dict = current_font.val.get(b'Private')
                char_width = charstring_to_width(
                    encrypted_charstring, ctxt, private_dict, current_font)
                if char_width is not None:
                    _advance_current_point(ctxt, currentpoint, char_width, current_font)

        elif font_type == 3:
            # Type 3: prefer BuildGlyph, fall back to BuildChar via Encoding reverse lookup
            build_glyph = current_font.val.get(b'BuildGlyph')
            if build_glyph and build_glyph.TYPE in ps.ARRAY_TYPES and build_glyph.attrib == ps.ATTRIB_EXEC:
                # BuildGlyph available: render directly by name
                _render_type3_glyph_by_name(ctxt, current_font, glyph_name)
            else:
                # BuildChar only: reverse-search Encoding for the glyph name
                char_code = _reverse_encoding_lookup(current_font, glyph_name)
                if char_code is not None:
                    char_width = _render_type3_character(ctxt, current_font, char_code)
                    if char_width is not None:
                        font_matrix = current_font.val.get(b'FontMatrix')
                        if font_matrix and font_matrix.TYPE in ps.ARRAY_TYPES:
                            user_width_x, _ = _transform_delta(font_matrix, char_width[0], char_width[1])
                        else:
                            user_width_x = char_width[0]
                        _advance_current_point(ctxt, currentpoint, user_width_x, current_font)
    except Exception:
        pass

    ctxt.gstate.path = saved_path


def _render_type3_glyph_by_name(ctxt, font_dict, glyph_name):
    """Render a Type 3 glyph by name using BuildGlyph procedure.

    Similar to _render_type3_character but takes glyph_name directly
    instead of char_code. Used by **glyphshow**.
    """
    from .text_show import _advance_current_point
    from .graphics_state import gsave, grestore
    from .matrix import translate, concat, itransform
    from . import control as ps_control

    build_glyph = font_dict.val.get(b'BuildGlyph')
    if not build_glyph or build_glyph.TYPE not in ps.ARRAY_TYPES or build_glyph.attrib != ps.ATTRIB_EXEC:
        return None

    # Set up Type 3 execution context
    ctxt._in_build_procedure = True
    ctxt._font_cache_mode = False
    ctxt._char_width = None
    ctxt._char_bbox = None

    cp = ctxt.gstate.currentpoint

    try:
        gsave(ctxt, ctxt.o_stack)

        if cp:
            ctxt.o_stack.append(ps.Real(cp.x))
            ctxt.o_stack.append(ps.Real(cp.y))
            itransform(ctxt, ctxt.o_stack)
            translate(ctxt, ctxt.o_stack)

        font_matrix = font_dict.val.get(b'FontMatrix')
        if font_matrix and font_matrix.TYPE in ps.ARRAY_TYPES:
            ctxt.o_stack.append(font_matrix)
            concat(ctxt, ctxt.o_stack)

        # Push font dict and glyph name for BuildGlyph
        ctxt.o_stack.append(font_dict)
        name_val = glyph_name.encode('ascii') if isinstance(glyph_name, str) else glyph_name
        ctxt.o_stack.append(ps.Name(name_val))

        ctxt.e_stack.append(ps.HardReturn())
        ctxt.e_stack.append(copy.copy(build_glyph))
        ps_control.exec_exec(ctxt, ctxt.o_stack, ctxt.e_stack)

        char_width = ctxt._char_width
        grestore(ctxt, ctxt.o_stack)

        if char_width is not None and cp:
            currentpoint = copy.copy(cp)
            font_matrix = font_dict.val.get(b'FontMatrix')
            if font_matrix and font_matrix.TYPE in ps.ARRAY_TYPES:
                user_width_x, _ = _transform_delta(font_matrix, char_width[0], char_width[1])
            else:
                user_width_x = char_width[0]
            _advance_current_point(ctxt, currentpoint, user_width_x, font_dict)

        return char_width

    except Exception:
        try:
            grestore(ctxt, ctxt.o_stack)
        except Exception:
            pass
        return None

    finally:
        ctxt._in_build_procedure = False
        ctxt._font_cache_mode = False


def _reverse_encoding_lookup(font_dict, glyph_name):
    """Find char_code for a glyph name by searching the Encoding array.

    Returns the first matching char_code, or None if not found.
    Falls back to searching for .notdef if the name is not found.
    """
    encoding = font_dict.val.get(b'Encoding')
    if not encoding or encoding.TYPE not in ps.ARRAY_TYPES:
        return None

    # Search for exact match
    for i in range(len(encoding.val)):
        obj = encoding.val[i]
        if obj.TYPE == ps.T_NAME and obj.val == glyph_name:
            return i

    # Not found - try .notdef
    if glyph_name != b'.notdef':
        for i in range(len(encoding.val)):
            obj = encoding.val[i]
            if obj.TYPE == ps.T_NAME and obj.val == b'.notdef':
                return i

    return None
