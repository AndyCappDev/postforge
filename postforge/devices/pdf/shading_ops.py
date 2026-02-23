# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
Shading operations for PDF content streams and PDF dict builders.

Handles axial, radial, function-based, mesh (Gouraud), and patch (Coons/tensor)
shadings. Includes both content stream emission and PDF object construction.
"""

import zlib

from ...core import types as ps
from ._common import _fmt, _cfmt
from .pdf_objects import (
    PdfArray, PdfBool, PdfDict, PdfName, PdfNumber, PdfStream,
)


# ---------------------------------------------------------------------------
# Content stream shading emission
# ---------------------------------------------------------------------------


def _emit_shading_ref(lines: list[bytes], sh_name: str,
                      ctm: tuple) -> None:
    """Emit a shading reference: q {CTM} cm /ShN sh Q.

    The CTM transforms user-space shading coordinates to device space.
    """
    a, b, c, d, tx, ty = ctm
    lines.append(b'q')
    lines.append(
        f'{_fmt(a)} {_fmt(b)} {_fmt(c)} {_fmt(d)} '
        f'{_fmt(tx)} {_fmt(ty)} cm'.encode())
    lines.append(f'{sh_name} sh'.encode())
    lines.append(b'Q')


def _encode_coord_16(value: float, vmin: float, vmax: float) -> bytes:
    """Map a float coordinate to a 16-bit unsigned integer using Decode range.

    Returns 2 bytes (big-endian).
    """
    span = vmax - vmin
    if span == 0:
        return b'\x00\x00'
    t = (value - vmin) / span
    t = max(0.0, min(1.0, t))
    ival = int(t * 65535.0 + 0.5)
    return ival.to_bytes(2, 'big')


def _build_axial_shading(shading: ps.AxialShadingFill) -> dict | None:
    """Build a Type 2 (Axial) PDF shading description dict.

    Creates a Type 0 sampled function from the color stops and returns
    a shading dict with coordinates in user space.
    """
    if not shading.color_stops:
        return None

    # Build Type 0 sampled function: 65 uniformly spaced RGB samples
    n_samples = 65
    stops = shading.color_stops
    stream_data = bytearray(n_samples * 3)
    for i in range(n_samples):
        t = i / (n_samples - 1)
        r, g, b = _interpolate_color_stops(stops, t)
        stream_data[i * 3] = _clamp_byte(r)
        stream_data[i * 3 + 1] = _clamp_byte(g)
        stream_data[i * 3 + 2] = _clamp_byte(b)

    func_desc = {
        'type': 0,
        'domain': [0.0, 1.0],
        'range': [0.0, 1.0, 0.0, 1.0, 0.0, 1.0],
        'size': [n_samples],
        'bps': 8,
        'order': 1,
        'stream_data': bytes(stream_data),
    }

    desc: dict = {
        'type': 2,
        'color_space': 'DeviceRGB',
        'coords': [shading.x0, shading.y0, shading.x1, shading.y1],
        'extend': [shading.extend_start, shading.extend_end],
        'function': func_desc,
    }
    if shading.bbox:
        desc['bbox'] = list(shading.bbox)
    return desc


def _build_radial_shading(shading: ps.RadialShadingFill) -> dict | None:
    """Build a Type 3 (Radial) PDF shading description dict.

    Same as axial but with ShadingType 3 and 6-element Coords.
    """
    if not shading.color_stops:
        return None

    n_samples = 65
    stops = shading.color_stops
    stream_data = bytearray(n_samples * 3)
    for i in range(n_samples):
        t = i / (n_samples - 1)
        r, g, b = _interpolate_color_stops(stops, t)
        stream_data[i * 3] = _clamp_byte(r)
        stream_data[i * 3 + 1] = _clamp_byte(g)
        stream_data[i * 3 + 2] = _clamp_byte(b)

    func_desc = {
        'type': 0,
        'domain': [0.0, 1.0],
        'range': [0.0, 1.0, 0.0, 1.0, 0.0, 1.0],
        'size': [n_samples],
        'bps': 8,
        'order': 1,
        'stream_data': bytes(stream_data),
    }

    desc: dict = {
        'type': 3,
        'color_space': 'DeviceRGB',
        'coords': [shading.x0, shading.y0, shading.r0,
                    shading.x1, shading.y1, shading.r1],
        'extend': [shading.extend_start, shading.extend_end],
        'function': func_desc,
    }
    if shading.bbox:
        desc['bbox'] = list(shading.bbox)
    return desc


def _interpolate_color_stops(stops: list[tuple[float, tuple[float, float, float]]],
                              t: float) -> tuple[float, float, float]:
    """Linearly interpolate color stops at parameter t in [0, 1]."""
    if not stops:
        return (0.0, 0.0, 0.0)
    if t <= stops[0][0]:
        return stops[0][1]
    if t >= stops[-1][0]:
        return stops[-1][1]
    for i in range(len(stops) - 1):
        t0, c0 = stops[i]
        t1, c1 = stops[i + 1]
        if t0 <= t <= t1:
            span = t1 - t0
            if span < 1e-10:
                return c0
            f = (t - t0) / span
            return (c0[0] + f * (c1[0] - c0[0]),
                    c0[1] + f * (c1[1] - c0[1]),
                    c0[2] + f * (c1[2] - c0[2]))
    return stops[-1][1]


def _clamp_byte(v: float) -> int:
    """Clamp a float [0, 1] to a byte [0, 255]."""
    return max(0, min(255, int(v * 255.0 + 0.5)))


def _emit_function_shading(lines: list[bytes],
                           shading: ps.FunctionShadingFill) -> None:
    """Emit function shading as an inline image."""
    if not shading.pixel_data:
        return

    # The pixel_data is ARGB32 (BGRA on little-endian). Convert to RGB.
    w, h = shading.width, shading.height
    rgb_data = bytearray(w * h * 3)
    for i in range(w * h):
        offset = i * 4
        rgb_data[i * 3] = shading.pixel_data[offset + 2]      # R
        rgb_data[i * 3 + 1] = shading.pixel_data[offset + 1]  # G
        rgb_data[i * 3 + 2] = shading.pixel_data[offset]      # B

    # Compute placement matrix from shading matrix x CTM
    sm = shading.matrix  # pixel -> user
    ctm = shading.ctm    # user -> device
    a1, b1, c1, d1, tx1, ty1 = sm
    a2, b2, c2, d2, tx2, ty2 = ctm
    cm_a = a1 * a2 + b1 * c2
    cm_b = a1 * b2 + b1 * d2
    cm_c = c1 * a2 + d1 * c2
    cm_d = c1 * b2 + d1 * d2
    cm_tx = tx1 * a2 + ty1 * c2 + tx2
    cm_ty = tx1 * b2 + ty1 * d2 + ty2

    # The rasterization stores domain y_min at pixel row 0 (image top).
    # PDF images render row 0 at the top of the unit square (y=1), so
    # unit (0,0) -- image bottom -- would land at the domain-bottom position,
    # placing domain-bottom at the visual top after the global Y-flip.
    # Fix by negating the Y basis and shifting the origin so the image
    # top (row 0, domain y_min) lands at the domain-bottom position.
    cm_c_h = cm_c * h
    cm_d_h = cm_d * h
    lines.append(b'q')
    lines.append(
        f'{_fmt(cm_a * w)} {_fmt(cm_b * w)} {_fmt(-cm_c_h)} {_fmt(-cm_d_h)} '
        f'{_fmt(cm_tx + cm_c_h)} {_fmt(cm_ty + cm_d_h)} cm'.encode())

    lines.append(b'BI')
    lines.append(f'/W {w}'.encode())
    lines.append(f'/H {h}'.encode())
    lines.append(b'/BPC 8')
    lines.append(b'/CS /RGB')
    lines.append(b'ID')
    lines.append(bytes(rgb_data))
    lines.append(b'EI')
    lines.append(b'Q')


def _build_mesh_shading(shading: ps.MeshShadingFill) -> dict | None:
    """Build a Type 4 (Free-form Gouraud) PDF shading description dict.

    Encodes triangles as binary stream with 16-bit coordinates and 8-bit
    color components. Coordinates are in user space (CTM applied via cm).
    """
    if not shading.triangles:
        return None

    # Compute coordinate bounds from all vertices (user space)
    all_x: list[float] = []
    all_y: list[float] = []
    for tri in shading.triangles:
        for (x, y), _ in tri:
            all_x.append(x)
            all_y.append(y)

    if not all_x:
        return None

    xmin, xmax = min(all_x), max(all_x)
    ymin, ymax = min(all_y), max(all_y)
    # Add small padding to avoid edge clipping
    pad = max(xmax - xmin, ymax - ymin) * 0.001
    if pad < 0.01:
        pad = 0.01
    xmin -= pad
    xmax += pad
    ymin -= pad
    ymax += pad

    # Encode triangles: each vertex = flag(1) + x(2) + y(2) + R(1) + G(1) + B(1)
    stream = bytearray()
    for tri in shading.triangles:
        for (x, y), (r, g, b) in tri:
            stream.append(0)  # flag = 0 (new triangle vertex)
            stream.extend(_encode_coord_16(x, xmin, xmax))
            stream.extend(_encode_coord_16(y, ymin, ymax))
            stream.append(_clamp_byte(r))
            stream.append(_clamp_byte(g))
            stream.append(_clamp_byte(b))

    desc: dict = {
        'type': 4,
        'color_space': 'DeviceRGB',
        'bits_per_coordinate': 16,
        'bits_per_component': 8,
        'bits_per_flag': 8,
        'decode': [xmin, xmax, ymin, ymax, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0],
        'stream_data': bytes(stream),
    }
    if shading.bbox:
        desc['bbox'] = list(shading.bbox)
    return desc


def _build_patch_shading(shading: ps.PatchShadingFill) -> dict | None:
    """Build a Type 6 (Coons) or Type 7 (Tensor-product) PDF shading dict.

    Detects type from control point count (12 = Coons, 16 = Tensor).
    Encodes as binary stream with 16-bit coordinates and 8-bit colors.
    """
    if not shading.patches:
        return None

    # Determine shading type from first patch's point count
    first_points = shading.patches[0][0]
    if len(first_points) >= 16:
        shading_type = 7  # Tensor-product
        n_points = 16
    elif len(first_points) >= 12:
        shading_type = 6  # Coons
        n_points = 12
    else:
        return None

    # Compute coordinate bounds from all control points (user space)
    all_x: list[float] = []
    all_y: list[float] = []
    for points, _ in shading.patches:
        for x, y in points[:n_points]:
            all_x.append(x)
            all_y.append(y)

    if not all_x:
        return None

    xmin, xmax = min(all_x), max(all_x)
    ymin, ymax = min(all_y), max(all_y)
    pad = max(xmax - xmin, ymax - ymin) * 0.001
    if pad < 0.01:
        pad = 0.01
    xmin -= pad
    xmax += pad
    ymin -= pad
    ymax += pad

    # Encode patches: flag(1) + n_points x (x(2) + y(2)) + 4 x (R(1) + G(1) + B(1))
    stream = bytearray()
    for points, colors in shading.patches:
        stream.append(0)  # flag = 0 (new patch)
        for x, y in points[:n_points]:
            stream.extend(_encode_coord_16(x, xmin, xmax))
            stream.extend(_encode_coord_16(y, ymin, ymax))
        for r, g, b in colors[:4]:
            stream.append(_clamp_byte(r))
            stream.append(_clamp_byte(g))
            stream.append(_clamp_byte(b))

    desc: dict = {
        'type': shading_type,
        'color_space': 'DeviceRGB',
        'bits_per_coordinate': 16,
        'bits_per_component': 8,
        'bits_per_flag': 8,
        'decode': [xmin, xmax, ymin, ymax, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0],
        'stream_data': bytes(stream),
    }
    if shading.bbox:
        desc['bbox'] = list(shading.bbox)
    return desc


# ---------------------------------------------------------------------------
# PDF dict builders (used by pdf_builder.py)
# ---------------------------------------------------------------------------


def _build_pdf_shading(writer: object,
                        desc: dict) -> object | None:
    """Convert a shading description dict to a PDF indirect object.

    Handles Type 2/3 (gradient with function) and Type 4/6/7 (stream-based).
    """
    shading_type = desc.get('type')
    if shading_type is None:
        return None

    if shading_type in (2, 3):
        # Gradient shading -- build function first
        func_desc = desc.get('function')
        if func_desc is None:
            return None
        func_ref = _build_pdf_function(writer, func_desc)
        if func_ref is None:
            return None

        shading_obj = PdfDict()
        shading_obj['/ShadingType'] = PdfNumber(shading_type)
        shading_obj['/ColorSpace'] = PdfName('/' + desc['color_space'])

        coords = desc['coords']
        shading_obj['/Coords'] = PdfArray(
            [_pdf_number(v) for v in coords])

        extend = desc.get('extend', [False, False])
        shading_obj['/Extend'] = PdfArray([
            PdfBool(extend[0]), PdfBool(extend[1])])

        shading_obj['/Function'] = func_ref

        if 'bbox' in desc:
            shading_obj['/BBox'] = PdfArray(
                [_pdf_number(v) for v in desc['bbox']])

        return writer.add_object(shading_obj)

    elif shading_type in (4, 6, 7):
        # Stream-based shading (mesh, Coons, tensor)
        stream_data = desc.get('stream_data')
        if not stream_data:
            return None

        compressed = zlib.compress(stream_data)
        sh_stream = PdfStream(compressed)
        sh_stream['/Filter'] = PdfName('/FlateDecode')
        sh_stream['/ShadingType'] = PdfNumber(shading_type)
        sh_stream['/ColorSpace'] = PdfName('/' + desc['color_space'])
        sh_stream['/BitsPerCoordinate'] = PdfNumber(
            desc['bits_per_coordinate'])
        sh_stream['/BitsPerComponent'] = PdfNumber(
            desc['bits_per_component'])
        sh_stream['/BitsPerFlag'] = PdfNumber(desc['bits_per_flag'])

        decode = desc['decode']
        sh_stream['/Decode'] = PdfArray(
            [_pdf_number(v) for v in decode])

        if 'bbox' in desc:
            sh_stream['/BBox'] = PdfArray(
                [_pdf_number(v) for v in desc['bbox']])

        return writer.add_object(sh_stream)

    return None


def _build_pdf_function(writer: object,
                         func_desc: dict) -> object | None:
    """Convert a function description dict to a PDF indirect object.

    Supports Type 0 (sampled) functions.
    """
    func_type = func_desc.get('type')
    if func_type != 0:
        return None

    stream_data = func_desc.get('stream_data')
    if not stream_data:
        return None

    compressed = zlib.compress(stream_data)
    func_stream = PdfStream(compressed)
    func_stream['/Filter'] = PdfName('/FlateDecode')
    func_stream['/FunctionType'] = PdfNumber(0)
    func_stream['/Domain'] = PdfArray(
        [_pdf_number(v) for v in func_desc['domain']])
    func_stream['/Range'] = PdfArray(
        [_pdf_number(v) for v in func_desc['range']])
    func_stream['/Size'] = PdfArray(
        [PdfNumber(s) for s in func_desc['size']])
    func_stream['/BitsPerSample'] = PdfNumber(func_desc['bps'])
    func_stream['/Order'] = PdfNumber(func_desc.get('order', 1))

    return writer.add_object(func_stream)


def _pdf_number(v: float) -> PdfNumber:
    """Create a PdfNumber, using int when the value is whole."""
    if isinstance(v, int) or (isinstance(v, float) and v == int(v)):
        return PdfNumber(int(v))
    return PdfNumber(round(v, 6))
