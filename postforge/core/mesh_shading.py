# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
Mesh Shading Parser — Types 4-7

Parses binary data streams for free-form Gouraud (Type 4), lattice Gouraud (Type 5),
Coons patch (Type 6), and tensor-product patch (Type 7) mesh shadings.

Reference: PLRM Third Edition Section 4.9.3
"""

from . import ps_function


class MeshVertex:
    """A vertex with position and color."""
    __slots__ = ('x', 'y', 'color')

    def __init__(self, x: float, y: float, color: list[float]) -> None:
        self.x = x
        self.y = y
        self.color = color  # list of floats in color space


class MeshTriangle:
    """A triangle defined by three vertices."""
    __slots__ = ('v0', 'v1', 'v2')

    def __init__(self, v0: MeshVertex, v1: MeshVertex, v2: MeshVertex) -> None:
        self.v0 = v0
        self.v1 = v1
        self.v2 = v2


class CoonsPatch:
    """A Coons patch with 12 control points and 4 corner colors."""
    __slots__ = ('points', 'colors')

    def __init__(self, points: list[tuple[float, float]], colors: list[list[float]]) -> None:
        self.points = points  # list of 12 (x, y) tuples
        self.colors = colors  # list of 4 color lists (corner colors)


class TensorPatch:
    """A tensor-product patch with 16 control points and 4 corner colors."""
    __slots__ = ('points', 'colors')

    def __init__(self, points: list[tuple[float, float]], colors: list[list[float]]) -> None:
        self.points = points  # list of 16 (x, y) tuples
        self.colors = colors  # list of 4 color lists (corner colors)


class _BitReader:
    """Read arbitrary bit-width values from a byte buffer.

    Optimized with fast paths for byte-aligned reads of common bit widths.
    """

    def __init__(self, data: bytes | bytearray | memoryview) -> None:
        if isinstance(data, memoryview):
            self.data = bytes(data)
        elif isinstance(data, (bytes, bytearray)):
            self.data = data
        else:
            self.data = bytes(data)
        self.bit_pos = 0
        self._len = len(self.data)

    def read_bits(self, n: int) -> int:
        """Read n bits as an unsigned integer.

        Optimized with fast paths for byte-aligned reads of 8, 16, 24, 32 bits.
        Falls back to bulk byte reading for other cases.
        """
        if n == 0:
            return 0

        bit_offset = self.bit_pos & 7
        byte_idx = self.bit_pos >> 3

        # Fast path: byte-aligned reads of common sizes
        if bit_offset == 0:
            if n == 8 and byte_idx < self._len:
                self.bit_pos += 8
                return self.data[byte_idx]
            elif n == 16 and byte_idx + 1 < self._len:
                self.bit_pos += 16
                return (self.data[byte_idx] << 8) | self.data[byte_idx + 1]
            elif n == 24 and byte_idx + 2 < self._len:
                self.bit_pos += 24
                return (self.data[byte_idx] << 16) | (self.data[byte_idx + 1] << 8) | self.data[byte_idx + 2]
            elif n == 32 and byte_idx + 3 < self._len:
                self.bit_pos += 32
                return (self.data[byte_idx] << 24) | (self.data[byte_idx + 1] << 16) | (self.data[byte_idx + 2] << 8) | self.data[byte_idx + 3]

        # General case: read bytes and extract bits
        # Calculate how many bytes we need to read
        end_bit = self.bit_pos + n
        end_byte = (end_bit + 7) >> 3

        if end_byte > self._len:
            end_byte = self._len

        # Accumulate bytes into a single integer
        accum = 0
        for i in range(byte_idx, end_byte):
            accum = (accum << 8) | self.data[i]

        # Calculate how many bits we actually accumulated
        bits_read = (end_byte - byte_idx) << 3

        # Shift right to remove unwanted low bits, then mask to get n bits
        # The bits we want start at (bits_read - bit_offset - n) from the right
        right_shift = bits_read - bit_offset - n
        if right_shift > 0:
            accum >>= right_shift
        elif right_shift < 0:
            # Not enough data, pad with zeros
            accum <<= -right_shift

        # Mask to n bits
        result = accum & ((1 << n) - 1)
        self.bit_pos += n
        return result

    def align_to_byte(self) -> None:
        """Advance to next byte boundary."""
        remainder = self.bit_pos & 7
        if remainder:
            self.bit_pos += 8 - remainder

    @property
    def exhausted(self) -> bool:
        return (self.bit_pos >> 3) >= self._len


def _decode_value(raw: int, scale: float, decode_min: float) -> float:
    """Decode a raw integer value to float using precomputed scale."""
    return decode_min + raw * scale


def _compute_decode_scale(bits: int, decode_min: float, decode_max: float) -> float:
    """Precompute the scale factor for decoding."""
    max_val = (1 << bits) - 1
    if max_val == 0:
        return 0.0
    return (decode_max - decode_min) / max_val


def _decode_coordinate(raw: int, bits: int, decode_min: float, decode_max: float) -> float:
    """Decode a raw integer coordinate to float using Decode array."""
    max_val = (1 << bits) - 1
    if max_val == 0:
        return decode_min
    return decode_min + (raw / max_val) * (decode_max - decode_min)


def _decode_color_component(raw: int, bits: int, decode_min: float, decode_max: float) -> float:
    """Decode a raw integer color component to float."""
    max_val = (1 << bits) - 1
    if max_val == 0:
        return decode_min
    return decode_min + (raw / max_val) * (decode_max - decode_min)


def _read_vertex(reader: _BitReader, bpc: int, bpco: int, bpfl: int, decode: list[float], n_comps: int, func: object | None = None) -> tuple[int, MeshVertex]:
    """Read a single vertex (coordinate + color) from the bit stream.

    Args:
        reader: _BitReader
        bpc: BitsPerCoordinate
        bpco: BitsPerComponent (color)
        bpfl: BitsPerFlag
        decode: Decode array [xmin xmax ymin ymax c0min c0max ...]
        n_comps: number of color components
        func: optional Function for parametric color

    Returns:
        (flag, MeshVertex)
    """
    flag = reader.read_bits(bpfl) if bpfl > 0 else 0

    raw_x = reader.read_bits(bpc)
    raw_y = reader.read_bits(bpc)
    x = _decode_coordinate(raw_x, bpc, decode[0], decode[1])
    y = _decode_coordinate(raw_y, bpc, decode[2], decode[3])

    if func is not None:
        # Parametric: read 1 value, evaluate function for color
        raw_t = reader.read_bits(bpco)
        t = _decode_color_component(raw_t, bpco, decode[4], decode[5])
        try:
            color = ps_function.evaluate_function(func, [t])
        except Exception:
            color = [0.0] * n_comps
    else:
        color = []
        for i in range(n_comps):
            raw_c = reader.read_bits(bpco)
            c_min = decode[4 + i * 2]
            c_max = decode[4 + i * 2 + 1]
            color.append(_decode_color_component(raw_c, bpco, c_min, c_max))

    return flag, MeshVertex(x, y, color)


def _read_point(reader: _BitReader, bpc: int, decode: list[float]) -> tuple[float, float]:
    """Read a coordinate pair from the bit stream (no flag, no color)."""
    raw_x = reader.read_bits(bpc)
    raw_y = reader.read_bits(bpc)
    x = _decode_coordinate(raw_x, bpc, decode[0], decode[1])
    y = _decode_coordinate(raw_y, bpc, decode[2], decode[3])
    return (x, y)


def _read_color(reader: _BitReader, bpco: int, decode: list[float], n_comps: int, func: object | None = None) -> list[float]:
    """Read color components from the bit stream."""
    if func is not None:
        raw_t = reader.read_bits(bpco)
        t = _decode_color_component(raw_t, bpco, decode[4], decode[5])
        try:
            return ps_function.evaluate_function(func, [t])
        except Exception:
            return [0.0] * n_comps
    color = []
    for i in range(n_comps):
        raw_c = reader.read_bits(bpco)
        c_min = decode[4 + i * 2]
        c_max = decode[4 + i * 2 + 1]
        color.append(_decode_color_component(raw_c, bpco, c_min, c_max))
    return color


def parse_type4_mesh(data: bytes | bytearray | memoryview, bpc: int, bpco: int, bpfl: int, decode: list[float], n_comps: int, func: object | None = None) -> list[MeshTriangle]:
    """Parse Type 4 free-form Gouraud-shaded triangle mesh.

    Returns list of MeshTriangle.

    Optimized with precomputed decode scales and inlined vertex reading.
    """
    reader = _BitReader(data)
    triangles = []
    vertices = []  # running list for edge-flag connectivity

    # Precompute decode scales to avoid repeated division
    x_min, x_scale = decode[0], _compute_decode_scale(bpc, decode[0], decode[1])
    y_min, y_scale = decode[2], _compute_decode_scale(bpc, decode[2], decode[3])

    # Precompute color decode parameters
    if func is None:
        color_params = []
        for i in range(n_comps):
            c_min = decode[4 + i * 2]
            c_max = decode[4 + i * 2 + 1]
            color_params.append((c_min, _compute_decode_scale(bpco, c_min, c_max)))
    else:
        t_min = decode[4]
        t_scale = _compute_decode_scale(bpco, decode[4], decode[5])

    # Local references for speed
    read_bits = reader.read_bits
    exhausted = lambda: reader.exhausted
    MeshVertex_local = MeshVertex
    MeshTriangle_local = MeshTriangle

    # Inline vertex reading for performance
    def read_vertex():
        flag = read_bits(bpfl) if bpfl > 0 else 0
        raw_x = read_bits(bpc)
        raw_y = read_bits(bpc)
        x = x_min + raw_x * x_scale
        y = y_min + raw_y * y_scale

        if func is not None:
            raw_t = read_bits(bpco)
            t = t_min + raw_t * t_scale
            try:
                color = ps_function.evaluate_function(func, [t])
            except Exception:
                color = [0.0] * n_comps
        else:
            color = [c_min + read_bits(bpco) * c_scale for c_min, c_scale in color_params]

        return flag, MeshVertex_local(x, y, color)

    while not exhausted():
        flag, vertex = read_vertex()

        if flag == 0:
            # New triangle — need 3 vertices total
            vertices = [vertex]
            for _ in range(2):
                if exhausted():
                    break
                _, v = read_vertex()
                vertices.append(v)
            if len(vertices) >= 3:
                triangles.append(MeshTriangle_local(vertices[-3], vertices[-2], vertices[-1]))
        elif flag == 1:
            # Shares edge BC of previous triangle
            if len(vertices) >= 2:
                vertices.append(vertex)
                triangles.append(MeshTriangle_local(vertices[-3], vertices[-2], vertices[-1]))
        elif flag == 2:
            # Shares edge AC of previous triangle
            if len(vertices) >= 2:
                prev_a = vertices[-3] if len(vertices) >= 3 else vertices[-2]
                prev_c = vertices[-1]
                vertices.append(vertex)
                triangles.append(MeshTriangle_local(prev_a, prev_c, vertex))

    return triangles


def parse_type5_mesh(data: bytes | bytearray | memoryview, bpc: int, bpco: int, decode: list[float], n_comps: int, vertices_per_row: int, func: object | None = None) -> list[MeshTriangle]:
    """Parse Type 5 lattice-form Gouraud-shaded triangle mesh.

    Returns list of MeshTriangle.

    Optimized with precomputed decode scales.
    """
    reader = _BitReader(data)
    all_vertices = []

    # Precompute decode scales
    x_min, x_scale = decode[0], _compute_decode_scale(bpc, decode[0], decode[1])
    y_min, y_scale = decode[2], _compute_decode_scale(bpc, decode[2], decode[3])

    if func is None:
        color_params = []
        for i in range(n_comps):
            c_min = decode[4 + i * 2]
            c_max = decode[4 + i * 2 + 1]
            color_params.append((c_min, _compute_decode_scale(bpco, c_min, c_max)))
    else:
        t_min = decode[4]
        t_scale = _compute_decode_scale(bpco, decode[4], decode[5])

    # Local references for speed
    read_bits = reader.read_bits
    MeshVertex_local = MeshVertex

    # Read all vertices (no flags in Type 5)
    while not reader.exhausted:
        raw_x = read_bits(bpc)
        raw_y = read_bits(bpc)
        x = x_min + raw_x * x_scale
        y = y_min + raw_y * y_scale

        if func is not None:
            raw_t = read_bits(bpco)
            t = t_min + raw_t * t_scale
            try:
                color = ps_function.evaluate_function(func, [t])
            except Exception:
                color = [0.0] * n_comps
        else:
            color = [c_min + read_bits(bpco) * c_scale for c_min, c_scale in color_params]

        all_vertices.append(MeshVertex_local(x, y, color))

    # Build triangles from lattice
    triangles = []
    num_rows = len(all_vertices) // vertices_per_row if vertices_per_row > 0 else 0
    MeshTriangle_local = MeshTriangle

    for row in range(num_rows - 1):
        row_start = row * vertices_per_row
        next_row_start = row_start + vertices_per_row
        for col in range(vertices_per_row - 1):
            idx = row_start + col
            v00 = all_vertices[idx]
            v10 = all_vertices[idx + 1]
            v01 = all_vertices[next_row_start + col]
            v11 = all_vertices[next_row_start + col + 1]
            # Two triangles per quad
            triangles.append(MeshTriangle_local(v00, v10, v01))
            triangles.append(MeshTriangle_local(v10, v11, v01))

    return triangles


def parse_type6_patches(data: bytes | bytearray | memoryview, bpc: int, bpco: int, bpfl: int, decode: list[float], n_comps: int, func: object | None = None) -> list[CoonsPatch]:
    """Parse Type 6 Coons patch mesh.

    Each patch: flag + 12 control points + 4 corner colors.
    Returns list of CoonsPatch.
    """
    reader = _BitReader(data)
    patches = []
    prev_patches = []

    while not reader.exhausted:
        flag = reader.read_bits(bpfl) if bpfl > 0 else 0

        if flag == 0:
            # New independent patch: 12 points + 4 colors
            points = [_read_point(reader, bpc, decode) for _ in range(12)]
            colors = [_read_color(reader, bpco, decode, n_comps, func) for _ in range(4)]
        elif flag == 1 and prev_patches:
            # Share side 1 (points 3,4,5 and colors 1,2) of previous patch
            prev = prev_patches[-1]
            # Inherited: points 3,4,5 → new 0,1,2; colors 1,2 → new 0,1
            inherited_pts = [prev.points[3], prev.points[4], prev.points[5]]
            new_pts = [_read_point(reader, bpc, decode) for _ in range(8)]
            points = inherited_pts[:3] + new_pts[:9]  # fill to 12
            # Actually: 4 new points for the other 3 sides (8 control points) + inherited 4
            # Simplified: read remaining 8 points
            points = list(prev.points[3:6])  # 3 inherited
            remaining = [_read_point(reader, bpc, decode) for _ in range(9)]
            points.extend(remaining[:9])
            colors = [prev.colors[1], prev.colors[2]]
            colors.extend([_read_color(reader, bpco, decode, n_comps, func) for _ in range(2)])
        elif flag == 2 and prev_patches:
            prev = prev_patches[-1]
            points = list(prev.points[6:9])
            remaining = [_read_point(reader, bpc, decode) for _ in range(9)]
            points.extend(remaining[:9])
            colors = [prev.colors[2], prev.colors[3]]
            colors.extend([_read_color(reader, bpco, decode, n_comps, func) for _ in range(2)])
        elif flag == 3 and prev_patches:
            prev = prev_patches[-1]
            points = list(prev.points[9:12])
            remaining = [_read_point(reader, bpc, decode) for _ in range(9)]
            points.extend(remaining[:9])
            colors = [prev.colors[3], prev.colors[0]]
            colors.extend([_read_color(reader, bpco, decode, n_comps, func) for _ in range(2)])
        else:
            # Invalid flag or no previous patch — try to read as new
            points = [_read_point(reader, bpc, decode) for _ in range(12)]
            colors = [_read_color(reader, bpco, decode, n_comps, func) for _ in range(4)]

        if len(points) >= 12 and len(colors) >= 4:
            patch = CoonsPatch(points[:12], colors[:4])
            patches.append(patch)
            prev_patches.append(patch)

    return patches


def parse_type7_patches(data: bytes | bytearray | memoryview, bpc: int, bpco: int, bpfl: int, decode: list[float], n_comps: int, func: object | None = None) -> list[TensorPatch]:
    """Parse Type 7 tensor-product patch mesh.

    Each patch: flag + 16 control points + 4 corner colors.
    Returns list of TensorPatch.
    """
    reader = _BitReader(data)
    patches = []

    while not reader.exhausted:
        flag = reader.read_bits(bpfl) if bpfl > 0 else 0

        # For simplicity, read all patches as independent (flag 0)
        # Full connectivity support would mirror Type 6 with 16 points
        points = [_read_point(reader, bpc, decode) for _ in range(16)]
        colors = [_read_color(reader, bpco, decode, n_comps, func) for _ in range(4)]

        if len(points) >= 16 and len(colors) >= 4:
            patches.append(TensorPatch(points[:16], colors[:4]))

    return patches
