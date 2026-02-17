# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Glyph Rendering Engine

Type-specific glyph rendering, caching, path construction, and TrueType parsing.
Handles Type 1, Type 3, Type 0/CID font rendering.
"""

import copy
import math
import struct

from ..core import error as ps_error
from ..core import types as ps
from ..core import color_space
from ..core.charstring_interpreter import CharStringError, charstring_to_width
from ..core.type2_charstring import Type2Error, type2_charstring_to_width
from ..core.display_list_builder import DisplayListBuilder
from .matrix import _transform_point, _transform_delta
from . import control as ps_control
from . import font_ops


# Per-font max glyph bbox tracker for Type 3 fonts with zero FontBBox.
# Keyed by id(font_dict.val), stores (char_bbox_tuple, height).
# Populated by _render_type3_character, consumed by text_show._emit_actual_text_start.
_font_max_bbox = {}


def _update_font_max_bbox(font_dict, char_bbox):
    """Update max bounding box tracker for a Type 3 font."""
    font_id = id(font_dict.val)
    height = abs(char_bbox[3] - char_bbox[1])
    entry = _font_max_bbox.get(font_id)
    if entry is None or height > entry[1]:
        _font_max_bbox[font_id] = (char_bbox, height)


def _render_type3_character(ctxt, font_dict, char_code):
    """
    Render a Type 3 font character by executing BuildGlyph or BuildChar procedure.

    Implements glyph caching for performance: if a glyph was previously rendered
    with the same font and CTM **scale**/rotation, the cached path is replayed instead
    of re-executing the BuildGlyph/BuildChar procedure.

    Cache eligibility (per PLRM):
    - Glyphs using **setcachedevice** are cacheable (graphics state restricted)
    - Glyphs using **setcharwidth** are NOT cacheable (color operations allowed)

    Args:
        ctxt: PostScript context
        font_dict: Type 3 font dictionary
        char_code: Character code (0-255)

    Returns:
        Tuple (wx, wy) character width in character coordinate system, or None if failed
    """
    from ..core.glyph_cache import make_cache_key, CachedGlyph
    from ..core.types.context import global_resources

    # Get glyph name for cache key (needed for both cache lookup and execution)
    glyph_name = font_ops._get_glyph_name(font_dict, char_code)
    if glyph_name is None:
        return None

    # Create char_selector bytes for cache key.
    # Use char_code directly for BuildChar fonts — different char_codes can produce
    # different glyphs even with the same encoding name (e.g., fonts that use integer
    # keys in charprocs and an uninitialized Encoding array).
    char_selector = bytes([char_code])

    # Check if caching is enabled (can be disabled via --no-cache flag)
    cache_enabled = not global_resources.glyph_cache_disabled

    cache_key = None
    if cache_enabled:
        # Create cache key using current CTM, color, and sub-pixel Y position
        cp = ctxt.gstate.currentpoint
        pos_y = cp.y if cp else 0.0
        cache_key = make_cache_key(font_dict, char_selector, ctxt.gstate.CTM, ctxt.gstate.color, pos_y)
        path_cache = global_resources.get_glyph_cache()

        # Check path cache — this skips BuildGlyph re-execution (the big win)
        cached = path_cache.get(cache_key)
        if cached is not None:
            # Path cache hit — emit GlyphRef for the renderer to blit the bitmap.
            # The renderer will have captured the bitmap on the first occurrence
            # earlier in the display list (GlyphStart/GlyphEnd processed sequentially).
            if cached.char_bbox:
                _update_font_max_bbox(font_dict, cached.char_bbox)
            cp = ctxt.gstate.currentpoint
            if cp and ctxt.display_list is not None:
                ctxt.display_list.append(ps.GlyphRef(cache_key, cp.x, cp.y))
            return cached.char_width

    # Cache miss (or caching disabled) - need to execute BuildGlyph/BuildChar procedure

    # Get BuildGlyph or BuildChar procedure from font dictionary
    build_glyph = font_dict.val.get(b'BuildGlyph')
    build_char = font_dict.val.get(b'BuildChar')

    # Prefer BuildGlyph over BuildChar per PLRM recommendations
    if build_glyph and build_glyph.TYPE in ps.ARRAY_TYPES and build_glyph.attrib == ps.ATTRIB_EXEC:
        # BuildGlyph: expects font and glyph name on stack
        ctxt.o_stack.append(font_dict)
        ctxt.o_stack.append(ps.Name(glyph_name.encode('ascii') if isinstance(glyph_name, str) else glyph_name))
        procedure = build_glyph

    elif build_char and build_char.TYPE in ps.ARRAY_TYPES and build_char.attrib == ps.ATTRIB_EXEC:
        # BuildChar: expects font and character code on stack
        ctxt.o_stack.append(font_dict)
        ctxt.o_stack.append(ps.Int(char_code))
        procedure = build_char

    else:
        # No valid BuildGlyph or BuildChar procedure
        return None

    # Set up Type 3 font execution context
    ctxt._in_build_procedure = True
    ctxt._font_cache_mode = False  # Will be set by setcachedevice if called
    ctxt._char_width = None
    ctxt._char_bbox = None

    # Store the display list length before BuildGlyph execution so we can extract
    # just the glyph's display elements for path caching
    display_list_start = len(ctxt.display_list) if ctxt.display_list else 0

    # Emit GlyphStart marker before BuildGlyph if caching is enabled
    cp = ctxt.gstate.currentpoint
    # Snapshot coordinate values NOW — _setcurrentpoint modifies the Point
    # object in-place during BuildGlyph, so cp.x/cp.y will be wrong later
    cp_x = cp.x if cp else 0.0
    cp_y = cp.y if cp else 0.0
    if cache_enabled and cache_key is not None and cp and ctxt.display_list is not None:
        ctxt.display_list.append(ps.GlyphStart(cache_key, cp_x, cp_y))

    try:
        # PLRM Type 3 font rendering: save graphics state, translate to currentpoint,
        # concat FontMatrix, execute BuildChar, then restore graphics state
        from .graphics_state import gsave, grestore
        from .matrix import translate, concat

        # 1. Save graphics state
        gsave(ctxt, ctxt.o_stack)

        # 2. Translate to current point (where the character should be drawn)
        # NOTE: gstate.currentpoint is in DEVICE space, but translate expects USER space
        # We need to use itransform to convert device coords to user coords
        if cp:
            from .matrix import itransform
            ctxt.o_stack.append(ps.Real(cp_x))
            ctxt.o_stack.append(ps.Real(cp_y))
            itransform(ctxt, ctxt.o_stack)  # Convert device to user space
            # Now stack has user-space x, y - translate uses these
            translate(ctxt, ctxt.o_stack)

        # 3. Concat FontMatrix to scale character coordinates to user space
        font_matrix = font_dict.val.get(b'FontMatrix')
        if font_matrix and font_matrix.TYPE in ps.ARRAY_TYPES:
            ctxt.o_stack.append(font_matrix)
            concat(ctxt, ctxt.o_stack)

        # Push HardReturn onto execution stack first
        ctxt.e_stack.append(ps.HardReturn())

        # Push BuildGlyph/BuildChar procedure onto execution stack
        # IMPORTANT: Must copy the procedure because exec_exec modifies array's
        # start and length fields during execution. Without copy, subsequent
        # character renders would use corrupted procedure state.
        ctxt.e_stack.append(copy.copy(procedure))

        # Execute the procedure
        ps_control.exec_exec(ctxt, ctxt.o_stack, ctxt.e_stack)

        # Get character width before restoring graphics state
        char_width = ctxt._char_width

        # Always emit GlyphEnd to close the capture region (renderer needs this
        # to clear _inside_glyph_capture even for non-cacheable glyphs)
        emitted_glyph_start = cache_enabled and cache_key is not None and cp and ctxt.display_list is not None
        if emitted_glyph_start:
            ctxt.display_list.append(ps.GlyphEnd())

        # Cache in path cache if setcachedevice was called (cacheable glyph)
        if cache_enabled and cache_key is not None and ctxt._font_cache_mode and char_width is not None and ctxt.display_list:
            # Extract display list elements for path cache (between GlyphStart and GlyphEnd)
            glyph_elements = list(ctxt.display_list[display_list_start + 1:-1])

            if glyph_elements:
                normalized_elements = _normalize_display_elements(glyph_elements, cp_x, cp_y)
                cached_glyph = CachedGlyph(
                    display_elements=normalized_elements,
                    char_width=char_width,
                    char_bbox=ctxt._char_bbox,
                    font_dict=font_dict
                )
                path_cache.put(cache_key, cached_glyph)

        # Track max glyph bbox for Type 3 fonts (used for ActualText sizing)
        if ctxt._char_bbox:
            _update_font_max_bbox(font_dict, ctxt._char_bbox)

        # 4. Restore graphics state
        grestore(ctxt, ctxt.o_stack)

        # Return character width if set by setcachedevice or setcharwidth
        return char_width

    except Exception as e:
        # Clean up and return None on error
        # Try to restore graphics state on error
        try:
            from .graphics_state import grestore
            grestore(ctxt, ctxt.o_stack)
        except:
            pass
        return None

    finally:
        # Clean up execution context
        ctxt._in_build_procedure = False
        ctxt._font_cache_mode = False


def _render_type1_character(ctxt, font_dict, char_code):
    """Render a Type 1 font character with bitmap cache support.

    On cache hit: emits GlyphRef to display list, returns char_width.
    On cache miss: emits GlyphStart, executes charstring (which adds Path+Fill
    to display list via endchar), emits GlyphEnd, returns char_width.

    Args:
        ctxt: PostScript context
        font_dict: Type 1 font dictionary
        char_code: Character code (0-255)

    Returns:
        char_width (float) or None if glyph is missing/failed
    """
    from ..core.glyph_cache import make_cache_key, CachedGlyph
    from ..core.types.context import global_resources

    glyph_name = font_ops._get_glyph_name(font_dict, char_code)
    encrypted_charstring = font_ops._get_charstring(font_dict, glyph_name)
    if encrypted_charstring is None:
        return None

    cache_enabled = not global_resources.glyph_cache_disabled

    cache_key = None
    if cache_enabled:
        if isinstance(glyph_name, str):
            char_selector = glyph_name.encode('latin-1')
        else:
            char_selector = glyph_name
        cp = ctxt.gstate.currentpoint
        pos_y = cp.y if cp else 0.0
        cache_key = make_cache_key(font_dict, char_selector, ctxt.gstate.CTM, ctxt.gstate.color, pos_y)
        path_cache = global_resources.get_glyph_cache()

        cached = path_cache.get(cache_key)
        if cached is not None:
            # Path cache hit — emit GlyphRef for renderer bitmap blit
            cp = ctxt.gstate.currentpoint
            if cp and ctxt.display_list is not None:
                ctxt.display_list.append(ps.GlyphRef(cache_key, cp.x, cp.y))
            return cached.char_width

    # Cache miss — execute charstring
    display_list_start = len(ctxt.display_list) if ctxt.display_list else 0

    cp = ctxt.gstate.currentpoint
    if cache_enabled and cache_key is not None and cp and ctxt.display_list is not None:
        ctxt.display_list.append(ps.GlyphStart(cache_key, cp.x, cp.y))

    private_dict = font_dict.val.get(b'Private')
    char_width = charstring_to_width(encrypted_charstring, ctxt, private_dict, font_dict)

    if cache_enabled and cache_key is not None and char_width is not None and ctxt.display_list:
        ctxt.display_list.append(ps.GlyphEnd())

        glyph_elements = list(ctxt.display_list[display_list_start + 1:-1])
        if glyph_elements and cp:
            normalized_elements = _normalize_display_elements(glyph_elements, cp.x, cp.y)
            cached_glyph = CachedGlyph(
                display_elements=normalized_elements,
                char_width=char_width,
                char_bbox=None,
                font_dict=font_dict
            )
            path_cache.put(cache_key, cached_glyph)

    return char_width


def _render_type2_character(ctxt, font_dict, char_code):
    """Render a Type 2 (CFF) font character with bitmap cache support.

    Same GlyphStart/GlyphEnd/GlyphRef cache pattern as _render_type1_character().
    Differences: uses type2_charstring_to_width, extracts defaultWidthX/nominalWidthX
    from Private dict, and gets subroutines from CFF-specific dict entries.

    Args:
        ctxt: PostScript context
        font_dict: Type 2 font dictionary
        char_code: Character code (0-255)

    Returns:
        char_width (float) or None if glyph is missing/failed
    """
    from ..core.glyph_cache import make_cache_key, CachedGlyph
    from ..core.types.context import global_resources

    glyph_name = font_ops._get_glyph_name(font_dict, char_code)
    charstring = font_ops._get_charstring(font_dict, glyph_name)
    if charstring is None:
        return None

    cache_enabled = not global_resources.glyph_cache_disabled

    cache_key = None
    if cache_enabled:
        if isinstance(glyph_name, str):
            char_selector = glyph_name.encode('latin-1')
        else:
            char_selector = glyph_name
        cp = ctxt.gstate.currentpoint
        pos_y = cp.y if cp else 0.0
        cache_key = make_cache_key(font_dict, char_selector, ctxt.gstate.CTM, ctxt.gstate.color, pos_y)
        path_cache = global_resources.get_glyph_cache()

        cached = path_cache.get(cache_key)
        if cached is not None:
            cp = ctxt.gstate.currentpoint
            if cp and ctxt.display_list is not None:
                ctxt.display_list.append(ps.GlyphRef(cache_key, cp.x, cp.y))
            return cached.char_width

    # Cache miss — execute charstring
    display_list_start = len(ctxt.display_list) if ctxt.display_list else 0

    cp = ctxt.gstate.currentpoint
    if cache_enabled and cache_key is not None and cp and ctxt.display_list is not None:
        ctxt.display_list.append(ps.GlyphStart(cache_key, cp.x, cp.y))

    # Extract CFF-specific parameters
    private_dict = font_dict.val.get(b'Private')
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

    gsubrs_obj = font_dict.val.get(b'_cff_global_subrs')
    if gsubrs_obj and gsubrs_obj.TYPE in ps.ARRAY_TYPES:
        global_subrs = [s.byte_string() if hasattr(s, 'byte_string') else bytes(s.val) for s in gsubrs_obj.val]

    width_only = getattr(ctxt, '_width_only_mode', False)

    char_width = type2_charstring_to_width(
        charstring, ctxt, font_dict,
        default_width_x, nominal_width_x,
        local_subrs, global_subrs,
        width_only=width_only)

    if cache_enabled and cache_key is not None and char_width is not None and ctxt.display_list:
        ctxt.display_list.append(ps.GlyphEnd())

        glyph_elements = list(ctxt.display_list[display_list_start + 1:-1])
        if glyph_elements and cp:
            normalized_elements = _normalize_display_elements(glyph_elements, cp.x, cp.y)
            cached_glyph = CachedGlyph(
                display_elements=normalized_elements,
                char_width=char_width,
                char_bbox=None,
                font_dict=font_dict
            )
            path_cache.put(cache_key, cached_glyph)

    return char_width


def _render_type42_character(ctxt, font_dict, char_code):
    """Render a simple (non-CID) Type 42 font character with bitmap cache support.

    Looks up the glyph name from Encoding, maps to GID via CharStrings,
    extracts glyf data from sfnts, and renders the TrueType outline.

    Args:
        ctxt: PostScript context
        font_dict: Type 42 font dictionary
        char_code: Character code (0-255)

    Returns:
        char_width (float) or None if glyph is missing/failed
    """
    from ..core.glyph_cache import make_cache_key, CachedGlyph
    from ..core.types.context import global_resources

    # Get glyph name from Encoding
    glyph_name = font_ops._get_glyph_name(font_dict, char_code)
    if glyph_name is None:
        return None

    # Get GID from CharStrings
    charstrings = font_dict.val.get(b'CharStrings')
    if not charstrings or charstrings.TYPE != ps.T_DICT:
        return None

    if isinstance(glyph_name, str):
        glyph_name_bytes = glyph_name.encode('latin-1')
    else:
        glyph_name_bytes = glyph_name

    gid_obj = charstrings.val.get(glyph_name_bytes)
    if gid_obj is None:
        # Try .notdef
        gid_obj = charstrings.val.get(b'.notdef')
        if gid_obj is None:
            return None

    gid = int(gid_obj.val) if gid_obj.TYPE in ps.NUMERIC_TYPES else 0

    # Cache lookup
    cache_enabled = not global_resources.glyph_cache_disabled
    cache_key = None
    if cache_enabled:
        char_selector = glyph_name_bytes
        cp = ctxt.gstate.currentpoint
        pos_y = cp.y if cp else 0.0
        cache_key = make_cache_key(font_dict, char_selector, ctxt.gstate.CTM, ctxt.gstate.color, pos_y)
        path_cache = global_resources.get_glyph_cache()

        cached = path_cache.get(cache_key)
        if cached is not None:
            cp = ctxt.gstate.currentpoint
            if cp and ctxt.display_list is not None:
                ctxt.display_list.append(ps.GlyphRef(cache_key, cp.x, cp.y))
            return cached.char_width

    # Cache miss — get sfnts data and extract glyf
    font_data = _get_sfnts_data(font_dict)
    if font_data is None:
        return None

    glyf_data = _get_glyf_data_from_sfnts(font_data, gid)

    # Get advance width
    advance_width = _get_truetype_advance_width(font_dict, gid)

    # Get units per em
    upem_obj = font_dict.val.get(b'_unitsPerEm')
    if upem_obj and upem_obj.TYPE in ps.NUMERIC_TYPES:
        units_per_em = int(upem_obj.val)
    else:
        units_per_em = _get_truetype_units_per_em(font_dict)
    em_scale = 1.0 / units_per_em if units_per_em > 0 else 0.001

    if advance_width is None:
        advance_width = 0

    # Width scale: em_scale × FontMatrix[0] (same formula as _render_truetype_glyf)
    # After scalefont, FontMatrix includes the scale factor (e.g., [24 0 0 24 0 0])
    font_matrix = font_dict.val.get(b'FontMatrix')
    if font_matrix and font_matrix.TYPE in ps.ARRAY_TYPES and len(font_matrix.val) >= 1:
        fm_scale = font_matrix.val[0].val if font_matrix.val[0].TYPE in ps.NUMERIC_TYPES else 1.0
    else:
        fm_scale = 1.0
    width_scale = em_scale * fm_scale

    if glyf_data is None or len(glyf_data) < 10:
        # Empty glyph (space, etc.) — just return width
        return advance_width * width_scale

    # Emit GlyphStart for cache capture
    display_list_start = len(ctxt.display_list) if ctxt.display_list else 0
    cp = ctxt.gstate.currentpoint
    if cache_enabled and cache_key is not None and cp and ctxt.display_list is not None:
        ctxt.display_list.append(ps.GlyphStart(cache_key, cp.x, cp.y))

    # Render the glyf data using the existing TrueType renderer
    # For simple Type 42 fonts, there's no CIDFont wrapper — the font_dict itself
    # contains sfnts/FontMatrix. _render_truetype_glyf expects a cidfont_dict with
    # sfnts and FontMatrix, so we pass font_dict directly.
    # type0_font=None since this isn't a composite font.
    char_width = _render_truetype_glyf(
        ctxt, font_dict, gid, glyf_data, type0_font=None,
        glyf_resolver=lambda comp_gid: _get_glyf_data_from_sfnts(font_data, comp_gid))

    # Emit GlyphEnd and cache
    if cache_enabled and cache_key is not None and char_width is not None and ctxt.display_list:
        ctxt.display_list.append(ps.GlyphEnd())
        glyph_elements = list(ctxt.display_list[display_list_start + 1:-1])
        if glyph_elements and cp:
            normalized_elements = _normalize_display_elements(glyph_elements, cp.x, cp.y)
            cached_glyph = CachedGlyph(
                display_elements=normalized_elements,
                char_width=char_width,
                char_bbox=None,
                font_dict=font_dict
            )
            path_cache.put(cache_key, cached_glyph)

    return char_width


def _get_glyf_data_from_sfnts(font_data, gid):
    """Extract glyf table data for a specific GID from raw TrueType font data.

    Parses the table directory to find loca and glyf tables, determines the
    loca format from the head table, and extracts the glyf bytes for the GID.

    Args:
        font_data: Raw TrueType font bytes
        gid: Glyph ID

    Returns:
        bytes of glyf data for this GID, or None if not found/empty
    """
    if len(font_data) < 12:
        return None

    num_tables = struct.unpack_from('>H', font_data, 4)[0]

    head_offset = None
    loca_offset = loca_length = None
    glyf_offset = glyf_length = None

    for i in range(num_tables):
        entry_offset = 12 + i * 16
        if entry_offset + 16 > len(font_data):
            break
        tag = font_data[entry_offset:entry_offset + 4]
        tbl_offset = struct.unpack_from('>I', font_data, entry_offset + 8)[0]
        tbl_length = struct.unpack_from('>I', font_data, entry_offset + 12)[0]
        if tag == b'head':
            head_offset = tbl_offset
        elif tag == b'loca':
            loca_offset = tbl_offset
            loca_length = tbl_length
        elif tag == b'glyf':
            glyf_offset = tbl_offset
            glyf_length = tbl_length

    if head_offset is None or loca_offset is None or glyf_offset is None:
        return None

    # Get indexToLocFormat from head table (offset 50)
    if head_offset + 52 > len(font_data):
        return None
    index_to_loc_format = struct.unpack_from('>h', font_data, head_offset + 50)[0]

    # Read loca entries for gid and gid+1
    if index_to_loc_format == 0:
        # Short format: offsets are uint16, actual offset = value * 2
        entry_off = loca_offset + gid * 2
        if entry_off + 4 > len(font_data):
            return None
        off1 = struct.unpack_from('>H', font_data, entry_off)[0] * 2
        off2 = struct.unpack_from('>H', font_data, entry_off + 2)[0] * 2
    else:
        # Long format: offsets are uint32
        entry_off = loca_offset + gid * 4
        if entry_off + 8 > len(font_data):
            return None
        off1 = struct.unpack_from('>I', font_data, entry_off)[0]
        off2 = struct.unpack_from('>I', font_data, entry_off + 4)[0]

    if off1 == off2:
        return None  # Empty glyph (space, etc.)

    abs_off1 = glyf_offset + off1
    abs_off2 = glyf_offset + off2
    if abs_off2 > len(font_data):
        return None

    return font_data[abs_off1:abs_off2]


def _render_type2_for_composite(ctxt, desc_font, char_code, type0_font):
    """Render a Type 2 (CFF) glyph as a descendant of a Type 0 composite font.

    Composes the descendant's FontMatrix with the Type 0's FontMatrix so that
    type2_charstring_to_width produces paths and widths in user space.

    Returns character width in user space, or None.
    """
    from ..core.glyph_cache import make_cache_key, CachedGlyph
    from ..core.types.context import global_resources

    glyph_name = font_ops._get_glyph_name(desc_font, char_code)
    charstring = font_ops._get_charstring(desc_font, glyph_name)
    if charstring is None:
        return None

    # Compose descendant FontMatrix x Type 0 FontMatrix
    desc_fm = desc_font.val.get(b'FontMatrix')
    type0_fm = type0_font.val.get(b'FontMatrix')
    composed_fm = _compose_font_matrices(desc_fm, type0_fm)

    width_only = getattr(ctxt, '_width_only_mode', False)
    charpath_mode = getattr(ctxt, '_charpath_mode', False)

    cache_enabled = (not global_resources.glyph_cache_disabled
                     and not width_only and not charpath_mode)
    cache_key = None
    if cache_enabled:
        if isinstance(glyph_name, str):
            char_selector = glyph_name.encode('latin-1')
        else:
            char_selector = glyph_name
        cp = ctxt.gstate.currentpoint
        pos_y = cp.y if cp else 0.0
        cache_key = make_cache_key(
            type0_font, char_selector, ctxt.gstate.CTM, ctxt.gstate.color, pos_y)
        path_cache = global_resources.get_glyph_cache()

        cached = path_cache.get(cache_key)
        if cached is not None:
            cp = ctxt.gstate.currentpoint
            if cp and ctxt.display_list is not None:
                ctxt.display_list.append(ps.GlyphRef(cache_key, cp.x, cp.y))
            return cached.char_width

    display_list_start = len(ctxt.display_list) if ctxt.display_list else 0

    cp = ctxt.gstate.currentpoint
    if cache_enabled and cache_key is not None and cp and ctxt.display_list is not None:
        ctxt.display_list.append(ps.GlyphStart(cache_key, cp.x, cp.y))

    # Temporarily swap FontMatrix
    original_fm = desc_font.val.get(b'FontMatrix')
    desc_font.val[b'FontMatrix'] = composed_fm

    try:
        private_dict = desc_font.val.get(b'Private')
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

        gsubrs_obj = desc_font.val.get(b'_cff_global_subrs')
        if gsubrs_obj and gsubrs_obj.TYPE in ps.ARRAY_TYPES:
            global_subrs = [s.byte_string() if hasattr(s, 'byte_string') else bytes(s.val) for s in gsubrs_obj.val]

        char_width = type2_charstring_to_width(
            charstring, ctxt, desc_font,
            default_width_x, nominal_width_x,
            local_subrs, global_subrs,
            width_only=width_only)
    finally:
        if original_fm is not None:
            desc_font.val[b'FontMatrix'] = original_fm
        else:
            del desc_font.val[b'FontMatrix']

    if cache_enabled and cache_key is not None and char_width is not None and ctxt.display_list:
        ctxt.display_list.append(ps.GlyphEnd())
        glyph_elements = list(ctxt.display_list[display_list_start + 1:-1])
        if glyph_elements and cp:
            normalized_elements = _normalize_display_elements(glyph_elements, cp.x, cp.y)
            cached_glyph = CachedGlyph(
                display_elements=normalized_elements,
                char_width=char_width,
                char_bbox=None,
                font_dict=type0_font
            )
            path_cache.put(cache_key, cached_glyph)

    return char_width


def _normalize_display_elements(display_elements, origin_x, origin_y):
    """Normalize display elements so glyph is at origin (0,0).

    Translates coordinates by (-origin_x, -origin_y) so the cached
    glyph can be translated to any position on replay.

    Args:
        display_elements: List of display list elements
        origin_x: X coordinate where glyph was originally rendered
        origin_y: Y coordinate where glyph was originally rendered

    Returns:
        New list of display elements with normalized coordinates
    """
    normalized = []
    for element in display_elements:
        if isinstance(element, ps.Path):
            normalized.append(_translate_path(element, -origin_x, -origin_y))
        elif isinstance(element, (ps.ImageElement, ps.ImageMaskElement, ps.ColorImageElement)):
            elem_copy = copy.copy(element)
            elem_copy.CTM = element.CTM.copy()
            elem_copy.CTM[4] -= origin_x
            elem_copy.CTM[5] -= origin_y
            if element.ctm is not None:
                elem_copy.ctm = element.ctm.copy()
                elem_copy.ctm[4] -= origin_x
                elem_copy.ctm[5] -= origin_y
                if element.ictm is not None:
                    elem_copy.ictm = _calculate_inverse_ctm(elem_copy.ctm)
            normalized.append(elem_copy)
        else:
            normalized.append(element)
    return normalized


def _translate_path(path, dx, dy):
    """Translate all coordinates in a Path by (dx, dy)."""
    translated = ps.Path()
    for subpath in path:
        new_subpath = ps.SubPath()
        for element in subpath:
            new_subpath.append(_translate_path_element(element, dx, dy))
        translated.append(new_subpath)
    return translated


def _translate_path_element(element, dx, dy):
    """Translate a path element by (dx, dy) in device space."""
    if isinstance(element, ps.MoveTo):
        return ps.MoveTo(ps.Point(element.p.x + dx, element.p.y + dy))
    elif isinstance(element, ps.LineTo):
        return ps.LineTo(ps.Point(element.p.x + dx, element.p.y + dy))
    elif isinstance(element, ps.CurveTo):
        return ps.CurveTo(
            ps.Point(element.p1.x + dx, element.p1.y + dy),
            ps.Point(element.p2.x + dx, element.p2.y + dy),
            ps.Point(element.p3.x + dx, element.p3.y + dy)
        )
    elif isinstance(element, ps.ClosePath):
        return ps.ClosePath()
    return element


def _decode_type0_characters(cmap_dict, text_bytes):
    """
    Decode a byte string through a CMap's codespace ranges to produce
    a list of (cid, font_index) tuples.

    For the initial implementation, supports fixed-width codespace ranges
    (1, 2, 3, or 4 byte widths) with CIDRange mappings.
    """
    # Get codespace ranges to determine byte width
    codespace = cmap_dict.val.get(b'CodeSpaceRange')
    byte_width = 1  # default

    if codespace and codespace.TYPE in ps.ARRAY_TYPES:
        # CodeSpaceRange is stored as [lo1 hi1 lo2 hi2 ...]
        # Determine byte width from first range's string length
        if len(codespace.val) >= 2:
            lo = codespace.val[0]
            if lo.TYPE == ps.T_STRING:
                lo_bytes = lo.byte_string()
                if isinstance(lo_bytes, str):
                    lo_bytes = lo_bytes.encode('latin-1')
                byte_width = len(lo_bytes)

    # Get font index (default 0)
    font_index_obj = cmap_dict.val.get(b'CurrentFontNum')
    font_index = font_index_obj.val if font_index_obj and font_index_obj.TYPE in ps.NUMERIC_TYPES else 0

    # Get CID range mappings
    cid_ranges = cmap_dict.val.get(b'CIDRangeMappings')

    # Decode characters
    characters = []
    i = 0
    while i < len(text_bytes):
        # Build character code from bytes
        remaining = len(text_bytes) - i
        char_code = 0
        if remaining >= byte_width:
            for j in range(byte_width):
                char_code = (char_code << 8) | text_bytes[i + j]
            i += byte_width
        else:
            # Short string: pad with leading zeros (left-pad)
            # This handles cshow callbacks that reconstruct single bytes from multi-byte CIDs
            for j in range(remaining):
                char_code = (char_code << 8) | text_bytes[i + j]
            i += remaining

        # Map through CID ranges
        cid = char_code  # Default: identity mapping
        if cid_ranges and cid_ranges.TYPE in ps.ARRAY_TYPES:
            range_vals = cid_ranges.val
            # Ranges are stored as [lo1 hi1 base_cid1 lo2 hi2 base_cid2 ...]
            for k in range(0, len(range_vals) - 2, 3):
                lo = range_vals[k]
                hi = range_vals[k + 1]
                base_cid = range_vals[k + 2]

                # Extract lo/hi values from strings
                lo_val = _string_to_int(lo)
                hi_val = _string_to_int(hi)
                base_cid_val = base_cid.val if base_cid.TYPE in ps.NUMERIC_TYPES else 0

                if lo_val <= char_code <= hi_val:
                    cid = base_cid_val + (char_code - lo_val)
                    break

        characters.append((cid, font_index))

    return characters, byte_width


def _string_to_int(obj):
    """Convert a PS string object to an integer (big-endian byte interpretation)."""
    if obj.TYPE in ps.NUMERIC_TYPES:
        return int(obj.val)
    if obj.TYPE == ps.T_STRING:
        bs = obj.byte_string()
        if isinstance(bs, str):
            bs = bs.encode('latin-1')
        val = 0
        for b in bs:
            val = (val << 8) | b
        return val
    return 0


def _decode_fmap_characters(font_dict, text_bytes):
    """
    Decode a byte string using the FMapType encoding scheme (pre-CIDFont era).

    FMapType 2 (8/8 mapping): consumes bytes in pairs where the first byte
    maps through Encoding to get a font index into FDepVector, and the second
    byte is the character code in the descendant font.

    Returns (characters, byte_width) where characters is [(char_code, font_index), ...]
    and byte_width is the number of bytes per character (2 for FMapType 2).
    """
    fmap_type_obj = font_dict.val.get(b'FMapType')
    fmap_type = fmap_type_obj.val if fmap_type_obj and fmap_type_obj.TYPE in ps.NUMERIC_TYPES else 2

    encoding = font_dict.val.get(b'Encoding')

    if fmap_type == 2:
        # 8/8 mapping: 2-byte characters (font_selector, char_code)
        characters = []
        i = 0
        while i + 1 < len(text_bytes):
            font_selector = text_bytes[i]
            char_code = text_bytes[i + 1]

            # Map font_selector through Encoding to get font index
            font_index = 0
            if encoding and encoding.TYPE in ps.ARRAY_TYPES:
                if font_selector < len(encoding.val):
                    enc_entry = encoding.val[font_selector]
                    if enc_entry.TYPE in ps.NUMERIC_TYPES:
                        font_index = int(enc_entry.val)

            characters.append((char_code, font_index))
            i += 2

        return characters, 2

    if fmap_type == 3:
        # Escape mapping: modal font switching via EscChar byte.
        # EscChar (default 255) followed by a font index byte switches the
        # current descendant font. All other bytes are character codes for
        # the currently selected font. Font 0 is selected at the start.
        esc_obj = font_dict.val.get(b'EscChar')
        esc_char = int(esc_obj.val) if esc_obj and esc_obj.TYPE in ps.NUMERIC_TYPES else 255

        current_font_index = 0
        # Map initial font index through Encoding
        if encoding and encoding.TYPE in ps.ARRAY_TYPES:
            if 0 < len(encoding.val):
                enc_entry = encoding.val[0]
                if enc_entry.TYPE in ps.NUMERIC_TYPES:
                    current_font_index = int(enc_entry.val)

        characters = []
        i = 0
        while i < len(text_bytes):
            b = text_bytes[i]
            if b == esc_char:
                # Next byte is the new font index
                i += 1
                if i < len(text_bytes):
                    new_selector = text_bytes[i]
                    if encoding and encoding.TYPE in ps.ARRAY_TYPES:
                        if new_selector < len(encoding.val):
                            enc_entry = encoding.val[new_selector]
                            if enc_entry.TYPE in ps.NUMERIC_TYPES:
                                current_font_index = int(enc_entry.val)
                            else:
                                current_font_index = new_selector
                        else:
                            current_font_index = new_selector
                    else:
                        current_font_index = new_selector
            else:
                characters.append((b, current_font_index))
            i += 1

        return characters, 1

    # Fallback: treat as single-byte (FMapType 6 = identity, etc.)
    characters = [(b, 0) for b in text_bytes]
    return characters, 1


def _compose_font_matrices(fm1, fm2):
    """
    Compose two 6-element font matrices: result = fm1 * fm2.

    Both are [a b c d tx ty] affine matrices. The composition applies fm1 first,
    then fm2 (i.e., character space → fm1 → fm2 → user space).
    """
    if not fm1 or fm1.TYPE not in ps.ARRAY_TYPES or len(fm1.val) < 6:
        return fm2
    if not fm2 or fm2.TYPE not in ps.ARRAY_TYPES or len(fm2.val) < 6:
        return fm1

    a1 = fm1.val[0].val if fm1.val[0].TYPE in ps.NUMERIC_TYPES else 0.0
    b1 = fm1.val[1].val if fm1.val[1].TYPE in ps.NUMERIC_TYPES else 0.0
    c1 = fm1.val[2].val if fm1.val[2].TYPE in ps.NUMERIC_TYPES else 0.0
    d1 = fm1.val[3].val if fm1.val[3].TYPE in ps.NUMERIC_TYPES else 0.0
    tx1 = fm1.val[4].val if fm1.val[4].TYPE in ps.NUMERIC_TYPES else 0.0
    ty1 = fm1.val[5].val if fm1.val[5].TYPE in ps.NUMERIC_TYPES else 0.0

    a2 = fm2.val[0].val if fm2.val[0].TYPE in ps.NUMERIC_TYPES else 0.0
    b2 = fm2.val[1].val if fm2.val[1].TYPE in ps.NUMERIC_TYPES else 0.0
    c2 = fm2.val[2].val if fm2.val[2].TYPE in ps.NUMERIC_TYPES else 0.0
    d2 = fm2.val[3].val if fm2.val[3].TYPE in ps.NUMERIC_TYPES else 0.0
    tx2 = fm2.val[4].val if fm2.val[4].TYPE in ps.NUMERIC_TYPES else 0.0
    ty2 = fm2.val[5].val if fm2.val[5].TYPE in ps.NUMERIC_TYPES else 0.0

    # Standard 2D affine matrix multiplication
    ra = a1 * a2 + b1 * c2
    rb = a1 * b2 + b1 * d2
    rc = c1 * a2 + d1 * c2
    rd = c1 * b2 + d1 * d2
    rtx = tx1 * a2 + ty1 * c2 + tx2
    rty = tx1 * b2 + ty1 * d2 + ty2

    result = ps.Array(None)
    result.val = [ps.Real(ra), ps.Real(rb), ps.Real(rc), ps.Real(rd),
                  ps.Real(rtx), ps.Real(rty)]
    result.length = 6
    return result


def _render_type1_for_composite(ctxt, desc_font, char_code, type0_font):
    """
    Render a Type 1 glyph as a descendant of a Type 0 composite font.

    Composes the descendant's FontMatrix with the Type 0's FontMatrix so that
    charstring_to_width produces paths and widths in user space that account
    for both matrices. Uses the Type 0 font as cache key (same pattern as
    _render_cidfont_glyph).

    Returns character width in user space, or None.
    """
    from ..core.glyph_cache import make_cache_key, CachedGlyph
    from ..core.types.context import global_resources

    glyph_name = font_ops._get_glyph_name(desc_font, char_code)
    encrypted_charstring = font_ops._get_charstring(desc_font, glyph_name)
    if encrypted_charstring is None:
        return None

    # Compose descendant FontMatrix × Type 0 FontMatrix
    desc_fm = desc_font.val.get(b'FontMatrix')
    type0_fm = type0_font.val.get(b'FontMatrix')
    composed_fm = _compose_font_matrices(desc_fm, type0_fm)

    width_only = getattr(ctxt, '_width_only_mode', False)
    charpath_mode = getattr(ctxt, '_charpath_mode', False)

    # Cache support using Type 0 font as key (carries the full scaling)
    cache_enabled = (not global_resources.glyph_cache_disabled
                     and not width_only and not charpath_mode)
    cache_key = None
    if cache_enabled:
        if isinstance(glyph_name, str):
            char_selector = glyph_name.encode('latin-1')
        else:
            char_selector = glyph_name
        cp = ctxt.gstate.currentpoint
        pos_y = cp.y if cp else 0.0
        cache_key = make_cache_key(
            type0_font, char_selector, ctxt.gstate.CTM, ctxt.gstate.color, pos_y)
        path_cache = global_resources.get_glyph_cache()

        cached = path_cache.get(cache_key)
        if cached is not None:
            cp = ctxt.gstate.currentpoint
            if cp and ctxt.display_list is not None:
                ctxt.display_list.append(ps.GlyphRef(cache_key, cp.x, cp.y))
            return cached.char_width

    # Cache miss — execute charstring with composed FontMatrix
    display_list_start = len(ctxt.display_list) if ctxt.display_list else 0

    cp = ctxt.gstate.currentpoint
    if cache_enabled and cache_key is not None and cp and ctxt.display_list is not None:
        ctxt.display_list.append(ps.GlyphStart(cache_key, cp.x, cp.y))

    # Temporarily swap FontMatrix to the composed version
    original_fm = desc_font.val.get(b'FontMatrix')
    desc_font.val[b'FontMatrix'] = composed_fm
    try:
        private_dict = desc_font.val.get(b'Private')
        char_width = charstring_to_width(
            encrypted_charstring, ctxt, private_dict, desc_font, width_only=width_only)
    finally:
        # Restore original FontMatrix
        if original_fm is not None:
            desc_font.val[b'FontMatrix'] = original_fm
        else:
            del desc_font.val[b'FontMatrix']

    if cache_enabled and cache_key is not None and char_width is not None and ctxt.display_list:
        ctxt.display_list.append(ps.GlyphEnd())
        glyph_elements = list(ctxt.display_list[display_list_start + 1:-1])
        if glyph_elements and cp:
            normalized_elements = _normalize_display_elements(glyph_elements, cp.x, cp.y)
            cached_glyph = CachedGlyph(
                display_elements=normalized_elements,
                char_width=char_width,
                char_bbox=None,
                font_dict=type0_font
            )
            path_cache.put(cache_key, cached_glyph)

    return char_width


def _render_type0_string(ctxt, font_dict, text_bytes):
    """
    Render a string using a Type 0 composite font.

    Decodes through the CMap, then renders each CID glyph from the
    appropriate descendant CIDFont.

    Returns list of (char_width, cid) tuples for advancement.
    """
    cmap_dict = font_dict.val.get(b'CMap')
    fdep_vector = font_dict.val.get(b'FDepVector')

    if not fdep_vector:
        return []

    if not cmap_dict:
        # No CMap — try FMapType-based decoding (legacy Type 0 composite fonts)
        return _render_fmap_type0_string(ctxt, font_dict, text_bytes, fdep_vector)

    characters, byte_width = _decode_type0_characters(cmap_dict, text_bytes)

    # Get Type 0 font's FontMatrix for transforming descendant widths
    type0_fm = font_dict.val.get(b'FontMatrix')
    type0_scale = 1.0
    if type0_fm and type0_fm.TYPE in ps.ARRAY_TYPES and len(type0_fm.val) >= 1:
        type0_scale = type0_fm.val[0].val if type0_fm.val[0].TYPE in ps.NUMERIC_TYPES else 1.0

    results = []
    for cid, font_index in characters:
        # Get descendant font
        if font_index < len(fdep_vector.val):
            desc_font = fdep_vector.val[font_index]
        else:
            desc_font = fdep_vector.val[0] if fdep_vector.val else None

        if desc_font is None:
            results.append((0.0, cid))
            continue

        # Save currentpoint BEFORE rendering (charstring interpreter modifies it)
        saved_cp = ctxt.gstate.currentpoint

        # Render glyph from CIDFont
        char_width = _render_cidfont_glyph(ctxt, desc_font, cid, font_dict)
        if char_width is not None:
            # Apply Type 0 font's FontMatrix scaling
            # (charstring_to_width already applied descendant's FontMatrix)
            char_width *= type0_scale
        effective_width = char_width if char_width is not None else 0.0
        results.append((effective_width, cid))

        # Advance currentpoint from the SAVED position (not glyph-modified currentpoint)
        if effective_width and saved_cp:
            dw_x, dw_y = _transform_delta(ctxt.gstate.CTM, effective_width, 0)
            ctxt.gstate.currentpoint = ps.Point(saved_cp.x + dw_x, saved_cp.y + dw_y)
        elif saved_cp:
            ctxt.gstate.currentpoint = saved_cp

    return results


def _render_fmap_type0_string(ctxt, font_dict, text_bytes, fdep_vector):
    """
    Render a string using a legacy FMapType-based Type 0 composite font.

    These fonts use FMapType/Encoding/FDepVector instead of CMap dictionaries.
    The descendant fonts are typically Type 1 (not CIDFonts).

    Advances ctxt.gstate.currentpoint after each glyph so that subsequent
    glyphs are positioned correctly.

    Returns list of (char_width, char_code) tuples.
    """
    characters, byte_width = _decode_fmap_characters(font_dict, text_bytes)

    results = []
    for char_code, font_index in characters:
        # Get descendant font
        if font_index < len(fdep_vector.val):
            desc_font = fdep_vector.val[font_index]
        else:
            desc_font = fdep_vector.val[0] if fdep_vector.val else None

        if desc_font is None:
            results.append((0.0, char_code))
            continue

        # Save currentpoint BEFORE rendering (charstring interpreter modifies it)
        saved_cp = ctxt.gstate.currentpoint

        # Determine descendant font type and render with per-character exception handling
        char_width = None
        try:
            desc_font_type_obj = desc_font.val.get(b'FontType')
            desc_font_type = desc_font_type_obj.val if desc_font_type_obj else 1

            if desc_font_type == 1:
                # Type 1 descendant — render with composed FontMatrix
                char_width = _render_type1_for_composite(ctxt, desc_font, char_code, font_dict)
            elif desc_font_type == 2:
                # Type 2 (CFF) descendant — render with composed FontMatrix
                char_width = _render_type2_for_composite(ctxt, desc_font, char_code, font_dict)
            elif desc_font_type == 3:
                # Type 3 descendant — render and apply Type 0 scaling
                char_width_raw = _render_type3_character(ctxt, desc_font, char_code)
                if char_width_raw is not None:
                    # Type 3 returns (wx, wy) in character space
                    desc_fm = desc_font.val.get(b'FontMatrix')
                    type0_fm = font_dict.val.get(b'FontMatrix')
                    if desc_fm and desc_fm.TYPE in ps.ARRAY_TYPES:
                        wx, _ = _transform_delta(desc_fm, char_width_raw[0], char_width_raw[1])
                    else:
                        wx = char_width_raw[0]
                    if type0_fm and type0_fm.TYPE in ps.ARRAY_TYPES:
                        wx, _ = _transform_delta(type0_fm, wx, 0)
                    char_width = wx
        except Exception:
            char_width = None

        effective_width = char_width if char_width is not None else 0.0
        results.append((effective_width, char_code))

        # Advance currentpoint from the SAVED position (not glyph-modified currentpoint)
        if effective_width and saved_cp:
            dw_x, dw_y = _transform_delta(ctxt.gstate.CTM, effective_width, 0)
            ctxt.gstate.currentpoint = ps.Point(saved_cp.x + dw_x, saved_cp.y + dw_y)
        elif saved_cp:
            # Zero width — restore currentpoint to pre-glyph position
            ctxt.gstate.currentpoint = saved_cp

    return results


def _render_cidfont_glyph(ctxt, cidfont_dict, cid, type0_font):
    """
    Render a glyph from a CIDFont by CID.

    For CIDFontType 2 (FontType 42) with GlyphDirectory containing
    TrueType glyf table data: parses the glyf outline and converts
    quadratic B-splines to cubic Bezier path operations.

    Uses the same GlyphStart/GlyphEnd/GlyphRef bitmap cache as Type 1
    rendering for repeated glyphs at the same size/color.

    Returns character width in user space, or None.
    """
    from ..core.glyph_cache import make_cache_key, CachedGlyph
    from ..core.types.context import global_resources

    width_only = getattr(ctxt, '_width_only_mode', False)
    charpath_mode = getattr(ctxt, '_charpath_mode', False)

    # Build cache key using the Type 0 font (carries scaled FontMatrix)
    # and CID as the character selector
    cache_enabled = (not global_resources.glyph_cache_disabled
                     and not width_only and not charpath_mode)
    cache_key = None
    if cache_enabled and type0_font is not None:
        char_selector = f'CID:{cid}'.encode('latin-1')
        cp = ctxt.gstate.currentpoint
        pos_y = cp.y if cp else 0.0
        cache_key = make_cache_key(
            type0_font, char_selector, ctxt.gstate.CTM, ctxt.gstate.color, pos_y)
        path_cache = global_resources.get_glyph_cache()

        cached = path_cache.get(cache_key)
        if cached is not None:
            cp = ctxt.gstate.currentpoint
            if cp and ctxt.display_list is not None:
                ctxt.display_list.append(ps.GlyphRef(cache_key, cp.x, cp.y))
            return cached.char_width

    glyph_dir = cidfont_dict.val.get(b'GlyphDirectory')

    if glyph_dir and glyph_dir.TYPE == ps.T_DICT:
        glyph_data = glyph_dir.val.get(cid)
        if glyph_data is None:
            glyph_data = glyph_dir.val.get(ps.Int(cid))

        if glyph_data is not None and glyph_data.TYPE == ps.T_STRING:
            charstring_bytes = glyph_data.byte_string()
            if isinstance(charstring_bytes, str):
                charstring_bytes = charstring_bytes.encode('latin-1')

            # Determine if this is TrueType glyf data or Type 1 charstring
            font_type = cidfont_dict.val.get(b'FontType')
            cid_font_type = cidfont_dict.val.get(b'CIDFontType')
            is_truetype = False
            if font_type and font_type.val == 42:
                is_truetype = True
            elif cid_font_type and cid_font_type.val == 2:
                is_truetype = True

            if is_truetype and len(charstring_bytes) >= 10:
                # Emit GlyphStart for cache capture
                display_list_start = len(ctxt.display_list) if ctxt.display_list else 0
                cp = ctxt.gstate.currentpoint
                if cache_enabled and cache_key is not None and cp and ctxt.display_list is not None:
                    ctxt.display_list.append(ps.GlyphStart(cache_key, cp.x, cp.y))

                char_width = _render_truetype_glyf(
                    ctxt, cidfont_dict, cid, charstring_bytes, type0_font,
                    glyf_resolver=_make_glyph_dir_resolver(glyph_dir))

                # Emit GlyphEnd and store in cache
                if cache_enabled and cache_key is not None and char_width is not None and ctxt.display_list:
                    ctxt.display_list.append(ps.GlyphEnd())
                    glyph_elements = list(ctxt.display_list[display_list_start + 1:-1])
                    if glyph_elements and cp:
                        normalized_elements = _normalize_display_elements(
                            glyph_elements, cp.x, cp.y)
                        cached_glyph = CachedGlyph(
                            display_elements=normalized_elements,
                            char_width=char_width,
                            char_bbox=None,
                            font_dict=type0_font
                        )
                        path_cache.put(cache_key, cached_glyph)

                return char_width
            else:
                try:
                    private_dict = cidfont_dict.val.get(b'Private')
                    return charstring_to_width(
                        charstring_bytes, ctxt, private_dict, cidfont_dict)
                except Exception:
                    return None

    return None


_sfnts_cache = {}

def _get_sfnts_data(cidfont_dict):
    """Concatenate sfnts array into a single byte buffer. Cached by dict id."""
    cache_key = id(cidfont_dict)
    if cache_key in _sfnts_cache:
        return _sfnts_cache[cache_key]

    sfnts = cidfont_dict.val.get(b'sfnts')
    if not sfnts or sfnts.TYPE not in ps.ARRAY_TYPES:
        _sfnts_cache[cache_key] = None
        return None

    font_data = bytearray()
    for s in sfnts.val:
        if s.TYPE == ps.T_STRING:
            b = s.byte_string()
            if isinstance(b, str):
                b = b.encode('latin-1')
            # Type 42 sfnts strings have a trailing padding byte (null
            # terminator) that is not part of the TrueType data. Per the
            # TrueType spec, actual data chunks have even length, so an
            # odd-length string indicates a padding byte that must be stripped
            # for table directory offsets to be correct.
            if len(b) & 1:
                b = b[:-1]
            font_data.extend(b)

    if len(font_data) < 12:
        _sfnts_cache[cache_key] = None
        return None

    result = bytes(font_data)
    _sfnts_cache[cache_key] = result
    return result


def _get_truetype_units_per_em(cidfont_dict):
    """Get unitsPerEm from the TrueType head table in sfnts."""
    font_data = _get_sfnts_data(cidfont_dict)
    if font_data is None or len(font_data) < 12:
        return 1000  # default

    num_tables = int.from_bytes(font_data[4:6], 'big')
    for i in range(num_tables):
        entry_offset = 12 + i * 16
        if entry_offset + 16 > len(font_data):
            break
        tag = font_data[entry_offset:entry_offset + 4]
        if tag == b'head':
            tbl_offset = int.from_bytes(font_data[entry_offset + 8:entry_offset + 12], 'big')
            # unitsPerEm is at offset 18 in head table
            if tbl_offset + 20 <= len(font_data):
                return int.from_bytes(font_data[tbl_offset + 18:tbl_offset + 20], 'big')
    return 1000


def _get_truetype_advance_width(cidfont_dict, cid):
    """
    Get advance width for a CID from the sfnts hmtx table.
    Returns width in font units, or None if not available.
    """
    font_data = _get_sfnts_data(cidfont_dict)
    if font_data is None:
        return None

    # Parse TrueType offset table
    num_tables = int.from_bytes(font_data[4:6], 'big')
    # Parse table directory
    hmtx_offset = hmtx_length = None
    maxp_offset = None
    hhea_offset = None
    for i in range(num_tables):
        entry_offset = 12 + i * 16
        if entry_offset + 16 > len(font_data):
            break
        tag = font_data[entry_offset:entry_offset + 4]
        tbl_offset = int.from_bytes(font_data[entry_offset + 8:entry_offset + 12], 'big')
        tbl_length = int.from_bytes(font_data[entry_offset + 12:entry_offset + 16], 'big')
        if tag == b'hmtx':
            hmtx_offset = tbl_offset
            hmtx_length = tbl_length
        elif tag == b'maxp':
            maxp_offset = tbl_offset
        elif tag == b'hhea':
            hhea_offset = tbl_offset

    if hmtx_offset is None or hhea_offset is None:
        return None

    # Get numberOfHMetrics from hhea table (offset 34)
    if hhea_offset + 36 > len(font_data):
        return None
    num_hmetrics = int.from_bytes(
        font_data[hhea_offset + 34:hhea_offset + 36], 'big')

    # Look up width in hmtx. CID maps to glyph index (identity for CIDToGIDMap Identity)
    gid = cid
    if gid < num_hmetrics:
        offset = hmtx_offset + gid * 4
        if offset + 2 <= len(font_data):
            return int.from_bytes(font_data[offset:offset + 2], 'big')
    else:
        # Use last full hmetric width
        if num_hmetrics > 0:
            offset = hmtx_offset + (num_hmetrics - 1) * 4
            if offset + 2 <= len(font_data):
                return int.from_bytes(font_data[offset:offset + 2], 'big')

    return None


def _parse_simple_glyph(data):
    """Parse a simple TrueType glyph's contour data from raw glyf bytes.

    Args:
        data: Raw glyf table bytes for this glyph (including 10-byte header)

    Returns:
        (end_pts, flags, x_coords, y_coords) tuple, or None on parse error.
        Coordinates are integers in TrueType font units.
    """
    if len(data) < 10:
        return None

    num_contours = struct.unpack('>h', data[0:2])[0]
    if num_contours <= 0:
        return None

    offset = 10
    end_pts = []
    for i in range(num_contours):
        if offset + 2 > len(data):
            return None
        ep = struct.unpack('>H', data[offset:offset + 2])[0]
        end_pts.append(ep)
        offset += 2

    num_points = end_pts[-1] + 1

    # Skip instructions
    if offset + 2 > len(data):
        return None
    instr_len = struct.unpack('>H', data[offset:offset + 2])[0]
    offset += 2 + instr_len

    # Parse flags
    flags = []
    while len(flags) < num_points:
        if offset >= len(data):
            return None
        flag = data[offset]
        offset += 1
        flags.append(flag)
        if flag & 0x08:  # repeat flag
            if offset >= len(data):
                return None
            repeat_count = data[offset]
            offset += 1
            for _ in range(repeat_count):
                flags.append(flag)

    # Parse x coordinates
    x_coords = []
    x = 0
    for flag in flags:
        if flag & 0x02:  # x is 1 byte
            if offset >= len(data):
                return None
            dx = data[offset]
            offset += 1
            if not (flag & 0x10):  # negative
                dx = -dx
            x += dx
        elif flag & 0x10:  # x is same as previous
            pass
        else:  # x is 2 bytes signed
            if offset + 2 > len(data):
                return None
            dx = struct.unpack('>h', data[offset:offset + 2])[0]
            offset += 2
            x += dx
        x_coords.append(x)

    # Parse y coordinates
    y_coords = []
    y = 0
    for flag in flags:
        if flag & 0x04:  # y is 1 byte
            if offset >= len(data):
                return None
            dy = data[offset]
            offset += 1
            if not (flag & 0x20):  # negative
                dy = -dy
            y += dy
        elif flag & 0x20:  # y is same as previous
            pass
        else:  # y is 2 bytes signed
            if offset + 2 > len(data):
                return None
            dy = struct.unpack('>h', data[offset:offset + 2])[0]
            offset += 2
            y += dy
        y_coords.append(y)

    return (end_pts, flags, x_coords, y_coords)


def _parse_composite_glyph(data, glyf_resolver, depth=0):
    """Parse a composite TrueType glyph by resolving and merging components.

    Composite glyphs reference other glyphs (simple or composite) with optional
    affine transforms and offsets. This function recursively resolves all
    components and returns unified contour data.

    Args:
        data: Raw glyf bytes for the composite glyph (including 10-byte header)
        glyf_resolver: Callable(gid) -> bytes or None, resolves GID to glyf data
        depth: Recursion depth guard (max 10)

    Returns:
        (end_pts, flags, x_coords, y_coords) tuple, or None on error.
        Coordinates are floats (transformed by component affine matrices).
    """
    if depth > 10 or len(data) < 12:
        return None

    all_end_pts = []
    all_flags = []
    all_x_coords = []
    all_y_coords = []
    point_offset = 0

    offset = 10  # Skip glyf header

    while True:
        if offset + 4 > len(data):
            break

        comp_flags = struct.unpack('>H', data[offset:offset + 2])[0]
        glyph_index = struct.unpack('>H', data[offset + 2:offset + 4])[0]
        offset += 4

        # Read x,y arguments (offsets or point indices)
        if comp_flags & 0x0001:  # ARG_1_AND_2_ARE_WORDS
            if offset + 4 > len(data):
                break
            if comp_flags & 0x0002:  # ARGS_ARE_XY_VALUES
                x_off = struct.unpack('>h', data[offset:offset + 2])[0]
                y_off = struct.unpack('>h', data[offset + 2:offset + 4])[0]
            else:
                x_off, y_off = 0, 0  # Point matching — fallback to no offset
            offset += 4
        else:
            if offset + 2 > len(data):
                break
            if comp_flags & 0x0002:  # ARGS_ARE_XY_VALUES
                x_off = struct.unpack('>b', data[offset:offset + 1])[0]
                y_off = struct.unpack('>b', data[offset + 1:offset + 2])[0]
            else:
                x_off, y_off = 0, 0
            offset += 2

        # Read optional transform (F2Dot14 fixed-point: int16 / 16384.0)
        scale_xx = 1.0
        scale_yy = 1.0
        scale_xy = 0.0
        scale_yx = 0.0

        if comp_flags & 0x0008:  # WE_HAVE_A_SCALE
            if offset + 2 > len(data):
                break
            scale_xx = scale_yy = struct.unpack('>h', data[offset:offset + 2])[0] / 16384.0
            offset += 2
        elif comp_flags & 0x0040:  # WE_HAVE_AN_XY_SCALE
            if offset + 4 > len(data):
                break
            scale_xx = struct.unpack('>h', data[offset:offset + 2])[0] / 16384.0
            scale_yy = struct.unpack('>h', data[offset + 2:offset + 4])[0] / 16384.0
            offset += 4
        elif comp_flags & 0x0080:  # WE_HAVE_A_TWO_BY_TWO
            if offset + 8 > len(data):
                break
            scale_xx = struct.unpack('>h', data[offset:offset + 2])[0] / 16384.0
            scale_xy = struct.unpack('>h', data[offset + 2:offset + 4])[0] / 16384.0
            scale_yx = struct.unpack('>h', data[offset + 4:offset + 6])[0] / 16384.0
            scale_yy = struct.unpack('>h', data[offset + 6:offset + 8])[0] / 16384.0
            offset += 8

        # Compute effective offset (SCALED_COMPONENT_OFFSET transforms the offset
        # by the component's matrix; default is unscaled/as-is)
        eff_x_off = float(x_off)
        eff_y_off = float(y_off)
        if comp_flags & 0x0800:  # SCALED_COMPONENT_OFFSET
            eff_x_off = x_off * scale_xx + y_off * scale_xy
            eff_y_off = x_off * scale_yx + y_off * scale_yy

        # Resolve component glyph
        comp_data = glyf_resolver(glyph_index)
        if comp_data is None or len(comp_data) < 10:
            if not (comp_flags & 0x0020):  # MORE_COMPONENTS
                break
            continue

        comp_num_contours = struct.unpack('>h', comp_data[0:2])[0]

        if comp_num_contours > 0:
            result = _parse_simple_glyph(comp_data)
        elif comp_num_contours < 0:
            result = _parse_composite_glyph(comp_data, glyf_resolver, depth + 1)
        else:
            # Empty component glyph
            if not (comp_flags & 0x0020):
                break
            continue

        if result is not None:
            end_pts, flags, x_coords, y_coords = result
            has_transform = (scale_xx != 1.0 or scale_yy != 1.0
                             or scale_xy != 0.0 or scale_yx != 0.0)

            transformed_x = []
            transformed_y = []
            for i in range(len(x_coords)):
                cx, cy = float(x_coords[i]), float(y_coords[i])
                if has_transform:
                    nx = cx * scale_xx + cy * scale_xy + eff_x_off
                    ny = cx * scale_yx + cy * scale_yy + eff_y_off
                else:
                    nx = cx + eff_x_off
                    ny = cy + eff_y_off
                transformed_x.append(nx)
                transformed_y.append(ny)

            adjusted_end_pts = [ep + point_offset for ep in end_pts]
            all_end_pts.extend(adjusted_end_pts)
            all_flags.extend(flags)
            all_x_coords.extend(transformed_x)
            all_y_coords.extend(transformed_y)
            point_offset += len(x_coords)

        if not (comp_flags & 0x0020):  # MORE_COMPONENTS
            break

    if not all_end_pts:
        return None

    return (all_end_pts, all_flags, all_x_coords, all_y_coords)


def _make_glyph_dir_resolver(glyph_dir):
    """Create a glyf_resolver callback for GlyphDirectory-based CID fonts.

    Returns a closure that looks up GID in a GlyphDirectory dict and returns
    raw glyf bytes, or None if not found.
    """
    def resolver(gid):
        entry = glyph_dir.val.get(gid)
        if entry is None:
            entry = glyph_dir.val.get(ps.Int(gid))
        if entry is None or entry.TYPE != ps.T_STRING:
            return None
        data = entry.byte_string()
        if isinstance(data, str):
            data = data.encode('latin-1')
        return data
    return resolver


def _render_truetype_glyf(ctxt, cidfont_dict, cid, glyf_data, type0_font=None,
                           glyf_resolver=None):
    """
    Parse TrueType glyf table entry and render as PostScript path.
    Converts quadratic B-splines to cubic Bezier curves.

    The coordinate **transform** chain is:
      glyph coords → CIDFont FontMatrix → Type0 FontMatrix → CTM → device space

    Args:
        glyf_resolver: Optional callable(gid) -> bytes, for resolving composite
            glyph component references. If None, composite glyphs return width only.

    Returns character width in user space, or None.
    """
    data = glyf_data
    if len(data) < 10:
        return None

    num_contours = struct.unpack('>h', data[0:2])[0]
    x_min = struct.unpack('>h', data[2:4])[0]
    y_min = struct.unpack('>h', data[4:6])[0]
    x_max = struct.unpack('>h', data[6:8])[0]
    y_max = struct.unpack('>h', data[8:10])[0]

    # Get advance width from hmtx table
    advance_width = _get_truetype_advance_width(cidfont_dict, cid)
    if advance_width is None:
        advance_width = x_max  # fallback to bbox

    # Get FontMatrix from CIDFont and compute width scale
    # For CIDFontType 2, apply 1/unitsPerEm then CIDFont FontMatrix
    units_per_em = _get_truetype_units_per_em(cidfont_dict)
    em_scale = 1.0 / units_per_em if units_per_em > 0 else 0.001
    font_matrix = cidfont_dict.val.get(b'FontMatrix')
    if font_matrix and font_matrix.TYPE in ps.ARRAY_TYPES and len(font_matrix.val) >= 1:
        fm_scale = font_matrix.val[0].val if font_matrix.val[0].TYPE in ps.NUMERIC_TYPES else 1.0
    else:
        fm_scale = 1.0
    # Total width scale: em_scale × fm_scale
    width_scale = em_scale * fm_scale

    if num_contours == 0:
        # Empty glyph (space, etc.)
        return advance_width * width_scale

    # Parse glyph contours (simple or composite)
    contour_data = None
    if num_contours > 0:
        contour_data = _parse_simple_glyph(data)
    elif num_contours < 0 and glyf_resolver is not None:
        contour_data = _parse_composite_glyph(data, glyf_resolver)

    if contour_data is None:
        # Parse failed or composite without resolver — return width only
        return advance_width * width_scale

    end_pts, flags, x_coords, y_coords = contour_data
    total_contours = len(end_pts)

    # Check for charpath mode and width-only mode
    width_only = getattr(ctxt, '_width_only_mode', False)
    charpath_mode = getattr(ctxt, '_charpath_mode', False)

    if not width_only:
        # Get FontMatrices for coordinate transformation
        # Chain: glyph coords → (1/unitsPerEm) → CIDFont FM → Type0 FM → CTM → device
        cid_fm = cidfont_dict.val.get(b'FontMatrix')
        type0_fm = type0_font.val.get(b'FontMatrix') if type0_font else None
        show_origin = ctxt.gstate.currentpoint

        # Get unitsPerEm for TrueType scaling
        units_per_em = _get_truetype_units_per_em(cidfont_dict)
        em_scale = 1.0 / units_per_em if units_per_em > 0 else 0.001

        # Build path from TrueType contours
        contour_start = 0
        path = ctxt.gstate.path

        for contour_idx in range(total_contours):
            contour_end = end_pts[contour_idx]
            contour_points = []
            for i in range(contour_start, contour_end + 1):
                on_curve = bool(flags[i] & 0x01)
                # Scale from TrueType units to character space
                gx = float(x_coords[i]) * em_scale
                gy = float(y_coords[i]) * em_scale
                # Step 1: CIDFont FontMatrix (char space → intermediate)
                if cid_fm and cid_fm.TYPE in ps.ARRAY_TYPES:
                    gx, gy = _transform_point(cid_fm, gx, gy)
                # Step 2: Type 0 FontMatrix (intermediate → user space)
                if type0_fm and type0_fm.TYPE in ps.ARRAY_TYPES:
                    gx, gy = _transform_point(type0_fm, gx, gy)
                # Step 3: CTM (user space → device space, delta only)
                dx, dy = _transform_delta(ctxt.gstate.CTM, gx, gy)
                # Step 4: Offset by current point (show origin)
                if show_origin:
                    dx += show_origin.x
                    dy += show_origin.y
                contour_points.append((dx, dy, on_curve))

            if not contour_points:
                contour_start = contour_end + 1
                continue

            _emit_truetype_contour(path, contour_points)
            contour_start = contour_end + 1

        # Paint the glyph to display list (like endchar does for Type 1)
        if not charpath_mode:
            if not hasattr(ctxt, 'display_list_builder'):
                ctxt.display_list_builder = DisplayListBuilder(ctxt.display_list)

            ctxt.display_list_builder.add_graphics_operation(
                ctxt, ctxt.gstate.path)

            device_color = color_space.convert_to_device_color(
                ctxt, ctxt.gstate.color, ctxt.gstate.color_space)
            fill_op = ps.Fill(device_color, ps.WINDING_NON_ZERO)
            ctxt.display_list_builder.add_graphics_operation(ctxt, fill_op)

            # Clear path after painting
            ctxt.gstate.path = ps.Path()

    return advance_width * width_scale


def _emit_truetype_contour(path, points):
    """
    Emit a single TrueType contour as PostScript path operations.
    Converts quadratic B-splines to cubic Bezier curves.
    Points are (x, y, on_curve) tuples already in user coordinates.
    """
    n = len(points)
    if n < 2:
        return

    # TrueType contours are closed. Find a starting on-curve point.
    # If the first point is off-curve, we may need to compute a midpoint.
    start_idx = 0
    start_x, start_y, start_on = points[0]

    if not start_on:
        # First point is off-curve. Check if last point is on-curve.
        last_x, last_y, last_on = points[-1]
        if last_on:
            # Start from last point
            start_x, start_y = last_x, last_y
            start_idx = 0  # We'll process all points including first
        else:
            # Both off-curve: start from midpoint between first and last
            start_x = (start_x + last_x) / 2.0
            start_y = (start_y + last_y) / 2.0
            start_idx = 0
    else:
        start_idx = 1

    # Add subpath and moveto
    subpath = ps.SubPath()
    subpath.append(ps.MoveTo(ps.Point(start_x, start_y)))

    i = start_idx
    count = 0
    while count < n:
        px, py, on_curve = points[i % n]

        if on_curve:
            subpath.append(ps.LineTo(ps.Point(px, py)))
        else:
            # Off-curve point: quadratic B-spline control point
            # Look at next point
            nx, ny, next_on = points[(i + 1) % n]
            if not next_on:
                # Two consecutive off-curve: insert implicit on-curve midpoint
                mid_x = (px + nx) / 2.0
                mid_y = (py + ny) / 2.0
                # Convert quadratic (start, control=px,py, end=mid) to cubic
                c1x = start_x + 2.0 / 3.0 * (px - start_x)
                c1y = start_y + 2.0 / 3.0 * (py - start_y)
                c2x = mid_x + 2.0 / 3.0 * (px - mid_x)
                c2y = mid_y + 2.0 / 3.0 * (py - mid_y)
                subpath.append(ps.CurveTo(
                    ps.Point(c1x, c1y), ps.Point(c2x, c2y),
                    ps.Point(mid_x, mid_y)))
                start_x, start_y = mid_x, mid_y
                count += 1
                i = (i + 1) % n
                continue
            else:
                # Next is on-curve: standard quadratic segment
                c1x = start_x + 2.0 / 3.0 * (px - start_x)
                c1y = start_y + 2.0 / 3.0 * (py - start_y)
                c2x = nx + 2.0 / 3.0 * (px - nx)
                c2y = ny + 2.0 / 3.0 * (py - ny)
                subpath.append(ps.CurveTo(
                    ps.Point(c1x, c1y), ps.Point(c2x, c2y),
                    ps.Point(nx, ny)))
                start_x, start_y = nx, ny
                count += 2
                i = (i + 2) % n
                continue

        start_x, start_y = px, py
        count += 1
        i = (i + 1) % n

    # Close the contour
    subpath.closed = True
    path.append(subpath)


def _calculate_inverse_ctm(ctm):
    """Calculate inverse of 6-element CTM [a, b, c, d, tx, ty]."""
    a, b, c, d, tx, ty = ctm
    determinant = a * d - b * c
    if abs(determinant) < 1e-10:
        return [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    inv_det = 1.0 / determinant
    return [
        d * inv_det, -b * inv_det, -c * inv_det, a * inv_det,
        (c * ty - d * tx) * inv_det, (b * tx - a * ty) * inv_det
    ]
