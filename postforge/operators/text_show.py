# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Text Show Operators

All text display operators, text measurement, positioning, and TextObj emission.
Operators: show, stringwidth, ashow, widthshow, awidthshow, xyshow, xshow, yshow,
cshow, charpath, setcachedevice, setcachedevice2, setcharwidth.
"""

import copy
import math

from ..core import error as ps_error
from ..core import types as ps
from ..core import color_space
from ..core.charstring_interpreter import CharStringError, charstring_to_width
from ..core.type2_charstring import Type2Error, type2_charstring_to_width
from ..core.unicode_mapping import glyph_name_to_unicode
from .matrix import _transform_point, _transform_delta
from . import font_ops
from . import font_rendering


def show(ctxt, ostack):
    """
    string **show** -

    PLRM Section 8.2: Paints glyphs for the characters identified by the elements
    of string on the current page starting at the current point, using the font
    face, size, and orientation specified by the current font. The spacing from
    each glyph to the next is determined by the glyph's width.

    **Errors**: **stackunderflow**, **typecheck**, **invalidfont**, **nocurrentpoint**
    """
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, show.__name__)

    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, show.__name__)

    # 3. INVALIDACCESS - Check access permission
    if ostack[-1].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, show.__name__)

    text_string = ostack[-1]
    current_font = ctxt.gstate.font

    # Additional validation: check if current font exists
    if current_font is None:
        return ps_error.e(ctxt, ps_error.INVALIDFONT, show.__name__)

    # Additional validation: check for current point
    if ctxt.gstate.currentpoint is None:
        return ps_error.e(ctxt, ps_error.NOCURRENTPOINT, show.__name__)

    # Check device preference from pagedevice dictionary
    page_device = getattr(ctxt.gstate, 'page_device', {})
    if hasattr(page_device, 'TYPE') and page_device.TYPE == ps.T_DICT:
        page_device = page_device.val

    text_mode_obj = page_device.get(b'TextRenderingMode', ps.Name(b'GlyphPaths'))
    text_mode = text_mode_obj.val

    # Get text content as bytes for character code processing
    text_bytes = text_string.byte_string()
    if isinstance(text_bytes, str):
        text_bytes = text_bytes.encode('latin-1')

    # TextObjs mode: emit TextObj to display list instead of rendering glyph paths
    type3_actual_text = False
    if text_mode == b'TextObjs':
        font_type_check = current_font.val.get(b'FontType', ps.Int(1)).val
        if font_type_check == 3:
            # Type 3: fall through to GlyphPaths rendering
            # Bracket with ActualText for searchability
            type3_actual_text = True
            type3_start_pos = (ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)
            ctxt._type3_dl_start = len(ctxt.display_list) if ctxt.display_list else 0
        elif font_type_check == 0 and not current_font.val.get(b'CMap'):
            # FMapType Type 0: fall through to GlyphPaths with ActualText
            type3_actual_text = True
            type3_start_pos = (ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)
            ctxt._type3_dl_start = len(ctxt.display_list) if ctxt.display_list else 0
        else:
            _show_as_text_objs(ctxt, text_bytes, current_font)
            ostack.pop()
            # Notify interactive display to refresh
            if hasattr(ctxt, 'on_paint_callback') and ctxt.on_paint_callback:
                try:
                    ctxt.on_paint_callback(ctxt, None)
                except Exception:
                    pass
            return

    # GlyphPaths mode (or Type 0/3 TextObjs fallthrough): render text as glyph paths

    # preserve the current path
    cp = ctxt.gstate.path

    # setup the current path
    ctxt.gstate.path = ps.Path()
    # start a new ps.SubPath
    ctxt.gstate.path.append(ps.SubPath())
    # add the moveto to the path
    ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))

    try:
        # Check font type once - Type 1 vs Type 3 vs Type 0
        font_type = current_font.val.get(b'FontType', ps.Int(1)).val

        if font_type == 0:
            # Type 0 composite font
            # Check for cshow pending CID (PLRM: show within cshow uses originally selected CID)
            pending_cid = getattr(ctxt, '_cshow_pending_cid', None)
            if pending_cid is not None:
                # Use the CID from cshow instead of decoding the string
                delattr(ctxt, '_cshow_pending_cid')
                cmap_dict = current_font.val.get(b'CMap')
                fdep_vector = current_font.val.get(b'FDepVector')
                if fdep_vector and fdep_vector.val:
                    desc_font = fdep_vector.val[0]
                    currentpoint = copy.copy(ctxt.gstate.currentpoint)
                    char_width = font_rendering._render_cidfont_glyph(ctxt, desc_font, pending_cid, current_font)
                    if char_width:
                        # Apply Type 0 FontMatrix scaling
                        type0_fm = current_font.val.get(b'FontMatrix')
                        if type0_fm and type0_fm.TYPE in ps.ARRAY_TYPES and len(type0_fm.val) >= 1:
                            type0_scale = type0_fm.val[0].val if type0_fm.val[0].TYPE in ps.NUMERIC_TYPES else 1.0
                            char_width *= type0_scale
                        _advance_current_point(ctxt, currentpoint, char_width, current_font)
                        ctxt.gstate.path.append(ps.SubPath())
                        ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))
            else:
                # Normal Type 0 show - decode through CMap/FMapType and render glyphs
                # Note: _render_type0_string advances currentpoint after each glyph
                results = font_rendering._render_type0_string(ctxt, current_font, text_bytes)
                # Update path with final currentpoint position
                if results:
                    ctxt.gstate.path.append(ps.SubPath())
                    ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))
        else:
            # Execute each character's CharString or Type 3 procedure
            for char_code in text_bytes:
                # save the currentpoint
                currentpoint = copy.copy(ctxt.gstate.currentpoint)

                if font_type == 3:
                    # Type 3 font - execute BuildGlyph or BuildChar procedure
                    try:
                        char_width = font_rendering._render_type3_character(ctxt, current_font, char_code)
                        if char_width is not None:
                            # Type 3 char_width is in character space - transform through FontMatrix
                            font_matrix = current_font.val.get(b'FontMatrix')
                            if font_matrix and font_matrix.TYPE in ps.ARRAY_TYPES:
                                user_width_x, _ = _transform_delta(font_matrix, char_width[0], char_width[1])
                            else:
                                user_width_x = char_width[0]
                            _advance_current_point(ctxt, currentpoint, user_width_x, current_font)

                            # start a new ps.SubPath
                            ctxt.gstate.path.append(ps.SubPath())
                            # add the moveto to the path
                            ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))
                    except Exception:
                        # Skip glyphs that fail to render
                        continue
                elif font_type == 2:
                    # Type 2 (CFF) font - execute Type 2 CharString with bitmap cache
                    try:
                        char_width = font_rendering._render_type2_character(ctxt, current_font, char_code)

                        if char_width is not None:
                            _advance_current_point(ctxt, currentpoint, char_width, current_font)

                            ctxt.gstate.path.append(ps.SubPath())
                            ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))

                    except Exception:
                        continue
                elif font_type == 42:
                    # Type 42 (TrueType) font
                    try:
                        char_width = font_rendering._render_type42_character(ctxt, current_font, char_code)

                        if char_width is not None:
                            _advance_current_point(ctxt, currentpoint, char_width, current_font)

                            ctxt.gstate.path.append(ps.SubPath())
                            ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))
                    except Exception:
                        continue
                else:
                    # Type 1 font - execute CharString with bitmap cache
                    try:
                        char_width = font_rendering._render_type1_character(ctxt, current_font, char_code)

                        if char_width is not None:
                            _advance_current_point(ctxt, currentpoint, char_width, current_font)

                            ctxt.gstate.path.append(ps.SubPath())
                            ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))

                    except Exception:
                        continue

    except Exception as e:
        if type3_actual_text:
            _emit_actual_text_start(ctxt, text_bytes, current_font, type3_start_pos)
            _emit_actual_text_end(ctxt)
        return ps_error.e(ctxt, ps_error.INVALIDFONT, show.__name__)

    # Emit ActualText span for Type 3 TextObjs (after loop so bbox data is available)
    if type3_actual_text:
        _emit_actual_text_start(ctxt, text_bytes, current_font, type3_start_pos)
        _emit_actual_text_end(ctxt)

    # Pop operand
    ostack.pop()

    #restore the current path
    ctxt.gstate.path = cp

    # Notify interactive display to refresh after rendering all glyphs
    if hasattr(ctxt, 'on_paint_callback') and ctxt.on_paint_callback:
        try:
            ctxt.on_paint_callback(ctxt, None)
        except Exception:
            pass


def stringwidth(ctxt, ostack):
    """
    string **stringwidth** wx wy

    PLRM Section 8.2: Calculates the change in the current point that would
    occur if string were given as the operand to **show** with the current font.
    wx and wy are computed by adding together the width vectors of all the
    individual glyphs for string and converting the result to user space.

    **Errors**: **stackunderflow**, **typecheck**, **invalidfont**
    """
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, stringwidth.__name__)

    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, stringwidth.__name__)

    # 3. INVALIDACCESS - Check access permission
    if ostack[-1].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, stringwidth.__name__)

    # 5. STACKOVERFLOW - Check result stack space
    if ctxt.MaxOpStack and len(ostack) + 1 > ctxt.MaxOpStack:
        ps_error.e(ctxt, ps_error.STACKOVERFLOW, stringwidth.__name__)
        return

    text_string = ostack[-1]
    current_font = ctxt.gstate.font

    # Additional validation: check if current font exists
    if current_font is None:
        return ps_error.e(ctxt, ps_error.INVALIDFONT, stringwidth.__name__)

    # Get text content as bytes for character code processing
    text_bytes = text_string.byte_string()
    if isinstance(text_bytes, str):
        text_bytes = text_bytes.encode('latin-1')

    try:
        font_type = current_font.val.get(b'FontType', ps.Int(1)).val
        if font_type == 0:
            total_width_x = _calculate_type0_string_width(text_bytes, current_font, ctxt)
            total_width_y = 0.0
        else:
            total_width_x, total_width_y = _calculate_string_width(text_bytes, current_font, ctxt)
    except Exception:
        return ps_error.e(ctxt, ps_error.INVALIDFONT, stringwidth.__name__)

    # Pop string operand and push width results
    ostack.pop()
    ostack.append(ps.Real(total_width_x))
    ostack.append(ps.Real(total_width_y))


def ashow(ctxt, ostack):
    """
    ax ay string **ashow** -

    PLRM Section 8.2: Paints glyphs for the characters of string in a manner
    similar to **show**; however, while doing so, **ashow** adjusts the width of each
    glyph shown by adding ax to the glyph's x width and ay to its y width, thus
    modifying the spacing between glyphs.

    **Errors**: **stackunderflow**, **typecheck**, **invalidfont**, **nocurrentpoint**
    """
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 3:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, ashow.__name__)

    # 2. TYPECHECK - Check operand types (string ay ax)
    if ostack[-1].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, ashow.__name__)
    if ostack[-2].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, ashow.__name__)
    if ostack[-3].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, ashow.__name__)

    # 3. INVALIDACCESS - Check access permission (string needs read access)
    if ostack[-1].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, ashow.__name__)

    ax = ostack[-3].val
    ay = ostack[-2].val
    text_string = ostack[-1]
    current_font = ctxt.gstate.font

    # Additional validation: check if current font exists
    if current_font is None:
        return ps_error.e(ctxt, ps_error.INVALIDFONT, ashow.__name__)

    # Additional validation: check for current point
    if not hasattr(ctxt.gstate, 'currentpoint') or ctxt.gstate.currentpoint is None:
        return ps_error.e(ctxt, ps_error.NOCURRENTPOINT, ashow.__name__)

    # Get text content as bytes for character code processing
    text_bytes = text_string.byte_string()
    if isinstance(text_bytes, str):
        text_bytes = text_bytes.encode('latin-1')

    # Check device preference from pagedevice dictionary
    page_device = getattr(ctxt.gstate, 'page_device', {})
    if hasattr(page_device, 'TYPE') and page_device.TYPE == ps.T_DICT:
        page_device = page_device.val

    text_mode_obj = page_device.get(b'TextRenderingMode', ps.Name(b'GlyphPaths'))
    text_mode = text_mode_obj.val

    # TextObjs mode: emit TextObj to display list instead of rendering glyph paths
    type3_actual_text = False
    if text_mode == b'TextObjs':
        font_type_check = current_font.val.get(b'FontType', ps.Int(1)).val
        if font_type_check == 3:
            # Type 3: fall through to GlyphPaths rendering
            # Bracket with ActualText for searchability
            type3_actual_text = True
            type3_start_pos = (ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)
            ctxt._type3_dl_start = len(ctxt.display_list) if ctxt.display_list else 0
        elif font_type_check == 0 and not current_font.val.get(b'CMap'):
            # FMapType Type 0: fall through to GlyphPaths with ActualText
            type3_actual_text = True
            type3_start_pos = (ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)
            ctxt._type3_dl_start = len(ctxt.display_list) if ctxt.display_list else 0
        else:
            _ashow_as_text_objs(ctxt, text_bytes, current_font, ax, ay)
            ostack.pop()  # string
            ostack.pop()  # ay
            ostack.pop()  # ax
            # Notify interactive display to refresh
            if hasattr(ctxt, 'on_paint_callback') and ctxt.on_paint_callback:
                try:
                    ctxt.on_paint_callback(ctxt, None)
                except Exception:
                    pass
            return

    # GlyphPaths mode (or Type 3 TextObjs fallthrough): render text as glyph paths

    # preserve the current path
    cp = ctxt.gstate.path

    # setup the current path
    ctxt.gstate.path = ps.Path()
    # start a new ps.SubPath
    ctxt.gstate.path.append(ps.SubPath())
    # add the moveto to the path
    ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))

    try:
        # Check font type once - Type 1 vs Type 3
        font_type = current_font.val.get(b'FontType', ps.Int(1)).val

        # Execute each character's CharString or Type 3 procedure
        for char_code in text_bytes:
            # save the currentpoint
            currentpoint = copy.copy(ctxt.gstate.currentpoint)

            if font_type == 3:
                # Type 3 font - execute BuildGlyph or BuildChar procedure
                try:
                    char_width = font_rendering._render_type3_character(ctxt, current_font, char_code)
                    if char_width is not None:
                        # Type 3 char_width is in character space - transform through FontMatrix
                        font_matrix = current_font.val.get(b'FontMatrix')
                        if font_matrix and font_matrix.TYPE in ps.ARRAY_TYPES:
                            user_width_x, _ = _transform_delta(font_matrix, char_width[0], char_width[1])
                        else:
                            user_width_x = char_width[0]
                        _advance_current_point_with_ashow_spacing(ctxt, currentpoint, user_width_x, current_font, ax, ay)
                except Exception:
                    # Skip glyphs that fail to render
                    continue
            elif font_type == 2:
                try:
                    char_width = font_rendering._render_type2_character(ctxt, current_font, char_code)
                    if char_width is not None:
                        _advance_current_point_with_ashow_spacing(ctxt, currentpoint, char_width, current_font, ax, ay)
                    ctxt.gstate.path.append(ps.SubPath())
                    ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))
                except Exception:
                    continue
            elif font_type == 42:
                try:
                    char_width = font_rendering._render_type42_character(ctxt, current_font, char_code)
                    if char_width is not None:
                        _advance_current_point_with_ashow_spacing(ctxt, currentpoint, char_width, current_font, ax, ay)
                    ctxt.gstate.path.append(ps.SubPath())
                    ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))
                except Exception:
                    continue
            else:
                # Type 1 font - execute CharString with bitmap cache
                try:
                    char_width = font_rendering._render_type1_character(ctxt, current_font, char_code)

                    if char_width is not None:
                        _advance_current_point_with_ashow_spacing(ctxt, currentpoint, char_width, current_font, ax, ay)

                    ctxt.gstate.path.append(ps.SubPath())
                    ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))
                except Exception:
                    continue

    except Exception:
        if type3_actual_text:
            _emit_actual_text_start(ctxt, text_bytes, current_font, type3_start_pos)
            _emit_actual_text_end(ctxt)
        return ps_error.e(ctxt, ps_error.INVALIDFONT, ashow.__name__)

    if type3_actual_text:
        _emit_actual_text_start(ctxt, text_bytes, current_font, type3_start_pos)
        _emit_actual_text_end(ctxt)

    # Pop operands
    ostack.pop()  # string
    ostack.pop()  # ay
    ostack.pop()  # ax

    #restore the current path
    ctxt.gstate.path = cp

    # Notify interactive display to refresh after rendering all glyphs
    if hasattr(ctxt, 'on_paint_callback') and ctxt.on_paint_callback:
        try:
            ctxt.on_paint_callback(ctxt, None)
        except Exception:
            pass


def widthshow(ctxt, ostack):
    """
    cx cy char string **widthshow** -

    PLRM Section 8.2: Paints glyphs for the characters of string in a manner
    similar to **show**; however, while doing so, it adjusts the width of each
    occurrence of the character char's glyph by adding cx to its x width and
    cy to its y width, thus modifying the spacing between it and the next glyph.
    This operator enables fitting a string of text to a specific width by
    adjusting the width of the glyph for a specific character, such as the space
    character.

    **Errors**: **stackunderflow**, **typecheck**, **invalidfont**, **nocurrentpoint**, **rangecheck**
    """
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 4:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, widthshow.__name__)

    # 2. TYPECHECK - Check operand types (string char cy cx)
    if ostack[-1].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, widthshow.__name__)
    if ostack[-2].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, widthshow.__name__)
    if ostack[-3].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, widthshow.__name__)
    if ostack[-4].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, widthshow.__name__)

    # 3. INVALIDACCESS - Check access permission (string needs read access)
    if ostack[-1].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, widthshow.__name__)

    cx = ostack[-4].val
    cy = ostack[-3].val
    char_to_modify = ostack[-2].val
    text_string = ostack[-1]
    current_font = ctxt.gstate.font

    # Additional validation: check if current font exists
    if current_font is None:
        return ps_error.e(ctxt, ps_error.INVALIDFONT, widthshow.__name__)

    # Additional validation: check for current point
    if not hasattr(ctxt.gstate, 'currentpoint') or ctxt.gstate.currentpoint is None:
        return ps_error.e(ctxt, ps_error.NOCURRENTPOINT, widthshow.__name__)

    # Additional validation: char must be valid character code for base fonts (0-255)
    if char_to_modify < 0 or char_to_modify > 255:
        return ps_error.e(ctxt, ps_error.RANGECHECK, widthshow.__name__)

    # Get text content as bytes for character code processing
    text_bytes = text_string.byte_string()
    if isinstance(text_bytes, str):
        text_bytes = text_bytes.encode('latin-1')

    # Check device preference from pagedevice dictionary
    page_device = getattr(ctxt.gstate, 'page_device', {})
    if hasattr(page_device, 'TYPE') and page_device.TYPE == ps.T_DICT:
        page_device = page_device.val

    text_mode_obj = page_device.get(b'TextRenderingMode', ps.Name(b'GlyphPaths'))
    text_mode = text_mode_obj.val

    # TextObjs mode: emit TextObj to display list instead of rendering glyph paths
    type3_actual_text = False
    if text_mode == b'TextObjs':
        font_type_check = current_font.val.get(b'FontType', ps.Int(1)).val
        if font_type_check == 3:
            # Type 3: fall through to GlyphPaths rendering
            # Bracket with ActualText for searchability
            type3_actual_text = True
            type3_start_pos = (ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)
            ctxt._type3_dl_start = len(ctxt.display_list) if ctxt.display_list else 0
        elif font_type_check == 0 and not current_font.val.get(b'CMap'):
            # FMapType Type 0: fall through to GlyphPaths with ActualText
            type3_actual_text = True
            type3_start_pos = (ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)
            ctxt._type3_dl_start = len(ctxt.display_list) if ctxt.display_list else 0
        else:
            _widthshow_as_text_objs(ctxt, text_bytes, current_font, cx, cy, char_to_modify)
            ostack.pop()  # string
            ostack.pop()  # char
            ostack.pop()  # cy
            ostack.pop()  # cx
            # Notify interactive display to refresh
            if hasattr(ctxt, 'on_paint_callback') and ctxt.on_paint_callback:
                try:
                    ctxt.on_paint_callback(ctxt, None)
                except Exception:
                    pass
            return

    # GlyphPaths mode (or Type 3 TextObjs fallthrough): render text as glyph paths

    # preserve the current path
    cp = ctxt.gstate.path

    # setup the current path
    ctxt.gstate.path = ps.Path()
    # start a new ps.SubPath
    ctxt.gstate.path.append(ps.SubPath())
    # add the moveto to the path
    ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))

    try:
        # Check font type once - Type 1 vs Type 3
        font_type = current_font.val.get(b'FontType', ps.Int(1)).val

        # Execute each character's CharString or Type 3 procedure
        for char_code in text_bytes:
            # save the currentpoint
            currentpoint = copy.copy(ctxt.gstate.currentpoint)

            if font_type == 3:
                # Type 3 font - execute BuildGlyph or BuildChar procedure
                try:
                    char_width = font_rendering._render_type3_character(ctxt, current_font, char_code)
                    if char_width is not None:
                        # Type 3 char_width is in character space - transform through FontMatrix
                        font_matrix = current_font.val.get(b'FontMatrix')
                        if font_matrix and font_matrix.TYPE in ps.ARRAY_TYPES:
                            user_width_x, _ = _transform_delta(font_matrix, char_width[0], char_width[1])
                        else:
                            user_width_x = char_width[0]
                        if char_code == char_to_modify:
                            _advance_current_point_with_widthshow_spacing(ctxt, currentpoint, user_width_x, current_font, cx, cy)
                        else:
                            _advance_current_point(ctxt, currentpoint, user_width_x, current_font)
                except Exception:
                    # Skip glyphs that fail to render
                    continue
            elif font_type == 2:
                # Type 2 (CFF) font - execute Type 2 CharString with bitmap cache
                try:
                    char_width = font_rendering._render_type2_character(ctxt, current_font, char_code)

                    if char_width is not None:
                        if char_code == char_to_modify:
                            _advance_current_point_with_widthshow_spacing(ctxt, currentpoint, char_width, current_font, cx, cy)
                        else:
                            _advance_current_point(ctxt, currentpoint, char_width, current_font)

                    ctxt.gstate.path.append(ps.SubPath())
                    ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))
                except Exception:
                    continue
            elif font_type == 42:
                try:
                    char_width = font_rendering._render_type42_character(ctxt, current_font, char_code)

                    if char_width is not None:
                        if char_code == char_to_modify:
                            _advance_current_point_with_widthshow_spacing(ctxt, currentpoint, char_width, current_font, cx, cy)
                        else:
                            _advance_current_point(ctxt, currentpoint, char_width, current_font)

                    ctxt.gstate.path.append(ps.SubPath())
                    ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))
                except Exception:
                    continue
            else:
                # Type 1 font - execute CharString with bitmap cache
                try:
                    char_width = font_rendering._render_type1_character(ctxt, current_font, char_code)

                    if char_width is not None:
                        if char_code == char_to_modify:
                            _advance_current_point_with_widthshow_spacing(ctxt, currentpoint, char_width, current_font, cx, cy)
                        else:
                            _advance_current_point(ctxt, currentpoint, char_width, current_font)

                    ctxt.gstate.path.append(ps.SubPath())
                    ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))
                except Exception:
                    continue

    except Exception:
        if type3_actual_text:
            _emit_actual_text_start(ctxt, text_bytes, current_font, type3_start_pos)
            _emit_actual_text_end(ctxt)
        return ps_error.e(ctxt, ps_error.INVALIDFONT, widthshow.__name__)

    if type3_actual_text:
        _emit_actual_text_start(ctxt, text_bytes, current_font, type3_start_pos)
        _emit_actual_text_end(ctxt)

    # Pop operands
    ostack.pop()  # string
    ostack.pop()  # char
    ostack.pop()  # cy
    ostack.pop()  # cx

    #restore the current path
    ctxt.gstate.path = cp

    # Notify interactive display to refresh after rendering all glyphs
    if hasattr(ctxt, 'on_paint_callback') and ctxt.on_paint_callback:
        try:
            ctxt.on_paint_callback(ctxt, None)
        except Exception:
            pass


def awidthshow(ctxt, ostack):
    """
    cx cy char ax ay string **awidthshow** -

    PLRM Section 8.2: Paints glyphs for the characters of string in a manner
    similar to **show**, but combines the special effects of **ashow** and **widthshow**.
    **awidthshow** adjusts the width of each glyph shown by adding ax to its x width
    and ay to its y width, thus modifying the spacing between glyphs. Furthermore,
    **awidthshow** modifies the width of each occurrence of the glyph for the character
    char by an additional amount (cx, cy). The interpretation of char is as described
    for the **widthshow** operator.

    **Errors**: **stackunderflow**, **typecheck**, **invalidfont**, **nocurrentpoint**, **rangecheck**
    """
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 6:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, awidthshow.__name__)

    # 2. TYPECHECK - Check operand types (string ay ax char cy cx)
    if ostack[-1].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, awidthshow.__name__)
    if ostack[-2].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, awidthshow.__name__)
    if ostack[-3].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, awidthshow.__name__)
    if ostack[-4].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, awidthshow.__name__)
    if ostack[-5].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, awidthshow.__name__)
    if ostack[-6].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, awidthshow.__name__)

    # 3. INVALIDACCESS - Check access permission (string needs read access)
    if ostack[-1].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, awidthshow.__name__)

    cx = ostack[-6].val
    cy = ostack[-5].val
    char_to_modify = ostack[-4].val
    ax = ostack[-3].val
    ay = ostack[-2].val
    text_string = ostack[-1]
    current_font = ctxt.gstate.font

    # Additional validation: check if current font exists
    if current_font is None:
        return ps_error.e(ctxt, ps_error.INVALIDFONT, awidthshow.__name__)

    # Additional validation: check for current point
    if not hasattr(ctxt.gstate, 'currentpoint') or ctxt.gstate.currentpoint is None:
        return ps_error.e(ctxt, ps_error.NOCURRENTPOINT, awidthshow.__name__)

    # Additional validation: char must be valid character code for base fonts (0-255)
    if char_to_modify < 0 or char_to_modify > 255:
        return ps_error.e(ctxt, ps_error.RANGECHECK, awidthshow.__name__)

    # Get text content as bytes for character code processing
    text_bytes = text_string.byte_string()
    if isinstance(text_bytes, str):
        text_bytes = text_bytes.encode('latin-1')

    # Check device preference from pagedevice dictionary
    page_device = getattr(ctxt.gstate, 'page_device', {})
    if hasattr(page_device, 'TYPE') and page_device.TYPE == ps.T_DICT:
        page_device = page_device.val

    text_mode_obj = page_device.get(b'TextRenderingMode', ps.Name(b'GlyphPaths'))
    text_mode = text_mode_obj.val

    # TextObjs mode: emit TextObj to display list instead of rendering glyph paths
    type3_actual_text = False
    if text_mode == b'TextObjs':
        font_type_check = current_font.val.get(b'FontType', ps.Int(1)).val
        if font_type_check == 3:
            # Type 3: fall through to GlyphPaths rendering
            # Bracket with ActualText for searchability
            type3_actual_text = True
            type3_start_pos = (ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)
            ctxt._type3_dl_start = len(ctxt.display_list) if ctxt.display_list else 0
        elif font_type_check == 0 and not current_font.val.get(b'CMap'):
            # FMapType Type 0: fall through to GlyphPaths with ActualText
            type3_actual_text = True
            type3_start_pos = (ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)
            ctxt._type3_dl_start = len(ctxt.display_list) if ctxt.display_list else 0
        else:
            _awidthshow_as_text_objs(ctxt, text_bytes, current_font, ax, ay, cx, cy, char_to_modify)
            ostack.pop()  # string
            ostack.pop()  # ay
            ostack.pop()  # ax
            ostack.pop()  # char
            ostack.pop()  # cy
            ostack.pop()  # cx
            # Notify interactive display to refresh
            if hasattr(ctxt, 'on_paint_callback') and ctxt.on_paint_callback:
                try:
                    ctxt.on_paint_callback(ctxt, None)
                except Exception:
                    pass
            return

    # GlyphPaths mode (or Type 3 TextObjs fallthrough): render text as glyph paths

    # preserve the current path
    cp = ctxt.gstate.path

    # setup the current path
    ctxt.gstate.path = ps.Path()
    # start a new ps.SubPath
    ctxt.gstate.path.append(ps.SubPath())
    # add the moveto to the path
    ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))

    try:
        # Check font type once - Type 1 vs Type 3
        font_type = current_font.val.get(b'FontType', ps.Int(1)).val

        # Execute each character's CharString or Type 3 procedure
        for char_code in text_bytes:
            # save the currentpoint
            currentpoint = copy.copy(ctxt.gstate.currentpoint)

            if font_type == 3:
                # Type 3 font - execute BuildGlyph or BuildChar procedure
                try:
                    char_width = font_rendering._render_type3_character(ctxt, current_font, char_code)
                    if char_width is not None:
                        # Type 3 char_width is in character space - transform through FontMatrix
                        font_matrix = current_font.val.get(b'FontMatrix')
                        if font_matrix and font_matrix.TYPE in ps.ARRAY_TYPES:
                            user_width_x, _ = _transform_delta(font_matrix, char_width[0], char_width[1])
                        else:
                            user_width_x = char_width[0]
                        if char_code == char_to_modify:
                            _advance_current_point_with_awidthshow_spacing(ctxt, currentpoint, user_width_x, current_font, ax, ay, cx, cy)
                        else:
                            _advance_current_point_with_ashow_spacing(ctxt, currentpoint, user_width_x, current_font, ax, ay)
                except Exception:
                    # Skip glyphs that fail to render
                    continue
            elif font_type == 2:
                # Type 2 (CFF) font - execute Type 2 CharString with bitmap cache
                try:
                    char_width = font_rendering._render_type2_character(ctxt, current_font, char_code)

                    if char_width is not None:
                        if char_code == char_to_modify:
                            _advance_current_point_with_awidthshow_spacing(ctxt, currentpoint, char_width, current_font, ax, ay, cx, cy)
                        else:
                            _advance_current_point_with_ashow_spacing(ctxt, currentpoint, char_width, current_font, ax, ay)

                    ctxt.gstate.path.append(ps.SubPath())
                    ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))
                except Exception:
                    continue
            elif font_type == 42:
                try:
                    char_width = font_rendering._render_type42_character(ctxt, current_font, char_code)

                    if char_width is not None:
                        if char_code == char_to_modify:
                            _advance_current_point_with_awidthshow_spacing(ctxt, currentpoint, char_width, current_font, ax, ay, cx, cy)
                        else:
                            _advance_current_point_with_ashow_spacing(ctxt, currentpoint, char_width, current_font, ax, ay)

                    ctxt.gstate.path.append(ps.SubPath())
                    ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))
                except Exception:
                    continue
            else:
                # Type 1 font - execute CharString with bitmap cache
                try:
                    char_width = font_rendering._render_type1_character(ctxt, current_font, char_code)

                    if char_width is not None:
                        if char_code == char_to_modify:
                            _advance_current_point_with_awidthshow_spacing(ctxt, currentpoint, char_width, current_font, ax, ay, cx, cy)
                        else:
                            _advance_current_point_with_ashow_spacing(ctxt, currentpoint, char_width, current_font, ax, ay)

                    ctxt.gstate.path.append(ps.SubPath())
                    ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))
                except Exception:
                    continue

    except Exception:
        if type3_actual_text:
            _emit_actual_text_start(ctxt, text_bytes, current_font, type3_start_pos)
            _emit_actual_text_end(ctxt)
        return ps_error.e(ctxt, ps_error.INVALIDFONT, awidthshow.__name__)

    if type3_actual_text:
        _emit_actual_text_start(ctxt, text_bytes, current_font, type3_start_pos)
        _emit_actual_text_end(ctxt)

    # Pop operands
    ostack.pop()  # string
    ostack.pop()  # ay
    ostack.pop()  # ax
    ostack.pop()  # char
    ostack.pop()  # cy
    ostack.pop()  # cx

    #restore the current path
    ctxt.gstate.path = cp

    # Notify interactive display to refresh after rendering all glyphs
    if hasattr(ctxt, 'on_paint_callback') and ctxt.on_paint_callback:
        try:
            ctxt.on_paint_callback(ctxt, None)
        except Exception:
            pass


def xyshow(ctxt, ostack):
    """
    string numarray **xyshow** -
    string numstring **xyshow** -

    PLRM Section 8.2: Paints glyphs for the characters of string in a manner
    similar to **show**. After painting each glyph, it extracts two successive numbers
    from the array numarray or the encoded number string numstring. These two
    numbers, interpreted in user space, determine the position of the origin of
    the next glyph relative to the origin of the glyph just shown. The first number
    is the x displacement and the second number is the y displacement. In other
    words, the two numbers override the glyph's normal width.

    **Errors**: **stackunderflow**, **typecheck**, **invalidfont**, **nocurrentpoint**, **rangecheck**
    """
    # Validate operands: string numarray|numstring (numarray/numstring is top of stack)
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, xyshow.__name__)

    # 2. TYPECHECK - Check operand types (displacement string)
    if ostack[-1].TYPE not in {ps.T_ARRAY, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, xyshow.__name__)
    if ostack[-2].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, xyshow.__name__)

    # 3. INVALIDACCESS - Check access permissions
    if ostack[-1].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, xyshow.__name__)
    if ostack[-2].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, xyshow.__name__)

    text_string = ostack[-2]
    displacement_array = ostack[-1]
    current_font = ctxt.gstate.font

    # Additional validation: check if current font exists
    if current_font is None:
        return ps_error.e(ctxt, ps_error.INVALIDFONT, xyshow.__name__)

    # Additional validation: check for current point
    if not hasattr(ctxt.gstate, 'currentpoint') or ctxt.gstate.currentpoint is None:
        return ps_error.e(ctxt, ps_error.NOCURRENTPOINT, xyshow.__name__)

    # Get text content as bytes for character code processing
    text_bytes = text_string.byte_string()
    if isinstance(text_bytes, str):
        text_bytes = text_bytes.encode('latin-1')

    # Parse displacement values from array or encoded number string
    try:
        displacement_values = _parse_displacement_values(displacement_array, "xyshow", pairs=True)
    except Exception as e:
        return ps_error.e(ctxt, ps_error.TYPECHECK, xyshow.__name__)

    # Determine character count (may differ from byte count for Type 0 fonts)
    font_type = current_font.val.get(b'FontType', ps.Int(1)).val
    if font_type == 0:
        char_count = _get_type0_char_count(current_font, text_bytes)
    else:
        char_count = len(text_bytes)

    # Check that we have enough displacement pairs for all characters
    if len(displacement_values) < char_count * 2:
        return ps_error.e(ctxt, ps_error.RANGECHECK, xyshow.__name__)

    # Check device preference from pagedevice dictionary
    page_device = getattr(ctxt.gstate, 'page_device', {})
    if hasattr(page_device, 'TYPE') and page_device.TYPE == ps.T_DICT:
        page_device = page_device.val

    text_mode_obj = page_device.get(b'TextRenderingMode', ps.Name(b'GlyphPaths'))
    text_mode = text_mode_obj.val

    # TextObjs mode: emit TextObj to display list instead of rendering glyph paths
    type3_actual_text = False
    if text_mode == b'TextObjs':
        font_type_check = current_font.val.get(b'FontType', ps.Int(1)).val
        if font_type_check == 3:
            # Type 3: fall through to GlyphPaths rendering
            # Bracket with ActualText for searchability
            type3_actual_text = True
            type3_start_pos = (ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)
            ctxt._type3_dl_start = len(ctxt.display_list) if ctxt.display_list else 0
        elif font_type_check == 0 and not current_font.val.get(b'CMap'):
            # FMapType Type 0: fall through to GlyphPaths with ActualText
            type3_actual_text = True
            type3_start_pos = (ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)
            ctxt._type3_dl_start = len(ctxt.display_list) if ctxt.display_list else 0
        else:
            _xyshow_as_text_objs(ctxt, text_bytes, current_font, displacement_values)
            ostack.pop()  # displacement_array
            ostack.pop()  # text_string
            # Notify interactive display to refresh
            if hasattr(ctxt, 'on_paint_callback') and ctxt.on_paint_callback:
                try:
                    ctxt.on_paint_callback(ctxt, None)
                except Exception:
                    pass
            return

    # GlyphPaths mode (or Type 3 TextObjs fallthrough): render text as glyph paths

    # preserve the current path
    cp = ctxt.gstate.path

    # setup the current path
    ctxt.gstate.path = ps.Path()
    # start a new ps.SubPath
    ctxt.gstate.path.append(ps.SubPath())
    # add the moveto to the path
    ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))

    try:
        if font_type == 0:
            # Type 0 composite font — render characters one at a time with advancement
            _xyshow_type0_glyphpaths(ctxt, current_font, text_bytes, displacement_values)
        else:
            # Execute each character's CharString or Type 3 procedure
            for i, char_code in enumerate(text_bytes):
                # save the currentpoint
                currentpoint = copy.copy(ctxt.gstate.currentpoint)

                if font_type == 3:
                    # Type 3 font - execute BuildGlyph or BuildChar procedure
                    try:
                        char_width = font_rendering._render_type3_character(ctxt, current_font, char_code)
                        # Use custom displacement from displacement_values (completely override glyph width)
                        x_displacement = displacement_values[i * 2]
                        y_displacement = displacement_values[i * 2 + 1]
                        _advance_current_point_with_custom_displacement(ctxt, currentpoint, x_displacement, y_displacement)
                    except Exception:
                        # Skip glyphs that fail to render but still advance
                        x_displacement = displacement_values[i * 2]
                        y_displacement = displacement_values[i * 2 + 1]
                        _advance_current_point_with_custom_displacement(ctxt, currentpoint, x_displacement, y_displacement)
                        continue
                elif font_type == 2:
                    # Type 2 (CFF) font - execute Type 2 CharString with bitmap cache
                    try:
                        font_rendering._render_type2_character(ctxt, current_font, char_code)
                    except Exception:
                        pass

                    # Always advance using custom displacement (ignoring char_width)
                    x_displacement = displacement_values[i * 2]
                    y_displacement = displacement_values[i * 2 + 1]
                    _advance_current_point_with_custom_displacement(ctxt, currentpoint, x_displacement, y_displacement)
                elif font_type == 42:
                    try:
                        font_rendering._render_type42_character(ctxt, current_font, char_code)
                    except Exception:
                        pass

                    x_displacement = displacement_values[i * 2]
                    y_displacement = displacement_values[i * 2 + 1]
                    _advance_current_point_with_custom_displacement(ctxt, currentpoint, x_displacement, y_displacement)
                else:
                    # Type 1 font - execute CharString with bitmap cache
                    try:
                        font_rendering._render_type1_character(ctxt, current_font, char_code)
                    except Exception:
                        pass

                    # Always advance using custom displacement (ignoring char_width)
                    x_displacement = displacement_values[i * 2]
                    y_displacement = displacement_values[i * 2 + 1]
                    _advance_current_point_with_custom_displacement(ctxt, currentpoint, x_displacement, y_displacement)

        # start a new ps.SubPath
        ctxt.gstate.path.append(ps.SubPath())
        # add the moveto to the path
        ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))

    except Exception:
        if type3_actual_text:
            _emit_actual_text_start(ctxt, text_bytes, current_font, type3_start_pos)
            _emit_actual_text_end(ctxt)
        return ps_error.e(ctxt, ps_error.INVALIDFONT, xyshow.__name__)

    if type3_actual_text:
        _emit_actual_text_start(ctxt, text_bytes, current_font, type3_start_pos)
        _emit_actual_text_end(ctxt)

    # Pop operands
    ostack.pop()  # displacement_array
    ostack.pop()  # text_string

    #restore the current path
    ctxt.gstate.path = cp

    # Notify interactive display to refresh after rendering all glyphs
    if hasattr(ctxt, 'on_paint_callback') and ctxt.on_paint_callback:
        try:
            ctxt.on_paint_callback(ctxt, None)
        except Exception:
            pass


def xshow(ctxt, ostack):
    """
    string numarray **xshow** -
    string numstring **xshow** -

    PLRM Section 8.2: Similar to **xyshow**; however, for each glyph shown, **xshow**
    extracts only one number from numarray or numstring. It uses that number as
    the x displacement and the value 0 as the y displacement. In all other respects,
    **xshow** behaves the same as **xyshow**.

    **Errors**: **stackunderflow**, **typecheck**, **invalidfont**, **nocurrentpoint**, **rangecheck**
    """
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, xshow.__name__)

    # 2. TYPECHECK - Check operand types (displacement string)
    if ostack[-1].TYPE not in {ps.T_ARRAY, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, xshow.__name__)
    if ostack[-2].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, xshow.__name__)

    # 3. INVALIDACCESS - Check access permissions
    if ostack[-1].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, xshow.__name__)
    if ostack[-2].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, xshow.__name__)

    text_string = ostack[-2]
    width_array = ostack[-1]
    current_font = ctxt.gstate.font

    # Additional validation: check if current font exists
    if current_font is None:
        return ps_error.e(ctxt, ps_error.INVALIDFONT, xshow.__name__)

    # Additional validation: check for current point
    if not hasattr(ctxt.gstate, 'currentpoint') or ctxt.gstate.currentpoint is None:
        return ps_error.e(ctxt, ps_error.NOCURRENTPOINT, xshow.__name__)

    # Get text content as bytes for character code processing
    text_bytes = text_string.byte_string()
    if isinstance(text_bytes, str):
        text_bytes = text_bytes.encode('latin-1')

    # Parse width values from array or encoded number string (single values, not pairs)
    try:
        width_values = _parse_displacement_values(width_array, "xshow", pairs=False)
    except Exception as e:
        return ps_error.e(ctxt, ps_error.TYPECHECK, xshow.__name__)

    # Determine character count (may differ from byte count for Type 0 fonts)
    font_type = current_font.val.get(b'FontType', ps.Int(1)).val
    if font_type == 0:
        char_count = _get_type0_char_count(current_font, text_bytes)
    else:
        char_count = len(text_bytes)

    # Check that we have enough width values for all characters
    if len(width_values) < char_count:
        return ps_error.e(ctxt, ps_error.RANGECHECK, xshow.__name__)

    # Check device preference from pagedevice dictionary
    page_device = getattr(ctxt.gstate, 'page_device', {})
    if hasattr(page_device, 'TYPE') and page_device.TYPE == ps.T_DICT:
        page_device = page_device.val

    text_mode_obj = page_device.get(b'TextRenderingMode', ps.Name(b'GlyphPaths'))
    text_mode = text_mode_obj.val

    # TextObjs mode: emit TextObj to display list instead of rendering glyph paths
    type3_actual_text = False
    if text_mode == b'TextObjs':
        font_type_check = current_font.val.get(b'FontType', ps.Int(1)).val
        if font_type_check == 3:
            # Type 3: fall through to GlyphPaths rendering
            # Bracket with ActualText for searchability
            type3_actual_text = True
            type3_start_pos = (ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)
            ctxt._type3_dl_start = len(ctxt.display_list) if ctxt.display_list else 0
        elif font_type_check == 0 and not current_font.val.get(b'CMap'):
            # FMapType Type 0: fall through to GlyphPaths with ActualText
            type3_actual_text = True
            type3_start_pos = (ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)
            ctxt._type3_dl_start = len(ctxt.display_list) if ctxt.display_list else 0
        else:
            _xshow_as_text_objs(ctxt, text_bytes, current_font, width_values)
            ostack.pop()  # width_array
            ostack.pop()  # text_string
            # Notify interactive display to refresh
            if hasattr(ctxt, 'on_paint_callback') and ctxt.on_paint_callback:
                try:
                    ctxt.on_paint_callback(ctxt, None)
                except Exception:
                    pass
            return

    # GlyphPaths mode (or Type 3 TextObjs fallthrough): render text as glyph paths

    # preserve the current path
    cp = ctxt.gstate.path

    # setup the current path
    ctxt.gstate.path = ps.Path()
    # start a new ps.SubPath
    ctxt.gstate.path.append(ps.SubPath())
    # add the moveto to the path
    ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))

    try:
        if font_type == 0:
            # Type 0 composite font — render characters one at a time with advancement
            _xshow_type0_glyphpaths(ctxt, current_font, text_bytes, width_values)
        else:
            # Execute each character's CharString or Type 3 procedure
            for i, char_code in enumerate(text_bytes):
                # save the currentpoint
                currentpoint = copy.copy(ctxt.gstate.currentpoint)

                if font_type == 3:
                    # Type 3 font - execute BuildGlyph or BuildChar procedure
                    try:
                        char_width = font_rendering._render_type3_character(ctxt, current_font, char_code)
                        # Use custom x displacement from width_values, y displacement = 0
                        x_displacement = width_values[i]
                        y_displacement = 0.0  # xshow uses y displacement = 0
                        _advance_current_point_with_custom_displacement(ctxt, currentpoint, x_displacement, y_displacement)
                    except Exception:
                        # Skip glyphs that fail to render but still advance
                        x_displacement = width_values[i]
                        y_displacement = 0.0
                        _advance_current_point_with_custom_displacement(ctxt, currentpoint, x_displacement, y_displacement)
                        continue
                elif font_type == 2:
                    # Type 2 (CFF) font - execute Type 2 CharString with bitmap cache
                    try:
                        font_rendering._render_type2_character(ctxt, current_font, char_code)
                    except Exception:
                        pass

                    # Always advance using custom displacement (ignoring char_width)
                    x_displacement = width_values[i]
                    y_displacement = 0.0
                    _advance_current_point_with_custom_displacement(ctxt, currentpoint, x_displacement, y_displacement)
                elif font_type == 42:
                    try:
                        font_rendering._render_type42_character(ctxt, current_font, char_code)
                    except Exception:
                        pass

                    x_displacement = width_values[i]
                    y_displacement = 0.0
                    _advance_current_point_with_custom_displacement(ctxt, currentpoint, x_displacement, y_displacement)
                else:
                    # Type 1 font - execute CharString with bitmap cache
                    try:
                        font_rendering._render_type1_character(ctxt, current_font, char_code)
                    except Exception:
                        pass

                    # Always advance using custom displacement (ignoring char_width)
                    x_displacement = width_values[i]
                    y_displacement = 0.0
                    _advance_current_point_with_custom_displacement(ctxt, currentpoint, x_displacement, y_displacement)

        # start a new ps.SubPath
        ctxt.gstate.path.append(ps.SubPath())
        # add the moveto to the path
        ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))

    except Exception:
        if type3_actual_text:
            _emit_actual_text_start(ctxt, text_bytes, current_font, type3_start_pos)
            _emit_actual_text_end(ctxt)
        return ps_error.e(ctxt, ps_error.INVALIDFONT, xshow.__name__)

    if type3_actual_text:
        _emit_actual_text_start(ctxt, text_bytes, current_font, type3_start_pos)
        _emit_actual_text_end(ctxt)

    # Pop operands
    ostack.pop()  # width_array
    ostack.pop()  # text_string

    #restore the current path
    ctxt.gstate.path = cp

    # Notify interactive display to refresh after rendering all glyphs
    if hasattr(ctxt, 'on_paint_callback') and ctxt.on_paint_callback:
        try:
            ctxt.on_paint_callback(ctxt, None)
        except Exception:
            pass


def yshow(ctxt, ostack):
    """
    string numarray **yshow** -
    string numstring **yshow** -

    PLRM Section 8.2: Similar to **xyshow**; however, for each glyph shown, **yshow**
    extracts only one number from numarray or numstring. It uses that number as
    the y displacement and the value 0 as the x displacement. In all other respects,
    **yshow** behaves the same as **xyshow**.

    **Errors**: **stackunderflow**, **typecheck**, **invalidfont**, **nocurrentpoint**, **rangecheck**
    """
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, yshow.__name__)

    # 2. TYPECHECK - Check operand types (displacement string)
    if ostack[-1].TYPE not in {ps.T_ARRAY, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, yshow.__name__)
    if ostack[-2].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, yshow.__name__)

    # 3. INVALIDACCESS - Check access permissions
    if ostack[-1].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, yshow.__name__)
    if ostack[-2].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, yshow.__name__)

    text_string = ostack[-2]
    height_array = ostack[-1]
    current_font = ctxt.gstate.font

    # Additional validation: check if current font exists
    if current_font is None:
        return ps_error.e(ctxt, ps_error.INVALIDFONT, yshow.__name__)

    # Additional validation: check for current point
    if not hasattr(ctxt.gstate, 'currentpoint') or ctxt.gstate.currentpoint is None:
        return ps_error.e(ctxt, ps_error.NOCURRENTPOINT, yshow.__name__)

    # Get text content as bytes for character code processing
    text_bytes = text_string.byte_string()
    if isinstance(text_bytes, str):
        text_bytes = text_bytes.encode('latin-1')

    # Parse height values from array or encoded number string (single values, not pairs)
    try:
        height_values = _parse_displacement_values(height_array, "yshow", pairs=False)
    except Exception as e:
        return ps_error.e(ctxt, ps_error.TYPECHECK, yshow.__name__)

    # Determine character count (may differ from byte count for Type 0 fonts)
    font_type = current_font.val.get(b'FontType', ps.Int(1)).val
    if font_type == 0:
        char_count = _get_type0_char_count(current_font, text_bytes)
    else:
        char_count = len(text_bytes)

    # Check that we have enough height values for all characters
    if len(height_values) < char_count:
        return ps_error.e(ctxt, ps_error.RANGECHECK, yshow.__name__)

    # Check device preference from pagedevice dictionary
    page_device = getattr(ctxt.gstate, 'page_device', {})
    if hasattr(page_device, 'TYPE') and page_device.TYPE == ps.T_DICT:
        page_device = page_device.val

    text_mode_obj = page_device.get(b'TextRenderingMode', ps.Name(b'GlyphPaths'))
    text_mode = text_mode_obj.val

    # TextObjs mode: emit TextObj to display list instead of rendering glyph paths
    type3_actual_text = False
    if text_mode == b'TextObjs':
        font_type_check = current_font.val.get(b'FontType', ps.Int(1)).val
        if font_type_check == 3:
            # Type 3: fall through to GlyphPaths rendering
            # Bracket with ActualText for searchability
            type3_actual_text = True
            type3_start_pos = (ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)
            ctxt._type3_dl_start = len(ctxt.display_list) if ctxt.display_list else 0
        elif font_type_check == 0 and not current_font.val.get(b'CMap'):
            # FMapType Type 0: fall through to GlyphPaths with ActualText
            type3_actual_text = True
            type3_start_pos = (ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)
            ctxt._type3_dl_start = len(ctxt.display_list) if ctxt.display_list else 0
        else:
            _yshow_as_text_objs(ctxt, text_bytes, current_font, height_values)
            ostack.pop()  # height_array
            ostack.pop()  # text_string
            # Notify interactive display to refresh
            if hasattr(ctxt, 'on_paint_callback') and ctxt.on_paint_callback:
                try:
                    ctxt.on_paint_callback(ctxt, None)
                except Exception:
                    pass
            return

    # GlyphPaths mode (or Type 3 TextObjs fallthrough): render text as glyph paths

    # preserve the current path
    cp = ctxt.gstate.path

    # setup the current path
    ctxt.gstate.path = ps.Path()
    # start a new ps.SubPath
    ctxt.gstate.path.append(ps.SubPath())
    # add the moveto to the path
    ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))

    try:
        if font_type == 0:
            # Type 0 composite font — render characters one at a time with advancement
            _yshow_type0_glyphpaths(ctxt, current_font, text_bytes, height_values)
        else:
            # Execute each character's CharString or Type 3 procedure
            for i, char_code in enumerate(text_bytes):
                # save the currentpoint
                currentpoint = copy.copy(ctxt.gstate.currentpoint)

                if font_type == 3:
                    # Type 3 font - execute BuildGlyph or BuildChar procedure
                    try:
                        char_width = font_rendering._render_type3_character(ctxt, current_font, char_code)
                        # Use custom y displacement from height_values, x displacement = 0
                        x_displacement = 0.0  # yshow uses x displacement = 0
                        y_displacement = height_values[i]
                        _advance_current_point_with_custom_displacement(ctxt, currentpoint, x_displacement, y_displacement)
                    except Exception:
                        # Skip glyphs that fail to render but still advance
                        x_displacement = 0.0
                        y_displacement = height_values[i]
                        _advance_current_point_with_custom_displacement(ctxt, currentpoint, x_displacement, y_displacement)
                        continue
                elif font_type == 2:
                    # Type 2 (CFF) font - execute Type 2 CharString with bitmap cache
                    try:
                        font_rendering._render_type2_character(ctxt, current_font, char_code)
                    except Exception:
                        pass

                    # Always advance using custom displacement (ignoring char_width)
                    x_displacement = 0.0
                    y_displacement = height_values[i]
                    _advance_current_point_with_custom_displacement(ctxt, currentpoint, x_displacement, y_displacement)
                elif font_type == 42:
                    try:
                        font_rendering._render_type42_character(ctxt, current_font, char_code)
                    except Exception:
                        pass

                    x_displacement = 0.0
                    y_displacement = height_values[i]
                    _advance_current_point_with_custom_displacement(ctxt, currentpoint, x_displacement, y_displacement)
                else:
                    # Type 1 font - execute CharString with bitmap cache
                    try:
                        font_rendering._render_type1_character(ctxt, current_font, char_code)
                    except Exception:
                        pass

                    # Always advance using custom displacement (ignoring char_width)
                    x_displacement = 0.0
                    y_displacement = height_values[i]
                    _advance_current_point_with_custom_displacement(ctxt, currentpoint, x_displacement, y_displacement)

        # start a new ps.SubPath
        ctxt.gstate.path.append(ps.SubPath())
        # add the moveto to the path
        ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))

    except Exception:
        if type3_actual_text:
            _emit_actual_text_start(ctxt, text_bytes, current_font, type3_start_pos)
            _emit_actual_text_end(ctxt)
        return ps_error.e(ctxt, ps_error.INVALIDFONT, yshow.__name__)

    if type3_actual_text:
        _emit_actual_text_start(ctxt, text_bytes, current_font, type3_start_pos)
        _emit_actual_text_end(ctxt)

    # Pop operands
    ostack.pop()  # height_array
    ostack.pop()  # text_string

    #restore the current path
    ctxt.gstate.path = cp

    # Notify interactive display to refresh after rendering all glyphs
    if hasattr(ctxt, 'on_paint_callback') and ctxt.on_paint_callback:
        try:
            ctxt.on_paint_callback(ctxt, None)
        except Exception:
            pass


def cshow(ctxt, ostack):
    """
    proc string **cshow** -

    PLRM Section 8.2: Invokes proc once for each operation of the character
    mapping algorithm. For each character, pushes wx wy charcode on the
    operand stack and calls proc. **cshow** does not paint glyphs.

    Stack: proc string **cshow** -
    **Errors**: **invalidaccess**, **invalidfont**, **rangecheck**, **stackunderflow**, **typecheck**
    """
    # 1. STACKUNDERFLOW
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, cshow.__name__)

    # 2. TYPECHECK - string must be a string
    if ostack[-1].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, cshow.__name__)

    # 3. TYPECHECK - proc must be an executable array
    if ostack[-2].TYPE not in ps.ARRAY_TYPES or ostack[-2].attrib != ps.ATTRIB_EXEC:
        return ps_error.e(ctxt, ps_error.TYPECHECK, cshow.__name__)

    # 4. INVALIDACCESS
    if ostack[-1].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, cshow.__name__)
    if ostack[-2].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, cshow.__name__)

    # 5. INVALIDFONT
    if ctxt.gstate.font is None:
        return ps_error.e(ctxt, ps_error.INVALIDFONT, cshow.__name__)

    string_obj = ostack[-1]
    proc_obj = ostack[-2]

    # Only set up loop if string is non-empty
    if string_obj.length > 0:
        cshow_loop = ps.Loop(ps.LT_CSHOW)
        cshow_loop.proc = proc_obj
        cshow_loop.obj = copy.copy(string_obj)
        ctxt.e_stack.append(cshow_loop)

    ostack.pop()
    ostack.pop()


def charpath(ctxt, ostack):
    """
    string bool **charpath** -

    PLRM Section 8.2: Obtains the path for the glyph outlines that would result
    if string were shown at the current point using **show**. Instead of painting the
    path, however, **charpath** appends it to the current path. This yields a result
    suitable for general filling, stroking, or clipping.

    The bool operand determines what happens if the glyph path is designed to be
    stroked rather than filled or outlined. If bool is false, **charpath** simply appends
    the glyph path to the current path; the result is suitable only for stroking.
    If bool is true, **charpath** applies the **strokepath** operator to the glyph path;
    the result is suitable for filling or clipping, but not for stroking.

    **Errors**: **stackunderflow**, **typecheck**, **invalidfont**, **nocurrentpoint**, **limitcheck**
    """
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, charpath.__name__)

    # 2. TYPECHECK - Check operand types (bool string)
    if ostack[-1].TYPE != ps.T_BOOL:
        return ps_error.e(ctxt, ps_error.TYPECHECK, charpath.__name__)
    if ostack[-2].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, charpath.__name__)

    # 3. INVALIDACCESS - Check access permissions
    if ostack[-2].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, charpath.__name__)

    text_string = ostack[-2]
    stroke_flag = ostack[-1].val
    current_font = ctxt.gstate.font

    # Additional validation: check if current font exists
    if current_font is None:
        return ps_error.e(ctxt, ps_error.INVALIDFONT, charpath.__name__)

    # Additional validation: check for current point
    if not hasattr(ctxt.gstate, 'currentpoint') or ctxt.gstate.currentpoint is None:
        return ps_error.e(ctxt, ps_error.NOCURRENTPOINT, charpath.__name__)

    # Get text content as bytes for character code processing
    text_bytes = text_string.byte_string()
    if isinstance(text_bytes, str):
        text_bytes = text_bytes.encode('latin-1')

    try:
        font_type = current_font.val.get(b'FontType', ps.Int(1)).val

        if font_type == 0:
            # Type 0 composite font - charpath through CMap
            ctxt._charpath_mode = True
            try:
                results = font_rendering._render_type0_string(ctxt, current_font, text_bytes)
                for char_width, cid in results:
                    currentpoint = copy.copy(ctxt.gstate.currentpoint)
                    if char_width:
                        _advance_current_point(ctxt, currentpoint, char_width, current_font)
            finally:
                if hasattr(ctxt, '_charpath_mode'):
                    delattr(ctxt, '_charpath_mode')
            ostack.pop()  # stroke_flag
            ostack.pop()  # text_string
            return

        if font_type == 42:
            # Type 42 (TrueType) charpath — use the rendering function in charpath mode
            ctxt._charpath_mode = True
            try:
                for char_code in text_bytes:
                    currentpoint = copy.copy(ctxt.gstate.currentpoint)
                    try:
                        char_width = font_rendering._render_type42_character(ctxt, current_font, char_code)
                        if char_width is not None:
                            _advance_current_point(ctxt, currentpoint, char_width, current_font)
                    except Exception:
                        continue
            finally:
                if hasattr(ctxt, '_charpath_mode'):
                    delattr(ctxt, '_charpath_mode')
        else:
            # Process each character to extract glyph paths (Type 1/2)
            for char_code in text_bytes:
                # Get glyph name from font encoding
                glyph_name = font_ops._get_glyph_name(current_font, char_code)

                # Get CharString for this glyph
                encrypted_charstring = font_ops._get_charstring(current_font, glyph_name)
                if encrypted_charstring is None:
                    # Skip missing glyphs, but advance current point by glyph width
                    # This maintains proper text spacing even for missing glyphs
                    try:
                        char_width = 0  # Default width for missing glyphs
                        currentpoint = copy.copy(ctxt.gstate.currentpoint)
                        _advance_current_point(ctxt, currentpoint, char_width, current_font)
                    except:
                        pass
                    continue

                # save the currentpoint before processing glyph
                currentpoint = copy.copy(ctxt.gstate.currentpoint)

                try:
                    # Execute CharString to extract glyph path using existing infrastructure
                    # The key insight: use the existing charstring_to_width but with a special
                    # flag to prevent path clearing in endchar

                    # Set a flag on the context to indicate we're in charpath mode
                    ctxt._charpath_mode = True

                    try:
                        if font_type == 2:
                            # Type 2 (CFF) charpath
                            private_dict = current_font.val.get(b'Private')
                            default_width_x = 0.0
                            nominal_width_x = 0.0
                            local_subrs = []
                            global_subrs = []
                            if private_dict and private_dict.TYPE == ps.T_DICT:
                                dwx = private_dict.val.get(b'defaultWidthX')
                                if dwx and dwx.TYPE in ps.NUMERIC_TYPES:
                                    default_width_x = float(dwx.val)
                                nwx = private_dict.val.get(b'nominalWidthX')
                                if nwx and nwx.TYPE in ps.NUMERIC_TYPES:
                                    nominal_width_x = float(nwx.val)
                                subrs_obj = private_dict.val.get(b'Subrs')
                                if subrs_obj and subrs_obj.TYPE in ps.ARRAY_TYPES:
                                    local_subrs = [s.byte_string() if hasattr(s, 'byte_string') else bytes(s.val) for s in subrs_obj.val]
                            gsubrs_obj = current_font.val.get(b'_cff_global_subrs')
                            if gsubrs_obj and gsubrs_obj.TYPE in ps.ARRAY_TYPES:
                                global_subrs = [s.byte_string() if hasattr(s, 'byte_string') else bytes(s.val) for s in gsubrs_obj.val]
                            char_width = type2_charstring_to_width(
                                encrypted_charstring, ctxt, current_font,
                                default_width_x, nominal_width_x,
                                local_subrs, global_subrs)
                        else:
                            char_width = charstring_to_width(
                                encrypted_charstring, ctxt,
                                current_font.val.get(b'Private'), current_font)
                    finally:
                        # Always clean up the flag
                        if hasattr(ctxt, '_charpath_mode'):
                            delattr(ctxt, '_charpath_mode')

                    # Advance current point by character width (like show would do)
                    if char_width is not None:
                        _advance_current_point(ctxt, currentpoint, char_width, current_font)

                except Exception:
                    # Skip glyphs that fail to process, advance by default width
                    try:
                        char_width = 0  # Default width for failed glyphs
                        _advance_current_point(ctxt, currentpoint, char_width, current_font)
                    except:
                        pass
                    continue

    except Exception:
        return ps_error.e(ctxt, ps_error.INVALIDFONT, charpath.__name__)

    # Pop operands
    ostack.pop()  # stroke_flag
    ostack.pop()  # text_string


# Type 3 Font Support Operators

def setcachedevice(ctxt, ostack):
    """
    wx wy llx lly urx ury **setcachedevice** -

    PLRM Section 5.7: Passes width and bounding box information to the PostScript
    interpreter's font machinery. **setcachedevice** may be executed only within the
    context of the BuildGlyph or BuildChar procedure for a type 3 font.

    All operands are in character coordinate system.

    **Errors**: **stackunderflow**, **typecheck**, **undefined**
    """
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 6:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setcachedevice.__name__)

    # 2. TYPECHECK - Check operand types
    for i in range(6):
        if ostack[-(i+1)].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setcachedevice.__name__)

    # 3. UNDEFINED - Must be in BuildGlyph/BuildChar context
    if not getattr(ctxt, '_in_build_procedure', False):
        return ps_error.e(ctxt, ps_error.UNDEFINED, setcachedevice.__name__)

    # Extract metrics (ALL IN CHARACTER COORDINATE SYSTEM)
    ury = ostack.pop().val  # Upper right Y of bounding box (character coords)
    urx = ostack.pop().val  # Upper right X of bounding box (character coords)
    lly = ostack.pop().val  # Lower left Y of bounding box (character coords)
    llx = ostack.pop().val  # Lower left X of bounding box (character coords)
    wy = ostack.pop().val   # Y component of width vector (character coords)
    wx = ostack.pop().val   # X component of width vector (character coords)

    # Store character metrics in character coordinate system
    # These will need FontMatrix transformation for user space advancement
    ctxt._char_width = (wx, wy)           # Character space coordinates
    ctxt._char_bbox = (llx, lly, urx, ury)  # Character space coordinates

    # Enable graphics state restrictions (required by PLRM)
    ctxt._font_cache_mode = True


def setcachedevice2(ctxt, ostack):
    """
    w0x w0y llx lly urx ury w1x w1y vx vy **setcachedevice2** -

    PLRM Section 5.7: Passes two sets of character metrics to the font machinery
    for writing modes 0 and 1. Level 2 feature.

    All operands are in character coordinate system.

    **Errors**: **stackunderflow**, **typecheck**, **undefined**
    """
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 10:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setcachedevice2.__name__)

    # 2. TYPECHECK - Check operand types
    for i in range(10):
        if ostack[-(i+1)].TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, setcachedevice2.__name__)

    # 3. UNDEFINED - Must be in BuildGlyph/BuildChar context
    if not getattr(ctxt, '_in_build_procedure', False):
        return ps_error.e(ctxt, ps_error.UNDEFINED, setcachedevice2.__name__)

    # Extract metrics (ALL IN CHARACTER COORDINATE SYSTEM)
    vy = ostack.pop().val   # Y component of origin vector (character coords)
    vx = ostack.pop().val   # X component of origin vector (character coords)
    w1y = ostack.pop().val  # Y component of width vector for writing mode 1
    w1x = ostack.pop().val  # X component of width vector for writing mode 1
    ury = ostack.pop().val  # Upper right Y of bounding box (character coords)
    urx = ostack.pop().val  # Upper right X of bounding box (character coords)
    lly = ostack.pop().val  # Lower left Y of bounding box (character coords)
    llx = ostack.pop().val  # Lower left X of bounding box (character coords)
    w0y = ostack.pop().val  # Y component of width vector for writing mode 0
    w0x = ostack.pop().val  # X component of width vector for writing mode 0

    # Store character metrics in character coordinate system
    # For now, only implement writing mode 0 (horizontal text)
    ctxt._char_width = (w0x, w0y)         # Character space coordinates
    ctxt._char_bbox = (llx, lly, urx, ury)  # Character space coordinates
    ctxt._char_width_mode1 = (w1x, w1y)  # Character space coordinates
    ctxt._char_origin_vector = (vx, vy)   # Character space coordinates

    # Enable graphics state restrictions (required by PLRM)
    ctxt._font_cache_mode = True


def setcharwidth(ctxt, ostack):
    """
    wx wy **setcharwidth** -

    PLRM Section 5.7: Passes character width information to the font machinery.
    Used when character includes color operators or other restricted operations.
    Character will not be cached.

    Operands are in character coordinate system.

    **Errors**: **stackunderflow**, **typecheck**, **undefined**
    """
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setcharwidth.__name__)

    # 2. TYPECHECK - Check operand types
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setcharwidth.__name__)
    if ostack[-2].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setcharwidth.__name__)

    # 3. UNDEFINED - Must be in BuildGlyph/BuildChar context
    if not getattr(ctxt, '_in_build_procedure', False):
        return ps_error.e(ctxt, ps_error.UNDEFINED, setcharwidth.__name__)

    # Extract width (IN CHARACTER COORDINATE SYSTEM)
    wy = ostack.pop().val   # Y component of width vector (character coords)
    wx = ostack.pop().val   # X component of width vector (character coords)

    # Store character width in character coordinate system
    # No bounding box, no graphics state restrictions (unlike setcachedevice)
    ctxt._char_width = (wx, wy)  # Character space coordinates
    # Note: No _font_cache_mode = True - setcharwidth allows color operations


# Private helpers - Text measurement & positioning

def _get_type0_char_count(font_dict, text_bytes):
    """Get the number of logical characters in text_bytes for a Type 0 font.

    For CMap-based fonts, decodes through the CMap to count characters.
    For FMapType-based fonts, uses the FMapType byte width.
    """
    cmap_dict = font_dict.val.get(b'CMap')
    if cmap_dict:
        characters, _ = font_rendering._decode_type0_characters(cmap_dict, text_bytes)
        return len(characters)
    # FMapType-based font
    characters, _ = font_rendering._decode_fmap_characters(font_dict, text_bytes)
    return len(characters)

def _decode_type0_chars_for_show(font_dict, text_bytes):
    """Decode a Type 0 font's text bytes into (char_code, font_index, desc_font) tuples.

    Works for both CMap-based and FMapType-based Type 0 fonts.
    """
    cmap_dict = font_dict.val.get(b'CMap')
    fdep_vector = font_dict.val.get(b'FDepVector')
    if not fdep_vector or not fdep_vector.val:
        return [], False

    is_cmap = bool(cmap_dict)
    if is_cmap:
        characters, _ = font_rendering._decode_type0_characters(cmap_dict, text_bytes)
    else:
        characters, _ = font_rendering._decode_fmap_characters(font_dict, text_bytes)

    result = []
    for char_code, font_index in characters:
        if font_index < len(fdep_vector.val):
            desc_font = fdep_vector.val[font_index]
        else:
            desc_font = fdep_vector.val[0]
        result.append((char_code, font_index, desc_font))

    return result, is_cmap


def _render_single_type0_char(ctxt, font_dict, desc_font, char_code, is_cmap):
    """Render a single character from a Type 0 composite font descendant."""
    if desc_font is None:
        return
    if is_cmap:
        font_rendering._render_cidfont_glyph(ctxt, desc_font, char_code, font_dict)
    else:
        desc_font_type = desc_font.val.get(b'FontType', ps.Int(1)).val if desc_font.TYPE == ps.T_DICT else 1
        if desc_font_type == 2:
            font_rendering._render_type2_for_composite(ctxt, desc_font, char_code, font_dict)
        else:
            font_rendering._render_type1_for_composite(ctxt, desc_font, char_code, font_dict)


def _xyshow_type0_glyphpaths(ctxt, font_dict, text_bytes, displacement_values):
    """Render Type 0 xyshow in GlyphPaths mode — one character at a time with advancement."""
    decoded, is_cmap = _decode_type0_chars_for_show(font_dict, text_bytes)
    for i, (char_code, font_index, desc_font) in enumerate(decoded):
        currentpoint = copy.copy(ctxt.gstate.currentpoint)
        try:
            _render_single_type0_char(ctxt, font_dict, desc_font, char_code, is_cmap)
        except Exception:
            pass
        x_displacement = displacement_values[i * 2]
        y_displacement = displacement_values[i * 2 + 1]
        _advance_current_point_with_custom_displacement(ctxt, currentpoint, x_displacement, y_displacement)
        ctxt.gstate.path.append(ps.SubPath())
        ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))


def _xshow_type0_glyphpaths(ctxt, font_dict, text_bytes, width_values):
    """Render Type 0 xshow in GlyphPaths mode — one character at a time with advancement."""
    decoded, is_cmap = _decode_type0_chars_for_show(font_dict, text_bytes)
    for i, (char_code, font_index, desc_font) in enumerate(decoded):
        currentpoint = copy.copy(ctxt.gstate.currentpoint)
        try:
            _render_single_type0_char(ctxt, font_dict, desc_font, char_code, is_cmap)
        except Exception:
            pass
        _advance_current_point_with_custom_displacement(ctxt, currentpoint, width_values[i], 0.0)
        ctxt.gstate.path.append(ps.SubPath())
        ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))


def _yshow_type0_glyphpaths(ctxt, font_dict, text_bytes, height_values):
    """Render Type 0 yshow in GlyphPaths mode — one character at a time with advancement."""
    decoded, is_cmap = _decode_type0_chars_for_show(font_dict, text_bytes)
    for i, (char_code, font_index, desc_font) in enumerate(decoded):
        currentpoint = copy.copy(ctxt.gstate.currentpoint)
        try:
            _render_single_type0_char(ctxt, font_dict, desc_font, char_code, is_cmap)
        except Exception:
            pass
        _advance_current_point_with_custom_displacement(ctxt, currentpoint, 0.0, height_values[i])
        ctxt.gstate.path.append(ps.SubPath())
        ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(ctxt.gstate.currentpoint.x, ctxt.gstate.currentpoint.y)))


def _calculate_string_width(text_bytes, current_font, ctxt):
    """Calculate total width of string in user space (PLRM requirement)"""

    total_width_x = 0.0
    total_width_y = 0.0

    font_type = current_font.val.get(b'FontType', ps.Int(1)).val

    if font_type == 42:
        # Type 42 (TrueType) — look up width from hmtx table
        upem_obj = current_font.val.get(b'_unitsPerEm')
        if upem_obj and upem_obj.TYPE in ps.NUMERIC_TYPES:
            units_per_em = int(upem_obj.val)
        else:
            units_per_em = font_rendering._get_truetype_units_per_em(current_font)
        em_scale = 1.0 / units_per_em if units_per_em > 0 else 0.001

        # Include FontMatrix[0] scaling (after scalefont, e.g., [24 0 0 24 0 0])
        font_matrix = current_font.val.get(b'FontMatrix')
        if font_matrix and font_matrix.TYPE in ps.ARRAY_TYPES and len(font_matrix.val) >= 1:
            fm_scale = font_matrix.val[0].val if font_matrix.val[0].TYPE in ps.NUMERIC_TYPES else 1.0
        else:
            fm_scale = 1.0
        width_scale = em_scale * fm_scale

        charstrings = current_font.val.get(b'CharStrings')

        for char_code in text_bytes:
            glyph_name = font_ops._get_glyph_name(current_font, char_code)
            if glyph_name is None:
                continue
            glyph_name_bytes = glyph_name.encode('latin-1') if isinstance(glyph_name, str) else glyph_name
            gid_obj = charstrings.val.get(glyph_name_bytes) if charstrings else None
            if gid_obj is None:
                continue
            gid = int(gid_obj.val) if gid_obj.TYPE in ps.NUMERIC_TYPES else 0
            advance_width = font_rendering._get_truetype_advance_width(current_font, gid)
            if advance_width is not None:
                total_width_x += advance_width * width_scale

        return total_width_x, total_width_y

    for char_code in text_bytes:
        glyph_name = font_ops._get_glyph_name(current_font, char_code)
        charstring_data = font_ops._get_charstring(current_font, glyph_name)

        if charstring_data:
            try:
                if font_type == 2:
                    # Type 2 (CFF) font
                    private_dict = current_font.val.get(b'Private')
                    default_width_x = 0.0
                    nominal_width_x = 0.0
                    local_subrs = []
                    global_subrs = []

                    if private_dict and private_dict.TYPE == ps.T_DICT:
                        dwx = private_dict.val.get(b'defaultWidthX')
                        if dwx and dwx.TYPE in ps.NUMERIC_TYPES:
                            default_width_x = float(dwx.val)
                        nwx = private_dict.val.get(b'nominalWidthX')
                        if nwx and nwx.TYPE in ps.NUMERIC_TYPES:
                            nominal_width_x = float(nwx.val)
                        subrs_obj = private_dict.val.get(b'Subrs')
                        if subrs_obj and subrs_obj.TYPE in ps.ARRAY_TYPES:
                            local_subrs = [s.byte_string() if hasattr(s, 'byte_string') else bytes(s.val) for s in subrs_obj.val]

                    gsubrs_obj = current_font.val.get(b'_cff_global_subrs')
                    if gsubrs_obj and gsubrs_obj.TYPE in ps.ARRAY_TYPES:
                        global_subrs = [s.byte_string() if hasattr(s, 'byte_string') else bytes(s.val) for s in gsubrs_obj.val]

                    char_width = type2_charstring_to_width(
                        charstring_data, ctxt, current_font,
                        default_width_x, nominal_width_x,
                        local_subrs, global_subrs,
                        width_only=True)
                else:
                    # Type 1 font
                    char_width = charstring_to_width(
                        charstring_data, ctxt,
                        current_font.val.get(b'Private'), current_font, width_only=True)

                if char_width is not None:
                    total_width_x += char_width
                    total_width_y += 0  # Assuming horizontal text

            except (CharStringError, Type2Error):
                default_glyph_width = 0.5
                font_matrix = current_font.val.get(b'FontMatrix')
                if font_matrix and font_matrix.TYPE in ps.ARRAY_TYPES:
                    font_matrix_values = [m.val for m in font_matrix.val]
                    default_user_width = default_glyph_width * font_matrix_values[0]
                    total_width_x += default_user_width
                    total_width_y += 0  # Assuming horizontal text

    return total_width_x, total_width_y


def _calculate_type0_string_width(text_bytes, font_dict, ctxt=None):
    """
    Calculate total width of a string for a Type 0 font in user space.

    Supports both CMap-based (CID) fonts and FMapType-based (legacy) fonts.
    For CMap fonts: decodes through CMap, looks up TrueType hmtx widths.
    For FMapType fonts: decodes through FMapType, uses Type 1 charstring widths.
    """
    cmap_dict = font_dict.val.get(b'CMap')
    fdep_vector = font_dict.val.get(b'FDepVector')

    if not fdep_vector or not fdep_vector.val:
        return 0.0

    if not cmap_dict:
        # FMapType-based font — use Type 1 width lookup
        return _calculate_fmap_type0_string_width(text_bytes, font_dict, ctxt)

    characters, _ = font_rendering._decode_type0_characters(cmap_dict, text_bytes)
    cidfont_dict = fdep_vector.val[0]

    # Get units_per_em for scaling
    units_per_em = font_rendering._get_truetype_units_per_em(cidfont_dict)
    em_scale = 1.0 / units_per_em if units_per_em > 0 else 0.001

    # Get CIDFont FontMatrix scale
    font_matrix = cidfont_dict.val.get(b'FontMatrix')
    if font_matrix and font_matrix.TYPE in ps.ARRAY_TYPES and len(font_matrix.val) >= 1:
        fm_scale = font_matrix.val[0].val if font_matrix.val[0].TYPE in ps.NUMERIC_TYPES else 1.0
    else:
        fm_scale = 1.0

    # Get Type 0 FontMatrix scale
    type0_fm = font_dict.val.get(b'FontMatrix')
    type0_scale = 1.0
    if type0_fm and type0_fm.TYPE in ps.ARRAY_TYPES and len(type0_fm.val) >= 1:
        type0_scale = type0_fm.val[0].val if type0_fm.val[0].TYPE in ps.NUMERIC_TYPES else 1.0

    total_width = 0.0
    for cid, font_index in characters:
        advance_width = font_rendering._get_truetype_advance_width(cidfont_dict, cid)
        if advance_width is not None:
            total_width += advance_width * em_scale * fm_scale * type0_scale
        else:
            total_width += 0.5 * type0_scale

    return total_width


def _calculate_fmap_type0_string_width(text_bytes, font_dict, ctxt=None):
    """
    Calculate total width for an FMapType-based Type 0 font.

    Decodes through FMapType encoding and looks up Type 1 charstring widths
    with composed FontMatrix (descendant × Type 0).
    """
    characters, _ = font_rendering._decode_fmap_characters(font_dict, text_bytes)
    fdep_vector = font_dict.val.get(b'FDepVector')
    if not fdep_vector or not fdep_vector.val:
        return 0.0

    desc_fm = None
    type0_fm = font_dict.val.get(b'FontMatrix')

    total_width = 0.0
    for char_code, font_index in characters:
        if font_index < len(fdep_vector.val):
            desc_font = fdep_vector.val[font_index]
        else:
            desc_font = fdep_vector.val[0]

        if desc_font is None:
            continue

        # Compose FontMatrices for width calculation
        desc_fm_cur = desc_font.val.get(b'FontMatrix')
        composed_fm = font_rendering._compose_font_matrices(desc_fm_cur, type0_fm)

        glyph_name = font_ops._get_glyph_name(desc_font, char_code)
        encrypted_charstring = font_ops._get_charstring(desc_font, glyph_name)
        if encrypted_charstring and ctxt:
            # Temporarily swap FontMatrix for width calculation
            original_fm = desc_font.val.get(b'FontMatrix')
            desc_font.val[b'FontMatrix'] = composed_fm
            try:
                char_width = charstring_to_width(
                    encrypted_charstring, ctxt,
                    desc_font.val.get(b'Private'), desc_font, width_only=True)
                if char_width is not None:
                    total_width += char_width
            except CharStringError:
                pass
            finally:
                if original_fm is not None:
                    desc_font.val[b'FontMatrix'] = original_fm
                else:
                    del desc_font.val[b'FontMatrix']

    return total_width


def _advance_current_point(ctxt, currentpoint, char_width, current_font):
    """Advance current point after character rendering.

    For Type 1 fonts, char_width is already in user space.
    For Type 3 fonts, the caller should pre-**transform** char_width through FontMatrix.
    """
    # Transform user-space width to device-space width
    device_width_x, device_width_y = _transform_delta(ctxt.gstate.CTM, char_width, 0)

    # Update current point
    ctxt.gstate.currentpoint = ps.Point(currentpoint.x + device_width_x, currentpoint.y + device_width_y)


def _advance_current_point_with_ashow_spacing(ctxt, currentpoint, char_width, current_font, ax, ay):
    """
    Advance current point after character rendering with **ashow** spacing

    PLRM: **ashow** adds (ax, ay) to each character's width in user coordinate system,
    not character coordinate system.
    """
    device_width_x, device_width_y = _transform_delta(ctxt.gstate.CTM, char_width, 0)
    device_ax, device_ay = _transform_delta(ctxt.gstate.CTM, ax, ay)
    ctxt.gstate.currentpoint = ps.Point(
        currentpoint.x + device_width_x + device_ax,
        currentpoint.y + device_width_y + device_ay
    )


def _advance_current_point_with_widthshow_spacing(ctxt, currentpoint, char_width, current_font, cx, cy):
    """
    Advance current point after character rendering with **widthshow** spacing

    PLRM: **widthshow** adds (cx, cy) to the width of specific character in user
    coordinate system.
    """
    device_width_x, device_width_y = _transform_delta(ctxt.gstate.CTM, char_width, 0)
    device_cx, device_cy = _transform_delta(ctxt.gstate.CTM, cx, cy)
    ctxt.gstate.currentpoint = ps.Point(
        currentpoint.x + device_width_x + device_cx,
        currentpoint.y + device_width_y + device_cy
    )


def _advance_current_point_with_awidthshow_spacing(ctxt, currentpoint, char_width, current_font, ax, ay, cx, cy):
    """
    Advance current point after character rendering with **awidthshow** spacing

    PLRM: **awidthshow** combines **ashow** and **widthshow** effects.
    """
    device_width_x, device_width_y = _transform_delta(ctxt.gstate.CTM, char_width, 0)
    device_ax, device_ay = _transform_delta(ctxt.gstate.CTM, ax, ay)
    device_cx, device_cy = _transform_delta(ctxt.gstate.CTM, cx, cy)
    ctxt.gstate.currentpoint = ps.Point(
        currentpoint.x + device_width_x + device_ax + device_cx,
        currentpoint.y + device_width_y + device_ay + device_cy
    )


def _advance_current_point_with_custom_displacement(ctxt, currentpoint, x_displacement, y_displacement):
    """
    Advance current point using custom displacement values from **xshow**/**xyshow**/**yshow**

    PLRM: The displacement values are interpreted in user coordinate system and
    completely override the glyph's normal width.
    """
    device_dx, device_dy = _transform_delta(ctxt.gstate.CTM, x_displacement, y_displacement)
    ctxt.gstate.currentpoint = ps.Point(
        currentpoint.x + device_dx,
        currentpoint.y + device_dy
    )


def _parse_displacement_values(displacement_array, operator_name, pairs=False):
    """
    Parse displacement values from array or encoded number string
    """
    if displacement_array.TYPE in ps.ARRAY_TYPES:
        displacement_values = []
        for item in displacement_array.val:
            if item.TYPE in ps.NUMERIC_TYPES:
                displacement_values.append(item.val)
            else:
                raise ValueError(f"Non-numeric value in array for {operator_name}")
        return displacement_values

    elif displacement_array.TYPE == ps.T_STRING:
        displacement_str = displacement_array.val
        if isinstance(displacement_str, bytes):
            displacement_str = displacement_str.decode('latin-1')
        try:
            displacement_values = [float(x) for x in displacement_str.split()]
            return displacement_values
        except ValueError:
            raise ValueError(f"Invalid number format in string for {operator_name}")
    else:
        raise ValueError(f"Invalid operand type for {operator_name}")


# Private helpers - TextObj emission

def _show_as_text_objs(ctxt, text_bytes, font_dict):
    """Process **show** in TextObjs mode."""
    if not text_bytes:
        return

    _emit_text_obj(ctxt, text_bytes, font_dict)

    font_type = font_dict.val.get(b'FontType', ps.Int(1)).val
    if font_type == 0:
        total_width = _calculate_type0_string_width(text_bytes, font_dict, ctxt)
    else:
        total_width, _ = _calculate_string_width(text_bytes, font_dict, ctxt)

    cp = ctxt.gstate.currentpoint
    device_width_x, device_width_y = _transform_delta(ctxt.gstate.CTM, total_width, 0)
    ctxt.gstate.currentpoint = ps.Point(cp.x + device_width_x, cp.y + device_width_y)


def _ashow_as_text_objs(ctxt, text_bytes, font_dict, ax, ay):
    """Process **ashow** in TextObjs mode."""
    if not text_bytes:
        return

    for char_bytes, char_width in _split_text_into_chars(text_bytes, font_dict, ctxt):
        _emit_text_obj(ctxt, char_bytes, font_dict)

        user_dx = char_width + ax
        user_dy = ay

        cp = ctxt.gstate.currentpoint
        device_dx, device_dy = _transform_delta(ctxt.gstate.CTM, user_dx, user_dy)
        ctxt.gstate.currentpoint = ps.Point(cp.x + device_dx, cp.y + device_dy)


def _widthshow_as_text_objs(ctxt, text_bytes, font_dict, cx, cy, char_to_modify):
    """Process **widthshow** in TextObjs mode."""
    if not text_bytes:
        return

    for char_bytes, char_width in _split_text_into_chars(text_bytes, font_dict, ctxt):
        _emit_text_obj(ctxt, char_bytes, font_dict)

        first_byte = char_bytes[0] if char_bytes else -1
        if first_byte == char_to_modify:
            user_dx = char_width + cx
            user_dy = cy
        else:
            user_dx = char_width
            user_dy = 0.0

        cp = ctxt.gstate.currentpoint
        device_dx, device_dy = _transform_delta(ctxt.gstate.CTM, user_dx, user_dy)
        ctxt.gstate.currentpoint = ps.Point(cp.x + device_dx, cp.y + device_dy)


def _awidthshow_as_text_objs(ctxt, text_bytes, font_dict, ax, ay, cx, cy, char_to_modify):
    """Process **awidthshow** in TextObjs mode."""
    if not text_bytes:
        return

    for char_bytes, char_width in _split_text_into_chars(text_bytes, font_dict, ctxt):
        _emit_text_obj(ctxt, char_bytes, font_dict)

        user_dx = char_width + ax
        user_dy = ay
        first_byte = char_bytes[0] if char_bytes else -1
        if first_byte == char_to_modify:
            user_dx += cx
            user_dy += cy

        cp = ctxt.gstate.currentpoint
        device_dx, device_dy = _transform_delta(ctxt.gstate.CTM, user_dx, user_dy)
        ctxt.gstate.currentpoint = ps.Point(cp.x + device_dx, cp.y + device_dy)


def _xyshow_as_text_objs(ctxt, text_bytes, font_dict, displacement_values):
    """Process **xyshow** in TextObjs mode."""
    if not text_bytes:
        return

    chars = _split_text_into_chars(text_bytes, font_dict, ctxt)
    for i, (char_bytes, _) in enumerate(chars):
        _emit_text_obj(ctxt, char_bytes, font_dict)

        x_disp = displacement_values[i * 2]
        y_disp = displacement_values[i * 2 + 1]

        cp = ctxt.gstate.currentpoint
        device_dx, device_dy = _transform_delta(ctxt.gstate.CTM, x_disp, y_disp)
        ctxt.gstate.currentpoint = ps.Point(cp.x + device_dx, cp.y + device_dy)


def _xshow_as_text_objs(ctxt, text_bytes, font_dict, width_values):
    """Process **xshow** in TextObjs mode."""
    if not text_bytes:
        return

    chars = _split_text_into_chars(text_bytes, font_dict, ctxt)
    for i, (char_bytes, _) in enumerate(chars):
        _emit_text_obj(ctxt, char_bytes, font_dict)

        x_disp = width_values[i]
        y_disp = 0.0

        cp = ctxt.gstate.currentpoint
        device_dx, device_dy = _transform_delta(ctxt.gstate.CTM, x_disp, y_disp)
        ctxt.gstate.currentpoint = ps.Point(cp.x + device_dx, cp.y + device_dy)


def _yshow_as_text_objs(ctxt, text_bytes, font_dict, height_values):
    """Process **yshow** in TextObjs mode."""
    if not text_bytes:
        return

    chars = _split_text_into_chars(text_bytes, font_dict, ctxt)
    for i, (char_bytes, _) in enumerate(chars):
        _emit_text_obj(ctxt, char_bytes, font_dict)

        x_disp = 0.0
        y_disp = height_values[i]

        cp = ctxt.gstate.currentpoint
        device_dx, device_dy = _transform_delta(ctxt.gstate.CTM, x_disp, y_disp)
        ctxt.gstate.currentpoint = ps.Point(cp.x + device_dx, cp.y + device_dy)


def _emit_text_obj(ctxt, text_bytes, font_dict):
    """Emit a TextObj to the display list."""
    if ctxt.display_list is None:
        return

    font_name_obj = font_dict.val.get(b'FontName', ps.Name(b'Unknown'))
    font_name = font_name_obj.val if font_name_obj.TYPE == ps.T_NAME else b'Unknown'

    # For Type 0 fonts, convert character codes to CID bytes for PDF Identity-H
    font_type = font_dict.val.get(b'FontType')
    if font_type and font_type.val == 0:
        text_bytes = _encode_type0_as_cid_bytes(text_bytes, font_dict)

    font_size = _compute_device_font_size(font_dict, ctxt.gstate.CTM)
    device_color = _get_current_device_color(ctxt)
    color_space_copy = ctxt.gstate.color_space.copy()
    ctm = [e.val for e in ctxt.gstate.CTM.val]

    # Compute user-space font matrix for non-uniform scaling support.
    # Type 1 fonts use 1000-unit design space (multiply by 1000),
    # Type 0/42 fonts use 1-unit design space (use as-is).
    is_type0_or_42 = font_type and font_type.val in (0, 42)
    fm_unit_scale = 1.0 if is_type0_or_42 else 1000.0
    font_matrix_obj = font_dict.val.get(b'FontMatrix')
    if (font_matrix_obj and font_matrix_obj.TYPE in ps.ARRAY_TYPES
            and len(font_matrix_obj.val) >= 6):
        user_fm = [font_matrix_obj.val[i].val * fm_unit_scale for i in range(6)]
    else:
        user_fm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]

    cp = ctxt.gstate.currentpoint
    start_x = cp.x
    start_y = cp.y

    text_obj = ps.TextObj(
        text=text_bytes,
        start_x=start_x,
        start_y=start_y,
        font_dict=font_dict,
        font_name=font_name,
        font_size=font_size,
        color=tuple(device_color),
        color_space=color_space_copy,
        ctm=ctm,
        font_matrix=user_fm,
    )
    ctxt.display_list.append(text_obj)


def _encode_type0_as_cid_bytes(text_bytes, font_dict):
    """Decode PostScript character codes through CMap and re-encode as 2-byte CIDs."""
    cmap_dict = font_dict.val.get(b'CMap')
    if not cmap_dict:
        return text_bytes

    characters, _ = font_rendering._decode_type0_characters(cmap_dict, text_bytes)

    result = bytearray()
    for cid, _ in characters:
        result.append((cid >> 8) & 0xFF)
        result.append(cid & 0xFF)

    return bytes(result)


def _fmap_text_bytes_to_unicode(text_bytes, font_dict):
    """Convert FMapType Type 0 text bytes to Unicode through descendant fonts.

    Note: For re-encoded fonts (e.g., poppler pdftops output with cXX glyph names),
    the Unicode mapping is approximate — the original ToUnicode CMap from the PDF
    is not preserved in the PostScript output.
    """
    characters, byte_width = font_rendering._decode_fmap_characters(font_dict, text_bytes)
    fdep_vector = font_dict.val.get(b'FDepVector')

    result = []
    for char_code, font_index in characters:
        desc_font = None
        if fdep_vector and fdep_vector.TYPE in ps.ARRAY_TYPES:
            if font_index < len(fdep_vector.val):
                desc_font = fdep_vector.val[font_index]
            elif fdep_vector.val:
                desc_font = fdep_vector.val[0]

        if desc_font is not None:
            glyph_name = font_ops._get_glyph_name(desc_font, char_code)
            unicode_char = glyph_name_to_unicode(glyph_name)
            # Replace null/control chars with space for PDF text extraction
            if unicode_char and ord(unicode_char) < 0x20:
                unicode_char = ' '
            result.append(unicode_char)
        else:
            result.append('\ufffd')
    return ''.join(result)


def _text_bytes_to_unicode(text_bytes, font_dict):
    """Convert text bytes to Unicode using font encoding and glyph name mapping."""
    # FMapType Type 0 fonts use multi-byte encoding through descendant fonts
    font_type_obj = font_dict.val.get(b'FontType')
    if font_type_obj and font_type_obj.val == 0 and not font_dict.val.get(b'CMap'):
        return _fmap_text_bytes_to_unicode(text_bytes, font_dict)

    result = []
    for char_code in text_bytes:
        glyph_name = font_ops._get_glyph_name(font_dict, char_code)
        if glyph_name == b'.notdef' and 0x20 <= char_code <= 0x7E:
            # Encoding lookup failed (null entry) — use ASCII code point directly.
            # Common for Type 3 fonts with empty Encoding arrays that use
            # ASCII-compatible character codes in their charprocs.
            result.append(chr(char_code))
        elif char_code in _TEX_LIGATURE_MAP:
            # TeX OT1 encoding ligature positions (char codes 11-15).
            # dvips fonts may return raw byte values or .notdef as glyph names
            # for these codes — use the known ligature decomposition when the
            # glyph name doesn't resolve to a printable Unicode character.
            unicode_char = glyph_name_to_unicode(glyph_name)
            if not unicode_char or ord(unicode_char) < 0x20 or unicode_char == '\ufffd':
                result.append(_TEX_LIGATURE_MAP[char_code])
            else:
                result.append(unicode_char)
        else:
            unicode_char = glyph_name_to_unicode(glyph_name)
            result.append(unicode_char)
    return ''.join(result)


# TeX OT1 encoding standard ligature positions.
# dvips Type 3 bitmap fonts often have empty Encoding arrays, so these
# char codes map to .notdef despite having valid charprocs.
_TEX_LIGATURE_MAP = {
    11: 'ff',
    12: 'fi',
    13: 'fl',
    14: 'ffi',
    15: 'ffl',
}


def _compute_visual_x_bounds(display_list, dl_start):
    """Compute device-space x bounds from rendered display list elements.

    Scans ImageElement entries (from Type 3 BuildChar cache misses) and path
    elements to find the actual visual extent of rendered text.

    Returns (min_x, max_x) or None if no renderable elements found.
    """
    min_x = float('inf')
    max_x = float('-inf')
    found = False

    for i in range(dl_start, len(display_list)):
        elem = display_list[i]

        if isinstance(elem, ps.ImageElement):
            ctm = elem.CTM
            im = elem.image_matrix
            w, h = elem.width, elem.height
            if not ctm or not im or not w or not h:
                continue
            # Inverse of image matrix: maps image space -> character space
            det_im = im[0] * im[3] - im[1] * im[2]
            if abs(det_im) < 1e-10:
                continue
            inv_a = im[3] / det_im
            inv_c = -im[2] / det_im
            inv_tx = (im[2] * im[5] - im[3] * im[4]) / det_im
            # Transform image corners -> char space -> device space
            for ix, iy in ((0, 0), (w, 0), (0, h), (w, h)):
                cx = ix * inv_a + iy * inv_c + inv_tx
                dx = cx * ctm[0] + ctm[4]  # simplified for cb=0 (horizontal text)
                min_x = min(min_x, dx)
                max_x = max(max_x, dx)
            found = True

        elif isinstance(elem, ps.MoveTo):
            min_x = min(min_x, elem.p.x)
            max_x = max(max_x, elem.p.x)
            found = True
        elif isinstance(elem, ps.LineTo):
            min_x = min(min_x, elem.p.x)
            max_x = max(max_x, elem.p.x)
            found = True
        elif isinstance(elem, ps.CurveTo):
            min_x = min(min_x, elem.p1.x, elem.p2.x, elem.p3.x)
            max_x = max(max_x, elem.p1.x, elem.p2.x, elem.p3.x)
            found = True

    return (min_x, max_x) if found else None


def _emit_actual_text_start(ctxt, text_bytes, font_dict, start_pos=None):
    """Emit ActualTextStart marker to display list for Type 3 font searchability."""
    if ctxt.display_list is None:
        return
    unicode_text = _text_bytes_to_unicode(text_bytes, font_dict)
    font_size = _compute_device_font_size(font_dict, ctxt.gstate.CTM)
    ctm = [e.val for e in ctxt.gstate.CTM.val]
    if start_pos:
        cp = start_pos
    else:
        cp = ctxt.gstate.currentpoint

    font_matrix = font_dict.val.get(b'FontMatrix')
    if font_matrix and font_matrix.TYPE in ps.ARRAY_TYPES and len(font_matrix.val) >= 6:
        fm = [font_matrix.val[i].val for i in range(6)]
    else:
        fm = [0.001, 0, 0, 0.001, 0, 0]

    font_bbox_obj = font_dict.val.get(b'FontBBox')
    if font_bbox_obj and font_bbox_obj.TYPE in ps.ARRAY_TYPES and len(font_bbox_obj.val) >= 4:
        fb = [font_bbox_obj.val[i].val for i in range(4)]
    else:
        fb = None

    if fb is None or (fb[0] == 0 and fb[1] == 0 and fb[2] == 0 and fb[3] == 0):
        bbox_entry = font_rendering._font_max_bbox.get(id(font_dict.val))
        if bbox_entry:
            fb = list(bbox_entry[0])

    if start_pos:
        sx, sy = cp[0], cp[1]
    else:
        sx, sy = cp.x, cp.y

    # Compute visual bounds from rendered display list elements
    visual_start_x = None
    visual_width = 0.0
    dl_start = getattr(ctxt, '_type3_dl_start', None)
    if dl_start is not None and ctxt.display_list:
        bounds = _compute_visual_x_bounds(ctxt.display_list, dl_start)
        if bounds:
            visual_start_x = bounds[0]
            visual_width = bounds[1] - bounds[0]

    # Fallback: use advance width if visual bounds unavailable
    if visual_start_x is None and start_pos and ctxt.gstate.currentpoint:
        visual_width = abs(ctxt.gstate.currentpoint.x - start_pos[0])

    # Capture PS advance width (distance currentpoint moved during show)
    advance_width = 0.0
    if start_pos and ctxt.gstate.currentpoint:
        advance_width = ctxt.gstate.currentpoint.x - start_pos[0]

    ctxt.display_list.append(ps.ActualTextStart(
        unicode_text, sx, sy, font_size, ctm, fm, fb, visual_start_x,
        visual_width, advance_width
    ))


def _emit_actual_text_end(ctxt):
    """Emit ActualTextEnd marker to display list."""
    if ctxt.display_list is None:
        return
    ctxt.display_list.append(ps.ActualTextEnd())


def _compute_device_font_size(font_dict, ctm):
    """Compute effective font size in device space."""
    font_type = font_dict.val.get(b'FontType')
    is_type0 = font_type and font_type.val == 0
    is_type42 = font_type and font_type.val == 42

    font_matrix = font_dict.val.get(b'FontMatrix')
    if font_matrix and font_matrix.TYPE in ps.ARRAY_TYPES and len(font_matrix.val) >= 4:
        fm_scale = font_matrix.val[0].val
    else:
        fm_scale = 0.001

    if is_type0 or is_type42:
        point_size = fm_scale
    else:
        point_size = fm_scale * 1000.0

    ctm_values = [e.val for e in ctm.val]
    a, b, c, d = ctm_values[0], ctm_values[1], ctm_values[2], ctm_values[3]

    scale_x = math.sqrt(a * a + b * b)
    scale_y = math.sqrt(c * c + d * d)
    ctm_scale = math.sqrt(scale_x * scale_y)

    font_size = point_size * ctm_scale
    return font_size


def _get_current_device_color(ctxt):
    """Get current color converted to device RGB space."""
    device_color = color_space.convert_to_device_color(
        ctxt, ctxt.gstate.color, ctxt.gstate.color_space
    )
    if not device_color:
        device_color = [0.0, 0.0, 0.0]
    return device_color


def _split_text_into_chars(text_bytes, font_dict, ctxt):
    """Split text bytes into individual character units with their widths."""
    font_type = font_dict.val.get(b'FontType', ps.Int(1)).val
    if font_type == 0:
        return _split_type0_text(text_bytes, font_dict, ctxt)
    else:
        result = []
        for char_code in text_bytes:
            char_bytes = bytes([char_code])
            char_width, _ = _calculate_string_width(char_bytes, font_dict, ctxt)
            result.append((char_bytes, char_width))
        return result


def _split_type0_text(text_bytes, font_dict, ctxt=None):
    """Split Type 0 text bytes into character units with widths.

    Supports both CMap-based (CID) fonts and FMapType-based (legacy) fonts.
    """
    cmap_dict = font_dict.val.get(b'CMap')
    fdep_vector = font_dict.val.get(b'FDepVector')

    if not fdep_vector or not fdep_vector.val:
        return [(text_bytes, 0.0)]

    if not cmap_dict:
        # FMapType-based font
        characters, byte_width = font_rendering._decode_fmap_characters(font_dict, text_bytes)
        type0_fm = font_dict.val.get(b'FontMatrix')

        result = []
        byte_pos = 0
        for char_code, font_index in characters:
            char_bytes = text_bytes[byte_pos:byte_pos + byte_width]
            byte_pos += byte_width

            # Look up width from Type 1 descendant
            char_width = 0.0
            if font_index < len(fdep_vector.val):
                desc_font = fdep_vector.val[font_index]
            else:
                desc_font = fdep_vector.val[0]

            if desc_font and ctxt:
                desc_fm = desc_font.val.get(b'FontMatrix')
                composed_fm = font_rendering._compose_font_matrices(desc_fm, type0_fm)
                glyph_name = font_ops._get_glyph_name(desc_font, char_code)
                encrypted_charstring = font_ops._get_charstring(desc_font, glyph_name)
                if encrypted_charstring:
                    original_fm = desc_font.val.get(b'FontMatrix')
                    desc_font.val[b'FontMatrix'] = composed_fm
                    try:
                        w = charstring_to_width(
                            encrypted_charstring, ctxt,
                            desc_font.val.get(b'Private'), desc_font, width_only=True)
                        if w is not None:
                            char_width = w
                    except CharStringError:
                        pass
                    finally:
                        if original_fm is not None:
                            desc_font.val[b'FontMatrix'] = original_fm
                        else:
                            del desc_font.val[b'FontMatrix']

            result.append((char_bytes, char_width))
        return result

    # CMap-based font (CID)
    characters, byte_width = font_rendering._decode_type0_characters(cmap_dict, text_bytes)
    cidfont_dict = fdep_vector.val[0]

    units_per_em = font_rendering._get_truetype_units_per_em(cidfont_dict)
    em_scale = 1.0 / units_per_em if units_per_em > 0 else 0.001

    font_matrix = cidfont_dict.val.get(b'FontMatrix')
    if font_matrix and font_matrix.TYPE in ps.ARRAY_TYPES and len(font_matrix.val) >= 1:
        fm_scale = font_matrix.val[0].val if font_matrix.val[0].TYPE in ps.NUMERIC_TYPES else 1.0
    else:
        fm_scale = 1.0

    type0_fm = font_dict.val.get(b'FontMatrix')
    type0_scale = 1.0
    if type0_fm and type0_fm.TYPE in ps.ARRAY_TYPES and len(type0_fm.val) >= 1:
        type0_scale = type0_fm.val[0].val if type0_fm.val[0].TYPE in ps.NUMERIC_TYPES else 1.0

    result = []
    byte_pos = 0
    for cid, font_index in characters:
        char_bytes = text_bytes[byte_pos:byte_pos + byte_width]
        byte_pos += byte_width

        advance_width = font_rendering._get_truetype_advance_width(cidfont_dict, cid)
        if advance_width is not None:
            char_width = advance_width * em_scale * fm_scale * type0_scale
        else:
            char_width = 0.5 * type0_scale

        result.append((char_bytes, char_width))

    return result
