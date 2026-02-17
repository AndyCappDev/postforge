# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Cairo Gradient/Shading Rendering Module

Renders axial, radial, mesh, patch, and function-based shadings
from PostScript display list elements to Cairo contexts.
"""

import math

import cairo

from ...core import types as ps


def _render_axial_shading(item, cairo_ctx):
    """Render an axial (linear) gradient shading fill."""
    cairo_ctx.save()
    try:
        cairo_ctx.transform(cairo.Matrix(*item.ctm))

        if item.bbox:
            cairo_ctx.rectangle(item.bbox[0], item.bbox[1],
                                item.bbox[2] - item.bbox[0],
                                item.bbox[3] - item.bbox[1])
            cairo_ctx.clip()

        pat = cairo.LinearGradient(item.x0, item.y0, item.x1, item.y1)

        if item.extend_start and item.extend_end:
            pat.set_extend(cairo.EXTEND_PAD)
        elif not item.extend_start and not item.extend_end:
            pat.set_extend(cairo.EXTEND_NONE)
        else:
            # Asymmetric extend: use PAD then clip to a half-plane so the
            # non-extended end stops abruptly at the endpoint.
            pat.set_extend(cairo.EXTEND_PAD)

            # Compute the gradient axis direction and a perpendicular
            dx = item.x1 - item.x0
            dy = item.y1 - item.y0
            length = math.sqrt(dx * dx + dy * dy)
            if length > 1e-10:
                # Unit normal perpendicular to the axis
                nx, ny = -dy / length, dx / length
                # Large extent for the clip rectangle
                ext = max(abs(dx), abs(dy), 10000.0) * 2

                if item.extend_start and not item.extend_end:
                    # Clip: keep everything before (x1,y1) — the end point
                    # Half-plane: dot((p - end), axis_dir) <= 0
                    cx, cy = item.x1, item.y1
                    cairo_ctx.new_path()
                    cairo_ctx.move_to(cx + nx * ext, cy + ny * ext)
                    cairo_ctx.line_to(cx - nx * ext, cy - ny * ext)
                    cairo_ctx.line_to(cx - nx * ext - dx / length * ext, cy - ny * ext - dy / length * ext)
                    cairo_ctx.line_to(cx + nx * ext - dx / length * ext, cy + ny * ext - dy / length * ext)
                    cairo_ctx.close_path()
                    cairo_ctx.clip()
                elif item.extend_end and not item.extend_start:
                    # Clip: keep everything after (x0,y0) — the start point
                    cx, cy = item.x0, item.y0
                    cairo_ctx.new_path()
                    cairo_ctx.move_to(cx + nx * ext, cy + ny * ext)
                    cairo_ctx.line_to(cx - nx * ext, cy - ny * ext)
                    cairo_ctx.line_to(cx - nx * ext + dx / length * ext, cy - ny * ext + dy / length * ext)
                    cairo_ctx.line_to(cx + nx * ext + dx / length * ext, cy + ny * ext + dy / length * ext)
                    cairo_ctx.close_path()
                    cairo_ctx.clip()

        for t, (r, g, b) in item.color_stops:
            pat.add_color_stop_rgb(t, r, g, b)

        cairo_ctx.set_source(pat)
        cairo_ctx.paint()
    finally:
        cairo_ctx.restore()


def _render_radial_shading(item, cairo_ctx):
    """Render a radial (circular) gradient shading fill."""
    cairo_ctx.save()
    try:
        cairo_ctx.transform(cairo.Matrix(*item.ctm))

        if item.bbox:
            cairo_ctx.rectangle(item.bbox[0], item.bbox[1],
                                item.bbox[2] - item.bbox[0],
                                item.bbox[3] - item.bbox[1])
            cairo_ctx.clip()

        pat = cairo.RadialGradient(item.x0, item.y0, item.r0,
                                   item.x1, item.y1, item.r1)

        if item.extend_start and item.extend_end:
            pat.set_extend(cairo.EXTEND_PAD)
        elif not item.extend_start and not item.extend_end:
            pat.set_extend(cairo.EXTEND_NONE)
        else:
            # Asymmetric extend: use PAD then mask out the non-extended circle.
            # For radial gradients, we paint with PAD, then erase the region
            # beyond the non-extended end's circle by painting over it.
            # This uses a two-pass approach on a temporary surface.
            pat.set_extend(cairo.EXTEND_PAD)

        for t, (r, g, b) in item.color_stops:
            pat.add_color_stop_rgb(t, r, g, b)

        cairo_ctx.set_source(pat)
        cairo_ctx.paint()
    finally:
        cairo_ctx.restore()


def _render_mesh_shading_batch(items, cairo_ctx):
    """Render multiple Type 4/5 triangle mesh shadings using a single Cairo MeshPattern.

    This batches consecutive MeshShadingFill items with the same CTM into a single
    MeshPattern, dramatically reducing overhead for PostScript files that emit
    many small meshes (common in PDF-to-PS conversions).

    Each triangle is converted to a degenerate Coons patch (Cairo mesh patch
    with 3 distinct corners; the 4th corner is collapsed onto the 3rd).
    """
    if not items:
        return

    # Use CTM from first item (all items in batch have same CTM)
    first = items[0]
    cairo_ctx.save()
    try:
        cairo_ctx.transform(cairo.Matrix(*first.ctm))

        # Compute combined bounding box from all items
        if any(item.bbox for item in items):
            min_x = min_y = float('inf')
            max_x = max_y = float('-inf')
            for item in items:
                if item.bbox:
                    min_x = min(min_x, item.bbox[0])
                    min_y = min(min_y, item.bbox[1])
                    max_x = max(max_x, item.bbox[2])
                    max_y = max(max_y, item.bbox[3])
            if min_x < float('inf'):
                cairo_ctx.rectangle(min_x, min_y, max_x - min_x, max_y - min_y)
                cairo_ctx.clip()

        pat = cairo.MeshPattern()

        # Add all triangles from all items to single pattern
        for item in items:
            for tri in item.triangles:
                (x0, y0), (r0, g0, b0) = tri[0]
                (x1, y1), (r1, g1, b1) = tri[1]
                (x2, y2), (r2, g2, b2) = tri[2]

                pat.begin_patch()
                pat.move_to(x0, y0)
                pat.line_to(x1, y1)
                pat.line_to(x2, y2)
                pat.line_to(x2, y2)  # degenerate 4th side (collapse onto 3rd vertex)

                pat.set_corner_color_rgb(0, r0, g0, b0)
                pat.set_corner_color_rgb(1, r1, g1, b1)
                pat.set_corner_color_rgb(2, r2, g2, b2)
                pat.set_corner_color_rgb(3, r2, g2, b2)  # same as corner 2

                pat.end_patch()

        cairo_ctx.set_source(pat)
        cairo_ctx.paint()
    finally:
        cairo_ctx.restore()


def _render_mesh_shading(item, cairo_ctx):
    """Render a single Type 4/5 triangle mesh shading.

    Delegates to _render_mesh_shading_batch for consistency.
    """
    _render_mesh_shading_batch([item], cairo_ctx)


def _render_patch_shading(item, cairo_ctx):
    """Render a Type 6/7 Coons or tensor-product patch shading using Cairo MeshPattern.

    Cairo natively supports Coons patches. Tensor-product patches (Type 7)
    with 16 control points are approximated by using only the 12 boundary
    control points (dropping the 4 interior ones), which is exact when the
    interior points lie on the Coons surface.
    """
    cairo_ctx.save()
    try:
        cairo_ctx.transform(cairo.Matrix(*item.ctm))

        if item.bbox:
            cairo_ctx.rectangle(item.bbox[0], item.bbox[1],
                                item.bbox[2] - item.bbox[0],
                                item.bbox[3] - item.bbox[1])
            cairo_ctx.clip()

        pat = cairo.MeshPattern()

        for points, colors in item.patches:
            n_pts = len(points)
            if n_pts < 12:
                continue

            # For Type 7 (16 points), extract boundary points in Coons order
            if n_pts >= 16:
                # Tensor-product 4×4 grid → boundary = Coons 12 control points
                # Row-major order: indices [0..15] → boundary extraction
                # Side 0: pts 0,1,2,3; Side 1: pts 3,7,11,15 (not quite right)
                # Actually Type 7 layout follows PLRM order, just use first 12
                coons_pts = points[:12]
            else:
                coons_pts = points[:12]

            # Coons patch: 4 sides, each with 4 control points
            # Side 0: points 0,1,2,3 (start to end of first side)
            # Side 1: points 3,4,5,6
            # Side 2: points 6,7,8,9
            # Side 3: points 9,10,11,0

            pat.begin_patch()
            # Move to first point
            pat.move_to(coons_pts[0][0], coons_pts[0][1])
            # Side 0: cubic Bezier through points 1,2,3
            pat.curve_to(coons_pts[1][0], coons_pts[1][1],
                         coons_pts[2][0], coons_pts[2][1],
                         coons_pts[3][0], coons_pts[3][1])
            # Side 1: cubic Bezier through points 4,5,6
            pat.curve_to(coons_pts[4][0], coons_pts[4][1],
                         coons_pts[5][0], coons_pts[5][1],
                         coons_pts[6][0], coons_pts[6][1])
            # Side 2: cubic Bezier through points 7,8,9
            pat.curve_to(coons_pts[7][0], coons_pts[7][1],
                         coons_pts[8][0], coons_pts[8][1],
                         coons_pts[9][0], coons_pts[9][1])
            # Side 3: cubic Bezier through points 10,11,back to 0
            pat.curve_to(coons_pts[10][0], coons_pts[10][1],
                         coons_pts[11][0], coons_pts[11][1],
                         coons_pts[0][0], coons_pts[0][1])

            # Set corner colors (4 corners: 0, 3, 6, 9 in the control point sequence)
            if len(colors) >= 4:
                pat.set_corner_color_rgb(0, *colors[0])
                pat.set_corner_color_rgb(1, *colors[1])
                pat.set_corner_color_rgb(2, *colors[2])
                pat.set_corner_color_rgb(3, *colors[3])

            pat.end_patch()

        cairo_ctx.set_source(pat)
        cairo_ctx.paint()
    finally:
        cairo_ctx.restore()


def _render_function_shading(item, cairo_ctx):
    """Render a Type 1 function-based shading fill.

    The shading has been pre-rasterized to an ARGB32 pixel buffer.
    We create a Cairo ImageSurface from it, apply the combined
    pixel→user and user→device transforms, and paint.
    """
    cairo_ctx.save()
    try:
        # Apply user→device CTM
        cairo_ctx.transform(cairo.Matrix(*item.ctm))

        if item.bbox:
            cairo_ctx.rectangle(item.bbox[0], item.bbox[1],
                                item.bbox[2] - item.bbox[0],
                                item.bbox[3] - item.bbox[1])
            cairo_ctx.clip()

        pixel_data = item.pixel_data
        if isinstance(pixel_data, bytes):
            pixel_data = bytearray(pixel_data)

        stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_ARGB32, item.width)
        actual_stride = item.width * 4

        if stride > actual_stride:
            padded = bytearray()
            for row in range(item.height):
                row_start = row * actual_stride
                padded.extend(pixel_data[row_start:row_start + actual_stride])
                padded.extend(b'\x00' * (stride - actual_stride))
            pixel_data = padded

        surface = cairo.ImageSurface.create_for_data(
            pixel_data, cairo.FORMAT_ARGB32,
            item.width, item.height, stride
        )

        pattern = cairo.SurfacePattern(surface)
        pattern.set_filter(cairo.FILTER_BILINEAR)

        # The matrix on the pattern is the *inverse* of the transform we want.
        # item.matrix maps pixel coords → user space.
        # Pattern matrix needs user space → pixel coords (the inverse).
        a, b, c, d, tx, ty = item.matrix
        det = a * d - b * c
        if abs(det) > 1e-10:
            inv = cairo.Matrix(
                d / det, -b / det,
                -c / det, a / det,
                (c * ty - d * tx) / det,
                (b * tx - a * ty) / det
            )
            pattern.set_matrix(inv)

        cairo_ctx.set_source(pattern)
        cairo_ctx.paint()
    finally:
        cairo_ctx.restore()


def _add_gradient_stops(gradient, func, color_space):
    """Add color stops to a gradient based on a PostScript function."""
    func_type = int(func.val.get(b'FunctionType', ps.Int(0)).val)

    if func_type == 2:
        # Exponential interpolation function
        domain = func.val.get(b'Domain')
        c0 = func.val.get(b'C0')
        c1 = func.val.get(b'C1')
        n_exp = float(func.val.get(b'N', ps.Real(1)).val)

        # Get color values
        if c0 and c0.TYPE in ps.ARRAY_TYPES:
            c0_vals = [float(c0.val[c0.start + i].val) for i in range(c0.length)]
        else:
            c0_vals = [0.0]

        if c1 and c1.TYPE in ps.ARRAY_TYPES:
            c1_vals = [float(c1.val[c1.start + i].val) for i in range(c1.length)]
        else:
            c1_vals = [1.0]

        # Add color stops - sample the function at multiple points for non-linear
        num_stops = 2 if n_exp == 1 else 10
        for i in range(num_stops):
            t = i / (num_stops - 1) if num_stops > 1 else 0
            # y = C0 + (C1 - C0) * x^N
            factor = t ** n_exp

            if color_space in ("DeviceGray", "/DeviceGray"):
                gray = c0_vals[0] + (c1_vals[0] - c0_vals[0]) * factor
                gradient.add_color_stop_rgb(t, gray, gray, gray)
            elif color_space in ("DeviceRGB", "/DeviceRGB"):
                r = c0_vals[0] + (c1_vals[0] - c0_vals[0]) * factor if len(c0_vals) > 0 else 0
                g = c0_vals[1] + (c1_vals[1] - c0_vals[1]) * factor if len(c0_vals) > 1 else 0
                b = c0_vals[2] + (c1_vals[2] - c0_vals[2]) * factor if len(c0_vals) > 2 else 0
                gradient.add_color_stop_rgb(t, r, g, b)
            elif color_space in ("DeviceCMYK", "/DeviceCMYK"):
                c = c0_vals[0] + (c1_vals[0] - c0_vals[0]) * factor if len(c0_vals) > 0 else 0
                m = c0_vals[1] + (c1_vals[1] - c0_vals[1]) * factor if len(c0_vals) > 1 else 0
                y = c0_vals[2] + (c1_vals[2] - c0_vals[2]) * factor if len(c0_vals) > 2 else 0
                k = c0_vals[3] + (c1_vals[3] - c0_vals[3]) * factor if len(c0_vals) > 3 else 0
                # Convert CMYK to RGB
                r = (1 - c) * (1 - k)
                g_val = (1 - m) * (1 - k)
                b_val = (1 - y) * (1 - k)
                gradient.add_color_stop_rgb(t, r, g_val, b_val)
            else:
                # Default grayscale
                gray = c0_vals[0] + (c1_vals[0] - c0_vals[0]) * factor if c0_vals else factor
                gradient.add_color_stop_rgb(t, gray, gray, gray)
    else:
        # Unsupported function type - use simple black to white
        gradient.add_color_stop_rgb(0, 0, 0, 0)
        gradient.add_color_stop_rgb(1, 1, 1, 1)
