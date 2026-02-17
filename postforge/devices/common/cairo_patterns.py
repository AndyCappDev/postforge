# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Cairo Pattern Fill Rendering Module

Renders Type 1 (tiling) and Type 2 (shading) pattern fills
from PostScript display list elements to Cairo contexts.
"""

import cairo

from ...core import types as ps
from .cairo_shading import _add_gradient_stops


def _render_pattern_fill(item, cairo_ctx, ctxt):
    """
    Render a pattern fill using Cairo surface patterns.

    This function:
    1. Gets pattern parameters from the pattern dictionary
    2. Creates an offscreen surface for the pattern cell
    3. Renders the cached display list (from makepattern) to the cell
    4. Creates a Cairo surface pattern for tiling
    5. Fills the current path with the pattern

    The PaintProc is executed during makepattern (not here) to ensure
    all dictionary definitions are in scope at execution time.

    Args:
        item: PatternFill display list element
        cairo_ctx: Cairo context to render to
        ctxt: PostScript context (unused, kept for interface compatibility)
    """
    pattern_dict = item.pattern_dict
    winding_rule = item.winding_rule
    underlying_color = item.underlying_color

    # Get Implementation data
    if not hasattr(pattern_dict, '_pattern_impl'):
        # Fall back to solid color if no implementation
        if underlying_color and len(underlying_color) >= 3:
            cairo_ctx.set_source_rgb(underlying_color[0], underlying_color[1], underlying_color[2])
        elif underlying_color and len(underlying_color) >= 1:
            cairo_ctx.set_source_rgb(underlying_color[0], underlying_color[0], underlying_color[0])
        else:
            cairo_ctx.set_source_rgb(0, 0, 0)
        cairo_ctx.set_fill_rule(winding_rule)
        cairo_ctx.fill()
        return

    impl = pattern_dict._pattern_impl
    pattern_matrix = impl['pattern_matrix']
    pattern_type = impl.get('pattern_type', 1)

    # Handle Type 2 (shading) patterns
    if pattern_type == 2:
        _render_shading_pattern_fill(item, cairo_ctx, impl)
        return

    # Type 1 (tiling) pattern handling
    bbox = impl['bbox']
    xstep = impl['xstep']
    ystep = impl['ystep']

    # Get paint type for Type 1 patterns
    paint_type = int(pattern_dict.val.get(b'PaintType', ps.Int(1)).val)

    # Calculate pattern cell size in device pixels
    # The pattern matrix transforms from pattern space to device space
    a, b, c, d, tx, ty = pattern_matrix

    # Transform BBox to get cell size
    # BBox is [left, bottom, right, top] in pattern coordinates
    llx, lly, urx, ury = bbox

    # Calculate device cell dimensions using the pattern matrix
    # We need to determine appropriate cell size for rendering
    cell_width = abs(xstep * a + 0 * c)
    cell_height = abs(0 * b + ystep * d)

    # Ensure minimum reasonable size
    cell_width = max(1, int(cell_width + 0.5))
    cell_height = max(1, int(cell_height + 0.5))

    # Cap maximum size to avoid memory issues
    MAX_CELL_SIZE = 2048
    if cell_width > MAX_CELL_SIZE or cell_height > MAX_CELL_SIZE:
        scale_factor = MAX_CELL_SIZE / max(cell_width, cell_height)
        cell_width = int(cell_width * scale_factor)
        cell_height = int(cell_height * scale_factor)

    if cell_width < 1 or cell_height < 1:
        # Degenerate pattern - use solid fill
        if underlying_color and len(underlying_color) >= 3:
            cairo_ctx.set_source_rgb(underlying_color[0], underlying_color[1], underlying_color[2])
        elif underlying_color and len(underlying_color) >= 1:
            cairo_ctx.set_source_rgb(underlying_color[0], underlying_color[0], underlying_color[0])
        else:
            cairo_ctx.set_source_rgb(0, 0, 0)
        cairo_ctx.set_fill_rule(winding_rule)
        cairo_ctx.fill()
        return

    # Create offscreen surface for pattern cell
    pattern_surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, cell_width, cell_height)
    pattern_ctx = cairo.Context(pattern_surface)

    # Set up the pattern context with transparent background
    pattern_ctx.set_source_rgba(0, 0, 0, 0)
    pattern_ctx.paint()

    # Use cached display list from makepattern (PaintProc was executed during makepattern)
    cached_dl = impl.get('cached_display_list', [])

    # Calculate transformation from pattern space to surface space
    # Pattern space coordinates are in the cached display list (with identity CTM)
    # Surface space: origin at (0, 0), extends to (cell_width, cell_height)
    # Need Y-flip: PostScript Y-up -> Cairo Y-down
    scale_x = cell_width / abs(xstep) if xstep != 0 else 1
    scale_y = cell_height / abs(ystep) if ystep != 0 else 1

    # Set up Cairo transformation matrix for pattern rendering
    # Transform: x' = scale_x * (x - llx), y' = cell_height - scale_y * (y - lly)
    pattern_ctx.translate(-llx * scale_x, cell_height + lly * scale_y)
    pattern_ctx.scale(scale_x, -scale_y)  # Negative Y for flip

    # Render the cached display list to the pattern surface
    for dl_item in cached_dl:
        if isinstance(dl_item, ps.Path):
            for subpath in dl_item:
                for pc_item in subpath:
                    if isinstance(pc_item, ps.MoveTo):
                        pattern_ctx.move_to(pc_item.p.x, pc_item.p.y)
                    elif isinstance(pc_item, ps.LineTo):
                        pattern_ctx.line_to(pc_item.p.x, pc_item.p.y)
                    elif isinstance(pc_item, ps.CurveTo):
                        pattern_ctx.curve_to(
                            pc_item.p1.x, pc_item.p1.y,
                            pc_item.p2.x, pc_item.p2.y,
                            pc_item.p3.x, pc_item.p3.y)
                    elif isinstance(pc_item, ps.ClosePath):
                        pattern_ctx.close_path()
        elif isinstance(dl_item, ps.Fill):
            # Safely extract RGB color with defaults
            color = dl_item.color if dl_item.color else [0, 0, 0]
            if len(color) >= 3:
                pattern_ctx.set_source_rgb(color[0], color[1], color[2])
            elif len(color) == 1:
                # Grayscale
                pattern_ctx.set_source_rgb(color[0], color[0], color[0])
            else:
                pattern_ctx.set_source_rgb(0, 0, 0)
            pattern_ctx.set_fill_rule(dl_item.winding_rule)
            pattern_ctx.fill()
        elif isinstance(dl_item, ps.Stroke):
            # Safely extract RGB color with defaults
            color = dl_item.color if dl_item.color else [0, 0, 0]
            if len(color) >= 3:
                pattern_ctx.set_source_rgb(color[0], color[1], color[2])
            elif len(color) == 1:
                # Grayscale
                pattern_ctx.set_source_rgb(color[0], color[0], color[0])
            else:
                pattern_ctx.set_source_rgb(0, 0, 0)
            # Line width is already in pattern space from cached display list
            pattern_ctx.set_line_width(dl_item.line_width)
            pattern_ctx.set_line_cap(dl_item.line_cap)
            pattern_ctx.set_line_join(dl_item.line_join)
            # Set dash pattern if present
            if hasattr(dl_item, 'dash_pattern') and dl_item.dash_pattern:
                dashes, offset = dl_item.dash_pattern
                if dashes:
                    pattern_ctx.set_dash(dashes, offset)
            pattern_ctx.stroke()

    # If cached display list is empty, draw a fallback pattern
    if not cached_dl:
        pattern_ctx.identity_matrix()
        pattern_ctx.set_source_rgba(0.7, 0.7, 0.7, 1.0)
        pattern_ctx.rectangle(0, 0, cell_width, cell_height)
        pattern_ctx.fill()
        pattern_ctx.set_source_rgba(0.3, 0.3, 0.3, 1.0)
        pattern_ctx.set_line_width(1)
        pattern_ctx.move_to(0, 0)
        pattern_ctx.line_to(cell_width, cell_height)
        pattern_ctx.stroke()

    # Create surface pattern from the rendered cell
    pattern = cairo.SurfacePattern(pattern_surface)
    pattern.set_extend(cairo.EXTEND_REPEAT)

    # Set up pattern matrix to position the pattern correctly in device space
    # The pattern_matrix maps from pattern space to device space
    # Cairo's pattern matrix maps from device space to pattern surface space
    #
    # We need: device_space -> pattern_surface_space
    # This is: scale(1/cell_width, 1/cell_height) × pattern_space_to_surface × inverse(pattern_matrix)
    #
    # For simplicity, we compute the inverse of the pattern_matrix and scale appropriately
    det = a * d - b * c
    if abs(det) > 1e-10:
        # Inverse of pattern_matrix
        inv_a = d / det
        inv_b = -b / det
        inv_c = -c / det
        inv_d = a / det
        inv_tx = (b * ty - d * tx) / det
        inv_ty = (c * tx - a * ty) / det

        # The pattern surface has coordinates [0, cell_width] x [0, cell_height]
        # which corresponds to pattern space [llx, llx+xstep] x [lly, lly+ystep]
        # So we need additional scaling: pattern_surface = (pattern - ll) * cell_size / step

        # Combined transformation from device space to pattern surface:
        # 1. Apply inverse pattern_matrix to get to pattern space
        # 2. Translate by -ll to move origin
        # 3. Scale by cell_size/step to get to surface coordinates
        # 4. Flip Y because we rendered with Y-flip

        sx = cell_width / abs(xstep) if xstep != 0 else 1
        sy = cell_height / abs(ystep) if ystep != 0 else 1

        # Build the combined matrix
        # First inverse(pattern_matrix), then translate(-llx, -lly), then scale, then Y-flip
        # For Y-flip in pattern surface: y_surface = cell_height - y_surface_unflipped
        # This changes the d component sign and adjusts ty

        # Step by step:
        # After inverse: (x', y') = inv_matrix * (device_x, device_y)
        # After translate: (x'', y'') = (x' - llx, y' - lly)
        # After scale: (xs, ys) = (x'' * sx, y'' * sy)
        # After Y-flip: (xf, yf) = (xs, cell_height - ys)

        # Combining these:
        # xf = (inv_a * dx + inv_c * dy + inv_tx - llx) * sx
        # yf = cell_height - (inv_b * dx + inv_d * dy + inv_ty - lly) * sy

        # This gives us the matrix coefficients:
        ma = inv_a * sx
        mb = -inv_b * sy  # Negated for Y-flip
        mc = inv_c * sx
        md = -inv_d * sy  # Negated for Y-flip
        mtx = (inv_tx - llx) * sx
        mty = cell_height + (lly - inv_ty) * sy

        m = cairo.Matrix(ma, mb, mc, md, mtx, mty)
        pattern.set_matrix(m)

    # Apply the pattern fill
    cairo_ctx.set_source(pattern)
    cairo_ctx.set_fill_rule(winding_rule)
    cairo_ctx.fill()


def _render_shading_pattern_fill(item, cairo_ctx, impl):
    """
    Render a Type 2 (shading) pattern fill.

    Type 2 patterns use a Shading dictionary to define the fill content.
    The shading is rendered using the pattern matrix to transform coordinates.

    Args:
        item: PatternFill display list element
        cairo_ctx: Cairo context to render to
        impl: Pattern implementation data
    """
    shading_dict = impl.get('shading')
    if not shading_dict:
        # Fallback to black fill if no shading
        cairo_ctx.set_source_rgb(0, 0, 0)
        cairo_ctx.fill()
        return

    winding_rule = item.winding_rule
    pattern_matrix = impl['pattern_matrix']
    a, b, c, d, tx, ty = pattern_matrix

    # Get shading type
    shading_type = int(shading_dict.val[b'ShadingType'].val)

    # Get color space
    cs_entry = shading_dict.val.get(b'ColorSpace', None)
    if cs_entry:
        if cs_entry.TYPE == ps.T_NAME:
            color_space = cs_entry.val.decode() if isinstance(cs_entry.val, bytes) else str(cs_entry.val)
        elif cs_entry.TYPE in ps.ARRAY_TYPES and cs_entry.length > 0:
            first = cs_entry.val[cs_entry.start]
            color_space = first.val.decode() if isinstance(first.val, bytes) else str(first.val)
        else:
            color_space = "DeviceGray"
    else:
        color_space = "DeviceGray"

    if shading_type == 2:
        # Axial shading
        coords = shading_dict.val.get(b'Coords')
        if coords and coords.TYPE in ps.ARRAY_TYPES and coords.length >= 4:
            x0 = float(coords.val[coords.start].val)
            y0 = float(coords.val[coords.start + 1].val)
            x1 = float(coords.val[coords.start + 2].val)
            y1 = float(coords.val[coords.start + 3].val)

            # Transform coordinates using pattern matrix
            x0_dev = a * x0 + c * y0 + tx
            y0_dev = b * x0 + d * y0 + ty
            x1_dev = a * x1 + c * y1 + tx
            y1_dev = b * x1 + d * y1 + ty

            # Create Cairo linear gradient
            gradient = cairo.LinearGradient(x0_dev, y0_dev, x1_dev, y1_dev)

            # Get function and evaluate color stops
            func = shading_dict.val.get(b'Function')
            if func:
                _add_gradient_stops(gradient, func, color_space)
            else:
                # Default gradient: black to white
                gradient.add_color_stop_rgb(0, 0, 0, 0)
                gradient.add_color_stop_rgb(1, 1, 1, 1)

            # Handle Extend parameter
            extend = shading_dict.val.get(b'Extend')
            if extend and extend.TYPE in ps.ARRAY_TYPES and extend.length >= 2:
                extend_start = extend.val[extend.start].val
                extend_end = extend.val[extend.start + 1].val
                if extend_start or extend_end:
                    gradient.set_extend(cairo.EXTEND_PAD)

            cairo_ctx.set_source(gradient)
            cairo_ctx.set_fill_rule(winding_rule)
            cairo_ctx.fill()
            return

    elif shading_type == 3:
        # Radial shading
        coords = shading_dict.val.get(b'Coords')
        if coords and coords.TYPE in ps.ARRAY_TYPES and coords.length >= 6:
            x0 = float(coords.val[coords.start].val)
            y0 = float(coords.val[coords.start + 1].val)
            r0 = float(coords.val[coords.start + 2].val)
            x1 = float(coords.val[coords.start + 3].val)
            y1 = float(coords.val[coords.start + 4].val)
            r1 = float(coords.val[coords.start + 5].val)

            # Transform center coordinates using pattern matrix
            x0_dev = a * x0 + c * y0 + tx
            y0_dev = b * x0 + d * y0 + ty
            x1_dev = a * x1 + c * y1 + tx
            y1_dev = b * x1 + d * y1 + ty

            # Scale radii (use average scale factor)
            scale = (abs(a) + abs(d)) / 2
            r0_dev = r0 * scale
            r1_dev = r1 * scale

            # Create Cairo radial gradient
            gradient = cairo.RadialGradient(x0_dev, y0_dev, r0_dev, x1_dev, y1_dev, r1_dev)

            # Get function and evaluate color stops
            func = shading_dict.val.get(b'Function')
            if func:
                _add_gradient_stops(gradient, func, color_space)
            else:
                # Default gradient: black to white
                gradient.add_color_stop_rgb(0, 0, 0, 0)
                gradient.add_color_stop_rgb(1, 1, 1, 1)

            # Handle Extend parameter
            extend = shading_dict.val.get(b'Extend')
            if extend and extend.TYPE in ps.ARRAY_TYPES and extend.length >= 2:
                extend_start = extend.val[extend.start].val
                extend_end = extend.val[extend.start + 1].val
                if extend_start or extend_end:
                    gradient.set_extend(cairo.EXTEND_PAD)

            cairo_ctx.set_source(gradient)
            cairo_ctx.set_fill_rule(winding_rule)
            cairo_ctx.fill()
            return

    # Unsupported shading type - fall back to gray fill
    cairo_ctx.set_source_rgb(0.5, 0.5, 0.5)
    cairo_ctx.set_fill_rule(winding_rule)
    cairo_ctx.fill()
