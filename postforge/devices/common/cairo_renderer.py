# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Shared Cairo Rendering Module

This module provides the main display list dispatcher and text/glyph rendering
logic used by multiple output devices (PNG, PDF, SVG, Qt).

Architecture:
- render_display_list() is the main entry point for device implementations
- Text rendering maps PostScript fonts to system fonts via Cairo
- Glyph bitmap caching captures glyph regions for reuse

Submodules:
- cairo_images: Image rendering and pixel format conversion
- cairo_shading: Gradient and shading fill rendering
- cairo_patterns: Tiling and shading pattern fills
"""

import copy
import math

import cairo

from ...core import types as ps
from ...core.glyph_cache import CachedBitmap
from ...core.types.context import global_resources
from .cairo_images import (
    _render_image_element, _render_imagemask_element, _render_colorimage_element,
    get_imagemask_cache_stats, clear_imagemask_cache,
)
from .cairo_shading import (
    _render_axial_shading, _render_radial_shading,
    _render_mesh_shading_batch, _render_mesh_shading,
    _render_patch_shading, _render_function_shading,
)
from .cairo_patterns import _render_pattern_fill


def render_display_list(ctxt: ps.Context, cairo_ctx, page_height: int, min_line_width: float = 1,
                        deferred_text_objs: list = None, defer_all_text: bool = False) -> None:
    """
    Render PostScript display list to a Cairo context.

    This is the main entry point for rendering. Device implementations should:
    1. Create a Cairo surface and context
    2. Set up any device-specific initialization (background color, etc.)
    3. Call this function to render the display list
    4. Finalize output (write to file, display to screen, etc.)

    Args:
        ctxt: PostScript context with display_list to render
        cairo_ctx: Cairo context to render to
        page_height: Height of the page in device units (for coordinate transforms)
        min_line_width: Minimum line width threshold for strokes
        deferred_text_objs: Optional list to collect TextObjs that should be rendered
            directly to PDF/SVG (non-Standard 14 fonts, or all text when defer_all_text
            is True). If None, all text is rendered with Cairo. If provided,
            non-Standard 14 fonts are skipped and added to this list for later injection.
        defer_all_text: If True, ALL TextObjs are deferred (not just non-Standard 14).
            Used by SVG device to capture all text for native SVG text elements.
    """
    _glyph_capture_stack = []
    _inside_glyph_capture = False

    # On vector surfaces (PDF), skip bitmap glyph caching and render paths directly.
    # Bitmap blitting loses vector fidelity and has coordinate issues with PDF scaling.
    _is_vector_surface = isinstance(cairo_ctx.get_target(), (cairo.PDFSurface, cairo.SVGSurface))

    # Track current clip state for deferred text objects (PDF injection)
    _current_clip_path = None
    _current_clip_winding = None

    for display_index, item in enumerate(ctxt.display_list):
        if isinstance(item, ps.ClipElement):
            # If this is initclip, only reset Cairo's clipping and skip path processing
            if item.is_initclip:
                _current_clip_path = None
                _current_clip_winding = None
                cairo_ctx.reset_clip()
                continue

            # Apply clipping path with winding rule for regular clip/eoclip
            if item.path:
                _current_clip_path = item.path
                _current_clip_winding = item.winding_rule
                # Start a new path for clipping
                cairo_ctx.new_path()

                # Build clipping path from PostScript path
                for subpath in item.path:
                    for pc_item in subpath:
                        if isinstance(pc_item, ps.MoveTo):
                            cairo_ctx.move_to(pc_item.p.x, pc_item.p.y)
                            continue
                        elif isinstance(pc_item, ps.LineTo):
                            cairo_ctx.line_to(pc_item.p.x, pc_item.p.y)
                            continue
                        elif isinstance(pc_item, ps.CurveTo):
                            cairo_ctx.curve_to(
                                pc_item.p1.x, pc_item.p1.y,
                                pc_item.p2.x, pc_item.p2.y,
                                pc_item.p3.x, pc_item.p3.y,
                            )
                            continue
                        elif isinstance(pc_item, ps.ClosePath):
                            cairo_ctx.close_path()

                # Check for degenerate clip paths (zero or near-zero width/height)
                # GhostScript handles these by giving them minimum pixel width
                x1, y1, x2, y2 = cairo_ctx.path_extents()
                path_width = abs(x2 - x1)
                path_height = abs(y2 - y1)
                # Use 0.1 device pixel — catches truly degenerate (zero-area)
                # paths while preserving legitimate narrow gradient strips
                # (e.g., Illustrator EPS gradients use ~1 device pixel strips).
                MIN_CLIP_DIMENSION = 0.1

                if path_width < MIN_CLIP_DIMENSION or path_height < MIN_CLIP_DIMENSION:
                    # Expand degenerate path to have minimum dimensions
                    # Calculate expansion needed
                    expand_x = (MIN_CLIP_DIMENSION - path_width) / 2 if path_width < MIN_CLIP_DIMENSION else 0
                    expand_y = (MIN_CLIP_DIMENSION - path_height) / 2 if path_height < MIN_CLIP_DIMENSION else 0

                    # Create expanded rectangular clip region
                    cairo_ctx.new_path()
                    cairo_ctx.rectangle(x1 - expand_x, y1 - expand_y,
                                       path_width + 2 * expand_x, path_height + 2 * expand_y)

                # Set fill rule based on winding rule
                if item.winding_rule == ps.WINDING_EVEN_ODD:
                    cairo_ctx.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
                else:
                    cairo_ctx.set_fill_rule(cairo.FILL_RULE_WINDING)

                # Apply clipping
                cairo_ctx.clip()
            continue

        # Glyph bitmap cache elements — must be checked before the capture skip
        if isinstance(item, ps.GlyphRef):
            if _is_vector_surface:
                _render_glyph_ref_vector(item, cairo_ctx)
            else:
                _render_glyph_ref(item, cairo_ctx)
            continue

        if isinstance(item, ps.GlyphStart):
            if _is_vector_surface:
                # On vector surfaces, skip bitmap capture — let Path+Fill render directly
                continue
            # Begin tracking glyph elements for bitmap capture.
            # Suppress normal rendering — glyph will be rendered offscreen at GlyphEnd.
            _glyph_capture_stack.append(_GlyphCaptureState(
                cache_key=item.cache_key,
                position_x=item.position_x,
                position_y=item.position_y,
                start_index=display_index,
            ))
            _inside_glyph_capture = True
            continue

        if isinstance(item, ps.GlyphEnd):
            if _is_vector_surface:
                continue
            _inside_glyph_capture = False
            if _glyph_capture_stack:
                state = _glyph_capture_stack.pop()
                _capture_glyph_bitmap(cairo_ctx, ctxt.display_list, display_index, state)
                # Blit the just-captured bitmap to the main surface
                _render_glyph_ref_by_key(state.cache_key, state.position_x, state.position_y, cairo_ctx)
            continue

        # Skip normal rendering when inside GlyphStart..GlyphEnd capture region.
        # These elements will be rendered offscreen at GlyphEnd instead.
        if _inside_glyph_capture:
            continue

        if isinstance(item, ps.Path):
            for subpath in item:
                # On vector surfaces (PDF/SVG), Cairo auto-closes subpaths
                # when the last point coincides with the first, creating an
                # unwanted line join. Nudge the endpoint to prevent this.
                _nudge_last_idx = -1
                if _is_vector_surface and len(subpath) >= 2 and not isinstance(subpath[-1], ps.ClosePath):
                    _first = subpath[0]
                    _last = subpath[-1]
                    if isinstance(_first, ps.MoveTo):
                        _sx, _sy = _first.p.x, _first.p.y
                        if isinstance(_last, ps.LineTo):
                            _ex, _ey = _last.p.x, _last.p.y
                        elif isinstance(_last, ps.CurveTo):
                            _ex, _ey = _last.p3.x, _last.p3.y
                        else:
                            _ex, _ey = _sx + 1, _sy  # won't match
                        if abs(_ex - _sx) < 1e-6 and abs(_ey - _sy) < 1e-6:
                            _nudge_last_idx = len(subpath) - 1

                for _sp_i, pc_item in enumerate(subpath):
                    if isinstance(pc_item, ps.MoveTo):
                        cairo_ctx.move_to(pc_item.p.x, pc_item.p.y)
                        continue
                    if isinstance(pc_item, ps.LineTo):
                        if _sp_i == _nudge_last_idx:
                            cairo_ctx.line_to(pc_item.p.x + 0.01, pc_item.p.y)
                        else:
                            cairo_ctx.line_to(pc_item.p.x, pc_item.p.y)
                        continue
                    if isinstance(pc_item, ps.CurveTo):
                        if _sp_i == _nudge_last_idx:
                            cairo_ctx.curve_to(
                                pc_item.p1.x, pc_item.p1.y,
                                pc_item.p2.x, pc_item.p2.y,
                                pc_item.p3.x + 0.01, pc_item.p3.y,
                            )
                        else:
                            cairo_ctx.curve_to(
                                pc_item.p1.x, pc_item.p1.y,
                                pc_item.p2.x, pc_item.p2.y,
                                pc_item.p3.x, pc_item.p3.y,
                            )
                        continue
                    if isinstance(pc_item, ps.ClosePath):
                        cairo_ctx.close_path()
                        continue

        if isinstance(item, ps.Fill):
            # Safely handle color - ensure we have at least 3 components
            color = item.color if item.color else [0, 0, 0]
            if len(color) >= 3:
                cairo_ctx.set_source_rgb(color[0], color[1], color[2])
            elif len(color) == 1:
                cairo_ctx.set_source_rgb(color[0], color[0], color[0])
            else:
                cairo_ctx.set_source_rgb(0, 0, 0)
            cairo_ctx.set_fill_rule(item.winding_rule)
            # PLRM 7.5.1: "A shape is scan-converted by painting any pixel
            # whose square region intersects the shape, no matter how small
            # the intersection is."  Cairo's fill() produces nothing for
            # zero-area paths (e.g. bare line segments).  Detect this by
            # comparing fill_extents (empty for degenerate paths) against
            # path_extents (non-empty if any segments exist) and fall back
            # to a 1-device-pixel hairline stroke.
            #
            # KNOWN LIMITATION: This only handles fully degenerate paths
            # (where fill_extents is empty).  Mixed paths containing both
            # enclosed areas and zero-area segments (e.g. a rectangle plus
            # a bare line, or coincident edges between subpaths) will fill
            # the enclosed areas correctly but the zero-area segments will
            # not be painted.  Fixing this would require detecting shared
            # edges between subpaths geometrically.
            fx1, fy1, fx2, fy2 = cairo_ctx.fill_extents()
            if fx1 == fx2 and fy1 == fy2:
                # Fill would produce nothing — check if path has extent
                px1, py1, px2, py2 = cairo_ctx.path_extents()
                if px1 != px2 or py1 != py2:
                    cairo_ctx.save()
                    cairo_ctx.identity_matrix()
                    cairo_ctx.set_line_width(1.0)
                    cairo_ctx.stroke()
                    cairo_ctx.restore()
                    continue
            cairo_ctx.fill()
            continue

        if isinstance(item, ps.PatternFill):
            _render_pattern_fill(item, cairo_ctx, ctxt)
            continue

        if isinstance(item, ps.AxialShadingFill):
            _render_axial_shading(item, cairo_ctx)
            continue

        if isinstance(item, ps.RadialShadingFill):
            _render_radial_shading(item, cairo_ctx)
            continue

        if isinstance(item, ps.FunctionShadingFill):
            _render_function_shading(item, cairo_ctx)
            continue

        if isinstance(item, ps.MeshShadingFill):
            # Batch consecutive MeshShadingFill items with same CTM for efficiency.
            # Many PS generators emit thousands of 1-triangle meshes; batching them
            # into a single Cairo MeshPattern is much faster.
            batch = [item]
            batch_ctm = item.ctm
            # Look ahead for more meshes with same CTM
            for j in range(display_index + 1, len(ctxt.display_list)):
                next_item = ctxt.display_list[j]
                if isinstance(next_item, ps.MeshShadingFill) and next_item.ctm == batch_ctm:
                    batch.append(next_item)
                else:
                    break
            # Mark batched items as processed by replacing them with None
            for j in range(1, len(batch)):
                ctxt.display_list[display_index + j] = None
            _render_mesh_shading_batch(batch, cairo_ctx)
            continue

        # Skip items that were batched (replaced with None)
        if item is None:
            continue

        if isinstance(item, ps.PatchShadingFill):
            _render_patch_shading(item, cairo_ctx)
            continue

        if isinstance(item, ps.Stroke):
            ctm = item.ctm
            a, b, c, d, tx, ty = ctm

            # Safely handle color - ensure we have at least 3 components
            color = item.color if item.color else [0, 0, 0]
            if len(color) >= 3:
                cairo_ctx.set_source_rgb(color[0], color[1], color[2])
            elif len(color) == 1:
                cairo_ctx.set_source_rgb(color[0], color[0], color[0])
            else:
                cairo_ctx.set_source_rgb(0, 0, 0)
            cairo_ctx.set_line_join(item.line_join)
            cairo_ctx.set_line_cap(item.line_cap)
            cairo_ctx.set_miter_limit(item.miter_limit)

            # Compute singular values of the CTM to detect anisotropy.
            # Singular values give the true max/min scale factors regardless
            # of rotation, unlike column-vector lengths which can appear
            # equal when rotation mixes the X/Y components.
            sum_sq = a*a + b*b + c*c + d*d
            diff_term = math.sqrt((a*a + b*b - c*c - d*d)**2 + 4*(a*c + b*d)**2)
            s_max = math.sqrt(max(0, 0.5 * (sum_sq + diff_term)))
            s_min = math.sqrt(max(0, 0.5 * (sum_sq - diff_term)))

            # Use anisotropic path when max/min scale ratio exceeds threshold
            det = a * d - b * c
            is_anisotropic = (s_min > 1e-10 and s_max / s_min > 1.01
                              and abs(det) > 1e-10)

            if is_anisotropic:
                # Anisotropic stroke: use Cairo's matrix to get correct
                # directional line widths.  Path points are in device space,
                # so we set Cairo's matrix to the CTM and transform each
                # point back to user space before stroking.
                inv_a = d / det
                inv_b = -b / det
                inv_c = -c / det
                inv_d = a / det

                # Extract current path from Cairo, transform to user space
                cairo_path = cairo_ctx.copy_path()
                cairo_ctx.new_path()

                # Compose CTM with existing base matrix (identity for bitmap,
                # device-to-PDF scaling for PDF surfaces) so the stroke
                # renders in the correct coordinate space.
                cairo_ctx.save()
                cairo_ctx.transform(cairo.Matrix(a, b, c, d, tx, ty))

                # Rebuild path in user space
                for path_type, points in cairo_path:
                    if path_type == 0:  # MOVE_TO
                        x, y = points
                        ux = inv_a * (x - tx) + inv_c * (y - ty)
                        uy = inv_b * (x - tx) + inv_d * (y - ty)
                        cairo_ctx.move_to(ux, uy)
                    elif path_type == 1:  # LINE_TO
                        x, y = points
                        ux = inv_a * (x - tx) + inv_c * (y - ty)
                        uy = inv_b * (x - tx) + inv_d * (y - ty)
                        cairo_ctx.line_to(ux, uy)
                    elif path_type == 2:  # CURVE_TO
                        x1, y1, x2, y2, x3, y3 = points
                        cairo_ctx.curve_to(
                            inv_a * (x1 - tx) + inv_c * (y1 - ty),
                            inv_b * (x1 - tx) + inv_d * (y1 - ty),
                            inv_a * (x2 - tx) + inv_c * (y2 - ty),
                            inv_b * (x2 - tx) + inv_d * (y2 - ty),
                            inv_a * (x3 - tx) + inv_c * (y3 - ty),
                            inv_b * (x3 - tx) + inv_d * (y3 - ty),
                        )
                    elif path_type == 3:  # CLOSE_PATH
                        cairo_ctx.close_path()

                # Stroke in user space — Cairo applies the CTM for
                # correct anisotropic line widths.
                # Ensure minimum 1 device pixel along the thinnest axis.
                min_user_lw = min_line_width / s_max if s_max > 0 else min_line_width
                cairo_ctx.set_line_width(max(item.line_width, min_user_lw))
                user_dashes = item.dash_pattern[0]
                user_offset = item.dash_pattern[1]
                cairo_ctx.set_dash(user_dashes, user_offset)
                cairo_ctx.stroke()
                cairo_ctx.restore()
                continue

            # Isotropic stroke: simple uniform scale
            scale_factor = s_max if s_max > 0 else 1.0
            device_line_width = item.line_width * scale_factor
            device_dashes = [dd * scale_factor for dd in item.dash_pattern[0]]
            device_offset = item.dash_pattern[1] * scale_factor

            cairo_ctx.set_line_width(max(min_line_width, device_line_width))
            cairo_ctx.set_dash(device_dashes, device_offset)
            cairo_ctx.stroke()
            continue

        # Check specific image types first before base ImageElement
        if isinstance(item, ps.ImageMaskElement):
            _render_imagemask_element(item, cairo_ctx, page_height)
            continue
        if isinstance(item, ps.ColorImageElement):
            _render_colorimage_element(item, cairo_ctx, page_height)
            continue
        if isinstance(item, ps.ImageElement):
            _render_image_element(item, cairo_ctx, page_height)
            continue

        # ActualText markers for Type 3 font searchability in PDF
        # Defer to PDF injector for invisible text rendering (text render mode 3)
        if isinstance(item, ps.ActualTextStart):
            if deferred_text_objs is not None:
                clip_info = (_current_clip_path, _current_clip_winding) if _current_clip_path else None
                deferred_text_objs.append((item, clip_info))
            continue
        if isinstance(item, ps.ActualTextEnd):
            continue

        # TextObj - native text rendering for PDF/SVG output
        if isinstance(item, ps.TextObj):
            _render_text_obj(item, cairo_ctx, page_height, deferred_text_objs,
                             _current_clip_path, _current_clip_winding, defer_all_text)
            continue


# TextObj rendering for searchable PDF text


# Standard 14 PDF fonts - guaranteed available in all PDF viewers
# These can be rendered with Cairo; non-Standard 14 fonts should be deferred
# for direct PDF injection to avoid Cairo's font substitution.
_STANDARD_14_FONTS = frozenset({
    b'Times-Roman',
    b'Times-Bold',
    b'Times-Italic',
    b'Times-BoldItalic',
    b'Helvetica',
    b'Helvetica-Bold',
    b'Helvetica-Oblique',
    b'Helvetica-BoldOblique',
    b'Courier',
    b'Courier-Bold',
    b'Courier-Oblique',
    b'Courier-BoldOblique',
    b'Symbol',
    b'ZapfDingbats',
})


# PostScript to system font mapping for common fonts
_PS_TO_SYSTEM_FONT = {
    # Standard 14 PDF fonts - map to common system equivalents
    b'Times-Roman': ('Times New Roman', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL),
    b'Times-Bold': ('Times New Roman', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD),
    b'Times-Italic': ('Times New Roman', cairo.FONT_SLANT_ITALIC, cairo.FONT_WEIGHT_NORMAL),
    b'Times-BoldItalic': ('Times New Roman', cairo.FONT_SLANT_ITALIC, cairo.FONT_WEIGHT_BOLD),
    b'Helvetica': ('Helvetica', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL),
    b'Helvetica-Bold': ('Helvetica', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD),
    b'Helvetica-Oblique': ('Helvetica', cairo.FONT_SLANT_OBLIQUE, cairo.FONT_WEIGHT_NORMAL),
    b'Helvetica-BoldOblique': ('Helvetica', cairo.FONT_SLANT_OBLIQUE, cairo.FONT_WEIGHT_BOLD),
    b'Courier': ('Courier', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL),
    b'Courier-Bold': ('Courier', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD),
    b'Courier-Oblique': ('Courier', cairo.FONT_SLANT_OBLIQUE, cairo.FONT_WEIGHT_NORMAL),
    b'Courier-BoldOblique': ('Courier', cairo.FONT_SLANT_OBLIQUE, cairo.FONT_WEIGHT_BOLD),
    b'Symbol': ('Symbol', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL),
    b'ZapfDingbats': ('Zapf Dingbats', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL),

    # URW fonts (commonly available on Linux) - map to system equivalents
    # NimbusRoman = Times clone
    b'NimbusRoman-Regular': ('Nimbus Roman', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL),
    b'NimbusRoman-Bold': ('Nimbus Roman', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD),
    b'NimbusRoman-Italic': ('Nimbus Roman', cairo.FONT_SLANT_ITALIC, cairo.FONT_WEIGHT_NORMAL),
    b'NimbusRoman-BoldItalic': ('Nimbus Roman', cairo.FONT_SLANT_ITALIC, cairo.FONT_WEIGHT_BOLD),
    # NimbusSans = Helvetica clone
    b'NimbusSans-Regular': ('Nimbus Sans', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL),
    b'NimbusSans-Bold': ('Nimbus Sans', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD),
    b'NimbusSans-Italic': ('Nimbus Sans', cairo.FONT_SLANT_OBLIQUE, cairo.FONT_WEIGHT_NORMAL),
    b'NimbusSans-BoldItalic': ('Nimbus Sans', cairo.FONT_SLANT_OBLIQUE, cairo.FONT_WEIGHT_BOLD),
    # NimbusSansNarrow = Helvetica Condensed clone
    b'NimbusSansNarrow-Regular': ('Nimbus Sans Narrow', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL),
    b'NimbusSansNarrow-Bold': ('Nimbus Sans Narrow', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD),
    b'NimbusSansNarrow-Oblique': ('Nimbus Sans Narrow', cairo.FONT_SLANT_OBLIQUE, cairo.FONT_WEIGHT_NORMAL),
    b'NimbusSansNarrow-BoldOblique': ('Nimbus Sans Narrow', cairo.FONT_SLANT_OBLIQUE, cairo.FONT_WEIGHT_BOLD),
    # NimbusMonoPS = Courier clone
    b'NimbusMonoPS-Regular': ('Nimbus Mono PS', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL),
    b'NimbusMonoPS-Bold': ('Nimbus Mono PS', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD),
    b'NimbusMonoPS-Italic': ('Nimbus Mono PS', cairo.FONT_SLANT_ITALIC, cairo.FONT_WEIGHT_NORMAL),
    b'NimbusMonoPS-BoldItalic': ('Nimbus Mono PS', cairo.FONT_SLANT_ITALIC, cairo.FONT_WEIGHT_BOLD),
    # C059 = Century Schoolbook clone
    b'C059-Roman': ('C059', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL),
    b'C059-Bold': ('C059', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD),
    b'C059-Italic': ('C059', cairo.FONT_SLANT_ITALIC, cairo.FONT_WEIGHT_NORMAL),
    b'C059-BdIta': ('C059', cairo.FONT_SLANT_ITALIC, cairo.FONT_WEIGHT_BOLD),
    # P052 = Palatino clone
    b'P052-Roman': ('P052', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL),
    b'P052-Bold': ('P052', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD),
    b'P052-Italic': ('P052', cairo.FONT_SLANT_ITALIC, cairo.FONT_WEIGHT_NORMAL),
    b'P052-BoldItalic': ('P052', cairo.FONT_SLANT_ITALIC, cairo.FONT_WEIGHT_BOLD),
    # URWBookman = ITC Bookman clone
    b'URWBookman-Light': ('URW Bookman', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL),
    b'URWBookman-Demi': ('URW Bookman', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD),
    b'URWBookman-LightItalic': ('URW Bookman', cairo.FONT_SLANT_ITALIC, cairo.FONT_WEIGHT_NORMAL),
    b'URWBookman-DemiItalic': ('URW Bookman', cairo.FONT_SLANT_ITALIC, cairo.FONT_WEIGHT_BOLD),
    # URWGothic = ITC Avant Garde clone
    b'URWGothic-Book': ('URW Gothic', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL),
    b'URWGothic-Demi': ('URW Gothic', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD),
    b'URWGothic-BookOblique': ('URW Gothic', cairo.FONT_SLANT_OBLIQUE, cairo.FONT_WEIGHT_NORMAL),
    b'URWGothic-DemiOblique': ('URW Gothic', cairo.FONT_SLANT_OBLIQUE, cairo.FONT_WEIGHT_BOLD),
    # Z003 = ITC Zapf Chancery clone
    b'Z003-MediumItalic': ('Z003', cairo.FONT_SLANT_ITALIC, cairo.FONT_WEIGHT_NORMAL),
}


def _render_text_obj(text_obj, cairo_ctx, page_height, deferred_text_objs=None,
                     clip_path=None, clip_winding=None, defer_all_text=False):
    """
    Render TextObj using Cairo's native text APIs or defer for PDF/SVG injection.

    When cairo_ctx is backed by PDFSurface, this produces native PDF text
    operators (searchable, selectable text) for Standard 14 fonts.

    For non-Standard 14 fonts, if deferred_text_objs is provided, the TextObj
    is added to that list for later PDF injection (bypassing Cairo's font
    substitution). This ensures proper font embedding with correct metrics.

    When defer_all_text is True (SVG device), ALL TextObjs are deferred
    regardless of font, so the device can emit native SVG text elements.

    Type 3 fonts never reach here — they use ActualTextStart/ActualTextEnd
    markers around GlyphPaths rendering instead.

    Args:
        text_obj: TextObj display list element
        cairo_ctx: Cairo context to render to
        page_height: Page height for Y-coordinate transformation
        deferred_text_objs: Optional list to collect non-Standard 14 TextObjs
        clip_path: Current clip path (for deferred text clipping in PDF)
        clip_winding: Current clip winding rule
        defer_all_text: If True, defer ALL TextObjs (used by SVG device)
    """

    # Build clip info tuple for deferred text (None if no active clip)
    clip_info = (clip_path, clip_winding) if clip_path else None

    # SVG mode: defer all text for native SVG text elements
    if defer_all_text and deferred_text_objs is not None:
        deferred_text_objs.append((text_obj, clip_info))
        return

    # Check if this font needs embedding via PDF injection
    font_name = text_obj.font_name
    if deferred_text_objs is not None:
        if font_name not in _STANDARD_14_FONTS:
            # Non-Standard 14 font: defer for direct PDF injection
            deferred_text_objs.append((text_obj, clip_info))
            return
        # Standard 14 font with custom encoding array (e.g., DiacriticEncoding):
        # must be embedded so the PDF viewer uses the correct encoding.
        # Cairo doesn't respect PostScript re-encoding, so character codes
        # would map to wrong glyphs if we let Cairo handle it.
        encoding = text_obj.font_dict.val.get(b'Encoding')
        if encoding is not None and encoding.TYPE in ps.ARRAY_TYPES:
            deferred_text_objs.append((text_obj, clip_info))
            return

    # Standard 14 fonts (or no deferral): Use Cairo text APIs
    _render_standard_text_obj(text_obj, cairo_ctx, page_height)


def _render_standard_text_obj(text_obj, cairo_ctx, page_height):
    """
    Render text using Cairo's native text APIs.

    Maps PostScript font names to system fonts and uses Cairo's show_text.
    Applies the stored CTM for correct rotation/skew rendering.

    Args:
        text_obj: TextObj display list element
        cairo_ctx: Cairo context
        page_height: Page height for Y-coordinate transformation
    """
    # Map PostScript font to system font
    font_name = text_obj.font_name
    if font_name in _PS_TO_SYSTEM_FONT:
        family, slant, weight = _PS_TO_SYSTEM_FONT[font_name]
    else:
        # Fallback: try to use the font name directly
        if isinstance(font_name, bytes):
            family = font_name.decode('latin-1', errors='replace')
        else:
            family = str(font_name)
        slant = cairo.FONT_SLANT_NORMAL
        weight = cairo.FONT_WEIGHT_NORMAL

    # Set color
    color = text_obj.color
    if len(color) >= 3:
        cairo_ctx.set_source_rgb(color[0], color[1], color[2])
    elif len(color) == 1:
        cairo_ctx.set_source_rgb(color[0], color[0], color[0])
    else:
        cairo_ctx.set_source_rgb(0, 0, 0)

    # Decode text bytes to string for Cairo
    try:
        text_str = text_obj.text.decode('latin-1')
    except (UnicodeDecodeError, AttributeError):
        text_str = str(text_obj.text)

    # Apply CTM for correct rotation/skew — same technique as anisotropic stroke.
    # Compose CTM with Cairo's base matrix, transform position to user space,
    # and set font size in user space so Cairo applies the full transformation.
    ctm = text_obj.ctm
    a, b, c, d, tx, ty = ctm
    det = a * d - b * c

    if abs(det) > 1e-10:
        # Ensure the CTM includes a Y-reflection for correct glyph orientation.
        # Font glyphs have Y-up; Cairo/PDF content space has Y-down.
        # Normal PS CTMs have det < 0 (Y-flip included). DVIPS CTMs have det > 0
        # (DVIPS applies its own neg scale, cancelling the default Y-flip).
        # When det(CTM)*det(FM) > 0, negate c and d to add the required reflection.
        # Using the combined determinant handles fonts with negative Y scaling
        # (e.g., DVIPS fonts using makefont [s 0 0 -s 0 0]).
        fm = text_obj.font_matrix
        fm_det = (fm[0] * fm[3] - fm[1] * fm[2]) if fm else 1.0
        if det * fm_det > 0:
            c, d = -c, -d
            det = -det

        # Inverse-CTM to transform device-space position to user space
        inv_a = d / det
        inv_b = -b / det
        inv_c = -c / det
        inv_d = a / det
        x = text_obj.start_x
        y = text_obj.start_y
        ux = inv_a * (x - tx) + inv_c * (y - ty)
        uy = inv_b * (x - tx) + inv_d * (y - ty)

        cairo_ctx.save()
        cairo_ctx.transform(cairo.Matrix(a, b, c, d, tx, ty))
        cairo_ctx.select_font_face(family, slant, weight)
        # Use font matrix for non-uniform scaling (e.g., [40 0 0 15 0 0] makefont)
        if fm:
            cairo_ctx.set_font_matrix(cairo.Matrix(fm[0], fm[1], fm[2], fm[3], 0, 0))
        else:
            sx = math.sqrt(a * a + b * b)
            sy = math.sqrt(c * c + d * d)
            ctm_scale = math.sqrt(sx * sy)
            user_font_size = text_obj.font_size / ctm_scale if ctm_scale > 0 else text_obj.font_size
            cairo_ctx.set_font_size(user_font_size)
        cairo_ctx.move_to(ux, uy)
        cairo_ctx.show_text(text_str)
        cairo_ctx.restore()
    else:
        # Degenerate CTM — fall back to simple rendering
        cairo_ctx.select_font_face(family, slant, weight)
        cairo_ctx.set_font_size(text_obj.font_size)
        cairo_ctx.move_to(text_obj.start_x, text_obj.start_y)
        cairo_ctx.show_text(text_str)


# Glyph bitmap capture state
class _GlyphCaptureState:
    """Tracks state between GlyphStart and GlyphEnd for bitmap capture."""
    __slots__ = ('cache_key', 'position_x', 'position_y', 'start_index')

    def __init__(self, cache_key, position_x, position_y, start_index):
        self.cache_key = cache_key
        self.position_x = position_x
        self.position_y = position_y
        self.start_index = start_index


def _render_glyph_ref_vector(glyph_ref, cairo_ctx):
    """Replay cached glyph display elements as vector paths (for PDF surfaces).

    Looks up the PS-level glyph cache for the normalized display elements
    and replays Path + Fill + ImageMask operations translated to the glyph position.
    """
    path_cache = global_resources.get_glyph_cache()
    cached = path_cache.get(glyph_ref.cache_key)
    if cached is None or not cached.display_elements:
        return

    ox = glyph_ref.position_x
    oy = glyph_ref.position_y

    for element in cached.display_elements:
        if isinstance(element, ps.Path):
            for subpath in element:
                for pc_item in subpath:
                    if isinstance(pc_item, ps.MoveTo):
                        cairo_ctx.move_to(pc_item.p.x + ox, pc_item.p.y + oy)
                    elif isinstance(pc_item, ps.LineTo):
                        cairo_ctx.line_to(pc_item.p.x + ox, pc_item.p.y + oy)
                    elif isinstance(pc_item, ps.CurveTo):
                        cairo_ctx.curve_to(
                            pc_item.p1.x + ox, pc_item.p1.y + oy,
                            pc_item.p2.x + ox, pc_item.p2.y + oy,
                            pc_item.p3.x + ox, pc_item.p3.y + oy,
                        )
                    elif isinstance(pc_item, ps.ClosePath):
                        cairo_ctx.close_path()
        elif isinstance(element, ps.Fill):
            color = element.color if element.color else [0, 0, 0]
            if len(color) >= 3:
                cairo_ctx.set_source_rgb(color[0], color[1], color[2])
            elif len(color) == 1:
                cairo_ctx.set_source_rgb(color[0], color[0], color[0])
            else:
                cairo_ctx.set_source_rgb(0, 0, 0)
            cairo_ctx.set_fill_rule(element.winding_rule)
            cairo_ctx.fill()
        elif isinstance(element, ps.ImageMaskElement):
            # Translate the cached imagemask CTM to the glyph position
            elem = copy.copy(element)
            if element.ctm is not None:
                elem.ctm = element.ctm.copy()
                elem.ctm[4] += ox
                elem.ctm[5] += oy
            elem.CTM = element.CTM.copy()
            elem.CTM[4] += ox
            elem.CTM[5] += oy
            _render_imagemask_element(elem, cairo_ctx, 0)


def _render_glyph_ref(glyph_ref, cairo_ctx):
    """Blit a cached glyph bitmap to the main surface.

    If the bitmap hasn't been captured yet (e.g., the page that would have
    captured it was skipped by --pages), captures it from the path cache
    so subsequent hits use the fast bitmap path.
    """
    bitmap_cache = global_resources.get_glyph_bitmap_cache()
    if bitmap_cache.get(glyph_ref.cache_key) is not None:
        _render_glyph_ref_by_key(glyph_ref.cache_key, glyph_ref.position_x, glyph_ref.position_y, cairo_ctx)
        return

    # Bitmap not captured (page rendering was skipped).
    # Get display elements from the path cache and capture a bitmap now.
    path_cache = global_resources.get_glyph_cache()
    cached = path_cache.get(glyph_ref.cache_key)
    if cached is None or not cached.display_elements:
        return

    # Path cache elements are normalized to origin (translation excluded from
    # cache key), so pass the glyph position as origin offset to place them
    # at the correct device-space coordinates during capture.
    _capture_glyph_elements(
        glyph_ref.cache_key, glyph_ref.position_x, glyph_ref.position_y,
        cached.display_elements, cairo_ctx,
        origin_offset_x=glyph_ref.position_x, origin_offset_y=glyph_ref.position_y,
    )
    _render_glyph_ref_by_key(glyph_ref.cache_key, glyph_ref.position_x, glyph_ref.position_y, cairo_ctx)


def _render_glyph_ref_by_key(cache_key, position_x, position_y, cairo_ctx):
    """Blit a cached glyph bitmap at the given position.

    The bitmap origin was computed relative to the floor'd (pixel-aligned)
    position during capture, so we must also floor the position here to
    ensure consistent pixel-grid alignment across all glyph instances.
    """
    bitmap_cache = global_resources.get_glyph_bitmap_cache()
    cached = bitmap_cache.get(cache_key)
    if cached is None:
        return

    cairo_ctx.save()
    # Use floor'd position + integer origin for consistent baseline alignment.
    # All glyphs on the same line have the same floor(position_y), ensuring
    # they all align to the same baseline regardless of their individual ink extents.
    floor_x = math.floor(position_x)
    floor_y = math.floor(position_y)
    place_x = floor_x + cached.origin_x
    place_y = floor_y + cached.origin_y

    cairo_ctx.set_source_surface(
        cached.surface,
        place_x,
        place_y,
    )
    # Use BILINEAR for smooth bitmap font rendering when scaled
    cairo_ctx.get_source().set_filter(cairo.FILTER_BILINEAR)
    cairo_ctx.paint()
    cairo_ctx.restore()


def _capture_glyph_bitmap(cairo_ctx, display_list, end_index, state):
    """Capture the glyph region rendered between GlyphStart and GlyphEnd to a bitmap.

    Uses RecordingSurface to get ink extents, then renders directly to an
    ImageSurface using the same rendering code as the main surface to ensure
    pixel-perfect matching.
    """
    bitmap_cache = global_resources.get_glyph_bitmap_cache()

    # Already cached (concurrent or duplicate)
    if bitmap_cache.get(state.cache_key) is not None:
        return

    # Collect the glyph's display elements between GlyphStart+1 and GlyphEnd
    glyph_elements = display_list[state.start_index + 1:end_index]
    if not glyph_elements:
        return

    _capture_glyph_elements(state.cache_key, state.position_x, state.position_y,
                            glyph_elements, cairo_ctx)


def _capture_glyph_elements(cache_key, position_x, position_y, glyph_elements, cairo_ctx,
                            origin_offset_x=0.0, origin_offset_y=0.0):
    """Capture glyph display elements to a bitmap and store in the bitmap cache.

    Renders the elements to a RecordingSurface to determine ink extents, then
    renders to an ImageSurface for pixel-perfect bitmap caching.  Called from
    both the normal GlyphEnd path and the --pages fallback path.

    Args:
        cache_key: GlyphCacheKey for bitmap cache storage
        position_x: Device-space X position of the glyph
        position_y: Device-space Y position of the glyph
        glyph_elements: Sequence of display list elements (Path, Fill, etc.)
        cairo_ctx: Cairo context (used for antialias setting)
        origin_offset_x: Extra X translation for elements stored at origin (0.0
            for display list elements already at device position, position_x for
            path cache elements normalized to origin)
        origin_offset_y: Extra Y translation (same convention as origin_offset_x)
    """
    bitmap_cache = global_resources.get_glyph_bitmap_cache()

    # Already cached (concurrent or duplicate)
    if bitmap_cache.get(cache_key) is not None:
        return

    # Compute translation to render at floor'd position (integer coordinates).
    # This ensures ink_extents are consistent regardless of original sub-pixel offset,
    # which is critical because Cairo's antialiasing produces different bounds at
    # different sub-pixel positions. Since replay uses floor(position) + origin,
    # capturing at floor'd position ensures perfect alignment.
    baseline_floor_x = math.floor(position_x)
    baseline_floor_y = math.floor(position_y)
    snap_translate_x = baseline_floor_x - position_x
    snap_translate_y = baseline_floor_y - position_y

    # First pass: render to RecordingSurface just to get ink extents
    rec_surface = cairo.RecordingSurface(cairo.CONTENT_COLOR_ALPHA, None)
    rec_ctx = cairo.Context(rec_surface)
    rec_ctx.set_antialias(cairo_ctx.get_antialias())
    rec_ctx.translate(snap_translate_x + origin_offset_x, snap_translate_y + origin_offset_y)
    _replay_glyph_elements(rec_ctx, glyph_elements)
    del rec_ctx  # flush

    ix, iy, iw, ih = rec_surface.ink_extents()
    rec_surface.finish()

    if iw <= 0 or ih <= 0:
        return

    # Surface dimensions with 1px padding
    pad = 1
    width = int(math.ceil(iw)) + 2 * pad
    height = int(math.ceil(ih)) + 2 * pad

    if width > 4096 or height > 4096:
        return

    # Absolute top-left of the bitmap in device space
    abs_origin_x = math.floor(ix) - pad
    abs_origin_y = math.floor(iy) - pad

    # Second pass: render directly to ImageSurface with same snap translation.
    # This ensures pixel-perfect matching with the ink extents from first pass.
    backing_data = bytearray(width * height * 4)
    surface = cairo.ImageSurface.create_for_data(
        backing_data, cairo.FORMAT_ARGB32, width, height
    )
    off_ctx = cairo.Context(surface)
    off_ctx.set_antialias(cairo_ctx.get_antialias())
    # First translate to position the bitmap, then apply snap translation
    off_ctx.translate(-abs_origin_x + snap_translate_x + origin_offset_x,
                      -abs_origin_y + snap_translate_y + origin_offset_y)
    _replay_glyph_elements(off_ctx, glyph_elements)
    del off_ctx
    surface.flush()

    # Store origin relative to floor'd position (integer-based).
    # Since we captured at floor'd position, ink_extents are consistent for all
    # captures of the same glyph regardless of original sub-pixel offset.
    origin_x = abs_origin_x - baseline_floor_x
    origin_y = abs_origin_y - baseline_floor_y

    bitmap = CachedBitmap(
        surface=surface,
        width=width,
        height=height,
        origin_x=origin_x,
        origin_y=origin_y,
        backing_data=backing_data,
    )
    bitmap_cache.put(cache_key, bitmap)


def _replay_glyph_elements(ctx, elements):
    """Replay a sequence of display list elements (Path, Fill, Stroke, ImageMask, etc.) to a Cairo context."""
    for elem in elements:
        if isinstance(elem, ps.Path):
            for subpath in elem:
                for pc_item in subpath:
                    if isinstance(pc_item, ps.MoveTo):
                        ctx.move_to(pc_item.p.x, pc_item.p.y)
                    elif isinstance(pc_item, ps.LineTo):
                        ctx.line_to(pc_item.p.x, pc_item.p.y)
                    elif isinstance(pc_item, ps.CurveTo):
                        ctx.curve_to(
                            pc_item.p1.x, pc_item.p1.y,
                            pc_item.p2.x, pc_item.p2.y,
                            pc_item.p3.x, pc_item.p3.y,
                        )
                    elif isinstance(pc_item, ps.ClosePath):
                        ctx.close_path()
        elif isinstance(elem, ps.Fill):
            color = elem.color if elem.color else [0, 0, 0]
            if len(color) >= 3:
                ctx.set_source_rgb(color[0], color[1], color[2])
            elif len(color) == 1:
                ctx.set_source_rgb(color[0], color[0], color[0])
            else:
                ctx.set_source_rgb(0, 0, 0)
            ctx.set_fill_rule(elem.winding_rule)
            ctx.fill()
        elif isinstance(elem, ps.Stroke):
            ctm = elem.ctm
            scale_factor = math.sqrt(ctm[0]**2 + ctm[1]**2)
            device_line_width = elem.line_width * scale_factor
            device_dashes = [d * scale_factor for d in elem.dash_pattern[0]]
            device_offset = elem.dash_pattern[1] * scale_factor
            color = elem.color if elem.color else [0, 0, 0]
            if len(color) >= 3:
                ctx.set_source_rgb(color[0], color[1], color[2])
            elif len(color) == 1:
                ctx.set_source_rgb(color[0], color[0], color[0])
            else:
                ctx.set_source_rgb(0, 0, 0)
            ctx.set_line_width(max(1, device_line_width))
            ctx.set_line_join(elem.line_join)
            ctx.set_line_cap(elem.line_cap)
            ctx.set_miter_limit(elem.miter_limit)
            ctx.set_dash(device_dashes, device_offset)
            ctx.stroke()
        elif isinstance(elem, ps.ImageMaskElement):
            _render_imagemask_element(elem, ctx, 0)
        elif isinstance(elem, ps.ColorImageElement):
            _render_colorimage_element(elem, ctx, 0)
        elif isinstance(elem, ps.ImageElement):
            _render_image_element(elem, ctx, 0)
