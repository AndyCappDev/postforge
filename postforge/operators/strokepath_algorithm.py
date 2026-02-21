# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Strokepath algorithm — converts a stroked path into a filled outline.

Produces moveto/lineto/curveto geometry (no tessellation).
Standalone module with no PostForge dependencies.

Components:
1. Dash pattern processor (de Casteljau splitting for curves)
2. Line/curve offset (Tiller-Hanson adaptive subdivision for cubics)
3. Line joins (miter/round/bevel)
4. Line caps (butt/round/projecting square)
5. Outline assembly
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Point:
    x: float
    y: float

    def __add__(self, other: Point) -> Point:
        return Point(self.x + other.x, self.y + other.y)

    def __sub__(self, other: Point) -> Point:
        return Point(self.x - other.x, self.y - other.y)

    def __mul__(self, s: float) -> Point:
        return Point(self.x * s, self.y * s)

    def __rmul__(self, s: float) -> Point:
        return self.__mul__(s)

    def length(self) -> float:
        return math.hypot(self.x, self.y)

    def normalized(self) -> Point:
        ln = self.length()
        if ln < 1e-12:
            return Point(0.0, 0.0)
        return Point(self.x / ln, self.y / ln)

    def dot(self, other: Point) -> float:
        return self.x * other.x + self.y * other.y

    def cross(self, other: Point) -> float:
        return self.x * other.y - self.y * other.x


@dataclass
class MoveTo:
    x: float
    y: float

@dataclass
class LineTo:
    x: float
    y: float

@dataclass
class CurveTo:
    x1: float; y1: float
    x2: float; y2: float
    x3: float; y3: float

@dataclass
class ClosePath:
    pass


PathElement = MoveTo | LineTo | CurveTo | ClosePath
SubPath = list[PathElement]
Path = list[SubPath]


def subpath_is_closed(sp: SubPath) -> bool:
    return len(sp) > 0 and isinstance(sp[-1], ClosePath)


def subpath_start(sp: SubPath) -> Point:
    """Return the starting point of a subpath."""
    if sp and isinstance(sp[0], MoveTo):
        return Point(sp[0].x, sp[0].y)
    return Point(0.0, 0.0)


def segment_endpoint(seg: PathElement) -> Point | None:
    if isinstance(seg, (MoveTo, LineTo)):
        return Point(seg.x, seg.y)
    if isinstance(seg, CurveTo):
        return Point(seg.x3, seg.y3)
    return None


# ---------------------------------------------------------------------------
# De Casteljau splitting
# ---------------------------------------------------------------------------

def _lerp(a: Point, b: Point, t: float) -> Point:
    return Point(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t)


def split_cubic(p0: Point, p1: Point, p2: Point, p3: Point, t: float) -> tuple[tuple[Point, Point, Point, Point], tuple[Point, Point, Point, Point]]:
    """Split cubic Bézier at parameter t. Returns (left_cps, right_cps) each as 4 Points."""
    q0 = _lerp(p0, p1, t)
    q1 = _lerp(p1, p2, t)
    q2 = _lerp(p2, p3, t)
    r0 = _lerp(q0, q1, t)
    r1 = _lerp(q1, q2, t)
    s = _lerp(r0, r1, t)
    return (p0, q0, r0, s), (s, r1, q2, p3)


def cubic_arc_length_approx(p0: Point, p1: Point, p2: Point, p3: Point,
                            _depth: int = 0, _max_depth: int = 12,
                            _tol: float = 0.1) -> float:
    """Approximate **arc** length of cubic Bézier via recursive subdivision.

    NOTE: When integrating into the interpreter, consider using the graphics
    state flatness value (**currentflat**) to drive _tol instead of hardcoding 0.1.
    """
    chord = (p3 - p0).length()
    poly = (p1 - p0).length() + (p2 - p1).length() + (p3 - p2).length()
    if _depth >= _max_depth or (poly - chord) < _tol:
        return (chord + poly) / 2.0
    left, right = split_cubic(p0, p1, p2, p3, 0.5)
    return (cubic_arc_length_approx(*left, _depth=_depth + 1,
                                     _max_depth=_max_depth, _tol=_tol)
            + cubic_arc_length_approx(*right, _depth=_depth + 1,
                                      _max_depth=_max_depth, _tol=_tol))


def line_length(p0: Point, p1: Point) -> float:
    return (p1 - p0).length()


# ---------------------------------------------------------------------------
# Dash pattern processor
# ---------------------------------------------------------------------------

def _split_line_at_length(p0: Point, p1: Point, target_len: float) -> tuple[Point, Point]:
    """Split a line segment at a given **arc** length. Returns (split_point, remaining_point)."""
    total = line_length(p0, p1)
    if total < 1e-12:
        return p0, p1
    t = target_len / total
    t = max(0.0, min(1.0, t))
    sp = _lerp(p0, p1, t)
    return sp, p1


