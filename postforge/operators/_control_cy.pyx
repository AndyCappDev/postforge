# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Cython-compiled version of exec_exec for performance optimization.

This module contains only the exec_exec function, compiled with Cython to
eliminate CPython bytecode interpreter overhead on the hot loop. All PSObject
classes remain pure Python. PostForge continues to work without Cython —
this compiled module is an optional accelerator.

IMPORTANT: This is a Cython copy of exec_exec from postforge/operators/control.py.
If the Python version changes, this file MUST be updated to match and rebuilt
with ./build_cython.sh. This version includes additional optimizations
(inlined dict lookup, C-typed locals) so the two are not structurally
identical, but they must remain functionally equivalent.
"""

import copy

from postforge.operators import dict as ps_dict
from postforge.core import error as ps_error
from postforge.core import tokenizer as ps_token
from postforge.operators import matrix as ps_matrix
from postforge.core import types as ps


# C-level type constants (avoid Python module attribute lookups in the loop)
cdef int C_T_ARRAY = 0
cdef int C_T_BOOL = 1
cdef int C_T_DICT = 2
cdef int C_T_FILE = 3
cdef int C_T_INT = 6
cdef int C_T_MARK = 7
cdef int C_T_NAME = 8
cdef int C_T_NULL = 9
cdef int C_T_OPERATOR = 10
cdef int C_T_PACKED_ARRAY = 11
cdef int C_T_REAL = 12
cdef int C_T_SAVE = 13
cdef int C_T_STRING = 14
cdef int C_T_STOPPED = 15
cdef int C_T_LOOP = 16
cdef int C_T_HARD_RETURN = 17

# C-level attribute constants
cdef int C_ATTRIB_LIT = 0
cdef int C_ATTRIB_EXEC = 1

# C-level loop type constants
cdef int C_LT_LOOP = 6
cdef int C_LT_REPEAT = 8
cdef int C_LT_FOR = 3
cdef int C_LT_FORALL = 4
cdef int C_LT_FILENAMEFORALL = 2
cdef int C_LT_PATHFORALL = 7
cdef int C_LT_CSHOW = 1
cdef int C_LT_KSHOW = 5


def exec_exec(ctxt, o_stack, e_stack):
    """
    Cython-compiled main PostScript execution engine.

    See control.py exec_exec for full documentation. This is a drop-in
    replacement that eliminates CPython bytecode dispatch overhead.
    """
    cdef int top_type, top_attrib
    cdef int obj_type
    cdef int event_counter = 0

    # Cache frequently accessed Python objects as local variables
    event_loop_callback = ctxt.event_loop_callback
    execution_history_enabled = ctxt.execution_history_enabled

    while e_stack:
        # Cache top-of-stack: one Python attr access -> C int
        top = e_stack[-1]
        top_type = top.TYPE
        top_attrib = top.attrib

        # Periodic event loop callback for GUI responsiveness
        if event_loop_callback is not None:
            event_counter += 1
            if event_counter >= 10000:
                event_counter = 0
                event_loop_callback()

        # Record every object being processed by the execution engine
        if execution_history_enabled and not ctxt.execution_history_paused:
            if not (top_type == C_T_FILE or top_type == C_T_STRING):
                ctxt.record_execution(top.__copy__(), None)

        # EXECUTION PATH 1: LITERAL OBJECTS
        # C int comparisons instead of frozenset.__contains__
        if (top_type == C_T_INT or top_type == C_T_REAL or top_type == C_T_BOOL
                or top_type == C_T_NULL or top_type == C_T_MARK
                or top_attrib == C_ATTRIB_LIT):
            o_stack.append(top)
            e_stack.pop()
            continue

        # EXECUTION PATH 2: OPERATOR OBJECTS
        elif top_type == C_T_OPERATOR:
            e_stack.pop()
            top.val(ctxt, o_stack)
            continue

        # EXECUTION PATH 3: NAME OBJECTS
        # Inlined lookup for Name objects (the hottest path in exec_exec).
        # For Name, create_key() returns the Name object itself, so key == top.
        elif top_type == C_T_NAME:
            # Fast-path: check operator table for unshadowed systemdict operators
            op_table = ctxt._operator_table
            if op_table is not None and top in op_table:
                d_stack = ctxt.d_stack
                d_len = len(d_stack)
                shadowed = 0
                for i in range(d_len - 1, 0, -1):
                    if top in d_stack[i].val:
                        shadowed = 1
                        break
                if not shadowed:
                    e_stack[-1] = op_table[top]
                    continue

            # Normal dict stack walk
            d_stack = ctxt.d_stack
            obj = None
            for i in range(len(d_stack) - 1, -1, -1):
                if d_stack[i].access() < 2:  # ACCESS_READ_ONLY = 2
                    continue
                obj = d_stack[i].val.get(top, None)
                if obj is not None:
                    break

            if obj is None:
                ps_error.e(ctxt, ps_error.UNDEFINED, top.val.decode("ascii"))
                continue
            obj_type = obj.TYPE
            if obj_type == C_T_OPERATOR:
                e_stack[-1] = obj
            else:
                e_stack[-1] = obj.__copy__()
            continue

        # EXECUTION PATH 4: TOKENIZABLE OBJECTS (Run, File, String)
        elif top_type == C_T_FILE or top_type == C_T_STRING:
            success, er_name, command, do_exec = ps_token.__token(ctxt, e_stack)
            if not success:
                # pop the false off the operand stack
                o_stack.pop()
                ps_error.e(ctxt, er_name, command)
                continue

            success = o_stack[-1].val

            # pop the true/false object off the operand stack
            o_stack.pop()

            # if return value is false - pop the stream object off the execution stack
            if not success:
                e_stack.pop()
            else:
                if o_stack[-1].TYPE == C_T_NAME and o_stack[-1].val == b"breaki":
                    e_stack.append(o_stack.pop())
                    continue
                else:
                    if do_exec:
                        # Inline the ps_exec call path for the common case
                        _ps_exec_from_token(ctxt, o_stack, e_stack)
            continue

        # EXECUTION PATH 5: EXECUTABLE ARRAYS (procedures)
        elif top_type == C_T_ARRAY or top_type == C_T_PACKED_ARRAY:

            if not top.length:
                # an empty executable array
                e_stack.pop()
                continue

            if top.length == 1:
                obj = top.val[top.start]
                obj_type = obj.TYPE

                if (obj_type == C_T_ARRAY or obj_type == C_T_PACKED_ARRAY) and obj.attrib == C_ATTRIB_EXEC:
                    # Copy executable arrays to prevent cvx/cvlit from corrupting
                    # the original procedure (same protection as literal objects).
                    o_stack.append(obj.__copy__())
                    top.length -= 1
                    top.start += 1
                elif obj.attrib == C_ATTRIB_LIT:
                    if (obj_type == C_T_INT or obj_type == C_T_REAL or obj_type == C_T_BOOL
                            or obj_type == C_T_NULL or obj_type == C_T_MARK):
                        o_stack.append(obj)
                    else:
                        o_stack.append(obj.__copy__())
                    top.length -= 1
                    top.start += 1
                else:
                    e_stack[-1] = top.val[top.start]
            else:
                obj = top.val[top.start]
                obj_type = obj.TYPE

                if (obj_type == C_T_ARRAY or obj_type == C_T_PACKED_ARRAY) and obj.attrib == C_ATTRIB_EXEC:
                    # Copy executable arrays to prevent cvx/cvlit from corrupting
                    # the original procedure (same protection as literal objects).
                    o_stack.append(obj.__copy__())
                elif obj.attrib == C_ATTRIB_LIT:
                    if (obj_type == C_T_INT or obj_type == C_T_REAL or obj_type == C_T_BOOL
                            or obj_type == C_T_NULL or obj_type == C_T_MARK):
                        o_stack.append(obj)
                    else:
                        o_stack.append(obj.__copy__())
                else:
                    e_stack.append(obj)

                top.length -= 1
                top.start += 1
            continue

        elif top_type == C_T_STOPPED:
            # this stopped context was not stopped
            # push false onto the operand stack
            ctxt.o_stack.append(ps.Bool(False))
            ctxt.e_stack.pop()
            continue

        elif top_type == C_T_LOOP:
            _handle_loop(ctxt, o_stack, e_stack, top)
            continue

        elif top_type == C_T_HARD_RETURN:
            e_stack.pop()
            return


cdef _handle_loop(ctxt, o_stack, e_stack, top):
    """Handle all loop types in exec_exec."""
    cdef int loop_type = top.val

    if loop_type == C_LT_LOOP:
        e_stack.append(top.proc.__copy__())
        return

    elif loop_type == C_LT_REPEAT:
        proc = top.proc
        top.limit -= 1
        if not top.limit:
            e_stack[-1] = proc.__copy__()
        else:
            e_stack.append(proc.__copy__())
        return

    elif loop_type == C_LT_FOR:
        if top.increment >= 0:
            if top.control <= top.limit:
                if type(top.control) == int:
                    ctxt.o_stack.append(ps.Int(top.control))
                else:
                    ctxt.o_stack.append(ps.Real(top.control))
                top.control += top.increment
                e_stack.append(top.proc.__copy__())
            else:
                e_stack.pop()
        else:
            if top.control >= top.limit:
                if type(top.control) == int:
                    ctxt.o_stack.append(ps.Int(top.control))
                else:
                    ctxt.o_stack.append(ps.Real(top.control))
                top.control += top.increment
                e_stack.append(top.proc.__copy__())
            else:
                e_stack.pop()
        return

    elif loop_type == C_LT_FORALL:
        _handle_forall(ctxt, o_stack, e_stack, top)
        return

    elif loop_type == C_LT_FILENAMEFORALL:
        try:
            fname = top.generator.__next__()
            if len(fname) > top.scratch.length:
                ps_error.e(ctxt, ps_error.RANGECHECK, "filenameforall")
                return
            substring = top.scratch.__copy__()
            dst = (
                ps.global_resources.global_strings
                if top.scratch.is_global
                else ctxt.local_strings
            )
            dst[
                substring.offset
                + substring.start : substring.offset
                + substring.start
                + len(fname)
            ] = fname
            substring.length = len(fname)

            o_stack.append(substring)
            e_stack.append(top.proc.__copy__())
        except (StopIteration, IndexError, AttributeError, TypeError, ValueError):
            e_stack.pop()
        return

    elif loop_type == C_LT_PATHFORALL:
        _handle_pathforall(ctxt, o_stack, e_stack, top)
        return

    elif loop_type == C_LT_CSHOW:
        _handle_cshow(ctxt, o_stack, e_stack, top)
        return

    elif loop_type == C_LT_KSHOW:
        _handle_kshow(ctxt, o_stack, e_stack, top)
        return


cdef _handle_kshow(ctxt, o_stack, e_stack, top):
    """Handle kshow loop in exec_exec.
    PLRM: 'When proc completes execution, the value of currentfont is restored.'
    """
    cdef int char_code
    cdef int next_char_code
    cdef int font_type = 1

    if top.obj.length:
        # Restore font saved before proc (PLRM requirement)
        if hasattr(top, '_saved_font') and top._saved_font is not None:
            ctxt.gstate.font = top._saved_font

        strings = (
            ps.global_resources.global_strings
            if top.obj.is_global
            else ps.contexts[top.obj.ctxt_id].local_strings
        )

        char_code = strings[top.obj.offset + top.obj.start]
        top.obj.start += 1
        top.obj.length -= 1

        # Render the current glyph
        current_font = ctxt.gstate.font
        if current_font is not None:
            ft_obj = current_font.val.get(b'FontType')
            if ft_obj is not None and ft_obj.TYPE in ps.NUMERIC_TYPES:
                font_type = ft_obj.val

        from postforge.operators.show_variants import _render_and_advance_single_glyph
        _render_and_advance_single_glyph(ctxt, current_font, char_code, font_type)

        # If there's a next character, push both char codes and call proc
        if top.obj.length:
            next_char_code = strings[top.obj.offset + top.obj.start]
            o_stack.append(ps.Int(char_code))
            o_stack.append(ps.Int(next_char_code))
            # Save font before proc (restored on next iteration)
            top._saved_font = ctxt.gstate.font
            e_stack.append(top.proc.__copy__())
    else:
        # Restore font on final exit too
        if hasattr(top, '_saved_font') and top._saved_font is not None:
            ctxt.gstate.font = top._saved_font
        e_stack.pop()


cdef _handle_cshow(ctxt, o_stack, e_stack, top):
    """Handle cshow loop in exec_exec."""
    cdef int char_code
    cdef double wx = 0.0
    cdef double wy = 0.0
    cdef int font_type = 1
    cdef int byte_width = 1
    cdef int bw
    if top.obj.length:
        strings = (
            ps.global_resources.global_strings
            if top.obj.is_global
            else ps.contexts[top.obj.ctxt_id].local_strings
        )

        # Check if current font is Type 0 (composite) for multi-byte decoding
        current_font = ctxt.gstate.font
        if current_font is not None:
            ft_obj = current_font.val.get(b'FontType')
            if ft_obj is not None and ft_obj.TYPE in ps.NUMERIC_TYPES:
                font_type = ft_obj.val

        if font_type == 0:
            # Type 0: decode multi-byte character from CMap codespace
            cmap_dict = current_font.val.get(b'CMap')
            byte_width = 1
            if cmap_dict is not None and cmap_dict.TYPE == C_T_DICT:
                codespace = cmap_dict.val.get(b'CodeSpaceRange')
                if codespace is not None and codespace.TYPE in ps.ARRAY_TYPES and len(codespace.val) >= 2:
                    lo = codespace.val[0]
                    if lo.TYPE == ps.T_STRING:
                        lo_bytes = lo.byte_string()
                        if isinstance(lo_bytes, str):
                            lo_bytes = lo_bytes.encode('latin-1')
                        byte_width = len(lo_bytes)

            if top.obj.length >= byte_width:
                full_char_code = 0
                for bw in range(byte_width):
                    full_char_code = (full_char_code << 8) | strings[top.obj.offset + top.obj.start]
                    top.obj.start += 1
                    top.obj.length -= 1
                char_code = full_char_code & 0xFF
                ctxt._cshow_pending_cid = full_char_code
            else:
                e_stack.pop()
                return
        else:
            char_code = strings[top.obj.offset + top.obj.start]
            top.obj.start += 1
            top.obj.length -= 1

        # Get character width from current font (only for non-Type 0)
        if current_font is not None and font_type != 0:
            try:
                encoding = current_font.val.get(b'Encoding')
                if encoding and encoding.TYPE in ps.ARRAY_TYPES:
                    glyph_name_obj = encoding.val[encoding.start + char_code]
                    glyph_name = glyph_name_obj.val if hasattr(glyph_name_obj, 'val') else b'.notdef'
                else:
                    glyph_name = b'.notdef'
                metrics = current_font.val.get(b'Metrics')
                # Try Metrics dict first (PLRM 5.9.2)
                # Check int char code (DVIPS) then glyph name (PLRM standard)
                w = None
                if metrics and metrics.TYPE == C_T_DICT:
                    w = metrics.val.get(char_code)
                    if w is None:
                        w = metrics.val.get(glyph_name)
                if w is not None and hasattr(w, 'TYPE'):
                    if w.TYPE in ps.NUMERIC_TYPES:
                        mw = float(w.val)
                    elif w.TYPE in ps.ARRAY_TYPES and len(w.val) >= 2:
                        mw = float(w.val[0].val) if w.val[0].TYPE in ps.NUMERIC_TYPES else None
                    else:
                        mw = None
                    if mw is not None:
                        # Convert character space to user space via FontMatrix[0]
                        fm = current_font.val.get(b'FontMatrix')
                        if fm and fm.TYPE in ps.ARRAY_TYPES and fm.val:
                            wx = mw * float(fm.val[0].val)
                        else:
                            wx = mw * 0.001
            except Exception:
                pass

        # PLRM: push charcode wx wy (charcode deepest, wy on top)
        o_stack.append(ps.Int(char_code))
        o_stack.append(ps.Real(wx))
        o_stack.append(ps.Real(wy))

        e_stack.append(top.proc.__copy__())
    else:
        # Clean up cshow pending CID when loop ends
        if hasattr(ctxt, '_cshow_pending_cid'):
            delattr(ctxt, '_cshow_pending_cid')
        e_stack.pop()


cdef _handle_forall(ctxt, o_stack, e_stack, top):
    """Handle forall loop in exec_exec."""
    cdef int obj_type = top.obj.TYPE

    if obj_type == C_T_STRING:
        if top.obj.length:
            strings = (
                ps.global_resources.global_strings
                if top.obj.is_global
                else ps.contexts[top.obj.ctxt_id].local_strings
            )
            ctxt.o_stack.append(
                ps.Int(
                    strings[top.obj.offset + top.obj.start]
                )
            )
            top.obj.start += 1
            top.obj.length -= 1
            e_stack.append(top.proc.__copy__())
        else:
            e_stack.pop()
        return

    elif obj_type == C_T_ARRAY or obj_type == C_T_PACKED_ARRAY:
        if top.obj.length:
            elem = top.obj.val[top.obj.start]
            # Defensive check: wrap raw Python types if they sneak into arrays
            if not isinstance(elem, ps.PSObject):
                if isinstance(elem, bool):
                    elem = ps.Bool(elem)
                elif isinstance(elem, int):
                    elem = ps.Int(elem)
                elif isinstance(elem, float):
                    elem = ps.Real(elem)
                elif elem is None:
                    elem = ps.Null()
                elif isinstance(elem, bytes):
                    elem = ps.Name(elem, is_global=ctxt.vm_alloc_mode)
            ctxt.o_stack.append(elem)
            top.obj.start += 1
            top.obj.length -= 1
            e_stack.append(top.proc.__copy__())
        else:
            e_stack.pop()
        return

    elif obj_type == C_T_DICT:
        try:
            key, val = top.generator.__next__()
        except StopIteration:
            e_stack.pop()
            return

        try:
            # Wrap raw Python key types as PSObjects
            if not isinstance(key, ps.PSObject):
                if isinstance(key, bool):
                    key = ps.Bool(key)
                elif isinstance(key, int):
                    key = ps.Int(key)
                elif isinstance(key, float):
                    key = ps.Real(key)
                elif key is None:
                    key = ps.Null()
                elif isinstance(key, bytes):
                    key = ps.Name(key, is_global=ctxt.vm_alloc_mode)
            # skip the __status__ key
            try:
                while key.val == b"__status__":
                    key, val = top.generator.__next__()
            except StopIteration:
                e_stack.pop()
                return
            # Wrap raw Python val types as PSObjects (defensive)
            if not isinstance(val, ps.PSObject):
                if isinstance(val, bool):
                    val = ps.Bool(val)
                elif isinstance(val, int):
                    val = ps.Int(val)
                elif isinstance(val, float):
                    val = ps.Real(val)
                elif val is None:
                    val = ps.Null()
                elif isinstance(val, bytes):
                    val = ps.Name(val, is_global=ctxt.vm_alloc_mode)
            ctxt.o_stack.append(key)
            ctxt.o_stack.append(val)
            e_stack.append(top.proc.__copy__())
        except (IndexError, AttributeError, TypeError, ValueError):
            e_stack.pop()
    return


cdef _handle_pathforall(ctxt, o_stack, e_stack, top):
    """Handle pathforall loop in exec_exec."""
    path = top.path
    path_index = top.path_index
    sub_path_index = top.sub_path_index
    moveto_proc = top.moveto_proc
    lineto_proc = top.lineto_proc
    curveto_proc = top.curveto_proc
    closepath_proc = top.closepath_proc
    pathforall_popped = False

    if (
        sub_path_index == len(path[path_index]) - 1
        and path_index == len(path) - 1
    ):
        e_stack.pop()
        pathforall_popped = True

    if (
        path_index < len(path) + 1
        and sub_path_index < len(path[path_index]) + 1
    ):
        if isinstance(
            path[path_index][sub_path_index], (ps.MoveTo, ps.LineTo)
        ):
            x, y = ps_matrix._transform_point(
                ctxt.gstate.iCTM,
                path[path_index][sub_path_index].p.x,
                path[path_index][sub_path_index].p.y,
            )
            o_stack.append(ps.Real(x))
            o_stack.append(ps.Real(y))
            if isinstance(path[path_index][sub_path_index], ps.MoveTo):
                e_stack.append(moveto_proc.__copy__())
            else:
                e_stack.append(lineto_proc.__copy__())
        if isinstance(path[path_index][sub_path_index], ps.CurveTo):
            x, y = ps_matrix._transform_point(
                ctxt.gstate.iCTM,
                path[path_index][sub_path_index].p1.x,
                path[path_index][sub_path_index].p1.y,
            )
            o_stack.append(ps.Real(x))
            o_stack.append(ps.Real(y))
            x, y = ps_matrix._transform_point(
                ctxt.gstate.iCTM,
                path[path_index][sub_path_index].p2.x,
                path[path_index][sub_path_index].p2.y,
            )
            o_stack.append(ps.Real(x))
            o_stack.append(ps.Real(y))
            x, y = ps_matrix._transform_point(
                ctxt.gstate.iCTM,
                path[path_index][sub_path_index].p3.x,
                path[path_index][sub_path_index].p3.y,
            )
            o_stack.append(ps.Real(x))
            o_stack.append(ps.Real(y))
            e_stack.append(curveto_proc.__copy__())
        if isinstance(path[path_index][sub_path_index], ps.ClosePath):
            e_stack.append(closepath_proc.__copy__())

        if not pathforall_popped:
            e_stack[-2].sub_path_index += 1
            if e_stack[-2].sub_path_index == len(path[path_index]):
                e_stack[-2].sub_path_index = 0
                e_stack[-2].path_index += 1
    else:
        e_stack.pop()


cdef _ps_exec_from_token(ctxt, o_stack, e_stack):
    """
    Inlined version of ps_exec called from the tokenizer path in exec_exec.
    Handles the common case of executing an object from tokenization.
    """
    cdef int obj_type, obj_attrib

    if len(o_stack) < 1:
        ps_error.e(ctxt, ps_error.STACKUNDERFLOW, "exec")
        return
    if o_stack[-1].access() < ps.ACCESS_READ_ONLY:
        ps_error.e(ctxt, ps_error.INVALIDACCESS, "exec")
        return

    obj_type = o_stack[-1].TYPE
    obj_attrib = o_stack[-1].attrib

    # always execute the '}' operator
    if obj_type == C_T_NAME and o_stack[-1].val and o_stack[-1].val[0] == 125:  # ord('}')
        if obj_attrib == C_ATTRIB_EXEC:
            e_stack.append(o_stack[-1])
            o_stack.pop()
            return

    if ctxt.proc_count == 0:
        if obj_type == C_T_FILE or obj_type == C_T_STRING:
            if obj_attrib == C_ATTRIB_EXEC:
                e_stack.append(o_stack[-1])
                o_stack.pop()

        elif obj_type == C_T_NAME:
            if obj_attrib == C_ATTRIB_EXEC:
                obj = ps_dict.lookup(ctxt, o_stack[-1])
                if obj is None:
                    ps_error.e(ctxt, ps_error.UNDEFINED, o_stack[-1].__str__())
                    return
                e_stack.append(obj)
                o_stack.pop()

        elif obj_type == C_T_OPERATOR:
            e_stack.append(o_stack[-1])
            o_stack.pop()

        elif obj_type == C_T_ARRAY or obj_type == C_T_PACKED_ARRAY:
            if obj_attrib == C_ATTRIB_EXEC:
                e_stack.append(o_stack[-1].__copy__())
                o_stack.pop()
