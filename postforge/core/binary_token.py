# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
Binary Token and Object Sequence Parser (PLRM Sections 3.14.1-3.14.2)

Parses binary-encoded tokens indicated by bytes 128-159 in the input stream.
Tags 128-131 are binary object sequences (compact arrays of typed objects);
tags 132-149 are individual binary tokens. This is a Level 2 feature used
by CUPS and some print pipelines for compact PostScript representation.

This module is self-contained to avoid circular imports with tokenizer.py.
It defines its own success/error return helpers matching the tokenizer's
tuple format: (success, error_code, command, do_exec).
"""

import struct

from . import error as ps_error
from . import types as ps


# ---------------------------------------------------------------------------
# Return helpers (same tuple format as tokenizer.py)
# ---------------------------------------------------------------------------

def _token_success(ctxt: ps.Context, stack: list, do_exec: bool = True) -> tuple[bool, None, None, bool]:
    """Token parsed successfully — push True, return success tuple."""
    ctxt.o_stack.append(ps.Bool(True))
    return (True, None, None, do_exec)


def _syntax_error(ctxt: ps.Context, source: ps.File, command: str) -> tuple[bool, int, str, None]:
    """Syntax error — close source, push False, return error tuple."""
    source.close()
    ctxt.o_stack.append(ps.Bool(False))
    ctxt.proc_count = 0
    return (False, ps_error.SYNTAXERROR, command, None)


# ---------------------------------------------------------------------------
# Byte reading helper
# ---------------------------------------------------------------------------

def _read_bytes(source: ps.File, ctxt: ps.Context, count: int) -> bytes | None:
    """Read exactly *count* bytes from source. Return bytes or None on EOF."""
    result = bytearray(count)
    for i in range(count):
        b = source.read(ctxt)
        if b is None:
            return None
        result[i] = b
    return bytes(result)


# ---------------------------------------------------------------------------
# System name table (PLRM Appendix F, indices 0-480)
# Indices 226-255 are reserved (None).
# ---------------------------------------------------------------------------

_SYSTEM_NAME_TABLE = (
    # 0-9
    b"abs", b"add", b"aload", b"anchorsearch", b"and",
    b"arc", b"arcn", b"arct", b"arcto", b"array",
    # 10-19
    b"ashow", b"astore", b"awidthshow", b"begin", b"bind",
    b"bitshift", b"ceiling", b"charpath", b"clear", b"cleartomark",
    # 20-29
    b"clip", b"clippath", b"closepath", b"concat", b"concatmatrix",
    b"copy", b"count", b"counttomark", b"currentcmykcolor", b"currentdash",
    # 30-39
    b"currentdict", b"currentfile", b"currentfont", b"currentgray", b"currentgstate",
    b"currenthsbcolor", b"currentlinecap", b"currentlinejoin", b"currentlinewidth", b"currentmatrix",
    # 40-49
    b"currentpoint", b"currentrgbcolor", b"currentshared", b"curveto", b"cvi",
    b"cvlit", b"cvn", b"cvr", b"cvrs", b"cvs",
    # 50-59
    b"cvx", b"def", b"defineusername", b"dict", b"div",
    b"dtransform", b"dup", b"end", b"eoclip", b"eofill",
    # 60-69
    b"eoviewclip", b"eq", b"exch", b"exec", b"exit",
    b"file", b"fill", b"findfont", b"flattenpath", b"floor",
    # 70-79
    b"flush", b"flushfile", b"for", b"forall", b"ge",
    b"get", b"getinterval", b"grestore", b"gsave", b"gstate",
    # 80-89
    b"gt", b"identmatrix", b"idiv", b"idtransform", b"if",
    b"ifelse", b"image", b"imagemask", b"index", b"ineofill",
    # 90-99
    b"infill", b"initviewclip", b"inueofill", b"inufill", b"invertmatrix",
    b"itransform", b"known", b"le", b"length", b"lineto",
    # 100-109
    b"load", b"loop", b"lt", b"makefont", b"matrix",
    b"maxlength", b"mod", b"moveto", b"mul", b"ne",
    # 110-119
    b"neg", b"newpath", b"not", b"null", b"or",
    b"pathbbox", b"pathforall", b"pop", b"print", b"printobject",
    # 120-129
    b"put", b"putinterval", b"rcurveto", b"read", b"readhexstring",
    b"readline", b"readstring", b"rectclip", b"rectfill", b"rectstroke",
    # 130-139
    b"rectviewclip", b"repeat", b"restore", b"rlineto", b"rmoveto",
    b"roll", b"rotate", b"round", b"save", b"scale",
    # 140-149
    b"scalefont", b"search", b"selectfont", b"setbbox", b"setcachedevice",
    b"setcachedevice2", b"setcharwidth", b"setcmykcolor", b"setdash", b"setfont",
    # 150-159
    b"setgray", b"setgstate", b"sethsbcolor", b"setlinecap", b"setlinejoin",
    b"setlinewidth", b"setmatrix", b"setrgbcolor", b"setshared", b"shareddict",
    # 160-169
    b"show", b"showpage", b"stop", b"stopped", b"store",
    b"string", b"stringwidth", b"stroke", b"strokepath", b"sub",
    # 170-179
    b"systemdict", b"token", b"transform", b"translate", b"truncate",
    b"type", b"uappend", b"ucache", b"ueofill", b"ufill",
    # 180-189
    b"undef", b"upath", b"userdict", b"ustroke", b"viewclip",
    b"viewclippath", b"where", b"widthshow", b"write", b"writehexstring",
    # 190-199
    b"writeobject", b"writestring", b"wtranslation", b"xor", b"xshow",
    b"xyshow", b"yshow", b"FontDirectory", b"SharedFontDirectory", b"Courier",
    # 200-209
    b"Courier-Bold", b"Courier-BoldOblique", b"Courier-Oblique", b"Helvetica",
    b"Helvetica-Bold", b"Helvetica-BoldOblique", b"Helvetica-Oblique", b"Symbol",
    b"Times-Bold", b"Times-BoldItalic",
    # 210-219
    b"Times-Italic", b"Times-Roman", b"execuserobject", b"currentcolor",
    b"currentcolorspace", b"currentglobal", b"execform", b"filter",
    b"findresource", b"globaldict",
    # 220-225
    b"makepattern", b"setcolor", b"setcolorspace", b"setglobal",
    b"setpagedevice", b"setpattern",
    # 226-255: reserved (30 entries)
    None, None, None, None, None, None, None, None, None, None,
    None, None, None, None, None, None, None, None, None, None,
    None, None, None, None, None, None, None, None, None, None,
    # 256-259
    b"=", b"==", b"ISOLatin1Encoding", b"StandardEncoding",
    # 260-269
    b"[", b"]", b"atan", b"banddevice", b"bytesavailable",
    b"cachestatus", b"closefile", b"colorimage", b"condition", b"copypage",
    # 270-279
    b"cos", b"countdictstack", b"countexecstack", b"cshow",
    b"currentblackgeneration", b"currentcacheparams", b"currentcolorscreen",
    b"currentcolortransfer", b"currentcontext", b"currentflat",
    # 280-289
    b"currenthalftone", b"currenthalftonephase", b"currentmiterlimit",
    b"currentobjectformat", b"currentpacking", b"currentscreen",
    b"currentstrokeadjust", b"currenttransfer", b"currentundercolorremoval",
    b"defaultmatrix",
    # 290-299
    b"definefont", b"deletefile", b"detach", b"deviceinfo", b"dictstack",
    b"echo", b"erasepage", b"errordict", b"execstack", b"executeonly",
    # 300-309
    b"exp", b"false", b"filenameforall", b"fileposition", b"fork",
    b"framedevice", b"grestoreall", b"handleerror", b"initclip", b"initgraphics",
    # 310-319
    b"initmatrix", b"instroke", b"inustroke", b"join", b"kshow",
    b"ln", b"lock", b"log", b"mark", b"monitor",
    # 320-329
    b"noaccess", b"notify", b"nulldevice", b"packedarray", b"quit",
    b"rand", b"rcheck", b"readonly", b"realtime", b"renamefile",
    # 330-339
    b"renderbands", b"resetfile", b"reversepath", b"rootfont", b"rrand",
    b"run", b"scheck", b"setblackgeneration", b"setcachelimit", b"setcacheparams",
    # 340-349
    b"setcolorscreen", b"setcolortransfer", b"setfileposition", b"setflat",
    b"sethalftone", b"sethalftonephase", b"setmiterlimit", b"setobjectformat",
    b"setpacking", b"setscreen",
    # 350-359
    b"setstrokeadjust", b"settransfer", b"setucacheparams",
    b"setundercolorremoval", b"sin", b"sqrt", b"srand", b"stack",
    b"status", b"statusdict",
    # 360-369
    b"true", b"ucachestatus", b"undefinefont", b"usertime", b"ustrokepath",
    b"version", b"vmreclaim", b"vmstatus", b"wait", b"wcheck",
    # 370-379
    b"xcheck", b"yield", b"defineuserobject", b"undefineuserobject",
    b"UserObjects", b"cleardictstack", b"A", b"B", b"C", b"D",
    # 380-389
    b"E", b"F", b"G", b"H", b"I", b"J", b"K", b"L", b"M", b"N",
    # 390-399
    b"O", b"P", b"Q", b"R", b"S", b"T", b"U", b"V", b"W", b"X",
    # 400-409
    b"Y", b"Z", b"a", b"b", b"c", b"d", b"e", b"f", b"g", b"h",
    # 410-419
    b"i", b"j", b"k", b"l", b"m", b"n", b"o", b"p", b"q", b"r",
    # 420-429
    b"s", b"t", b"u", b"v", b"w", b"x", b"y", b"z",
    b"setvmthreshold", b"<<",
    # 430-439
    b">>", b"currentcolorrendering", b"currentdevparams", b"currentoverprint",
    b"currentpagedevice", b"currentsystemparams", b"currentuserparams",
    b"defineresource", b"findencoding", b"gcheck",
    # 440-449
    b"glyphshow", b"languagelevel", b"product", b"pstack", b"resourceforall",
    b"resourcestatus", b"revision", b"serialnumber", b"setcolorrendering",
    b"setdevparams",
    # 450-459
    b"setoverprint", b"setsystemparams", b"setuserparams", b"startjob",
    b"undefineresource", b"GlobalFontDirectory", b"ASCII85Decode",
    b"ASCII85Encode", b"ASCIIHexDecode", b"ASCIIHexEncode",
    # 460-469
    b"CCITTFaxDecode", b"CCITTFaxEncode", b"DCTDecode", b"DCTEncode",
    b"LZWDecode", b"LZWEncode", b"NullEncode", b"RunLengthDecode",
    b"RunLengthEncode", b"SubFileDecode",
    # 470-479
    b"CIEBasedA", b"CIEBasedABC", b"DeviceCMYK", b"DeviceGray", b"DeviceRGB",
    b"Indexed", b"Pattern", b"Separation", b"CIEBasedDEF", b"CIEBasedDEFG",
    # 480
    b"DeviceN",
)


# ---------------------------------------------------------------------------
# Binary object sequence parser (PLRM Section 3.14.2, tags 128-131)
# ---------------------------------------------------------------------------

class _BOSParseError(Exception):
    """Internal: structural error in binary object sequence."""


class _BOSUndefinedError(Exception):
    """Internal: immediately evaluated name not found."""
    def __init__(self, name_bytes: bytes) -> None:
        self.name_bytes = name_bytes


def _bos_errmsg(token_type: int, elements: int = 0, size: int = 0, reason: str = "malformed") -> str:
    """Format PLRM-style error description for binary object sequence errors."""
    return f"bin obj seq, type={token_type}, elements={elements}, size={size}, {reason}"


_MAX_BOS_DEPTH = 100


def _parse_binary_object_sequence(ctxt: ps.Context, stack: list, source: ps.File, token_type: int) -> tuple[bool, int | None, str | None, bool | None]:
    """
    Parse a binary object sequence (PLRM 3.14.2).

    Tags 128-131 encode byte order and real format:
      128, 130: big-endian (>)
      129, 131: little-endian (<)

    Header is 4 bytes (normal) or 8 bytes (extended, when byte 1 is 0).
    Body contains 8-byte object entries followed by string/name data.
    """
    endian = ">" if token_type in (128, 130) else "<"

    # --- Read header ---
    byte1 = source.read(ctxt)
    if byte1 is None:
        return _syntax_error(ctxt, source, _bos_errmsg(token_type))

    count_data = _read_bytes(source, ctxt, 2)
    if count_data is None:
        return _syntax_error(ctxt, source, _bos_errmsg(token_type))
    top_level_count = struct.unpack(endian + "H", count_data)[0]

    if byte1 > 0:
        # Normal header (4 bytes): byte1 = overall length
        overall_length = byte1
        header_size = 4
    else:
        # Extended header (8 bytes): bytes 4-7 = overall length
        len_data = _read_bytes(source, ctxt, 4)
        if len_data is None:
            return _syntax_error(ctxt, source, _bos_errmsg(token_type))
        overall_length = struct.unpack(endian + "I", len_data)[0]
        header_size = 8

    # Read body (everything after the header)
    data_size = overall_length - header_size
    if data_size < 0 or data_size < top_level_count * 8:
        return _syntax_error(ctxt, source,
            _bos_errmsg(token_type, top_level_count, overall_length,
                        "insufficient data for top-level objects"))

    data = _read_bytes(source, ctxt, data_size)
    if data is None:
        return _syntax_error(ctxt, source,
            _bos_errmsg(token_type, top_level_count, overall_length,
                        "unexpected EOF"))

    # --- Build PS objects from the buffer ---
    is_global = ctxt.vm_alloc_mode
    strings_buf = ps.global_resources.global_strings if is_global else ctxt.local_strings
    building = set()  # cycle detection for arrays

    def _build(pos, depth=0):
        """Recursively build a PS object from the 8-byte entry at byte offset *pos*."""
        if depth > _MAX_BOS_DEPTH:
            raise _BOSParseError("nesting too deep")
        if pos < 0 or pos + 8 > data_size:
            raise _BOSParseError("object offset out of bounds")

        type_byte = data[pos]
        type_code = type_byte & 0x7F
        is_exec = bool(type_byte & 0x80)
        length_u16 = struct.unpack(endian + "H", data[pos + 2:pos + 4])[0]
        value_u32 = struct.unpack(endian + "I", data[pos + 4:pos + 8])[0]
        attrib = ps.ATTRIB_EXEC if is_exec else ps.ATTRIB_LIT

        # --- null (0) ---
        if type_code == 0:
            return ps.Null()

        # --- integer (1) ---
        if type_code == 1:
            val = struct.unpack(endian + "i", data[pos + 4:pos + 8])[0]
            return ps.Int(val)

        # --- real (2) ---
        if type_code == 2:
            if length_u16 == 0:
                val = struct.unpack(endian + "f", data[pos + 4:pos + 8])[0]
            else:
                raw = struct.unpack(endian + "i", data[pos + 4:pos + 8])[0]
                val = raw / (1 << length_u16)
            obj = ps.Real(val)
            obj.attrib = attrib
            return obj

        # --- name (3) / immediately evaluated name (6) ---
        if type_code in (3, 6):
            signed_len = struct.unpack(endian + "h", data[pos + 2:pos + 4])[0]
            if signed_len == -1:
                # System name table lookup
                if value_u32 >= len(_SYSTEM_NAME_TABLE) or _SYSTEM_NAME_TABLE[value_u32] is None:
                    raise _BOSParseError(f"invalid system name index {value_u32}")
                name_bytes = _SYSTEM_NAME_TABLE[value_u32]
            elif signed_len > 0:
                offset = value_u32
                if offset + signed_len > data_size:
                    raise _BOSParseError("name string out of bounds")
                name_bytes = bytes(data[offset:offset + signed_len])
            else:
                raise _BOSParseError(f"invalid name length {signed_len}")

            if type_code == 6:
                # Immediately evaluated name: look up in dict stack
                for d in reversed(ctxt.d_stack):
                    if name_bytes in d.val:
                        return d.val[name_bytes]
                raise _BOSUndefinedError(name_bytes)

            return ps.Name(name_bytes, attrib=attrib, is_global=is_global)

        # --- boolean (4) ---
        if type_code == 4:
            return ps.Bool(value_u32 != 0)

        # --- string (5) ---
        if type_code == 5:
            if length_u16 == 0:
                str_off = len(strings_buf)
                return ps.String(ctxt.id, str_off, 0, is_global=is_global)
            offset = value_u32
            if offset + length_u16 > data_size:
                raise _BOSParseError("string data out of bounds")
            str_off = len(strings_buf)
            strings_buf.extend(data[offset:offset + length_u16])
            obj = ps.String(ctxt.id, str_off, length_u16, is_global=is_global)
            obj.attrib = attrib
            return obj

        # --- array (9) ---
        if type_code == 9:
            if length_u16 == 0:
                arr = ps.Array(ctxt.id, is_global=is_global)
                arr.attrib = attrib
                arr.length = 0
                return arr
            offset = value_u32
            if offset % 8 != 0:
                raise _BOSParseError("array offset not multiple of 8")
            if pos in building:
                raise _BOSParseError("circular array reference")
            building.add(pos)
            try:
                arr = ps.Array(ctxt.id, is_global=is_global)
                arr.attrib = attrib
                for i in range(length_u16):
                    arr.val.append(_build(offset + i * 8, depth + 1))
                arr.length = length_u16
                return arr
            finally:
                building.discard(pos)

        # --- mark (10) ---
        if type_code == 10:
            return ps.Mark()

        raise _BOSParseError(f"unknown object type {type_code}")

    # Build the top-level objects
    try:
        results = []
        for i in range(top_level_count):
            results.append(_build(i * 8))
    except _BOSParseError as e:
        return _syntax_error(ctxt, source,
            _bos_errmsg(token_type, top_level_count, overall_length, str(e)))
    except _BOSUndefinedError as e:
        source.close()
        ctxt.o_stack.append(ps.Bool(False))
        ctxt.proc_count = 0
        return (False, ps_error.UNDEFINED,
                e.name_bytes.decode("latin-1", errors="replace"), None)

    # Wrap in an executable array (PLRM: "a binary object sequence is an
    # ordinary executable array")
    arr = ps.Array(ctxt.id, is_global=is_global)
    arr.attrib = ps.ATTRIB_EXEC
    arr.val = results
    arr.length = top_level_count
    ctxt.o_stack.append(arr)

    # Implicit exec when not in deferred context (PLRM 3.14.2)
    do_exec = (ctxt.proc_count == 0)
    return _token_success(ctxt, stack, do_exec=do_exec)


# ---------------------------------------------------------------------------
# Dispatch entry point
# ---------------------------------------------------------------------------

def parse_binary_token(ctxt: ps.Context, stack: list, source: ps.File, token_type: int) -> tuple[bool, int | None, str | None, bool | None]:
    """
    Parse a binary token (PLRM Section 3.14.1, Table 3.25).

    Called from tokenizer.__token() when byte 128-159 is encountered.
    """
    # 128-131: Binary object sequences
    if token_type <= 131:
        return _parse_binary_object_sequence(ctxt, stack, source, token_type)

    # 132-136: Integers
    if token_type <= 136:
        if token_type == 132:
            return _parse_int(ctxt, stack, source, ">i", 4)
        if token_type == 133:
            return _parse_int(ctxt, stack, source, "<i", 4)
        if token_type == 134:
            return _parse_int(ctxt, stack, source, ">h", 2)
        if token_type == 135:
            return _parse_int(ctxt, stack, source, "<h", 2)
        # 136: 8-bit signed
        return _parse_int8(ctxt, stack, source)

    # 137: Fixed-point
    if token_type == 137:
        return _parse_fixed_point(ctxt, stack, source)

    # 138-140: Reals
    if token_type <= 140:
        if token_type == 138:
            return _parse_real(ctxt, stack, source, ">f")
        # 139 and 140 (native) both little-endian on x86
        return _parse_real(ctxt, stack, source, "<f")

    # 141: Boolean
    if token_type == 141:
        return _parse_bool(ctxt, stack, source)

    # 142-144: Strings
    if token_type == 142:
        return _parse_string_short(ctxt, stack, source)
    if token_type == 143:
        return _parse_string_long(ctxt, stack, source, ">H")
    if token_type == 144:
        return _parse_string_long(ctxt, stack, source, "<H")

    # 145-146: System names
    if token_type == 145:
        return _parse_system_name(ctxt, stack, source, literal=True)
    if token_type == 146:
        return _parse_system_name(ctxt, stack, source, literal=False)

    # 147-148: Reserved
    if token_type <= 148:
        return _syntax_error(ctxt, source, "reserved binary token type")

    # 149: Homogeneous number array
    if token_type == 149:
        return _parse_homogeneous_number_array(ctxt, stack, source)

    # 150-159: Unassigned
    return _syntax_error(ctxt, source, "unassigned binary token type")


# ---------------------------------------------------------------------------
# Integer parsers
# ---------------------------------------------------------------------------

def _parse_int(ctxt: ps.Context, stack: list, source: ps.File, fmt: str, nbytes: int) -> tuple[bool, int | None, str | None, bool | None]:
    """Parse 16-bit or 32-bit integer with given struct format."""
    data = _read_bytes(source, ctxt, nbytes)
    if data is None:
        return _syntax_error(ctxt, source, "unexpected EOF in binary integer")
    value = struct.unpack(fmt, data)[0]
    ctxt.o_stack.append(ps.Int(value))
    return _token_success(ctxt, stack)


def _parse_int8(ctxt: ps.Context, stack: list, source: ps.File) -> tuple[bool, int | None, str | None, bool | None]:
    """Parse 8-bit signed integer (type 136)."""
    b = source.read(ctxt)
    if b is None:
        return _syntax_error(ctxt, source, "unexpected EOF in binary int8")
    # Convert unsigned byte to signed
    value = b if b < 128 else b - 256
    ctxt.o_stack.append(ps.Int(value))
    return _token_success(ctxt, stack)


# ---------------------------------------------------------------------------
# Real parsers
# ---------------------------------------------------------------------------

def _parse_real(ctxt: ps.Context, stack: list, source: ps.File, fmt: str) -> tuple[bool, int | None, str | None, bool | None]:
    """Parse 32-bit IEEE float with given struct format."""
    data = _read_bytes(source, ctxt, 4)
    if data is None:
        return _syntax_error(ctxt, source, "unexpected EOF in binary real")
    value = struct.unpack(fmt, data)[0]
    ctxt.o_stack.append(ps.Real(value))
    return _token_success(ctxt, stack)


# ---------------------------------------------------------------------------
# Boolean parser
# ---------------------------------------------------------------------------

def _parse_bool(ctxt: ps.Context, stack: list, source: ps.File) -> tuple[bool, int | None, str | None, bool | None]:
    """Parse binary boolean (type 141): 0=false, nonzero=true."""
    b = source.read(ctxt)
    if b is None:
        return _syntax_error(ctxt, source, "unexpected EOF in binary boolean")
    ctxt.o_stack.append(ps.Bool(b != 0))
    return _token_success(ctxt, stack)


# ---------------------------------------------------------------------------
# String parsers
# ---------------------------------------------------------------------------

def _parse_string_short(ctxt: ps.Context, stack: list, source: ps.File) -> tuple[bool, int | None, str | None, bool | None]:
    """Parse string with 1-byte length (type 142)."""
    length_byte = source.read(ctxt)
    if length_byte is None:
        return _syntax_error(ctxt, source, "unexpected EOF in binary string length")
    return _read_binary_string(ctxt, stack, source, length_byte)


def _parse_string_long(ctxt: ps.Context, stack: list, source: ps.File, fmt: str) -> tuple[bool, int | None, str | None, bool | None]:
    """Parse string with 2-byte length (types 143-144)."""
    data = _read_bytes(source, ctxt, 2)
    if data is None:
        return _syntax_error(ctxt, source, "unexpected EOF in binary string length")
    length = struct.unpack(fmt, data)[0]
    return _read_binary_string(ctxt, stack, source, length)


def _read_binary_string(ctxt: ps.Context, stack: list, source: ps.File, length: int) -> tuple[bool, int | None, str | None, bool | None]:
    """Read *length* bytes of string data and create a String object."""
    strings = ps.global_resources.global_strings if ctxt.vm_alloc_mode else ctxt.local_strings
    offset = len(strings)
    for _ in range(length):
        b = source.read(ctxt)
        if b is None:
            return _syntax_error(ctxt, source, "unexpected EOF in binary string data")
        strings.append(b)
    ctxt.o_stack.append(
        ps.String(ctxt.id, offset, length, is_global=ctxt.vm_alloc_mode)
    )
    return _token_success(ctxt, stack)


# ---------------------------------------------------------------------------
# System name parser
# ---------------------------------------------------------------------------

def _parse_system_name(ctxt: ps.Context, stack: list, source: ps.File, literal: bool) -> tuple[bool, int | None, str | None, bool | None]:
    """Parse encoded system name (types 145-146). Index is single byte 0-255."""
    index_byte = source.read(ctxt)
    if index_byte is None:
        return _syntax_error(ctxt, source, "unexpected EOF in system name index")
    if index_byte >= len(_SYSTEM_NAME_TABLE) or _SYSTEM_NAME_TABLE[index_byte] is None:
        ctxt.o_stack.append(ps.Bool(False))
        return (False, ps_error.UNDEFINED, f"system name index {index_byte}", None)
    name_bytes = _SYSTEM_NAME_TABLE[index_byte]
    attrib = ps.ATTRIB_LIT if literal else ps.ATTRIB_EXEC
    ctxt.o_stack.append(
        ps.Name(name_bytes, attrib=attrib, is_global=ctxt.vm_alloc_mode)
    )
    do_exec = not literal
    return _token_success(ctxt, stack, do_exec=do_exec)


# ---------------------------------------------------------------------------
# Fixed-point parser (type 137)
# ---------------------------------------------------------------------------

def _parse_fixed_point(ctxt: ps.Context, stack: list, source: ps.File) -> tuple[bool, int | None, str | None, bool | None]:
    """
    Parse fixed-point number (type 137).

    Representation byte *r* encodes size, byte-order, and scale:
      0 <= r <= 31:   32-bit BE, scale = r
      32 <= r <= 47:  16-bit BE, scale = r - 32
      128 <= r <= 159: 32-bit LE, scale = r - 128
      160 <= r <= 175: 16-bit LE, scale = r - 160
    """
    r = source.read(ctxt)
    if r is None:
        return _syntax_error(ctxt, source, "unexpected EOF in fixed-point representation")

    if r <= 31:
        fmt, nbytes, scale = ">i", 4, r
    elif r <= 47:
        fmt, nbytes, scale = ">h", 2, r - 32
    elif 128 <= r <= 159:
        fmt, nbytes, scale = "<i", 4, r - 128
    elif 160 <= r <= 175:
        fmt, nbytes, scale = "<h", 2, r - 160
    else:
        return _syntax_error(ctxt, source, "invalid fixed-point representation byte")

    data = _read_bytes(source, ctxt, nbytes)
    if data is None:
        return _syntax_error(ctxt, source, "unexpected EOF in fixed-point data")

    raw = struct.unpack(fmt, data)[0]
    if scale == 0:
        ctxt.o_stack.append(ps.Int(raw))
    else:
        ctxt.o_stack.append(ps.Real(raw / (1 << scale)))
    return _token_success(ctxt, stack)


# ---------------------------------------------------------------------------
# Homogeneous number array parser (type 149)
# ---------------------------------------------------------------------------

def _parse_homogeneous_number_array(ctxt: ps.Context, stack: list, source: ps.File) -> tuple[bool, int | None, str | None, bool | None]:
    """
    Parse homogeneous number array (type 149).

    Header: 1-byte representation + 2-byte count.
    Byte order of count (and elements) determined by representation byte.
    Representation byte *r*:
      0-31:   32-bit fixed, BE, scale = r
      32-47:  16-bit fixed, BE, scale = r - 32
      48:     32-bit IEEE real, BE
      49:     32-bit native real
      128-159: 32-bit fixed, LE, scale = r - 128
      160-175: 16-bit fixed, LE, scale = r - 160
      176:    32-bit IEEE real, LE
      177:    32-bit native real (LE)
    """
    r = source.read(ctxt)
    if r is None:
        return _syntax_error(ctxt, source, "unexpected EOF in number array representation")

    # Determine element format, byte-order for count, and scale
    if r <= 31:
        elem_fmt, elem_size, scale, count_fmt = ">i", 4, r, ">H"
        is_real = False
    elif r <= 47:
        elem_fmt, elem_size, scale, count_fmt = ">h", 2, r - 32, ">H"
        is_real = False
    elif r == 48:
        elem_fmt, elem_size, scale, count_fmt = ">f", 4, 0, ">H"
        is_real = True
    elif r == 49:
        elem_fmt, elem_size, scale, count_fmt = "<f", 4, 0, ">H"
        is_real = True
    elif 128 <= r <= 159:
        elem_fmt, elem_size, scale, count_fmt = "<i", 4, r - 128, "<H"
        is_real = False
    elif 160 <= r <= 175:
        elem_fmt, elem_size, scale, count_fmt = "<h", 2, r - 160, "<H"
        is_real = False
    elif r == 176:
        elem_fmt, elem_size, scale, count_fmt = "<f", 4, 0, "<H"
        is_real = True
    elif r == 177:
        elem_fmt, elem_size, scale, count_fmt = "<f", 4, 0, "<H"
        is_real = True
    else:
        return _syntax_error(ctxt, source, "invalid number array representation byte")

    # Read 2-byte element count
    count_data = _read_bytes(source, ctxt, 2)
    if count_data is None:
        return _syntax_error(ctxt, source, "unexpected EOF in number array count")
    count = struct.unpack(count_fmt, count_data)[0]

    # Build array
    arr = ps.Array(ctxt.id, is_global=ctxt.vm_alloc_mode)
    for _ in range(count):
        data = _read_bytes(source, ctxt, elem_size)
        if data is None:
            return _syntax_error(ctxt, source, "unexpected EOF in number array data")
        raw = struct.unpack(elem_fmt, data)[0]
        if is_real:
            arr.val.append(ps.Real(raw))
        elif scale == 0:
            arr.val.append(ps.Int(raw))
        else:
            arr.val.append(ps.Real(raw / (1 << scale)))
    arr.length = count

    ctxt.o_stack.append(arr)
    return _token_success(ctxt, stack)