def _find_cubic_t_for_length(p0: Point, p1: Point, p2: Point, p3: Point, target_len: float, depth: int = 20) -> float:
    """Find parameter t where cumulative **arc** length ≈ target_len. Binary **search**."""
    lo, hi = 0.0, 1.0
    for _ in range(depth):
        mid = (lo + hi) / 2.0
        left, _ = split_cubic(p0, p1, p2, p3, mid)
        left_len = cubic_arc_length_approx(*left)
        if left_len < target_len:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def apply_dash_pattern(subpaths: Path, dash_array: list[float], dash_offset: float) -> Path:
    """
    Apply dash pattern to path, returning new list of subpaths (dashed segments).
    Empty dash_array = solid (return as-is but split into segments suitable for stroking).
    Curves are split via de Casteljau — output remains cubic Béziers.
    """
    if not dash_array:
        return subpaths

    result = []

    for sp in subpaths:
        result_start = len(result)  # track where this subpath's dashes start
        closed = subpath_is_closed(sp)
        # Build a list of geometric segments (skip MoveTo, ClosePath)
        segments = []  # (type, start_point, element)
        current = subpath_start(sp)
        for elem in sp:
            if isinstance(elem, MoveTo):
                current = Point(elem.x, elem.y)
            elif isinstance(elem, LineTo):
                segments.append(('line', current, Point(elem.x, elem.y)))
                current = Point(elem.x, elem.y)
            elif isinstance(elem, CurveTo):
                segments.append(('curve', current, Point(elem.x1, elem.y1),
                                 Point(elem.x2, elem.y2), Point(elem.x3, elem.y3)))
                current = Point(elem.x3, elem.y3)
            elif isinstance(elem, ClosePath):
                # Add closing segment if needed
                start = subpath_start(sp)
                if (current.x - start.x) ** 2 + (current.y - start.y) ** 2 > 1e-12:
                    segments.append(('line', current, start))
                    current = start

        if not segments:
            continue

        # Compute total length
        total_length = 0.0
        for seg in segments:
            if seg[0] == 'line':
                total_length += line_length(seg[1], seg[2])
            else:
                total_length += cubic_arc_length_approx(seg[1], seg[2], seg[3], seg[4])

        if total_length < 1e-12:
            continue

        # Normalize offset into the dash cycle.
        # Per PLRM, odd-length dash arrays are conceptually doubled
        # (e.g. [d] becomes [d, d]) so the cycle is 2 * sum.
        dash_cycle = sum(dash_array)
        if len(dash_array) % 2 == 1:
            dash_cycle *= 2
        if dash_cycle < 1e-12:
            # Degenerate dash — treat as solid
            result.append(sp)
            continue

        offset = dash_offset % dash_cycle

        # Find starting dash index and remaining length in that dash
        dash_idx = 0
        remaining_offset = offset
        while remaining_offset >= dash_array[dash_idx % len(dash_array)]:
            remaining_offset -= dash_array[dash_idx % len(dash_array)]
            dash_idx += 1

        drawing = (dash_idx % 2 == 0)  # Even indices = draw, odd = gap
        starts_drawing = drawing  # whether the walk begins in an "on" phase
        dash_remaining = dash_array[dash_idx % len(dash_array)] - remaining_offset

        # Walk segments, splitting at dash boundaries
        current_subpath: SubPath = []
        seg_idx = 0
        seg_consumed = 0.0  # how much of current segment has been consumed

        while seg_idx < len(segments):
            seg = segments[seg_idx]

            # Get remaining length of current segment
            if seg[0] == 'line':
                seg_total = line_length(seg[1], seg[2])
            else:
                seg_total = cubic_arc_length_approx(seg[1], seg[2], seg[3], seg[4])

            seg_left = seg_total - seg_consumed

            if seg_left <= 1e-12:
                seg_idx += 1
                seg_consumed = 0.0
                continue

            if dash_remaining >= seg_left - 1e-12:
                # Entire remaining segment fits in current dash
                if drawing:
                    if not current_subpath:
                        # Get current point on segment
                        if seg[0] == 'line':
                            if seg_consumed > 1e-12:
                                t = seg_consumed / seg_total
                                cp = _lerp(seg[1], seg[2], t)
                            else:
                                cp = seg[1]
                            current_subpath.append(MoveTo(cp.x, cp.y))
                            current_subpath.append(LineTo(seg[2].x, seg[2].y))
                        else:
                            p0, p1, p2, p3 = seg[1], seg[2], seg[3], seg[4]
                            if seg_consumed > 1e-12:
                                t = _find_cubic_t_for_length(p0, p1, p2, p3, seg_consumed)
                                _, right = split_cubic(p0, p1, p2, p3, t)
                                p0, p1, p2, p3 = right
                            current_subpath.append(MoveTo(p0.x, p0.y))
                            current_subpath.append(CurveTo(p1.x, p1.y, p2.x, p2.y, p3.x, p3.y))
                    else:
                        if seg[0] == 'line':
                            if seg_consumed > 1e-12:
                                t = seg_consumed / seg_total
                                cp = _lerp(seg[1], seg[2], t)
                                current_subpath.append(LineTo(seg[2].x, seg[2].y))
                            else:
                                current_subpath.append(LineTo(seg[2].x, seg[2].y))
                        else:
                            p0, p1, p2, p3 = seg[1], seg[2], seg[3], seg[4]
                            if seg_consumed > 1e-12:
                                t = _find_cubic_t_for_length(p0, p1, p2, p3, seg_consumed)
                                _, right = split_cubic(p0, p1, p2, p3, t)
                                p0, p1, p2, p3 = right
                            current_subpath.append(CurveTo(p1.x, p1.y, p2.x, p2.y, p3.x, p3.y))

                dash_remaining -= seg_left
                seg_idx += 1
                seg_consumed = 0.0
            else:
                # Dash boundary falls within this segment — split it
                split_at = seg_consumed + dash_remaining

                if seg[0] == 'line':
                    t = split_at / seg_total if seg_total > 1e-12 else 0.0
                    split_pt = _lerp(seg[1], seg[2], t)
                    if drawing:
                        if not current_subpath:
                            if seg_consumed > 1e-12:
                                t0 = seg_consumed / seg_total
                                cp = _lerp(seg[1], seg[2], t0)
                            else:
                                cp = seg[1]
                            current_subpath.append(MoveTo(cp.x, cp.y))
                        current_subpath.append(LineTo(split_pt.x, split_pt.y))
                else:
                    p0, p1, p2, p3 = seg[1], seg[2], seg[3], seg[4]
                    t = _find_cubic_t_for_length(p0, p1, p2, p3, split_at)
                    left, right = split_cubic(p0, p1, p2, p3, t)
                    if drawing:
                        lp0, lp1, lp2, lp3 = left
                        if seg_consumed > 1e-12:
                            t0 = _find_cubic_t_for_length(p0, p1, p2, p3, seg_consumed)
                            _, rem = split_cubic(p0, p1, p2, p3, t0)
                            # Re-split the remaining part
                            rem_total = cubic_arc_length_approx(*rem)
                            if rem_total > 1e-12:
                                t2 = _find_cubic_t_for_length(*rem, dash_remaining)
                                left2, _ = split_cubic(*rem, t2)
                                lp0, lp1, lp2, lp3 = left2
                        if not current_subpath:
                            current_subpath.append(MoveTo(lp0.x, lp0.y))
                        current_subpath.append(CurveTo(lp1.x, lp1.y, lp2.x, lp2.y, lp3.x, lp3.y))

                seg_consumed = split_at

                # Switch dash phase
                if drawing and current_subpath:
                    result.append(current_subpath)
                    current_subpath = []
                elif not drawing:
                    current_subpath = []

                dash_idx += 1
                drawing = (dash_idx % 2 == 0)
                dash_remaining = dash_array[dash_idx % len(dash_array)]

        # Finish last dash segment
        ends_mid_dash = drawing and bool(current_subpath)
        if ends_mid_dash:
            result.append(current_subpath)

        # For closed subpaths: closepath creates a line join at the closure
        # point.  If the walk started in a dash (starts_drawing), merge the
        # first and last dashes so the outline assembler produces a join
        # instead of butt caps at the closure.
        num_dashes = len(result) - result_start
        if closed and starts_drawing and ends_mid_dash and num_dashes >= 2:
            first_sp = result[result_start]
            last_sp = result[-1]
            # Merge: last dash segments followed by first dash segments.
            # The join at the closure point will be computed naturally by
            # _assemble_open_outline.
            for elem in first_sp:
                if not isinstance(elem, MoveTo):
                    last_sp.append(elem)
            # Remove the original first subpath
            del result[result_start]
        elif closed and starts_drawing and num_dashes == 1:
            # Single dash covers the entire closed path — mark it closed.
            result[-1].append(ClosePath())

    return result


# ---------------------------------------------------------------------------
# Offset computations
# ---------------------------------------------------------------------------

def _normal(p0: Point, p1: Point) -> Point:
    """Unit normal perpendicular to line p0→p1 (pointing left of travel)."""
    d = p1 - p0
    ln = d.length()
    if ln < 1e-12:
        return Point(0.0, 0.0)
    return Point(-d.y / ln, d.x / ln)


def _offset_line(p0: Point, p1: Point, dist: float) -> tuple[Point, Point]:
    """Offset line segment by dist along its left normal."""
    n = _normal(p0, p1)
    off = n * dist
    return (p0 + off, p1 + off)


# ---------------------------------------------------------------------------
# Raw-float versions of the cubic offset hot path.
# All points are (x, y) float tuples to avoid Point object overhead.
# ---------------------------------------------------------------------------

_hypot = math.hypot
_fabs = abs

def _normal_raw(p0x: float, p0y: float, p1x: float, p1y: float) -> tuple[float, float]:
    """Unit normal as (nx, ny) from raw float coords."""
    dx = p1x - p0x
    dy = p1y - p0y
    ln = _hypot(dx, dy)
    if ln < 1e-12:
        return 0.0, 0.0
    return -dy / ln, dx / ln


