# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
Type 2 CharString Interpreter for CFF Fonts

Executes Type 2 charstrings (Adobe TN#5177) for glyph rendering.
Type 2 differs from Type 1 in several key ways:
- No eexec encryption (raw bytes)
- Width is encoded as optional first operand before first stack-clearing op
- Width = extra_operand + nominalWidthX (or defaultWidthX if omitted)
- 5-byte number is 16.16 fixed-point (not integer)
- Built-in flex operators (no OtherSubrs mechanism)
- Multiple path operations per operator (e.g., rlineto takes N pairs)
- Different hint operators with explicit hint masks

Based on: Adobe Technical Note #5177 - The Type 2 Charstring Format
"""

import math
import struct

from . import types as ps
from . import color_space
from . import error as ps_error
from .display_list_builder import DisplayListBuilder
from ..operators.matrix import _transform_point, _transform_delta


class Type2Error(Exception):
    """Error during Type 2 charstring execution."""
    pass


def _subr_bias(n_subrs: int) -> int:
    """Calculate subroutine bias per Type 2 spec."""
    if n_subrs < 1240:
        return 107
    elif n_subrs < 33900:
        return 1131
    else:
        return 32768


class Type2CharStringInterpreter:
    """Type 2 CharString execution engine for CFF fonts.

    Executes Type 2 charstring programs, adding path operations directly to
    the PostScript graphics state path, following the same patterns as the
    Type 1 CharStringInterpreter.
    """

    def __init__(self, ctxt: ps.Context, font_dict: ps.Dict, default_width_x: float, nominal_width_x: float,
                 local_subrs: list[bytes], global_subrs: list[bytes], width_only_mode: bool = False) -> None:
        self.ctxt = ctxt
        self.font_dict = font_dict
        self.default_width_x = default_width_x
        self.nominal_width_x = nominal_width_x
        self.local_subrs = local_subrs
        self.global_subrs = global_subrs
        self.width_only_mode = width_only_mode

        self.stack = []
        self.current_point = (0.0, 0.0)
        self.advance_width = None
        self.width_parsed = False

        self.num_h_hints = 0
        self.num_v_hints = 0

        self.transient_array = [0.0] * 32
        self.call_stack = []

        # Store show origin for coordinate transform (same pattern as Type 1)
        self.show_origin = ctxt.gstate.currentpoint if ctxt and not width_only_mode else None

    def execute(self, charstring_data: bytes) -> float | None:
        """Execute a Type 2 charstring.

        Returns advance width in character space, or None on failure.
        """
        self._execute_bytes(charstring_data)
        return self.advance_width

    def _execute_bytes(self, data: bytes) -> None:
        """Execute charstring byte stream."""
        i = 0
        length = len(data)

        while i < length:
            b0 = data[i]

            if b0 <= 27 or (29 <= b0 <= 31):
                # Operator (bytes 0-27, 29-31; byte 28 is a number)
                if b0 == 12:
                    # Two-byte operator
                    i += 1
                    if i >= length:
                        break
                    b1 = data[i]
                    self._execute_operator_12(b1)
                    i += 1
                elif b0 == 19 or b0 == 20:
                    # hintmask / cntrmask — consume implicit vstem args then read mask bytes
                    self._handle_hint_mask(b0)
                    i += 1
                    # Read ceil((num_h_hints + num_v_hints) / 8) mask bytes
                    n_mask_bytes = (self.num_h_hints + self.num_v_hints + 7) // 8
                    i += n_mask_bytes  # Skip mask bytes
                else:
                    self._execute_operator(b0)
                    i += 1

            elif 32 <= b0 <= 246:
                self.stack.append(float(b0 - 139))
                i += 1

            elif 247 <= b0 <= 250:
                if i + 1 >= length:
                    break
                b1 = data[i + 1]
                self.stack.append(float((b0 - 247) * 256 + b1 + 108))
                i += 2

            elif 251 <= b0 <= 254:
                if i + 1 >= length:
                    break
                b1 = data[i + 1]
                self.stack.append(float(-(b0 - 251) * 256 - b1 - 108))
                i += 2

            elif b0 == 255:
                # 16.16 fixed-point number
                if i + 4 >= length:
                    break
                raw = struct.unpack_from('>i', data, i + 1)[0]
                self.stack.append(raw / 65536.0)
                i += 5

            elif b0 == 28:
                # 3-byte signed integer (byte 28 is a number, not an operator)
                if i + 2 >= length:
                    break
                val = struct.unpack_from('>h', data, i + 1)[0]
                self.stack.append(float(val))
                i += 3

            else:
                i += 1

    # -------------------------------------------------------------------
    # Width handling
    # -------------------------------------------------------------------

    def _check_width(self, expected_args: int) -> None:
        """Check for optional width argument before first stack-clearing operator.

        Type 2 width rule: if stack has one extra argument beyond what the operator
        expects, the bottom element is the width operand.

        Called by each stack-clearing operator with `expected_args` = the number
        of args that operator itself consumes (possibly 0).
        """
        if self.width_parsed:
            return

        self.width_parsed = True

        if len(self.stack) > expected_args:
            # Extra arg at bottom of stack is width
            w = self.stack.pop(0)
            self.advance_width = w + self.nominal_width_x
        else:
            self.advance_width = self.default_width_x

    # -------------------------------------------------------------------
    # Coordinate transforms (same pattern as Type 1 interpreter)
    # -------------------------------------------------------------------

    def _transform_glyph_to_device_space(self, glyph_x: float, glyph_y: float) -> tuple[float, float]:
        """Transform from character space to device space."""
        font_matrix = self.font_dict.val.get(b'FontMatrix')
        if font_matrix and font_matrix.TYPE == ps.T_ARRAY:
            user_x, user_y = _transform_point(font_matrix, glyph_x, glyph_y)
        else:
            user_x, user_y = glyph_x, glyph_y

        device_rel_x, device_rel_y = _transform_delta(self.ctxt.gstate.CTM, user_x, user_y)

        if self.show_origin is not None:
            return device_rel_x + self.show_origin.x, device_rel_y + self.show_origin.y
        return device_rel_x, device_rel_y

    # -------------------------------------------------------------------
    # Path helpers
    # -------------------------------------------------------------------

    def _close_current_subpath(self) -> None:
        """Close the current subpath if it has path elements beyond MoveTo.

        Type 2 implicitly closes subpaths on each moveto and endchar.
        """
        if (self.ctxt.gstate.path and len(self.ctxt.gstate.path[-1]) > 1):
            self.ctxt.gstate.path[-1].append(ps.ClosePath())

    def _do_moveto(self, dx: float, dy: float) -> None:
        """Relative moveto with path building."""
        self.current_point = (self.current_point[0] + dx, self.current_point[1] + dy)

        if self.width_only_mode:
            return

        device_x, device_y = self._transform_glyph_to_device_space(
            self.current_point[0], self.current_point[1])

        # Type 2: each moveto implicitly closes the previous subpath
        self._close_current_subpath()

        # Per PLRM: consecutive movetos replace the previous moveto point
        if (self.ctxt.gstate.path and len(self.ctxt.gstate.path[-1]) == 1
                and isinstance(self.ctxt.gstate.path[-1][0], ps.MoveTo)):
            self.ctxt.gstate.path[-1][0] = ps.MoveTo(ps.Point(device_x, device_y))
        else:
            self.ctxt.gstate.path.append(ps.SubPath())
            self.ctxt.gstate.path[-1].append(ps.MoveTo(ps.Point(device_x, device_y)))

        self.ctxt.gstate.currentpoint = ps.Point(float(device_x), float(device_y))

    def _do_lineto(self, dx: float, dy: float) -> None:
        """Relative lineto with path building."""
        self.current_point = (self.current_point[0] + dx, self.current_point[1] + dy)

        if self.width_only_mode:
            return

        device_x, device_y = self._transform_glyph_to_device_space(
            self.current_point[0], self.current_point[1])

        self.ctxt.gstate.path[-1].append(ps.LineTo(ps.Point(device_x, device_y)))
        self.ctxt.gstate.currentpoint = ps.Point(float(device_x), float(device_y))

    def _do_curveto(self, dx1: float, dy1: float, dx2: float, dy2: float, dx3: float, dy3: float) -> None:
        """Relative curveto with path building."""
        x1 = self.current_point[0] + dx1
        y1 = self.current_point[1] + dy1
        x2 = x1 + dx2
        y2 = y1 + dy2
        x3 = x2 + dx3
        y3 = y2 + dy3
        self.current_point = (x3, y3)

        if self.width_only_mode:
            return

        d_x1, d_y1 = self._transform_glyph_to_device_space(x1, y1)
        d_x2, d_y2 = self._transform_glyph_to_device_space(x2, y2)
        d_x3, d_y3 = self._transform_glyph_to_device_space(x3, y3)

        self.ctxt.gstate.path[-1].append(ps.CurveTo(
            ps.Point(d_x1, d_y1),
            ps.Point(d_x2, d_y2),
            ps.Point(d_x3, d_y3),
        ))
        self.ctxt.gstate.currentpoint = ps.Point(float(d_x3), float(d_y3))

    # -------------------------------------------------------------------
    # Operator dispatch
    # -------------------------------------------------------------------

    def _execute_operator(self, op: int) -> None:
        """Execute a single-byte Type 2 operator."""
        if op == 1:
            self._op_hstem()
        elif op == 3:
            self._op_vstem()
        elif op == 4:
            self._op_vmoveto()
        elif op == 5:
            self._op_rlineto()
        elif op == 6:
            self._op_hlineto()
        elif op == 7:
            self._op_vlineto()
        elif op == 8:
            self._op_rrcurveto()
        elif op == 10:
            self._op_callsubr()
        elif op == 11:
            self._op_return()
        elif op == 14:
            self._op_endchar()
        elif op == 18:
            self._op_hstemhm()
        elif op == 21:
            self._op_rmoveto()
        elif op == 22:
            self._op_hmoveto()
        elif op == 23:
            self._op_vstemhm()
        elif op == 24:
            self._op_rcurveline()
        elif op == 25:
            self._op_rlinecurve()
        elif op == 26:
            self._op_vvcurveto()
        elif op == 27:
            self._op_hhcurveto()
        elif op == 29:
            self._op_callgsubr()
        elif op == 30:
            self._op_vhcurveto()
        elif op == 31:
            self._op_hvcurveto()
        # else: unknown operator — ignore

    def _execute_operator_12(self, sub_op: int) -> None:
        """Execute a two-byte (12, sub_op) operator."""
        if sub_op == 0:
            pass  # dotsection — deprecated, no-op
        elif sub_op == 3:
            self._op12_and()
        elif sub_op == 4:
            self._op12_or()
        elif sub_op == 5:
            self._op12_not()
        elif sub_op == 9:
            self._op12_abs()
        elif sub_op == 10:
            self._op12_add()
        elif sub_op == 11:
            self._op12_sub()
        elif sub_op == 12:
            self._op12_div()
        elif sub_op == 14:
            self._op12_neg()
        elif sub_op == 15:
            self._op12_eq()
        elif sub_op == 18:
            self._op12_drop()
        elif sub_op == 20:
            self._op12_put()
        elif sub_op == 21:
            self._op12_get()
        elif sub_op == 22:
            self._op12_ifelse()
        elif sub_op == 23:
            self._op12_random()
        elif sub_op == 24:
            self._op12_mul()
        elif sub_op == 26:
            self._op12_sqrt()
        elif sub_op == 27:
            self._op12_dup()
        elif sub_op == 28:
            self._op12_exch()
        elif sub_op == 29:
            self._op12_index()
        elif sub_op == 30:
            self._op12_roll()
        elif sub_op == 34:
            self._op12_hflex()
        elif sub_op == 35:
            self._op12_flex()
        elif sub_op == 36:
            self._op12_hflex1()
        elif sub_op == 37:
            self._op12_flex1()
        # else: unknown 12,N — ignore

    # -------------------------------------------------------------------
    # Hint operators (stack-clearing, count stems, no path output)
    # -------------------------------------------------------------------

    def _op_hstem(self) -> None:
        """hstem: horizontal stem hints."""
        n_pairs = len(self.stack) // 2
        self._check_width(n_pairs * 2)
        self.num_h_hints += len(self.stack) // 2
        self.stack.clear()

    def _op_vstem(self) -> None:
        """vstem: vertical stem hints."""
        n_pairs = len(self.stack) // 2
        self._check_width(n_pairs * 2)
        self.num_v_hints += len(self.stack) // 2
        self.stack.clear()

    def _op_hstemhm(self) -> None:
        """hstemhm: horizontal stem hints (hintmask may follow)."""
        n_pairs = len(self.stack) // 2
        self._check_width(n_pairs * 2)
        self.num_h_hints += len(self.stack) // 2
        self.stack.clear()

    def _op_vstemhm(self) -> None:
        """vstemhm: vertical stem hints (hintmask may follow)."""
        n_pairs = len(self.stack) // 2
        self._check_width(n_pairs * 2)
        self.num_v_hints += len(self.stack) // 2
        self.stack.clear()

    def _handle_hint_mask(self, op: int) -> None:
        """Handle hintmask (19) / cntrmask (20) — consume implicit vstems first."""
        # If stack has operands, they are implicit vstem hints
        if self.stack:
            n_pairs = len(self.stack) // 2
            self._check_width(n_pairs * 2)
            self.num_v_hints += len(self.stack) // 2
            self.stack.clear()
        elif not self.width_parsed:
            self._check_width(0)

    # -------------------------------------------------------------------
    # Path construction operators
    # -------------------------------------------------------------------

    def _op_rmoveto(self) -> None:
        """rmoveto: dx dy"""
        self._check_width(2)
        if len(self.stack) < 2:
            self.stack.clear()
            return
        dy = self.stack.pop()
        dx = self.stack.pop()
        self.stack.clear()
        self._do_moveto(dx, dy)

    def _op_hmoveto(self) -> None:
        """hmoveto: dx"""
        self._check_width(1)
        if len(self.stack) < 1:
            self.stack.clear()
            return
        dx = self.stack.pop()
        self.stack.clear()
        self._do_moveto(dx, 0.0)

    def _op_vmoveto(self) -> None:
        """vmoveto: dy"""
        self._check_width(1)
        if len(self.stack) < 1:
            self.stack.clear()
            return
        dy = self.stack.pop()
        self.stack.clear()
        self._do_moveto(0.0, dy)

    def _op_rlineto(self) -> None:
        """rlineto: {dx dy}+ — multiple relative lines."""
        args = self.stack[:]
        self.stack.clear()
        i = 0
        while i + 1 < len(args):
            self._do_lineto(args[i], args[i + 1])
            i += 2

    def _op_hlineto(self) -> None:
        """hlineto: alternating horizontal/vertical lines.

        If odd arg count starts with dx; if even starts with dx.
        Pattern: dx1 dy2 dx3 dy4 ... (alternating h/v)
        """
        args = self.stack[:]
        self.stack.clear()
        horizontal = True
        for val in args:
            if horizontal:
                self._do_lineto(val, 0.0)
            else:
                self._do_lineto(0.0, val)
            horizontal = not horizontal

    def _op_vlineto(self) -> None:
        """vlineto: alternating vertical/horizontal lines."""
        args = self.stack[:]
        self.stack.clear()
        vertical = True
        for val in args:
            if vertical:
                self._do_lineto(0.0, val)
            else:
                self._do_lineto(val, 0.0)
            vertical = not vertical

    def _op_rrcurveto(self) -> None:
        """rrcurveto: {dx1 dy1 dx2 dy2 dx3 dy3}+ — multiple curves."""
        args = self.stack[:]
        self.stack.clear()
        i = 0
        while i + 5 < len(args):
            self._do_curveto(args[i], args[i + 1], args[i + 2],
                             args[i + 3], args[i + 4], args[i + 5])
            i += 6

    def _op_hhcurveto(self) -> None:
        """hhcurveto: dy1? {dxa dxb dyb dxc}+

        All curves have dy1=0 and dyc=0 (horizontal tangents at start/end),
        except optionally dy1 at the very start.
        """
        args = self.stack[:]
        self.stack.clear()
        i = 0
        dy1_extra = 0.0
        if len(args) % 4 != 0:
            # Odd number: first arg is dy1 for the first curve
            dy1_extra = args[0]
            i = 1

        while i + 3 < len(args):
            dxa = args[i]
            dxb = args[i + 1]
            dyb = args[i + 2]
            dxc = args[i + 3]
            self._do_curveto(dxa, dy1_extra, dxb, dyb, dxc, 0.0)
            dy1_extra = 0.0
            i += 4

    def _op_vvcurveto(self) -> None:
        """vvcurveto: dx1? {dya dxb dyb dyc}+

        All curves have dx1=0 and dxc=0 (vertical tangents at start/end),
        except optionally dx1 at the very start.
        """
        args = self.stack[:]
        self.stack.clear()
        i = 0
        dx1_extra = 0.0
        if len(args) % 4 != 0:
            dx1_extra = args[0]
            i = 1

        while i + 3 < len(args):
            dya = args[i]
            dxb = args[i + 1]
            dyb = args[i + 2]
            dyc = args[i + 3]
            self._do_curveto(dx1_extra, dya, dxb, dyb, 0.0, dyc)
            dx1_extra = 0.0
            i += 4

    def _op_hvcurveto(self) -> None:
        """hvcurveto: alternating h-start/v-end and v-start/h-end curves.

        First curve starts horizontal (dy1=0), ends vertical (dxf=0).
        Optional final tangent adjustment on last curve.
        """
        args = self.stack[:]
        self.stack.clear()
        self._alternating_curves(args, start_horizontal=True)

    def _op_vhcurveto(self) -> None:
        """vhcurveto: alternating v-start/h-end and h-start/v-end curves."""
        args = self.stack[:]
        self.stack.clear()
        self._alternating_curves(args, start_horizontal=False)

    def _alternating_curves(self, args: list[float], start_horizontal: bool) -> None:
        """Shared logic for hvcurveto / vhcurveto."""
        i = 0
        phase = start_horizontal
        n = len(args)

        while i + 3 < n:
            remaining = n - i
            is_last = (remaining < 9)  # Last curve group can have optional extra arg

            if phase:
                # H-start curve: dx1 dx2 dy2 dy3 [dxf]
                dx1 = args[i]
                dx2 = args[i + 1]
                dy2 = args[i + 2]
                dy3 = args[i + 3]
                dxf = args[i + 4] if is_last and remaining == 5 else 0.0
                self._do_curveto(dx1, 0.0, dx2, dy2, dxf, dy3)
                i += 5 if (is_last and remaining == 5) else 4
            else:
                # V-start curve: dy1 dx2 dy2 dx3 [dyf]
                dy1 = args[i]
                dx2 = args[i + 1]
                dy2 = args[i + 2]
                dx3 = args[i + 3]
                dyf = args[i + 4] if is_last and remaining == 5 else 0.0
                self._do_curveto(0.0, dy1, dx2, dy2, dx3, dyf)
                i += 5 if (is_last and remaining == 5) else 4

            phase = not phase

    def _op_rcurveline(self) -> None:
        """rcurveline: {dx1 dy1 dx2 dy2 dx3 dy3}+ dxl dyl — curves then one line."""
        args = self.stack[:]
        self.stack.clear()
        if len(args) < 2:
            return
        # Last two args are the line
        i = 0
        curve_end = len(args) - 2
        while i + 5 < curve_end + 1:
            self._do_curveto(args[i], args[i + 1], args[i + 2],
                             args[i + 3], args[i + 4], args[i + 5])
            i += 6
        # Final line
        if i + 1 < len(args):
            self._do_lineto(args[i], args[i + 1])

    def _op_rlinecurve(self) -> None:
        """rlinecurve: {dx dy}+ dx1 dy1 dx2 dy2 dx3 dy3 — lines then one curve."""
        args = self.stack[:]
        self.stack.clear()
        if len(args) < 6:
            return
        # Last 6 args are the curve
        curve_start = len(args) - 6
        i = 0
        while i + 1 <= curve_start:
            self._do_lineto(args[i], args[i + 1])
            i += 2
        self._do_curveto(args[curve_start], args[curve_start + 1],
                         args[curve_start + 2], args[curve_start + 3],
                         args[curve_start + 4], args[curve_start + 5])

    # -------------------------------------------------------------------
    # endchar
    # -------------------------------------------------------------------

    def _op_endchar(self) -> None:
        """endchar: finish character.

        If 4 extra args on stack: deprecated seac (accent composite).
        Otherwise: render glyph path.
        """
        # Check for seac (deprecated: adx ady bchar achar endchar)
        if len(self.stack) >= 4 and not self.width_parsed:
            self._check_width(4)
        elif not self.width_parsed:
            self._check_width(0)

        # For now skip seac handling — just render what we have
        self.stack.clear()

        if self.width_only_mode:
            return

        # Skip rendering in charpath mode
        if hasattr(self.ctxt, '_charpath_mode') and self.ctxt._charpath_mode:
            # Still close the final subpath for charpath
            self._close_current_subpath()
            return

        # Type 2: endchar implicitly closes the final subpath
        self._close_current_subpath()

        # Emit path to display list (same pattern as Type 1 endchar)
        if not hasattr(self.ctxt, 'display_list_builder'):
            self.ctxt.display_list_builder = DisplayListBuilder(self.ctxt.display_list)

        self.ctxt.display_list_builder.add_graphics_operation(self.ctxt, self.ctxt.gstate.path)

        paint_type = self.font_dict.val.get(b'PaintType', ps.Int(0)).val

        if paint_type == 2:
            device_color = color_space.convert_to_device_color(
                self.ctxt, self.ctxt.gstate.color, self.ctxt.gstate.color_space)
            stroke_op = ps.Stroke(device_color, self.ctxt.gstate)
            self.ctxt.display_list_builder.add_graphics_operation(self.ctxt, stroke_op)
        else:
            device_color = color_space.convert_to_device_color(
                self.ctxt, self.ctxt.gstate.color, self.ctxt.gstate.color_space)
            fill_op = ps.Fill(device_color, ps.WINDING_NON_ZERO)
            self.ctxt.display_list_builder.add_graphics_operation(self.ctxt, fill_op)

        if not (hasattr(self.ctxt, '_charpath_mode') and self.ctxt._charpath_mode):
            self.ctxt.gstate.path = ps.Path()

    # -------------------------------------------------------------------
    # Subroutine operators
    # -------------------------------------------------------------------

    def _op_callsubr(self) -> None:
        """callsubr: pop index, apply bias, execute local subroutine."""
        if not self.stack:
            return
        idx = int(self.stack.pop())
        bias = _subr_bias(len(self.local_subrs))
        biased = idx + bias
        if 0 <= biased < len(self.local_subrs):
            self.call_stack.append(None)  # Placeholder for return address
            self._execute_bytes(self.local_subrs[biased])
            if self.call_stack:
                self.call_stack.pop()

    def _op_callgsubr(self) -> None:
        """callgsubr: pop index, apply bias, execute global subroutine."""
        if not self.stack:
            return
        idx = int(self.stack.pop())
        bias = _subr_bias(len(self.global_subrs))
        biased = idx + bias
        if 0 <= biased < len(self.global_subrs):
            self.call_stack.append(None)
            self._execute_bytes(self.global_subrs[biased])
            if self.call_stack:
                self.call_stack.pop()

    def _op_return(self) -> None:
        """return: return from subroutine (handled by call stack pop in callsubr/callgsubr)."""
        pass  # Actual return is handled by the call stack in _execute_bytes

    # -------------------------------------------------------------------
    # Flex operators (12, 34-37)
    # -------------------------------------------------------------------

    def _op12_hflex(self) -> None:
        """hflex: 7 args — dx1 dx2 dy2 dx3 dx4 dx5 dx6"""
        if len(self.stack) < 7:
            self.stack.clear()
            return
        dx1 = self.stack[0]
        dx2 = self.stack[1]
        dy2 = self.stack[2]
        dx3 = self.stack[3]
        dx4 = self.stack[4]
        dx5 = self.stack[5]
        dx6 = self.stack[6]
        self.stack.clear()
        # First curve
        self._do_curveto(dx1, 0.0, dx2, dy2, dx3, 0.0)
        # Second curve
        self._do_curveto(dx4, 0.0, dx5, -dy2, dx6, 0.0)

    def _op12_flex(self) -> None:
        """flex: 13 args — dx1 dy1 dx2 dy2 dx3 dy3 dx4 dy4 dx5 dy5 dx6 dy6 fd"""
        if len(self.stack) < 13:
            self.stack.clear()
            return
        dx1, dy1 = self.stack[0], self.stack[1]
        dx2, dy2 = self.stack[2], self.stack[3]
        dx3, dy3 = self.stack[4], self.stack[5]
        dx4, dy4 = self.stack[6], self.stack[7]
        dx5, dy5 = self.stack[8], self.stack[9]
        dx6, dy6 = self.stack[10], self.stack[11]
        # fd = self.stack[12]  # Flex depth — not used for rendering
        self.stack.clear()
        self._do_curveto(dx1, dy1, dx2, dy2, dx3, dy3)
        self._do_curveto(dx4, dy4, dx5, dy5, dx6, dy6)

    def _op12_hflex1(self) -> None:
        """hflex1: 9 args — dx1 dy1 dx2 dy2 dx3 dx4 dx5 dy5 dx6"""
        if len(self.stack) < 9:
            self.stack.clear()
            return
        dx1, dy1 = self.stack[0], self.stack[1]
        dx2, dy2 = self.stack[2], self.stack[3]
        dx3 = self.stack[4]
        dx4 = self.stack[5]
        dx5, dy5 = self.stack[6], self.stack[7]
        dx6 = self.stack[8]
        self.stack.clear()
        # First curve
        self._do_curveto(dx1, dy1, dx2, dy2, dx3, 0.0)
        # Second curve (dy6 = -(dy1+dy2+dy5))
        self._do_curveto(dx4, 0.0, dx5, dy5, dx6, -(dy1 + dy2 + dy5))

    def _op12_flex1(self) -> None:
        """flex1: 11 args — dx1 dy1 dx2 dy2 dx3 dy3 dx4 dy4 dx5 dy5 d6

        The last arg d6 is either dx6 or dy6 depending on cumulative direction.
        """
        if len(self.stack) < 11:
            self.stack.clear()
            return
        dx1, dy1 = self.stack[0], self.stack[1]
        dx2, dy2 = self.stack[2], self.stack[3]
        dx3, dy3 = self.stack[4], self.stack[5]
        dx4, dy4 = self.stack[6], self.stack[7]
        dx5, dy5 = self.stack[8], self.stack[9]
        d6 = self.stack[10]
        self.stack.clear()

        # Determine whether d6 is dx or dy based on cumulative deltas
        sum_dx = dx1 + dx2 + dx3 + dx4 + dx5
        sum_dy = dy1 + dy2 + dy3 + dy4 + dy5

        if abs(sum_dx) > abs(sum_dy):
            dx6 = d6
            dy6 = -sum_dy
        else:
            dx6 = -sum_dx
            dy6 = d6

        self._do_curveto(dx1, dy1, dx2, dy2, dx3, dy3)
        self._do_curveto(dx4, dy4, dx5, dy5, dx6, dy6)

    # -------------------------------------------------------------------
    # Arithmetic operators (12, N)
    # -------------------------------------------------------------------

    def _op12_abs(self) -> None:
        if self.stack:
            self.stack[-1] = abs(self.stack[-1])

    def _op12_add(self) -> None:
        if len(self.stack) >= 2:
            b = self.stack.pop()
            a = self.stack.pop()
            self.stack.append(a + b)

    def _op12_sub(self) -> None:
        if len(self.stack) >= 2:
            b = self.stack.pop()
            a = self.stack.pop()
            self.stack.append(a - b)

    def _op12_div(self) -> None:
        if len(self.stack) >= 2:
            b = self.stack.pop()
            a = self.stack.pop()
            self.stack.append(a / b if b != 0 else 0.0)

    def _op12_neg(self) -> None:
        if self.stack:
            self.stack[-1] = -self.stack[-1]

    def _op12_mul(self) -> None:
        if len(self.stack) >= 2:
            b = self.stack.pop()
            a = self.stack.pop()
            self.stack.append(a * b)

    def _op12_sqrt(self) -> None:
        if self.stack:
            self.stack[-1] = math.sqrt(abs(self.stack[-1]))

    def _op12_random(self) -> None:
        # Return a pseudo-random number (spec says > 0, <= 1)
        self.stack.append(1.0)  # Simplification

    # -------------------------------------------------------------------
    # Logic operators (12, N)
    # -------------------------------------------------------------------

    def _op12_and(self) -> None:
        if len(self.stack) >= 2:
            b = self.stack.pop()
            a = self.stack.pop()
            self.stack.append(1.0 if (a != 0 and b != 0) else 0.0)

    def _op12_or(self) -> None:
        if len(self.stack) >= 2:
            b = self.stack.pop()
            a = self.stack.pop()
            self.stack.append(1.0 if (a != 0 or b != 0) else 0.0)

    def _op12_not(self) -> None:
        if self.stack:
            a = self.stack.pop()
            self.stack.append(1.0 if a == 0 else 0.0)

    def _op12_eq(self) -> None:
        if len(self.stack) >= 2:
            b = self.stack.pop()
            a = self.stack.pop()
            self.stack.append(1.0 if a == b else 0.0)

    def _op12_ifelse(self) -> None:
        """ifelse: s1 s2 v1 v2 → s1 if v1<=v2, else s2"""
        if len(self.stack) >= 4:
            v2 = self.stack.pop()
            v1 = self.stack.pop()
            s2 = self.stack.pop()
            s1 = self.stack.pop()
            self.stack.append(s1 if v1 <= v2 else s2)

    # -------------------------------------------------------------------
    # Stack manipulation operators (12, N)
    # -------------------------------------------------------------------

    def _op12_drop(self) -> None:
        if self.stack:
            self.stack.pop()

    def _op12_dup(self) -> None:
        if self.stack:
            self.stack.append(self.stack[-1])

    def _op12_exch(self) -> None:
        if len(self.stack) >= 2:
            self.stack[-1], self.stack[-2] = self.stack[-2], self.stack[-1]

    def _op12_index(self) -> None:
        """index: i → stack[-(i+1)] (copy ith element from top)."""
        if self.stack:
            idx = int(self.stack.pop())
            if idx < 0:
                idx = 0
            if idx < len(self.stack):
                self.stack.append(self.stack[-(idx + 1)])

    def _op12_roll(self) -> None:
        """roll: n j — roll top n elements by j positions."""
        if len(self.stack) >= 2:
            j = int(self.stack.pop())
            n = int(self.stack.pop())
            if n > 0 and n <= len(self.stack):
                subset = self.stack[-n:]
                j = j % n
                rolled = subset[-j:] + subset[:-j]
                self.stack[-n:] = rolled

    # -------------------------------------------------------------------
    # Storage operators (12, N) — transient array
    # -------------------------------------------------------------------

    def _op12_put(self) -> None:
        """put: val i → transient[i] = val"""
        if len(self.stack) >= 2:
            i = int(self.stack.pop())
            val = self.stack.pop()
            if 0 <= i < 32:
                self.transient_array[i] = val

    def _op12_get(self) -> None:
        """get: i → transient[i]"""
        if self.stack:
            i = int(self.stack.pop())
            if 0 <= i < 32:
                self.stack.append(self.transient_array[i])
            else:
                self.stack.append(0.0)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def type2_charstring_to_width(charstring_data: bytes, ctxt: ps.Context, font_dict: ps.Dict,
                               default_width_x: float, nominal_width_x: float,
                               local_subrs: list[bytes], global_subrs: list[bytes],
                               width_only: bool = False) -> float | None:
    """Execute a Type 2 charstring and return width in user space.

    This is the main entry point for CFF font rendering, mirroring
    charstring_to_width() for Type 1 fonts.

    Args:
        charstring_data: Raw charstring bytes (no encryption)
        ctxt: PostScript context for graphics state access
        font_dict: Font dictionary containing FontMatrix
        default_width_x: From CFF Private DICT (default 0)
        nominal_width_x: From CFF Private DICT (default 0)
        local_subrs: List of local subroutine bytes
        global_subrs: List of global subroutine bytes
        width_only: If True, skip path operations (for stringwidth)

    Returns:
        Character advance width in user space, or None if failed
    """
    interpreter = Type2CharStringInterpreter(
        ctxt, font_dict, default_width_x, nominal_width_x,
        local_subrs, global_subrs, width_only_mode=width_only)

    raw_width = interpreter.execute(charstring_data)

    if raw_width is not None:
        # Apply FontMatrix to transform character space → user space
        font_matrix = font_dict.val.get(b'FontMatrix')
        if font_matrix and font_matrix.TYPE == ps.T_ARRAY:
            font_matrix_values = [m.val for m in font_matrix.val]
            return raw_width * font_matrix_values[0]
    return None
