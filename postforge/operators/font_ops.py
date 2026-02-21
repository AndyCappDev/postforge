# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Font Dictionary Operations

Font dictionary manipulation, state management, and basic font utilities.
Operators: scalefont, makefont, setfont, currentfont, rootfont, composefont.
"""

import copy

from ..core import error as ps_error
from ..core import types as ps


def scalefont(ctxt, ostack):
    """
    font **scale** **scalefont** font'

    PLRM Section 8.2: Returns a font whose characters are scaled by **scale** from
    those in font. The value of **scale** is typically a positive number; if **scale**
    is negative, the font is rotated by 180 degrees. **scalefont** does not change
    the original font; it creates a new font.

    **Errors**: **stackunderflow**, **typecheck**, **invalidfont**
    """
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, scalefont.__name__)

    # 2. TYPECHECK - Check operand types (scale_factor font)
    if ostack[-1].TYPE not in ps.NUMERIC_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, scalefont.__name__)
    if ostack[-2].TYPE != ps.T_DICT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, scalefont.__name__)

    # 3. INVALIDACCESS - Check font dictionary access
    if ostack[-2].access < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, scalefont.__name__)

    original_font = ostack[-2]
    scale_factor = ostack[-1].val

    # Additional validation: check if font dictionary is valid
    if not _is_valid_font_dict(original_font):
        return ps_error.e(ctxt, ps_error.INVALIDFONT, scalefont.__name__)

    # Create scaled copy using helper (inputs already validated)
    try:
        new_font = _create_font_copy_in_same_vm(ctxt, original_font, {'scale': scale_factor})
    except Exception:
       return ps_error.e(ctxt, ps_error.INVALIDFONT, scalefont.__name__)

    # Pop operands and push result
    ostack.pop()  # Remove scale factor
    ostack[-1] = new_font  # Replace original font with scaled font


def makefont(ctxt, ostack):
    """
    font matrix **makefont** font'

    PLRM Section 8.2: Returns a font whose characters are transformed from
    those in font by the transformation matrix. matrix must be a 6-element
    array. **makefont** does not change the original font; it creates a new font.

    **Errors**: **stackunderflow**, **typecheck**, **invalidfont**, **rangecheck**
    """
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, makefont.__name__)

    # 2. TYPECHECK - Check operand types (matrix font)
    if ostack[-1].TYPE not in ps.ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, makefont.__name__)
    if ostack[-2].TYPE != ps.T_DICT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, makefont.__name__)

    # 3. INVALIDACCESS - Check access permissions
    if ostack[-1].access < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, makefont.__name__)
    if ostack[-2].access < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, makefont.__name__)

    original_font = ostack[-2]
    transform_matrix = ostack[-1]

    # Additional validation: matrix must be 6 elements
    if len(transform_matrix.val) != 6:
        return ps_error.e(ctxt, ps_error.RANGECHECK, makefont.__name__)

    # Validate all matrix elements are numeric
    for element in transform_matrix.val:
        if element.TYPE not in ps.NUMERIC_TYPES:
            return ps_error.e(ctxt, ps_error.TYPECHECK, makefont.__name__)

    # Additional validation: check if font dictionary is valid
    if not _is_valid_font_dict(original_font):
        return ps_error.e(ctxt, ps_error.INVALIDFONT, makefont.__name__)

    # Extract numeric values from matrix
    matrix_values = [elem.val for elem in transform_matrix.val]

    # Create transformed copy using helper (inputs already validated)
    try:
        new_font = _create_font_copy_in_same_vm(ctxt, original_font, {'matrix': matrix_values})
    except Exception:
        return ps_error.e(ctxt, ps_error.INVALIDFONT, makefont.__name__)

    # Pop operands and push result
    ostack.pop()  # Remove matrix
    ostack[-1] = new_font  # Replace original font with transformed font


def setfont(ctxt, ostack):
    """
    font **setfont** -

    PLRM Section 8.2: Sets the font parameter in the graphics state to font.
    The current font determines the appearance of characters painted by **show**
    and related operators.

    **Errors**: **stackunderflow**, **typecheck**, **invalidfont**
    """
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setfont.__name__)

    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE != ps.T_DICT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setfont.__name__)

    # 3. INVALIDACCESS - Check access permission
    if ostack[-1].access < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, setfont.__name__)

    font_dict = ostack[-1]

    # Additional validation: check if font dictionary is valid
    if not _is_valid_font_dict(font_dict):
        return ps_error.e(ctxt, ps_error.INVALIDFONT, setfont.__name__)

    # Set current font in graphics state
    ctxt.gstate.font = font_dict

    # Pop operand
    ostack.pop()


def currentfont(ctxt, ostack):
    """
    – **currentfont** font|cidfont

    Returns the current font or CIDFont dictionary, based on the font
    parameter in the graphics state. Normally, **currentfont** returns
    the value of the font parameter, as set by **setfont** or
    **selectfont** (and also returned by **rootfont**). However, when
    the font parameter denotes a composite font, and **currentfont** is
    executed inside the BuildGlyph, BuildChar, or CharStrings procedure
    of a descendant base font or CIDFont, **currentfont** returns the
    current descendant base font or CIDFont.

    **Errors**: **stackoverflow**
    **See Also**: **rootfont**, **selectfont**, **setfont**
    """
    # 5. STACKOVERFLOW - Check result stack space
    if ctxt.MaxOpStack and len(ostack) + 1 > ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentfont.__name__)

    # Get current font from graphics state
    current_font = ctxt.gstate.font

    if current_font is None:
        # Return an empty font dictionary if no font is set
        # This matches some PostScript implementations
        empty_font = ps.Dict(ctxt.id, is_global=ctxt.vm_alloc_mode)
        ostack.append(empty_font)
    else:
        ostack.append(current_font)


def rootfont(ctxt, ostack):
    """
    – **rootfont** font

    Returns the root font dictionary from the graphics state. Normally
    identical to **currentfont**. They differ only inside BuildGlyph/BuildChar
    of a descendant base font in a composite font hierarchy, where
    **currentfont** returns the descendant but **rootfont** returns the root.

    Stack: – **rootfont** font
    **Errors**: **stackoverflow**
    """
    if ctxt.MaxOpStack and len(ostack) + 1 > ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, rootfont.__name__)

    current_font = ctxt.gstate.font

    if current_font is None:
        empty_font = ps.Dict(ctxt.id, is_global=ctxt.vm_alloc_mode)
        ostack.append(empty_font)
    else:
        ostack.append(current_font)


def composefont(ctxt, ostack):
    """
    key cmapname array **composefont** font

    PLRM Section 5.11.2: Constructs a Type 0 composite font from a CMap and
    an array of descendant CIDFonts or fonts.

    **Errors**: **stackunderflow**, **typecheck**, **invalidfont**
    """
    if len(ostack) < 3:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, composefont.__name__)

    if ostack[-1].TYPE not in ps.ARRAY_TYPES:
        return ps_error.e(ctxt, ps_error.TYPECHECK, composefont.__name__)
    if ostack[-2].TYPE not in (ps.T_NAME, ps.T_STRING, ps.T_DICT):
        return ps_error.e(ctxt, ps_error.TYPECHECK, composefont.__name__)
    if ostack[-3].TYPE not in (ps.T_NAME, ps.T_STRING):
        return ps_error.e(ctxt, ps_error.TYPECHECK, composefont.__name__)

    fdep_array = ostack[-1]
    cmap_ref = ostack[-2]
    font_key = ostack[-3]

    # Resolve CMap
    if cmap_ref.TYPE == ps.T_DICT:
        cmap_dict = cmap_ref
    else:
        cmap_name = cmap_ref.val if isinstance(cmap_ref.val, bytes) else cmap_ref.val.encode('latin-1')
        cmap_dict = _find_resource(ctxt, cmap_name, b'CMap')
        if cmap_dict is None:
            return ps_error.e(ctxt, ps_error.UNDEFINEDRESOURCE, composefont.__name__)

    # Resolve descendant fonts
    fdep_vector = []
    for item in fdep_array.val:
        if item.TYPE == ps.T_DICT:
            fdep_vector.append(item)
        elif item.TYPE in (ps.T_NAME, ps.T_STRING):
            fname = item.val if isinstance(item.val, bytes) else item.val.encode('latin-1')
            # Try CIDFont category first, then Font
            font = _find_resource(ctxt, fname, b'CIDFont')
            if font is None:
                font = _find_resource(ctxt, fname, b'Font')
            if font is None:
                return ps_error.e(ctxt, ps_error.UNDEFINEDRESOURCE, composefont.__name__)
            fdep_vector.append(font)
        else:
            return ps_error.e(ctxt, ps_error.TYPECHECK, composefont.__name__)

    # Build Type 0 font dictionary
    font_name = font_key.val if isinstance(font_key.val, bytes) else font_key.val.encode('latin-1')
    type0_font = ps.Dict(ctxt.id, is_global=ctxt.vm_alloc_mode)
    type0_font.val[b'FontType'] = ps.Int(0)
    type0_font.val[b'FMapType'] = ps.Int(9)
    fm_arr = ps.Array(ctxt.id)
    fm_arr.val = [ps.Real(1.0), ps.Real(0.0), ps.Real(0.0),
                  ps.Real(1.0), ps.Real(0.0), ps.Real(0.0)]
    fm_arr.length = len(fm_arr.val)
    type0_font.val[b'FontMatrix'] = fm_arr
    # Identity encoding
    enc = ps.Array(ctxt.id)
    enc.val = [ps.Int(i) for i in range(len(fdep_vector))]
    enc.length = len(enc.val)
    type0_font.val[b'Encoding'] = enc
    fdep_arr = ps.Array(ctxt.id)
    fdep_arr.val = list(fdep_vector)
    fdep_arr.length = len(fdep_arr.val)
    type0_font.val[b'FDepVector'] = fdep_arr
    type0_font.val[b'CMap'] = cmap_dict
    type0_font.val[b'FontName'] = ps.Name(font_name)
    # Get WMode from CMap
    wmode = 0
    if cmap_dict.TYPE == ps.T_DICT:
        wmode_obj = cmap_dict.val.get(b'WMode')
        if wmode_obj and wmode_obj.TYPE in ps.NUMERIC_TYPES:
            wmode = wmode_obj.val
    type0_font.val[b'WMode'] = ps.Int(wmode)

    # Register as Font resource
    gvm = ps.global_resources.get_gvm()
    if gvm:
        resource = gvm.val.get(b'resource')
        if resource and resource.TYPE == ps.T_DICT:
            font_cat = resource.val.get(b'Font')
            if font_cat and font_cat.TYPE == ps.T_DICT:
                font_cat.val[font_name] = type0_font

    # Also define in FontDirectory for findfont
    for d in reversed(ctxt.d_stack):
        if d.TYPE == ps.T_DICT and b'FontDirectory' in d.val:
            font_dir = d.val[b'FontDirectory']
            if font_dir.TYPE == ps.T_DICT:
                font_dir.val[font_name] = type0_font
            break

    ostack.pop()  # array
    ostack.pop()  # cmapname
    ostack[-1] = type0_font  # replace key with font


# Helper Functions

def _is_valid_font_dict(font_dict):
    """Check if dictionary is a valid font dictionary"""
    if font_dict.TYPE != ps.T_DICT:
        return False

    font_type = font_dict.val.get(b'FontType', ps.Int(100)).val

    if font_type == 1:
        # Check for required font dictionary keys
        required_keys = [b'FontType', b'FontMatrix', b'FontName']
        for key in required_keys:
            if key not in font_dict.val:
                return False

        return True
    elif font_type == 3:
        # Check for required font dictionary keys
        required_keys = [b'FontType', b'FontMatrix', b'FontBBox', b'Encoding']
        for key in required_keys:
            if key not in font_dict.val:
                return False
        if b'BuildGlyph' not in font_dict.val and b'BuildChar' not in font_dict.val:
            return False

        return True
    elif font_type == 0:
        # Type 0 composite font
        required_keys = [b'FontType', b'FDepVector']
        for key in required_keys:
            if key not in font_dict.val:
                return False
        return True
    elif font_type == 2:
        # Type 2 (CFF) font
        required_keys = [b'FontType', b'FontMatrix', b'FontName', b'CharStrings']
        for key in required_keys:
            if key not in font_dict.val:
                return False
        return True
    elif font_type == 42:
        # Type 42 (TrueType) font
        required_keys = [b'FontType', b'FontMatrix', b'sfnts', b'CharStrings']
        for key in required_keys:
            if key not in font_dict.val:
                return False
        return True
    else:
        return False


def _create_font_copy_in_same_vm(ctxt, original_font, modifications):
    """
    Create font copy preserving original VM allocation mode using deepcopy

    Internal helper function - assumes inputs are already validated by calling operator.
    No additional validation performed here for performance and architectural clarity.

    Args:
        ctxt: PostScript context
        original_font: Original font dictionary
        modifications: Dict containing modifications ('**scale**' or 'matrix')

    Returns:
        New font dictionary with modifications applied
    """
    # Determine original font's VM allocation using is_global
    target_is_global = original_font.is_global

    # Switch to appropriate VM mode
    old_vm_mode = ctxt.vm_alloc_mode
    ctxt.vm_alloc_mode = target_is_global  # True for global, False for local

    try:
        # Create shallow copy of font dictionary - share CharProcs, Encoding,
        # BuildGlyph etc. Only FontMatrix needs to be independent since we modify it.
        # Per PLRM, makefont/scalefont create a new dict with modified FontMatrix
        # but internal structures are shared (not deep-copied).
        new_font = copy.copy(original_font)
        new_font.val = dict(original_font.val)  # shallow copy of dict contents

        # Deep-copy only FontMatrix since we modify it.
        # Use copy.copy() to get a proper Array with correct ctxt_id, then
        # copy the individual numeric elements so we don't mutate the original.
        old_fm = original_font.val[b'FontMatrix']
        new_fm = copy.copy(old_fm)
        new_fm.val = [copy.copy(elem) for elem in old_fm.val]
        new_fm.start = 0
        new_fm.length = len(new_fm.val)
        new_font.val[b'FontMatrix'] = new_fm

        # Remove FID from copy per PLRM (makefont creates a new font identity)
        new_font.val.pop(b'FID', None)

        # Apply specific modification (inputs already validated by calling operator)
        if 'scale' in modifications:
            # scalefont: Scale FontMatrix by scale factor (PLRM: single number)
            scale_factor = modifications['scale']
            for elem in new_fm.val:
                elem.val = elem.val * scale_factor

        elif 'matrix' in modifications:
            # makefont: Concatenate matrix with FontMatrix (PLRM: 6-element array)
            transform_matrix = modifications['matrix']
            old_matrix_values = [m.val for m in old_fm.val]
            new_matrix_values = _compose_matrices(transform_matrix, old_matrix_values)
            for i, elem in enumerate(new_fm.val):
                elem.val = new_matrix_values[i]

        return new_font

    finally:
        ctxt.vm_alloc_mode = old_vm_mode


def _compose_matrices(matrix1, matrix2):
    """
    Compose two 6-element transformation matrices

    PostScript matrices are [a b c d tx ty] representing:
    | a  c  tx |
    | b  d  ty |
    | 0  0  1  |

    Result = matrix1 × matrix2
    """
    a1, b1, c1, d1, tx1, ty1 = matrix1
    a2, b2, c2, d2, tx2, ty2 = matrix2

    # Matrix multiplication
    a = a1 * a2 + c1 * b2
    b = b1 * a2 + d1 * b2
    c = a1 * c2 + c1 * d2
    d = b1 * c2 + d1 * d2
    tx = a1 * tx2 + c1 * ty2 + tx1
    ty = b1 * tx2 + d1 * ty2 + ty1

    return [a, b, c, d, tx, ty]


def _get_glyph_name(font_dict, char_code):
    """Get glyph name from character code using font encoding"""
    encoding = font_dict.val.get(b'Encoding')
    if encoding and encoding.TYPE in ps.ARRAY_TYPES and char_code < len(encoding.val):
        glyph_name_obj = encoding.val[char_code]
        if glyph_name_obj.TYPE == ps.T_NAME:
            return glyph_name_obj.val

    # Fallback to .notdef for missing characters
    return b'.notdef'


def _get_charstring(font_dict, glyph_name):
    """Get encrypted CharString for glyph name"""
    charstrings = font_dict.val.get(b'CharStrings')

    if charstrings and charstrings.TYPE == ps.T_DICT:
        charstring_obj = charstrings.val.get(glyph_name)

        if charstring_obj and charstring_obj.TYPE == ps.T_STRING:
            # Note: String objects use byte_string() to get actual data, not val
            return charstring_obj.byte_string()

    return None


def _find_resource(ctxt, name, category):
    """Look up a resource instance by name and category.

    Searches global then local resource dictionaries.
    Returns the resource instance or None.
    """
    gvm = ps.global_resources.get_gvm()
    if gvm:
        resource = gvm.val.get(b'resource')
        if resource and resource.TYPE == ps.T_DICT:
            cat_dict = resource.val.get(category)
            if cat_dict and cat_dict.TYPE == ps.T_DICT:
                inst = cat_dict.val.get(name)
                if inst is not None:
                    return inst
    # Also check local VM
    if hasattr(ctxt, 'lvm') and ctxt.lvm:
        resource = ctxt.lvm.val.get(b'resource')
        if resource and resource.TYPE == ps.T_DICT:
            cat_dict = resource.val.get(category)
            if cat_dict and cat_dict.TYPE == ps.T_DICT:
                inst = cat_dict.val.get(name)
                if inst is not None:
                    return inst
    return None


def nextfid(ctxt, ostack):
    """
    Internal operator: **.nextfid** fontID

    Generates a new unique fontID object for use as a font's FID entry.
    Called by the Font category DefineResource to implement the PLRM requirement
    that definefont inserts an FID entry of type fontID.
    """
    ostack.append(ps.Font())