def _offset_cubic_recursive_raw(p0x: float, p0y: float, p1x: float, p1y: float,
                                 p2x: float, p2y: float, p3x: float, p3y: float,
                                 dist: float, tol: float, depth: int,
                                 max_depth: int, result: list[CurveTo]) -> tuple[float, float]:
    """
    Offset a cubic Bézier using raw floats.  Appends CurveTo elements to
    *result* list in-place and returns (off_start_x, off_start_y).
    """
    hypot = _hypot
    fabs = _fabs

    # --- endpoint normals (start) ---
    n0x, n0y = _normal_raw(p0x, p0y, p1x, p1y)
    if hypot(p1x - p0x, p1y - p0y) < 1e-4:
        n0x, n0y = _normal_raw(p0x, p0y, p2x, p2y)
    if hypot(p2x - p0x, p2y - p0y) < 1e-4:
        n0x, n0y = _normal_raw(p0x, p0y, p3x, p3y)

    n3x, n3y = _normal_raw(p2x, p2y, p3x, p3y)
    if hypot(p3x - p2x, p3y - p2y) < 1e-4:
        n3x, n3y = _normal_raw(p1x, p1y, p3x, p3y)
    if hypot(p3x - p1x, p3y - p1y) < 1e-4:
        n3x, n3y = _normal_raw(p0x, p0y, p3x, p3y)

    # --- decide: flatten or subdivide ---
    flat = False
    if depth >= max_depth:
        flat = True
    else:
        # inline _cubic_is_flat
        cdx = p3x - p0x
        cdy = p3y - p0y
        cln = hypot(cdx, cdy)
        if cln < 1e-12:
            is_flat = (hypot(p1x - p0x, p1y - p0y) < tol
                       and hypot(p2x - p0x, p2y - p0y) < tol)
        else:
            nx = -cdy / cln
            ny = cdx / cln
            d1 = fabs((p1x - p0x) * nx + (p1y - p0y) * ny)
            d2 = fabs((p2x - p0x) * nx + (p2y - p0y) * ny)
            is_flat = max(d1, d2) < tol

        if is_flat:
            # inline _normals_are_close
            dot = n0x * n3x + n0y * n3y
            if dot < 0.966:
                flat = False
            else:
                clamped = max(-1.0, min(1.0, dot))
                deviation = fabs(dist) * (1.0 - clamped)
                flat = deviation <= tol

    if flat:
        # Tiller-Hanson offset
        off0x = p0x + n0x * dist
        off0y = p0y + n0y * dist
        result.append(CurveTo(
            p1x + n0x * dist, p1y + n0y * dist,
            p2x + n3x * dist, p2y + n3y * dist,
            p3x + n3x * dist, p3y + n3y * dist))
        return off0x, off0y

    # --- subdivide at t=0.5 (de Casteljau inlined) ---
    q0x = (p0x + p1x) * 0.5;  q0y = (p0y + p1y) * 0.5
    q1x = (p1x + p2x) * 0.5;  q1y = (p1y + p2y) * 0.5
    q2x = (p2x + p3x) * 0.5;  q2y = (p2y + p3y) * 0.5
    r0x = (q0x + q1x) * 0.5;  r0y = (q0y + q1y) * 0.5
    r1x = (q1x + q2x) * 0.5;  r1y = (q1y + q2y) * 0.5
    sx  = (r0x + r1x) * 0.5;  sy  = (r0y + r1y) * 0.5

    nd1 = depth + 1
    off_start = _offset_cubic_recursive_raw(
        p0x, p0y, q0x, q0y, r0x, r0y, sx, sy,
        dist, tol, nd1, max_depth, result)
    _offset_cubic_recursive_raw(
        sx, sy, r1x, r1y, q2x, q2y, p3x, p3y,
        dist, tol, nd1, max_depth, result)
    return off_start


def _normals_are_close(n0: Point, n3: Point, dist: float, tol: float) -> bool:
    """Check if endpoint normals are similar enough for Tiller-Hanson accuracy.

    When normals diverge, the offset curve deviates from the true offset by
    approximately |dist| * (1 - **cos**(angle_between_normals)).  We subdivide
    further if this exceeds the tolerance.
    """
    dot = n0.x * n3.x + n0.y * n3.y
    # dot = cos(angle); deviation ≈ |dist| * (1 - dot)
    deviation = abs(dist) * (1.0 - max(-1.0, min(1.0, dot)))
    # Also subdivide if normals diverge more than ~15 degrees regardless of
    # distance, to keep control points close to the true offset curve.
    if dot < 0.966:  # cos(15°) ≈ 0.966
        return False
    return deviation <= tol


def offset_segment(start: Point, seg: PathElement, dist: float, tol: float = 0.1) -> tuple[list[PathElement], Point, Point]:
    """
    Offset a single path segment. Returns (list_of_elements, offset_start, offset_end).
    Elements are LineTo or CurveTo.
    """
    if isinstance(seg, LineTo):
        end = Point(seg.x, seg.y)
        op0, op1 = _offset_line(start, end, dist)
        return [LineTo(op1.x, op1.y)], op0, op1

    if isinstance(seg, CurveTo):
        p0x, p0y = start.x, start.y
        p1x, p1y = seg.x1, seg.y1
        p2x, p2y = seg.x2, seg.y2
        p3x, p3y = seg.x3, seg.y3
        # If the curve is very short relative to the offset distance,
        # treat it as a line to avoid degenerate Tiller-Hanson results.
        chord = math.hypot(p3x - p0x, p3y - p0y)
        if chord < abs(dist) * 0.1:
            end = Point(p3x, p3y)
            op0, op1 = _offset_line(start, end, dist)
            return [LineTo(op1.x, op1.y)], op0, op1
        result = []
        osx, osy = _offset_cubic_recursive_raw(
            p0x, p0y, p1x, p1y, p2x, p2y, p3x, p3y,
            dist, tol, 0, 10, result)
        off_start = Point(osx, osy)
        if result:
            last = result[-1]
            off_end = Point(last.x3, last.y3)
        else:
            off_end = off_start
        return result, off_start, off_end

    return [], start, start


def _tangent_at_start(start: Point, seg: PathElement) -> Point:
    """Get tangent direction at start of segment."""
    if isinstance(seg, LineTo):
        return Point(seg.x - start.x, seg.y - start.y)
    if isinstance(seg, CurveTo):
        t = Point(seg.x1 - start.x, seg.y1 - start.y)
        if t.length() < 1e-4:
            t = Point(seg.x2 - start.x, seg.y2 - start.y)
        if t.length() < 1e-4:
            t = Point(seg.x3 - start.x, seg.y3 - start.y)
        return t
    return Point(1, 0)


def _tangent_at_end(start: Point, seg: PathElement) -> Point:
    """Get tangent direction at end of segment."""
    if isinstance(seg, LineTo):
        return Point(seg.x - start.x, seg.y - start.y)
    if isinstance(seg, CurveTo):
        end = Point(seg.x3, seg.y3)
        t = Point(end.x - seg.x2, end.y - seg.y2)
        if t.length() < 1e-4:
            t = Point(end.x - seg.x1, end.y - seg.y1)
        if t.length() < 1e-4:
            t = Point(end.x - start.x, end.y - start.y)
        return t
    return Point(1, 0)


# ---------------------------------------------------------------------------
# Line joins
# ---------------------------------------------------------------------------

def _arc_to_cubics(center: Point, radius: float, start_angle: float, end_angle: float) -> list[CurveTo]:
    """Convert **arc** to cubic Bézier approximations (max 90° per segment)."""
    result = []
    angle = end_angle - start_angle
    # Normalize to (-pi, pi] with epsilon tolerance to avoid
    # flipping ±π arcs due to floating-point rounding
    while angle > math.pi + 1e-10:
        angle -= 2 * math.pi
    while angle < -math.pi - 1e-10:
        angle += 2 * math.pi

    n_segs = max(1, int(math.ceil(abs(angle) / (math.pi / 2))))
    seg_angle = angle / n_segs

    for i in range(n_segs):
        a0 = start_angle + i * seg_angle
        a1 = a0 + seg_angle
        # Cubic approximation of arc
        alpha = 4.0 * math.tan(seg_angle / 4.0) / 3.0

        cos0, sin0 = math.cos(a0), math.sin(a0)
        cos1, sin1 = math.cos(a1), math.sin(a1)

        p1x = center.x + radius * (cos0 - alpha * sin0)
        p1y = center.y + radius * (sin0 + alpha * cos0)
        p2x = center.x + radius * (cos1 + alpha * sin1)
        p2y = center.y + radius * (sin1 - alpha * cos1)
        p3x = center.x + radius * cos1
        p3y = center.y + radius * sin1

        result.append(CurveTo(p1x, p1y, p2x, p2y, p3x, p3y))

    return result


def _circle_line_intersection(center: Point, radius: float,
                              line_pt: Point, line_dir: Point) -> list[Point]:
    """Find intersections of a circle with a line (point + direction vector).
    Returns 0, 1, or 2 intersection points."""
    dx = line_pt.x - center.x
    dy = line_pt.y - center.y
    a = line_dir.x ** 2 + line_dir.y ** 2
    if a < 1e-24:
        return []
    b = 2 * (dx * line_dir.x + dy * line_dir.y)
    c = dx ** 2 + dy ** 2 - radius ** 2
    disc = b * b - 4 * a * c
    if disc < 0:
        return []
    sqrt_disc = math.sqrt(max(0, disc))
    t1 = (-b - sqrt_disc) / (2 * a)
    t2 = (-b + sqrt_disc) / (2 * a)
    return [
        Point(line_pt.x + t1 * line_dir.x, line_pt.y + t1 * line_dir.y),
        Point(line_pt.x + t2 * line_dir.x, line_pt.y + t2 * line_dir.y),
    ]


