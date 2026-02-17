# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
SVG Output Device

This device renders PostScript graphics to SVG files using Cairo's SVGSurface
for all graphics, with native SVG <text> elements for selectable/editable text.

Cairo renders text as glyph outlines, which is unusable for editing in tools
like Inkscape. Instead, all TextObj items are deferred during Cairo rendering
and injected as native SVG <text> elements via XML post-processing.
"""

import io
import math
import os
import xml.etree.ElementTree as ET

import cairo

from ...core import types as ps
from ...core.unicode_mapping import text_to_unicode
from ..common.cairo_renderer import render_display_list
from ..pdf.cid_font_embedder import CIDFontEmbedder

# SVG namespace
_SVG_NS = 'http://www.w3.org/2000/svg'
_XLINK_NS = 'http://www.w3.org/1999/xlink'


# PostScript font name to CSS font properties mapping
# Returns: (family, generic_fallback, font_style, font_weight)
_PS_TO_CSS_FONT = {
    b'Times-Roman':           ('Nimbus Roman', "'Times New Roman', serif", 'normal', 'normal'),
    b'Times-Bold':            ('Nimbus Roman', "'Times New Roman', serif", 'normal', 'bold'),
    b'Times-Italic':          ('Nimbus Roman', "'Times New Roman', serif", 'italic', 'normal'),
    b'Times-BoldItalic':      ('Nimbus Roman', "'Times New Roman', serif", 'italic', 'bold'),
    b'Helvetica':             ('Nimbus Sans', "'Helvetica', sans-serif", 'normal', 'normal'),
    b'Helvetica-Bold':        ('Nimbus Sans', "'Helvetica', sans-serif", 'normal', 'bold'),
    b'Helvetica-Oblique':     ('Nimbus Sans', "'Helvetica', sans-serif", 'oblique', 'normal'),
    b'Helvetica-BoldOblique': ('Nimbus Sans', "'Helvetica', sans-serif", 'oblique', 'bold'),
    b'Courier':               ('Nimbus Mono PS', "'Courier New', monospace", 'normal', 'normal'),
    b'Courier-Bold':          ('Nimbus Mono PS', "'Courier New', monospace", 'normal', 'bold'),
    b'Courier-Oblique':       ('Nimbus Mono PS', "'Courier New', monospace", 'oblique', 'normal'),
    b'Courier-BoldOblique':   ('Nimbus Mono PS', "'Courier New', monospace", 'oblique', 'bold'),
    b'Symbol':                ('Symbol', 'serif', 'normal', 'normal'),
    b'ZapfDingbats':          ('Zapf Dingbats', 'serif', 'normal', 'normal'),

    # URW fonts (commonly available on Linux)
    b'NimbusRoman-Regular':       ('Nimbus Roman', 'serif', 'normal', 'normal'),
    b'NimbusRoman-Bold':          ('Nimbus Roman', 'serif', 'normal', 'bold'),
    b'NimbusRoman-Italic':        ('Nimbus Roman', 'serif', 'italic', 'normal'),
    b'NimbusRoman-BoldItalic':    ('Nimbus Roman', 'serif', 'italic', 'bold'),
    b'NimbusSans-Regular':        ('Nimbus Sans', 'sans-serif', 'normal', 'normal'),
    b'NimbusSans-Bold':           ('Nimbus Sans', 'sans-serif', 'normal', 'bold'),
    b'NimbusSans-Italic':         ('Nimbus Sans', 'sans-serif', 'oblique', 'normal'),
    b'NimbusSans-BoldItalic':     ('Nimbus Sans', 'sans-serif', 'oblique', 'bold'),
    b'NimbusMonoPS-Regular':      ('Nimbus Mono PS', 'monospace', 'normal', 'normal'),
    b'NimbusMonoPS-Bold':         ('Nimbus Mono PS', 'monospace', 'normal', 'bold'),
    b'NimbusMonoPS-Italic':       ('Nimbus Mono PS', 'monospace', 'oblique', 'normal'),
    b'NimbusMonoPS-BoldItalic':   ('Nimbus Mono PS', 'monospace', 'oblique', 'bold'),
}


def showpage(ctxt: ps.Context, pd: dict) -> None:
    """
    Render the current page to an SVG file.

    Uses Cairo's SVGSurface for all graphics rendering, then post-processes
    the SVG to inject native <text> elements for selectable/editable text.

    Args:
        ctxt: PostScript context with display_list to render
        pd: Page device dictionary containing MediaSize, PageCount, LineWidthMin, etc.
    """
    # Compute transformation from device space to SVG space (points)
    hw_res_x = pd[b"HWResolution"].get(ps.Int(0))[1].val
    hw_res_y = pd[b"HWResolution"].get(ps.Int(1))[1].val

    scale_x = 72.0 / hw_res_x
    scale_y = 72.0 / hw_res_y

    min_line_width = pd[b"LineWidthMin"].val

    # Get page dimensions in device space
    WIDTH_device = pd[b"MediaSize"].get(ps.Int(0))[1].val
    HEIGHT_device = pd[b"MediaSize"].get(ps.Int(1))[1].val

    # Convert to SVG points
    width_svg = WIDTH_device * scale_x
    height_svg = HEIGHT_device * scale_y

    # Render to an in-memory buffer so we can post-process the SVG
    svg_buffer = io.BytesIO()
    surface = cairo.SVGSurface(svg_buffer, width_svg, height_svg)
    surface.set_document_unit(cairo.SVG_UNIT_PT)
    cc = cairo.Context(surface)

    # Apply scale matrix (at 72 DPI this is identity)
    cc.set_matrix(cairo.Matrix(scale_x, 0, 0, scale_y, 0, 0))
    cc.set_tolerance(ctxt.gstate.flatness / 10.0)

    # Fill white background
    cc.set_source_rgb(1.0, 1.0, 1.0)
    cc.rectangle(0, 0, WIDTH_device, HEIGHT_device)
    cc.fill()

    # Collect ALL text objects for native SVG text rendering
    deferred_text_objs = []

    # Render display list — all TextObjs are deferred
    render_display_list(ctxt, cc, HEIGHT_device, min_line_width,
                        deferred_text_objs, defer_all_text=True)

    # Finish Cairo surface to flush SVG output
    surface.finish()

    # Post-process SVG: inject text elements and fix image rendering
    svg_bytes = svg_buffer.getvalue()
    svg_bytes = _post_process_svg(svg_bytes, deferred_text_objs,
                                  scale_x, scale_y)

    # Write SVG output
    page_num = pd[b"PageCount"].val

    if b"OutputBaseName" in pd:
        base_name = pd[b"OutputBaseName"].python_string()
    else:
        base_name = "page"

    if b"OutputDirectory" in pd:
        output_dir = pd[b"OutputDirectory"].python_string()
    else:
        output_dir = ps.OUTPUT_DIRECTORY

    output_file = os.path.join(os.getcwd(), output_dir, f"{base_name}-{page_num:04d}.svg")
    with open(output_file, 'wb') as f:
        f.write(svg_bytes)


def _post_process_svg(svg_bytes, deferred_text_objs, scale_x, scale_y):
    """
    Parse Cairo's SVG output and apply post-processing fixes.

    - Set image-rendering: pixelated on all <image> elements so SVG viewers
      use nearest-neighbor scaling instead of bilinear smoothing.
    - Inject native <text> elements for selectable/editable text.
    """
    ET.register_namespace('', _SVG_NS)
    ET.register_namespace('xlink', _XLINK_NS)

    root = ET.fromstring(svg_bytes)

    # Set image-rendering on all <image> elements.  Cairo doesn't emit this
    # attribute, so SVG viewers default to smooth interpolation which makes
    # low-resolution and indexed-color images look blurry.
    for img_elem in root.iter(f'{{{_SVG_NS}}}image'):
        img_elem.set('image-rendering', 'pixelated')

    if deferred_text_objs:
        _inject_text_into_tree(root, deferred_text_objs, scale_x, scale_y)

    ET.indent(root)
    return ET.tostring(root, encoding='unicode', xml_declaration=True).encode('utf-8')


def _inject_text_into_tree(root, deferred_text_objs, scale_x, scale_y):
    """
    Append native <text> elements to an already-parsed SVG tree.

    Args:
        root: Parsed SVG root element (xml.etree.ElementTree.Element)
        deferred_text_objs: List of (TextObj, clip_info) tuples
        scale_x: Device to SVG X scale factor
        scale_y: Device to SVG Y scale factor
    """
    # Build CID → Unicode maps for any CID (Type 0) fonts
    cid_tounicode_maps = _build_cid_tounicode_maps(deferred_text_objs)

    for text_obj, _clip_info in deferred_text_objs:
        # Skip ActualTextStart/End markers (Type 3 font searchability for PDF)
        if not isinstance(text_obj, ps.TextObj):
            continue

        # Convert text bytes to Unicode
        font_type = text_obj.font_dict.val.get(b'FontType')
        is_cid = font_type and font_type.val == 0

        if is_cid:
            # CID fonts: decode 2-byte CID codes and map through cmap table
            text_str = _cid_text_to_unicode(
                text_obj.text, text_obj.font_dict,
                cid_tounicode_maps)
        else:
            # Type 1/42 fonts: use encoding-based mapping
            try:
                text_str = text_to_unicode(text_obj.text, text_obj.font_dict)
            except Exception:
                try:
                    text_str = text_obj.text.decode('latin-1')
                except (UnicodeDecodeError, AttributeError):
                    text_str = str(text_obj.text)

            # Strip unmappable .notdef characters (U+FFFD replacement chars,
            # e.g., newlines in PS strings)
            text_str = text_str.replace('\ufffd', '')

        if not text_str or text_str.isspace():
            continue

        # Get CSS font properties
        font_name = text_obj.font_name
        if font_name in _PS_TO_CSS_FONT:
            family, fallback, font_style, font_weight = _PS_TO_CSS_FONT[font_name]
            font_family = f"'{family}', {fallback}"
        else:
            # Fallback: use the PostScript font name directly
            if isinstance(font_name, bytes):
                family = font_name.decode('latin-1', errors='replace')
            else:
                family = str(font_name)
            name_lower = family.lower()
            font_style = 'italic' if ('italic' in name_lower or 'oblique' in name_lower) else 'normal'
            font_weight = 'bold' if 'bold' in name_lower else 'normal'
            if any(k in name_lower for k in ('mono', 'courier', 'typewriter', 'consol')):
                fallback = 'monospace'
            elif any(k in name_lower for k in ('roman', 'times', 'serif', 'garamond',
                                               'palatino', 'bookman', 'century')):
                fallback = 'serif'
            else:
                fallback = 'sans-serif'
            font_family = f"'{family}', {fallback}"

        # Compute SVG text transform from PostScript CTM.
        #
        # The PS CTM maps from PS user space (Y-up) to device space (Y-down).
        # SVG text renders naturally in Y-down coordinates. If we applied the
        # full PS CTM as an SVG transform, the Y-flip would double-flip text
        # (upside down). Fix: negate c and d when det < 0 (standard PS) to
        # strip the Y-reflection. When det > 0 (DVIPS-style, no Y-flip in
        # CTM), keep c and d as-is.
        ctm = text_obj.ctm
        a, b, c, d, tx, ty = ctm
        det = a * d - b * c

        text_elem = ET.SubElement(root, f'{{{_SVG_NS}}}text')

        fm = text_obj.font_matrix

        if abs(det) > 1e-10:
            # Check if CTM is simple (no rotation/skew, uniform scaling)
            # AND font matrix is uniform (no non-uniform makefont)
            fm_is_uniform = (fm is None or
                             (abs(fm[1]) < 1e-10 and abs(fm[2]) < 1e-10
                              and abs(abs(fm[0]) - abs(fm[3])) < 1e-10 * max(abs(fm[0]), 1)))
            is_simple = (fm_is_uniform
                         and abs(b) < 1e-10 and abs(c) < 1e-10
                         and abs(abs(a) - abs(d)) < 1e-10 * max(abs(a), abs(d), 1))

            if is_simple:
                # Simple case: uniform scale + Y-flip, use plain x,y positioning
                svg_x = text_obj.start_x * scale_x
                svg_y = text_obj.start_y * scale_y
                text_elem.set('x', _fmt(svg_x))
                text_elem.set('y', _fmt(svg_y))
                text_elem.set('font-size', _fmt(text_obj.font_size * scale_y))
            else:
                # General case: rotation, skew, anisotropic scaling, or
                # non-uniform font matrix (e.g., [40 0 0 15 0 0] makefont).
                # Strip Y-reflection from CTM for SVG text rendering.
                fm_det = (fm[0] * fm[3] - fm[1] * fm[2]) if fm else 1.0
                if det * fm_det < 0:
                    c_adj, d_adj = -c, -d
                else:
                    c_adj, d_adj = c, d

                # Compose font matrix with adjusted CTM
                if fm:
                    # Use Y-axis magnitude of font matrix as the SVG font-size
                    # (keeps a reasonable size for font hinting), and normalize
                    # the font matrix by that magnitude so the transform carries
                    # only the aspect ratio and CTM orientation.
                    fm_y_mag = math.sqrt(fm[2] * fm[2] + fm[3] * fm[3])
                    if fm_y_mag > 1e-10:
                        nfm = [v / fm_y_mag for v in fm[:4]]
                    else:
                        nfm = [1.0, 0.0, 0.0, 1.0]
                    svg_a = (nfm[0] * a + nfm[1] * c_adj) * scale_x
                    svg_b = (nfm[0] * b + nfm[1] * d_adj) * scale_y
                    svg_c = (nfm[2] * a + nfm[3] * c_adj) * scale_x
                    svg_d = (nfm[2] * b + nfm[3] * d_adj) * scale_y
                    font_size_svg = fm_y_mag
                else:
                    svg_a = a * scale_x
                    svg_b = b * scale_y
                    svg_c = c_adj * scale_x
                    svg_d = d_adj * scale_y
                    sx = math.sqrt(a * a + b * b)
                    sy = math.sqrt(c * c + d * d)
                    ctm_scale = math.sqrt(sx * sy)
                    font_size_svg = text_obj.font_size / ctm_scale if ctm_scale > 0 else text_obj.font_size

                # Place text at (0,0) in local coords; translation positions it
                svg_tx = text_obj.start_x * scale_x
                svg_ty = text_obj.start_y * scale_y

                text_elem.set('transform',
                              f'matrix({_fmt(svg_a)},{_fmt(svg_b)},{_fmt(svg_c)},{_fmt(svg_d)},{_fmt(svg_tx)},{_fmt(svg_ty)})')
                text_elem.set('x', '0')
                text_elem.set('y', '0')
                text_elem.set('font-size', _fmt(font_size_svg))
        else:
            # Degenerate CTM — simple positioning
            svg_x = text_obj.start_x * scale_x
            svg_y = text_obj.start_y * scale_y
            text_elem.set('x', _fmt(svg_x))
            text_elem.set('y', _fmt(svg_y))
            text_elem.set('font-size', _fmt(text_obj.font_size))

        text_elem.set('font-family', font_family)
        if font_style != 'normal':
            text_elem.set('font-style', font_style)
        if font_weight != 'normal':
            text_elem.set('font-weight', font_weight)

        # Set fill color
        color = text_obj.color
        if color and len(color) >= 3:
            r, g, b_val = color[0], color[1], color[2]
            if r == 0.0 and g == 0.0 and b_val == 0.0:
                # Black — omit fill attribute (SVG default)
                pass
            else:
                text_elem.set('fill', _rgb_to_hex(r, g, b_val))
        elif color and len(color) == 1:
            gray = color[0]
            if gray != 0.0:
                text_elem.set('fill', _rgb_to_hex(gray, gray, gray))

        text_elem.text = text_str


def _build_cid_tounicode_maps(deferred_text_objs):
    """
    Build CID → Unicode maps for all CID (Type 0) fonts in the deferred text.

    Uses CIDFontEmbedder to parse the TrueType cmap table from each font's
    sfnts data, producing a mapping from CID codes to Unicode characters.

    Args:
        deferred_text_objs: List of (TextObj/ActualTextStart, clip_info) tuples

    Returns:
        dict: {font_dict_id: {cid: unicode_char}} for each CID font
    """
    embedder = CIDFontEmbedder()
    maps = {}

    # Collect all CIDs per font (keyed by font dict identity)
    font_info = {}  # font_dict_id → (font_dict, set_of_cids)
    for item, _ in deferred_text_objs:
        if not isinstance(item, ps.TextObj):
            continue
        font_type = item.font_dict.val.get(b'FontType')
        if not font_type or font_type.val != 0:
            continue
        font_id = id(item.font_dict.val)
        if font_id not in font_info:
            font_info[font_id] = (item.font_dict, set())
        text = item.text
        for i in range(0, len(text) - 1, 2):
            cid = (text[i] << 8) | text[i + 1]
            font_info[font_id][1].add(cid)

    # Build ToUnicode map for each CID font
    for font_id, (font_dict, cids) in font_info.items():
        try:
            cid_to_gid = embedder.get_cid_to_gid_dict(font_dict, cids)
            tounicode = embedder.build_tounicode_map(
                font_dict, cids, cid_to_gid)
            maps[font_id] = tounicode
        except Exception:
            maps[font_id] = {}

    return maps


def _cid_text_to_unicode(text_bytes, font_dict, cid_tounicode_maps):
    """
    Convert CID font text bytes (2-byte CID codes) to Unicode string.

    Args:
        text_bytes: Raw text bytes containing 2-byte CID codes
        font_dict: PostScript Type 0 font dictionary
        cid_tounicode_maps: Pre-built {font_dict_id: {cid: unicode_char}} maps

    Returns:
        Unicode string, or empty string if unmappable
    """
    font_id = id(font_dict.val)
    tounicode = cid_tounicode_maps.get(font_id, {})

    result = []
    for i in range(0, len(text_bytes) - 1, 2):
        cid = (text_bytes[i] << 8) | text_bytes[i + 1]
        char = tounicode.get(cid)
        if char:
            result.append(char)

    return ''.join(result)


def _fmt(value):
    """Format a float for SVG attribute output, stripping trailing zeros."""
    if value == int(value):
        return str(int(value))
    # Use enough precision for clean coordinates
    formatted = f'{value:.4f}'.rstrip('0').rstrip('.')
    return formatted


def _rgb_to_hex(r, g, b):
    """Convert RGB floats (0.0-1.0) to hex color string."""
    ri = max(0, min(255, int(round(r * 255))))
    gi = max(0, min(255, int(round(g * 255))))
    bi = max(0, min(255, int(round(b * 255))))
    return f'#{ri:02x}{gi:02x}{bi:02x}'