def _line_line_intersection(p1: Point, d1: Point, p2: Point, d2: Point) -> Point | None:
    """Intersect ray p1+t*d1 with ray p2+s*d2. Returns intersection point or None."""
    denom = d1.x * d2.y - d1.y * d2.x
    if abs(denom) < 1e-12:
        return None
    diff = Point(p2.x - p1.x, p2.y - p1.y)
    t = (diff.x * d2.y - diff.y * d2.x) / denom
    return Point(p1.x + t * d1.x, p1.y + t * d1.y)



def _compute_inner_join_point(prev_end: Point, prev_tangent: Point,
                              next_start: Point, next_tangent: Point,
                              half_width: float = 0.0) -> Point | None:
    """Compute intersection of inner offset lines at a join to prevent self-crossing.

    Rejects intersections that are unreasonably far from the join, using
    half_width as the **scale** reference.  This prevents degenerate spikes from
    Tiller-Hanson offset drift while allowing legitimate sharp joins.
    """
    d_prev = prev_tangent.normalized()
    d_next = next_tangent.normalized()

    # Reject near-parallel cases (numerically unstable intersection)
    cross = d_prev.cross(d_next)
    if abs(cross) < 1e-6:
        return None

    pt = _line_line_intersection(prev_end, d_prev, next_start, d_next)
    if pt is None:
        return None

    # Direction check: the trim point must be "forward" from prev_end
    # (positive t along d_prev) and "backward" from next_start (negative t
    # along d_next).  A trim point in the wrong direction creates spikes.
    t_prev = (pt - prev_end).dot(d_prev)
    t_next = (pt - next_start).dot(d_next)
    if t_prev < 0 or t_next > 0:
        return None

    # Distance sanity check: the trim point should be within a reasonable
    # distance of the gap midpoint.  Use half_width as the scale — the trim
    # point should never be farther than a few line-widths from the join.
    if half_width > 0:
        mid = Point((prev_end.x + next_start.x) * 0.5,
                     (prev_end.y + next_start.y) * 0.5)
        dist = (pt - mid).length()
        gap = (next_start - prev_end).length()
        limit = max(gap * 2.0, half_width * 4.0)
        if dist > limit:
            return None

    return pt


def make_join(prev_end: Point, prev_tangent: Point, next_start: Point,
              next_tangent: Point, half_width: float, join_type: int,
              miter_limit: float, side: float) -> list[PathElement]:
    """
    Generate join geometry between two consecutive offset segments.
    side: +1 for left offset, -1 for right offset.
    Returns list of LineTo/CurveTo elements (no more _TrimJoin sentinels).
    For inside joins, returns empty list — the caller handles trimming.
    """
    # The offset endpoints that need joining
    n_prev = Point(-prev_tangent.y, prev_tangent.x).normalized() * (half_width * side)
    n_next = Point(-next_tangent.y, next_tangent.x).normalized() * (half_width * side)

    # Check if this is an outside join (where material is added).
    # Anti-parallel tangents (U-turn) have cross ≈ 0 but dot < 0 and are
    # always outside joins on both sides.
    cross = prev_tangent.cross(next_tangent)
    dot = prev_tangent.dot(next_tangent)
    is_outside = (cross * side) < 0
    if abs(cross) < 1e-6 and dot < 0:
        is_outside = True  # U-turn: outside on both sides

    if not is_outside:
        # Inside join — handled by caller via trim_point, nothing to emit
        return []

    if join_type == 2:  # Bevel
        return [LineTo(next_start.x, next_start.y)]

    if join_type == 1:  # Round
        # Arc from prev_end to next_start around the path point
        # The path point (without offset) is the join vertex
        vertex = prev_end - n_prev  # undo offset to get path point
        a0 = math.atan2(n_prev.y, n_prev.x)
        a1 = math.atan2(n_next.y, n_next.x)

        # For near-180° arcs (U-turns), floating-point noise in atan2
        # can flip the sweep direction when the normal's minor component
        # crosses zero. Verify the arc midpoint is on the outside of the
        # join (in the direction the path was traveling) and flip if not.
        raw = a1 - a0
        while raw > math.pi + 1e-10:
            raw -= 2 * math.pi
        while raw < -math.pi - 1e-10:
            raw += 2 * math.pi
        if abs(abs(raw) - math.pi) < 0.1:
            mid_angle = a0 + raw / 2
            arc_mid_dir = Point(math.cos(mid_angle), math.sin(mid_angle))
            pt = prev_tangent.normalized()
            if arc_mid_dir.dot(pt) < 0:
                # Arc sweeps through interior — flip direction
                if raw > 0:
                    raw -= 2 * math.pi
                else:
                    raw += 2 * math.pi
                a1 = a0 + raw

        curves = _arc_to_cubics(vertex, half_width, a0, a1)
        if not curves:
            return [LineTo(next_start.x, next_start.y)]
        return curves

    # Miter (type 0)
    # Find intersection of the two offset lines
    d_prev = prev_tangent.normalized()
    d_next = next_tangent.normalized()
    denom = d_prev.cross(d_next)

    if abs(denom) < 1e-12:
        # Parallel — bevel fallback
        return [LineTo(next_start.x, next_start.y)]

    # Check miter limit before computing intersection.
    # PLRM: miter_length / line_width = 1 / sin(φ/2)
    # where φ is the angle between the two path segments.
    # d_prev and d_next are the tangent directions of the offset segments,
    # which are parallel to the original path tangents.
    dot = max(-1.0, min(1.0, d_prev.x * d_next.x + d_prev.y * d_next.y))
    # Angle between tangents = π - φ (since they point in the direction of travel)
    # cos(angle_between) = dot, so φ = π - angle_between
    # sin(φ/2) = sin((π - angle_between)/2) = cos(angle_between/2)
    # cos(angle_between/2) = sqrt((1 + cos(angle_between))/2) = sqrt((1 + dot)/2)
    cos_half = math.sqrt(max(0.0, (1.0 + dot) / 2.0))
    if cos_half < 1e-12:
        return [LineTo(next_start.x, next_start.y)]
    miter_ratio = 1.0 / cos_half  # miter_length / line_width
    if miter_ratio > miter_limit:
        # Bevel fallback
        return [LineTo(next_start.x, next_start.y)]

    # Parameterize: prev_end + t * d_prev = next_start + s * d_next
    diff = next_start - prev_end
    t = diff.cross(d_next) / denom
    miter_pt = Point(prev_end.x + t * d_prev.x, prev_end.y + t * d_prev.y)

    return [LineTo(miter_pt.x, miter_pt.y), LineTo(next_start.x, next_start.y)]


# ---------------------------------------------------------------------------
# Line caps
# ---------------------------------------------------------------------------

def make_cap(point: Point, tangent: Point, half_width: float, cap_type: int,
             is_start: bool) -> list[PathElement]:
    """
    Generate cap geometry at an endpoint.
    tangent: direction of travel AT the endpoint.
    is_start: True for start cap (tangent points away from path), False for end cap.
    Returns list of LineTo/CurveTo elements connecting left offset to right offset.
    """
    t = tangent.normalized()
    if is_start:
        t = Point(-t.x, -t.y)  # Reverse for start cap

    n = Point(-t.y, t.x)  # Left normal

    left = point + n * half_width
    right = point - n * half_width

    if cap_type == 0:  # Butt
        return [LineTo(right.x, right.y)]

    if cap_type == 1:  # Round
        # Semicircle from left to right around point.
        # Use two explicit 90° arcs to avoid ±π floating-point normalization issues.
        a0 = math.atan2(n.y, n.x)
        a_mid = a0 - math.pi / 2.0
        a1 = a_mid - math.pi / 2.0
        curves = _arc_to_cubics(point, half_width, a0, a_mid)
        curves += _arc_to_cubics(point, half_width, a_mid, a1)
        if not curves:
            return [LineTo(right.x, right.y)]
        return curves

    if cap_type == 2:  # Projecting square
        ext = t * half_width
        p1 = left + ext
        p2 = right + ext
        return [LineTo(p1.x, p1.y), LineTo(p2.x, p2.y), LineTo(right.x, right.y)]

    return [LineTo(right.x, right.y)]


# ---------------------------------------------------------------------------
# Outline assembly
# ---------------------------------------------------------------------------

def _get_geometric_segments(sp: SubPath) -> list[tuple[Point, PathElement]]:
    """Extract geometric segments from a subpath. Returns list of (start_point, element)."""
    segments = []
    current = subpath_start(sp)
    for elem in sp:
        if isinstance(elem, MoveTo):
            current = Point(elem.x, elem.y)
        elif isinstance(elem, (LineTo, CurveTo)):
            ep = segment_endpoint(elem)
            # Skip near-zero-length segments that would produce zero tangents.
            # The tangent functions fall back through cp2→cp1→start with a
            # 1e-4 threshold, so any segment with all points within that
            # distance of each other yields a zero tangent and must be skipped.
            if isinstance(elem, CurveTo):
                cp1 = Point(elem.x1, elem.y1)
                cp2 = Point(elem.x2, elem.y2)
                if ((ep - current).length() < 1e-4 and
                        (cp1 - current).length() < 1e-4 and
                        (cp2 - current).length() < 1e-4):
                    continue
            else:
                if (ep - current).length() < 1e-4:
                    continue
            segments.append((current, elem))
            current = ep
        elif isinstance(elem, ClosePath):
            # Close with line if needed
            start = subpath_start(sp)
            dist = (current - start).length()
            if dist > 1e-12:
                close_seg = LineTo(start.x, start.y)
                segments.append((current, close_seg))
    return segments


def _filter_uturn_segments(segments: list[tuple[Point, PathElement]], half_width: float) -> list[tuple[Point, PathElement]]:
    """Remove short segments that create back-to-back U-turns.

    When roundpath creates nearly-overlapping arcs, a tiny connecting segment
    appears between them with anti-parallel tangents on both sides.  These
    produce overlapping 180° joins that cancel under **fill**.  Removing the
    degenerate segment lets the neighboring segments join directly with a
    normal round join.
    """
    if len(segments) < 3:
        return segments

    def _is_uturn(tangent_a: Point, tangent_b: Point) -> bool:
        la = tangent_a.length()
        lb = tangent_b.length()
        if la < 1e-10 or lb < 1e-10:
            return False
        na = Point(tangent_a.x / la, tangent_a.y / la)
        nb = Point(tangent_b.x / lb, tangent_b.y / lb)
        cross = na.cross(nb)
        dot = na.dot(nb)
        return abs(cross) < 0.1 and dot < -0.5

    # Mark segments for removal
    to_remove = set()
    for i in range(len(segments)):
        start_i, seg_i = segments[i]
        ep_i = segment_endpoint(seg_i)
        seg_len = (ep_i - start_i).length()
        if seg_len > half_width:
            continue

        # Check tangent continuity with previous and next segments
        prev_idx = i - 1
        next_idx = i + 1
        if prev_idx < 0 or next_idx >= len(segments):
            continue

        prev_start, prev_seg = segments[prev_idx]
        next_start, next_seg = segments[next_idx]

        end_tang_prev = _tangent_at_end(prev_start, prev_seg)
        start_tang_cur = _tangent_at_start(start_i, seg_i)
        end_tang_cur = _tangent_at_end(start_i, seg_i)
        start_tang_next = _tangent_at_start(next_start, next_seg)

        ut1 = _is_uturn(end_tang_prev, start_tang_cur)
        ut2 = _is_uturn(end_tang_cur, start_tang_next)

        if ut1 and ut2:
            to_remove.add(i)
        elif (ut1 or ut2) and seg_len < half_width * 0.25:
            to_remove.add(i)

    if not to_remove:
        return segments

    # Rebuild segments, adjusting start points to close gaps left by removals
    result = []
    for i, (start, seg) in enumerate(segments):
        if i in to_remove:
            continue
        if result and i > 0 and (i - 1) in to_remove:
            # Previous segment was removed — update our start to match
            # the endpoint of the last kept segment
            prev_end = segment_endpoint(result[-1][1])
            start = prev_end
        result.append((start, seg))
    return result


def strokepath(path: Path, line_width: float, line_cap: int = 0,
               line_join: int = 0, miter_limit: float = 10.0,
               dash_array: list[float] | None = None,
               dash_offset: float = 0.0, tolerance: float = 0.1) -> Path:
    """
    Convert a stroked path to a filled outline path.

    Returns a flat list of subpaths (the complete outline path).
    Use strokepath_grouped() if you need independent **fill** groups.
    """
    groups = strokepath_grouped(path, line_width, line_cap, line_join,
                                 miter_limit, dash_array, dash_offset, tolerance)
    result: Path = []
    for group in groups:
        result.extend(group)
    return result


def strokepath_grouped(path: Path, line_width: float, line_cap: int = 0,
                       line_join: int = 0, miter_limit: float = 10.0,
                       dash_array: list[float] | None = None,
                       dash_offset: float = 0.0,
                       tolerance: float = 0.1) -> list[Path]:
    """
    Convert a stroked path to filled outline path(s), returned as groups.

    Each group is a list of subpaths that must be filled together (e.g. outer
    and inner outlines of a closed-path **stroke**). Independent outlines (e.g.
    individual dash segments) are returned as separate groups so they can be
    filled independently without winding-rule interference.

    Returns:
        List of groups, where each group is a Path (list of SubPaths).
    """
    half_width = line_width / 2.0
    if half_width < 1e-12:
        return []

    # Apply dash pattern
    if dash_array:
        working_subpaths = apply_dash_pattern(path, dash_array, dash_offset)
    else:
        working_subpaths = path

    groups: list[Path] = []

    for sp in working_subpaths:
        closed = subpath_is_closed(sp)
        segments = _get_geometric_segments(sp)

        if not segments:
            continue

        segments = _filter_uturn_segments(segments, half_width)

        if not segments:
            continue

        # Check for degenerate (zero-length) subpath.
        # Use control polygon length for curves, not chord length,
        # because a looping curve (e.g. rcurveto 0 0 endpoint) has
        # zero chord but non-zero arc length.
        total_len = 0.0
        for start, seg in segments:
            if isinstance(seg, CurveTo):
                p1 = Point(seg.x1, seg.y1)
                p2 = Point(seg.x2, seg.y2)
                p3 = Point(seg.x3, seg.y3)
                total_len += ((p1 - start).length() +
                              (p2 - p1).length() +
                              (p3 - p2).length())
            else:
                ep = segment_endpoint(seg)
                total_len += (ep - start).length()

        if total_len < 1e-12:
            # Degenerate subpath
            if line_cap == 1:  # Round cap = circle
                pt = segments[0][0]
                outline = _make_circle(pt, half_width)
                groups.append([outline])
            continue

        # Split any individual CurveTo segments that loop back to their
        # start (p0 ≈ p3).  The stroke outline of a self-overlapping curve
        # creates a figure-8 that cancels under any fill rule.  Splitting
        # at t=0.5 produces two non-overlapping halves.
        split_segments = []
        for start, seg in segments:
            if isinstance(seg, CurveTo):
                p3 = Point(seg.x3, seg.y3)
                if (p3 - start).length() < 1e-2:
                    p0 = start
                    p1 = Point(seg.x1, seg.y1)
                    p2 = Point(seg.x2, seg.y2)
                    left_half, right_half = split_cubic(p0, p1, p2, p3, 0.5)
                    lp0, lp1, lp2, lp3 = left_half
                    rp0, rp1, rp2, rp3 = right_half
                    split_segments.append((lp0, CurveTo(lp1.x, lp1.y, lp2.x, lp2.y, lp3.x, lp3.y)))
                    split_segments.append((rp0, CurveTo(rp1.x, rp1.y, rp2.x, rp2.y, rp3.x, rp3.y)))
                    continue
            split_segments.append((start, seg))
        segments = split_segments

        # Compute left and right offsets
        left_offsets = []  # list of (elements, start_pt, end_pt)
        right_offsets = []

        for start, seg in segments:
            l_elems, l_start, l_end = offset_segment(start, seg, half_width, tolerance)
            r_elems, r_start, r_end = offset_segment(start, seg, -half_width, tolerance)
            left_offsets.append((l_elems, l_start, l_end))
            right_offsets.append((r_elems, r_start, r_end))

        if closed:
            # Closed subpath: outer + inner must be filled together so the
            # non-zero winding rule cuts out the interior.
            left_outline = _assemble_closed_outline(
                segments, left_offsets, half_width, line_join, miter_limit, +1)
            right_outline = _assemble_closed_outline(
                segments, right_offsets, half_width, line_join, miter_limit, -1)
            group: Path = []
            if left_outline:
                group.append(left_outline)
            if right_outline:
                group.append(_reverse_closed_outline(right_outline))
            if group:
                groups.append(group)
        else:
            # Open subpath: single self-contained outline
            outline = _assemble_open_outline(
                segments, left_offsets, right_offsets,
                half_width, line_cap, line_join, miter_limit)
            if outline:
                groups.append([outline])

    return groups


def _make_circle(center: Point, radius: float) -> SubPath:
    """Make a circle as a closed subpath (4 × 90° cubic arcs)."""
    result: SubPath = []
    result.append(MoveTo(center.x + radius, center.y))
    # Four quarter-circle arcs to avoid angle normalization issues
    for i in range(4):
        a0 = i * math.pi / 2.0
        a1 = a0 + math.pi / 2.0
        arcs = _arc_to_cubics(center, radius, a0, a1)
        result.extend(arcs)
    result.append(ClosePath())
    return result


def _reverse_closed_outline(sp: SubPath) -> SubPath:
    """
    Reverse the winding direction of a closed outline subpath.
    Assumes sp starts with MoveTo, ends with ClosePath, and contains
    LineTo/CurveTo in between.
    """
    if len(sp) < 3:
        return sp

    # Collect all points: start from MoveTo, then walk elements
    # Build a list of (element, endpoint) pairs, then reverse them
    start = Point(sp[0].x, sp[0].y)  # MoveTo
    # Inner elements (between MoveTo and ClosePath)
    inner = sp[1:-1]  # skip MoveTo and ClosePath

    # Build the point chain: start -> elem endpoints
    points = [start]
    for e in inner:
        if isinstance(e, LineTo):
            points.append(Point(e.x, e.y))
        elif isinstance(e, CurveTo):
            points.append(Point(e.x3, e.y3))

    # Reversed outline: start at what was the last point, traverse backwards
    result: SubPath = []
    result.append(MoveTo(points[-1].x, points[-1].y))

    for i in range(len(inner) - 1, -1, -1):
        target = points[i]  # endpoint when reversed = the previous start point
        e = inner[i]
        if isinstance(e, LineTo):
            result.append(LineTo(target.x, target.y))
        elif isinstance(e, CurveTo):
            # Reverse cubic: swap control points and endpoint
            result.append(CurveTo(e.x2, e.y2, e.x1, e.y1, target.x, target.y))

    result.append(ClosePath())
    return result


def _trim_outline_end(outline: SubPath, pt: Point) -> None:
    """Replace the endpoint of the last LineTo/CurveTo in the outline."""
    for j in range(len(outline) - 1, -1, -1):
        last = outline[j]
        if isinstance(last, LineTo):
            outline[j] = LineTo(pt.x, pt.y)
            return
        elif isinstance(last, CurveTo):
            # Check if the trim point is close to the original endpoint.
            # If so, just adjust the endpoint. If it's far away, the old
            # control points would distort the curve and create a spike —
            # replace the entire curve with a LineTo instead.
            orig_end = Point(last.x3, last.y3)
            dist = (pt - orig_end).length()
            cp_span = max(
                abs(last.x2 - last.x3) + abs(last.y2 - last.y3),
                abs(last.x1 - last.x3) + abs(last.y1 - last.y3),
                1e-6)
            if dist < cp_span * 0.5:
                outline[j] = CurveTo(last.x1, last.y1, last.x2, last.y2, pt.x, pt.y)
            else:
                outline[j] = LineTo(pt.x, pt.y)
            return


def _trim_elements_start(elems: list[PathElement], pt: Point) -> list[PathElement]:
    """Return a copy of elems with the first element's implicit start replaced.

    For the first element, we adjust so the segment starts from pt instead of
    its original start. For LineTo this is a no-op (endpoint unchanged).
    For CurveTo we keep the endpoint but shift cp1 toward pt.
    In practice we just skip/replace — the MoveTo or join already positioned us at pt.
    """
    # The elements don't encode their start point (it's implicit from MoveTo or
    # previous element). So we don't need to modify them — the outline is already
    # at pt from the trim. The elements will draw FROM pt to their endpoints.
    return elems


def _assemble_closed_outline(segments: list[tuple[Point, PathElement]],
                              offsets: list[tuple[list[PathElement], Point, Point]],
                              half_width: float, line_join: int,
                              miter_limit: float, side: float) -> SubPath:
    """Assemble a closed outline from offset segments with joins."""
    if not offsets:
        return []

    outline: SubPath = []

    # Start with first offset
    first_start = offsets[0][1]
    outline.append(MoveTo(first_start.x, first_start.y))

    for i in range(len(offsets)):
        elems, _, end = offsets[i]
        outline.extend(elems)

        # Add join to next segment
        next_i = (i + 1) % len(offsets)
        next_start = offsets[next_i][1]

        prev_tangent = _tangent_at_end(segments[i][0], segments[i][1])
        next_tangent = _tangent_at_start(segments[next_i][0], segments[next_i][1])

        # Check if inside join (anti-parallel = U-turn = always outside)
        cross = prev_tangent.cross(next_tangent)
        dot = prev_tangent.dot(next_tangent)
        is_outside = (cross * side) < 0
        if abs(cross) < 1e-6 and dot < 0:
            is_outside = True
        if not is_outside:
            trim_pt = _compute_inner_join_point(end, prev_tangent, next_start, next_tangent, half_width)
            if trim_pt:
                _trim_outline_end(outline, trim_pt)
                continue
            else:
                outline.append(LineTo(next_start.x, next_start.y))
                continue

        join_elems = make_join(end, prev_tangent, next_start,
                               next_tangent, half_width, line_join,
                               miter_limit, side)
        outline.extend(join_elems)

    outline.append(ClosePath())
    return outline


def _assemble_open_outline(segments: list[tuple[Point, PathElement]],
                            left_offsets: list[tuple[list[PathElement], Point, Point]],
                            right_offsets: list[tuple[list[PathElement], Point, Point]],
                            half_width: float, line_cap: int, line_join: int,
                            miter_limit: float) -> SubPath:
    """Assemble an open subpath outline: left → end cap → right reversed → start cap."""
    if not left_offsets:
        return []

    outline: SubPath = []

    # --- Pre-detect degenerate first left segment ---
    # Mirror of the end cap truncation: if the first left segment's inner join
    # trim overshoots past its start, we skip it and truncate the start cap.
    # Only for round caps — butt/square caps handle self-intersection via fill rule.
    degenerate_first_left = False
    degenerate_first_trim_pt = None
    if line_cap == 1 and len(left_offsets) > 1:
        end0 = left_offsets[0][2]
        next_start0 = left_offsets[1][1]
        prev_tang0 = _tangent_at_end(segments[0][0], segments[0][1])
        next_tang0 = _tangent_at_start(segments[1][0], segments[1][1])
        cross0 = prev_tang0.cross(next_tang0)
        dot0 = prev_tang0.dot(next_tang0)
        is_outside0 = (cross0 * 1) < 0  # side=+1 for left
        if abs(cross0) < 1e-6 and dot0 < 0:
            is_outside0 = True
        if not is_outside0:
            trim_pt0 = _compute_inner_join_point(end0, prev_tang0, next_start0, next_tang0, half_width)
            if trim_pt0:
                fwd_dir = Point(left_offsets[0][2].x - left_offsets[0][1].x,
                                left_offsets[0][2].y - left_offsets[0][1].y)
                trim_from_start = Point(trim_pt0.x - left_offsets[0][1].x,
                                        trim_pt0.y - left_offsets[0][1].y)
                if fwd_dir.dot(trim_from_start) < 0:
                    degenerate_first_left = True
                    degenerate_first_trim_pt = trim_pt0

    # --- Left side (forward direction) ---
    if degenerate_first_left:
        # Start from the trim point instead of the first segment's start
        outline.append(MoveTo(degenerate_first_trim_pt.x, degenerate_first_trim_pt.y))
    else:
        first_left_start = left_offsets[0][1]
        outline.append(MoveTo(first_left_start.x, first_left_start.y))

    degenerate_last_left = False
    degenerate_last_left_trim_pt = None
    skip_left_indices = set()

    for i in range(len(left_offsets)):
        # Skip degenerate first segment — already handled by start cap truncation
        if i == 0 and degenerate_first_left:
            continue
        # Skip segments flagged as degenerate by previous iteration
        if i in skip_left_indices:
            continue

        elems, _, end = left_offsets[i]
        outline.extend(elems)

        if i < len(left_offsets) - 1:
            next_start = left_offsets[i + 1][1]
            prev_tangent = _tangent_at_end(segments[i][0], segments[i][1])
            next_tangent = _tangent_at_start(segments[i + 1][0], segments[i + 1][1])

            # Check if this is an inside join — if so, trim both sides to intersection
            cross = prev_tangent.cross(next_tangent)
            dot = prev_tangent.dot(next_tangent)
            is_outside = (cross * 1) < 0  # side=+1 for left
            if abs(cross) < 1e-6 and dot < 0:
                is_outside = True

            if not is_outside:
                trim_pt = _compute_inner_join_point(end, prev_tangent, next_start, next_tangent, half_width)
                if trim_pt:
                    # Check if trim overshoots past the NEXT segment's end
                    next_end = left_offsets[i + 1][2]
                    next_fwd = Point(next_end.x - next_start.x,
                                     next_end.y - next_start.y)
                    trim_from_next_start = Point(trim_pt.x - next_start.x,
                                                  trim_pt.y - next_start.y)
                    next_len_sq = next_fwd.dot(next_fwd)
                    if next_len_sq > 0:
                        t_param = next_fwd.dot(trim_from_next_start) / next_len_sq
                    else:
                        t_param = 0
                    if t_param > 1.0:
                        if line_cap == 1:
                            # Round cap: skip next segment, truncate cap arc
                            _trim_outline_end(outline, trim_pt)
                            skip_left_indices.add(i + 1)
                            if i + 1 == len(left_offsets) - 1:
                                degenerate_last_left = True
                                degenerate_last_left_trim_pt = trim_pt
                            continue
                        # Non-round cap: fall through to normal trim below.
                        # Small gap at inner corner is inherent to outline
                        # approach when segment < half_width.
                    _trim_outline_end(outline, trim_pt)
                    continue
                else:
                    outline.append(LineTo(next_start.x, next_start.y))
                    continue

            join_elems = make_join(end, prev_tangent, next_start,
                                   next_tangent, half_width, line_join,
                                   miter_limit, +1)
            outline.extend(join_elems)

    # --- End cap (with possible truncation for degenerate last segment) ---
    last_seg_start, last_seg = segments[-1]
    end_tangent = _tangent_at_end(last_seg_start, last_seg)
    end_point = segment_endpoint(last_seg)

    # Pre-detect: will the last right segment overshoot at the inner join?
    # If so, we truncate the end cap instead of drawing a full semicircle.
    # Only for round caps — butt/square caps handle via self-intersection.
    degenerate_last_right = False
    degenerate_trim_pt = None
    last_ri = len(right_offsets) - 1
    if line_cap == 1 and last_ri > 0:
        prev_right_start = right_offsets[last_ri][1]
        next_right_end = right_offsets[last_ri - 1][2]
        prev_tangent_r = _tangent_at_start(segments[last_ri][0], segments[last_ri][1])
        prev_tangent_r = Point(-prev_tangent_r.x, -prev_tangent_r.y)
        next_tangent_r = _tangent_at_end(segments[last_ri - 1][0], segments[last_ri - 1][1])
        next_tangent_r = Point(-next_tangent_r.x, -next_tangent_r.y)

        cross = prev_tangent_r.cross(next_tangent_r)
        dot_r = prev_tangent_r.dot(next_tangent_r)
        is_outside = (cross * 1) < 0
        if abs(cross) < 1e-6 and dot_r < 0:
            is_outside = True
        if not is_outside:
            trim_pt = _compute_inner_join_point(prev_right_start, prev_tangent_r,
                                                 next_right_end, next_tangent_r, half_width)
            if trim_pt:
                orig_dir = Point(right_offsets[last_ri][1].x - right_offsets[last_ri][2].x,
                                 right_offsets[last_ri][1].y - right_offsets[last_ri][2].y)
                trim_dir = Point(trim_pt.x - right_offsets[last_ri][2].x,
                                 trim_pt.y - right_offsets[last_ri][2].y)
                if orig_dir.dot(trim_dir) < 0:
                    degenerate_last_right = True
                    degenerate_trim_pt = trim_pt

    if degenerate_last_left and line_cap == 1:
        # The last left segment was skipped because the inner trim overshoots past it.
        # The cursor is at the trim point. We need to truncate the end cap on the LEFT
        # side: arc from the circle-line intersection to the right side.
        # Find where the previous segment's left offset line intersects the cap circle.
        prev_left_idx = len(left_offsets) - 2  # The segment before the skipped one
        prev_left_end = left_offsets[prev_left_idx][2]
        prev_left_tangent = _tangent_at_end(segments[prev_left_idx][0], segments[prev_left_idx][1])
        prev_left_dir = prev_left_tangent.normalized()

        hits = _circle_line_intersection(end_point, half_width, prev_left_end, prev_left_dir)
        if len(hits) >= 2:
            t_n = end_tangent.normalized()
            n = Point(-t_n.y, t_n.x)  # Left normal
            # The right side of the end cap
            right_n = Point(t_n.y, -t_n.x)  # Right normal
            a_right = math.atan2(right_n.y, right_n.x)

            # Pick the hit closest to the left side (smallest clockwise dist from left angle)
            a_left = math.atan2(n.y, n.x)
            best_hit = None
            best_angle_dist = float('inf')
            for h in hits:
                a_h = math.atan2(h.y - end_point.y, h.x - end_point.x)
                delta = a_left - a_h
                while delta < 0:
                    delta += 2 * math.pi
                while delta > 2 * math.pi:
                    delta -= 2 * math.pi
                if delta < math.pi + 0.01:
                    if delta < best_angle_dist:
                        best_angle_dist = delta
                        best_hit = h
                        best_a_h = a_h

            if best_hit is not None:
                # LineTo from trim point to the circle intersection
                outline.append(LineTo(best_hit.x, best_hit.y))
                # Arc from the circle intersection to the right side
                if abs(best_a_h - a_right) > 1e-10:
                    arc_elems = _arc_to_cubics(end_point, half_width, best_a_h, a_right)
                    outline.extend(arc_elems)
            else:
                cap_elems = make_cap(end_point, end_tangent, half_width, line_cap, is_start=False)
                outline.extend(cap_elems)
        else:
            cap_elems = make_cap(end_point, end_tangent, half_width, line_cap, is_start=False)
            outline.extend(cap_elems)
    elif degenerate_last_right and line_cap == 1:
        # Truncated round cap: arc from left end to where the previous segment's
        # right offset line intersects the cap circle, then LineTo to trim point.
        # Cap circle: center=end_point, radius=half_width
        # Line: through next_right_end along the previous segment's tangent direction
        seg0_right_end = right_offsets[last_ri - 1][2]
        seg0_tangent = _tangent_at_end(segments[last_ri - 1][0], segments[last_ri - 1][1])
        seg0_dir = seg0_tangent.normalized()

        hits = _circle_line_intersection(end_point, half_width, seg0_right_end, seg0_dir)
        if len(hits) >= 2:
            # Pick the intersection point that's on the cap side (between left and right)
            # The left end of the cap is where the arc starts
            left_end = left_offsets[-1][2]
            t_n = end_tangent.normalized()
            n = Point(-t_n.y, t_n.x)  # Left normal
            a_start = math.atan2(n.y, n.x)  # Angle of left offset point

            # Pick the hit furthest along the arc (largest clockwise angle within
            # the semicircle). The arc crosses the line twice — once entering,
            # once exiting. We want the exit point (furthest around the arc).
            best_hit = None
            best_angle_dist = -1.0
            for h in hits:
                a_h = math.atan2(h.y - end_point.y, h.x - end_point.x)
                # Arc goes clockwise from a_start, so angle decreases
                # Compute clockwise angular distance from a_start to a_h
                delta = a_start - a_h
                while delta < 0:
                    delta += 2 * math.pi
                while delta > 2 * math.pi:
                    delta -= 2 * math.pi
                # We want delta in (0, pi) — the hit should be within the semicircle
                if 1e-6 < delta < math.pi + 0.01:
                    if delta > best_angle_dist:
                        best_angle_dist = delta
                        best_hit = h

            if best_hit is not None:
                a_end = math.atan2(best_hit.y - end_point.y, best_hit.x - end_point.x)
                # Generate arc from a_start to a_end
                if abs(a_start - a_end) > 1e-10:
                    arc_elems = _arc_to_cubics(end_point, half_width, a_start, a_end)
                    outline.extend(arc_elems)
                # LineTo to the trim point (inner intersection of offset lines)
                outline.append(LineTo(degenerate_trim_pt.x, degenerate_trim_pt.y))
            else:
                # Fallback: full cap
                cap_elems = make_cap(end_point, end_tangent, half_width, line_cap, is_start=False)
                outline.extend(cap_elems)
        else:
            # No circle-line intersection — fallback to full cap
            cap_elems = make_cap(end_point, end_tangent, half_width, line_cap, is_start=False)
            outline.extend(cap_elems)
    else:
        cap_elems = make_cap(end_point, end_tangent, half_width, line_cap, is_start=False)
        outline.extend(cap_elems)

    # --- Right side (reverse direction) ---
    for i in range(len(right_offsets) - 1, -1, -1):
        elems, start, end = right_offsets[i]
        reversed_elems = _reverse_offset_elements(elems, start)

        # If this is the degenerate last segment we already handled via
        # truncated cap, skip it entirely.
        skip_reversed = False
        if i == last_ri and degenerate_last_right:
            skip_reversed = True
        elif line_cap == 1 and i > 0:
            # Check if there's an inside join that overshoots (round caps only).
            # For butt/square caps, self-intersection is handled by fill rule.
            prev_right_start = right_offsets[i][1]
            next_right_end = right_offsets[i - 1][2]
            prev_tangent_chk = _tangent_at_start(segments[i][0], segments[i][1])
            prev_tangent_chk = Point(-prev_tangent_chk.x, -prev_tangent_chk.y)
            next_tangent_chk = _tangent_at_end(segments[i - 1][0], segments[i - 1][1])
            next_tangent_chk = Point(-next_tangent_chk.x, -next_tangent_chk.y)

            cross = prev_tangent_chk.cross(next_tangent_chk)
            dot_chk = prev_tangent_chk.dot(next_tangent_chk)
            is_outside = (cross * 1) < 0
            if abs(cross) < 1e-6 and dot_chk < 0:
                is_outside = True
            if not is_outside:
                trim_pt = _compute_inner_join_point(prev_right_start, prev_tangent_chk,
                                                     next_right_end, next_tangent_chk, half_width)
                if trim_pt:
                    orig_dir = Point(right_offsets[i][1].x - right_offsets[i][2].x,
                                     right_offsets[i][1].y - right_offsets[i][2].y)
                    trim_dir = Point(trim_pt.x - right_offsets[i][2].x,
                                     trim_pt.y - right_offsets[i][2].y)
                    if orig_dir.dot(trim_dir) < 0:
                        skip_reversed = True

        if not skip_reversed:
            outline.extend(reversed_elems)

        if i > 0:
            if skip_reversed:
                # Degenerate segment skipped — cursor is at trim point or
                # cap truncation endpoint. Continue to next segment.
                continue

            prev_right_start = right_offsets[i][1]
            next_right_end = right_offsets[i - 1][2]
            prev_tangent = _tangent_at_start(segments[i][0], segments[i][1])
            prev_tangent = Point(-prev_tangent.x, -prev_tangent.y)
            next_tangent = _tangent_at_end(segments[i - 1][0], segments[i - 1][1])
            next_tangent = Point(-next_tangent.x, -next_tangent.y)

            cross = prev_tangent.cross(next_tangent)
            dot_rt = prev_tangent.dot(next_tangent)
            is_outside = (cross * 1) < 0
            if abs(cross) < 1e-6 and dot_rt < 0:
                is_outside = True
            if not is_outside:
                trim_pt = _compute_inner_join_point(prev_right_start, prev_tangent,
                                                     next_right_end, next_tangent, half_width)
                if trim_pt:
                    _trim_outline_end(outline, trim_pt)
                    continue
                else:
                    outline.append(LineTo(next_right_end.x, next_right_end.y))
                    continue

            join_elems = make_join(prev_right_start, prev_tangent, next_right_end,
                                   next_tangent, half_width, line_join,
                                   miter_limit, +1)
            outline.extend(join_elems)

    # --- Start cap (with possible truncation for degenerate first segment) ---
    first_seg_start, first_seg = segments[0]
    start_tangent = _tangent_at_start(first_seg_start, first_seg)

    if degenerate_first_left and line_cap == 1:
        # Truncated round start cap: arc from right[0].start around to where
        # seg 1's left offset line intersects the cap circle, then LineTo to trim point.
        # The start cap with is_start=True reverses the tangent, so the arc goes
        # from right side to left side.
        t_s = start_tangent.normalized()
        t_s = Point(-t_s.x, -t_s.y)  # Reverse for start cap
        n_s = Point(-t_s.y, t_s.x)   # Left normal of reversed tangent

        # Arc starts at "left" of reversed tangent = right_offsets[0].start
        a_start_angle = math.atan2(n_s.y, n_s.x)

        # Circle-line intersection with seg 1's left offset line
        seg1_left_start = left_offsets[1][1]
        seg1_tangent = _tangent_at_start(segments[1][0], segments[1][1])
        seg1_dir = seg1_tangent.normalized()

        hits = _circle_line_intersection(first_seg_start, half_width, seg1_left_start, seg1_dir)
        if len(hits) >= 2:
            # Pick the hit furthest along the arc (largest clockwise distance
            # within the semicircle), same logic as end cap truncation
            best_hit = None
            best_angle_dist = -1.0
            for h in hits:
                a_h = math.atan2(h.y - first_seg_start.y, h.x - first_seg_start.x)
                delta = a_start_angle - a_h
                while delta < 0:
                    delta += 2 * math.pi
                while delta > 2 * math.pi:
                    delta -= 2 * math.pi
                if 1e-6 < delta < math.pi + 0.01:
                    if delta > best_angle_dist:
                        best_angle_dist = delta
                        best_hit = h

            if best_hit is not None:
                a_end_angle = math.atan2(best_hit.y - first_seg_start.y,
                                         best_hit.x - first_seg_start.x)
                if abs(a_start_angle - a_end_angle) > 1e-10:
                    arc_elems = _arc_to_cubics(first_seg_start, half_width,
                                               a_start_angle, a_end_angle)
                    outline.extend(arc_elems)
                # LineTo to the trim point, then ClosePath returns to MoveTo(trim_pt)
                outline.append(LineTo(degenerate_first_trim_pt.x, degenerate_first_trim_pt.y))
            else:
                cap_elems = make_cap(first_seg_start, start_tangent, half_width, line_cap, is_start=True)
                outline.extend(cap_elems)
        else:
            cap_elems = make_cap(first_seg_start, start_tangent, half_width, line_cap, is_start=True)
            outline.extend(cap_elems)
    else:
        cap_elems = make_cap(first_seg_start, start_tangent, half_width, line_cap, is_start=True)
        outline.extend(cap_elems)

    outline.append(ClosePath())
    return outline


def _reverse_offset_elements(elems: list[PathElement], start: Point) -> list[PathElement]:
    """Reverse a list of offset elements (LineTo/CurveTo) so they go backwards."""
    if not elems:
        return []

    # Build list of points: start, then each endpoint
    points = [start]
    for e in elems:
        if isinstance(e, LineTo):
            points.append(Point(e.x, e.y))
        elif isinstance(e, CurveTo):
            points.append(Point(e.x3, e.y3))

    result = []
    for i in range(len(elems) - 1, -1, -1):
        e = elems[i]
        target = points[i]  # Where this reversed segment should end up at
        if isinstance(e, LineTo):
            result.append(LineTo(target.x, target.y))
        elif isinstance(e, CurveTo):
            # Reverse cubic: swap endpoints, swap control points
            result.append(CurveTo(e.x2, e.y2, e.x1, e.y1, target.x, target.y))
    return result
