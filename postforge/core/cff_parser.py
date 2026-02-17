# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
CFF (Compact Font Format) Binary Parser

Parses CFF binary data (Adobe TN#5176) into structured Python objects.
CFF is a compact binary encoding for Type 1-style fonts using Type 2 charstrings.

CFF data appears in PostScript via the FontSet resource mechanism:
  FontSetInit /ProcSet findresource begin ... StartData

This parser handles:
- CFF Header, Name INDEX, Top DICT, String INDEX, Global Subr INDEX
- CharStrings INDEX, charset, encoding, Private DICT, Local Subr INDEX
- Both name-keyed and CID-keyed fonts
- Predefined charsets (ISOAdobe, Expert, ExpertSubset) and encodings

Based on: Adobe Technical Note #5176 - The Compact Font Format Specification
"""

import struct
import math


class CFFError(Exception):
    """Error during CFF parsing."""
    pass


class CFFFont:
    """Parsed CFF font data."""
    __slots__ = (
        'name', 'top_dict', 'char_strings', 'charset', 'encoding',
        'font_matrix', 'font_bbox', 'private_dict', 'local_subrs',
        'global_subrs', 'default_width_x', 'nominal_width_x',
        'is_cid', 'fd_array', 'fd_select', 'ros', 'raw_data',
    )

    def __init__(self):
        self.name = ''
        self.top_dict = {}
        self.char_strings = []      # GID-indexed charstring bytes
        self.charset = []            # GID -> glyph name
        self.encoding = [0] * 256   # char_code -> GID
        self.font_matrix = [0.001, 0.0, 0.0, 0.001, 0.0, 0.0]
        self.font_bbox = [0, 0, 0, 0]
        self.private_dict = {}
        self.local_subrs = []
        self.global_subrs = []
        self.default_width_x = 0.0
        self.nominal_width_x = 0.0
        self.is_cid = False
        self.fd_array = []           # Per-FD private dicts (CID only)
        self.fd_select = []          # GID -> FD index (CID only)
        self.ros = None              # (Registry, Ordering, Supplement) for CID
        self.raw_data = None         # Original binary data (for PDF embedding)


# ---------------------------------------------------------------------------
# Standard Strings (SID 0..390) — CFF Specification Appendix A
# ---------------------------------------------------------------------------
_STANDARD_STRINGS = [
    # SID 0-9
    ".notdef", "space", "exclam", "quotedbl", "numbersign",
    "dollar", "percent", "ampersand", "quoteright", "parenleft",
    # SID 10-19
    "parenright", "asterisk", "plus", "comma", "hyphen",
    "period", "slash", "zero", "one", "two",
    # SID 20-29
    "three", "four", "five", "six", "seven",
    "eight", "nine", "colon", "semicolon", "less",
    # SID 30-39
    "equal", "greater", "question", "at", "A",
    "B", "C", "D", "E", "F",
    # SID 40-49
    "G", "H", "I", "J", "K",
    "L", "M", "N", "O", "P",
    # SID 50-59
    "Q", "R", "S", "T", "U",
    "V", "W", "X", "Y", "Z",
    # SID 60-69
    "bracketleft", "backslash", "bracketright", "asciicircum", "underscore",
    "quoteleft", "a", "b", "c", "d",
    # SID 70-79
    "e", "f", "g", "h", "i",
    "j", "k", "l", "m", "n",
    # SID 80-89
    "o", "p", "q", "r", "s",
    "t", "u", "v", "w", "x",
    # SID 90-99
    "y", "z", "braceleft", "bar", "braceright",
    "asciitilde", "exclamdown", "cent", "sterling", "fraction",
    # SID 100-109
    "yen", "florin", "section", "currency", "quotesingle",
    "quotedblleft", "guillemotleft", "guilsinglleft", "guilsinglright", "fi",
    # SID 110-119
    "fl", "endash", "dagger", "daggerdbl", "periodcentered",
    "paragraph", "bullet", "quotesinglbase", "quotedblbase", "quotedblright",
    # SID 120-129
    "guillemotright", "ellipsis", "perthousand", "questiondown", "grave",
    "acute", "circumflex", "tilde", "macron", "breve",
    # SID 130-139
    "dotaccent", "dieresis", "ring", "cedilla", "hungarumlaut",
    "ogonek", "caron", "emdash", "AE", "ordfeminine",
    # SID 140-149
    "Lslash", "Oslash", "OE", "ordmasculine", "ae",
    "dotlessi", "lslash", "oslash", "oe", "germandbls",
    # SID 150-159
    "onesuperior", "logicalnot", "mu", "trademark", "Eth",
    "onehalf", "plusminus", "Thorn", "onequarter", "divide",
    # SID 160-169
    "brokenbar", "degree", "thorn", "threequarters", "twosuperior",
    "registered", "minus", "eth", "multiply", "threesuperior",
    # SID 170-179
    "copyright", "Aacute", "Acircumflex", "Adieresis", "Agrave",
    "Aring", "Atilde", "Ccedilla", "Eacute", "Ecircumflex",
    # SID 180-189
    "Edieresis", "Egrave", "Iacute", "Icircumflex", "Idieresis",
    "Igrave", "Ntilde", "Oacute", "Ocircumflex", "Odieresis",
    # SID 190-199
    "Ograve", "Otilde", "Scaron", "Uacute", "Ucircumflex",
    "Udieresis", "Ugrave", "Yacute", "Ydieresis", "Zcaron",
    # SID 200-209
    "aacute", "acircumflex", "adieresis", "agrave", "aring",
    "atilde", "ccedilla", "eacute", "ecircumflex", "edieresis",
    # SID 210-219
    "egrave", "iacute", "icircumflex", "idieresis", "igrave",
    "ntilde", "oacute", "ocircumflex", "odieresis", "ograve",
    # SID 220-229
    "otilde", "scaron", "uacute", "ucircumflex", "udieresis",
    "ugrave", "yacute", "ydieresis", "zcaron", "exclamsmall",
    # SID 230-239
    "Hungarumlautsmall", "dollaroldstyle", "dollarsuperior", "ampersandsmall",
    "Acutesmall", "parenleftsuperior", "parenrightsuperior", "twodotenleader",
    "onedotenleader", "zerooldstyle",
    # SID 240-249
    "oneoldstyle", "twooldstyle", "threeoldstyle", "fouroldstyle",
    "fiveoldstyle", "sixoldstyle", "sevenoldstyle", "eightoldstyle",
    "nineoldstyle", "commasuperior",
    # SID 250-259
    "threequartersemdash", "periodsuperior", "questionsmall", "asuperior",
    "bsuperior", "centsuperior", "dsuperior", "esuperior", "isuperior",
    "lsuperior",
    # SID 260-269
    "msuperior", "nsuperior", "osuperior", "rsuperior", "ssuperior",
    "tsuperior", "ff", "ffi", "ffl", "parenleftinferior",
    # SID 270-279
    "parenrightinferior", "Circumflexsmall", "hyphensuperior", "Gravesmall",
    "Asmall", "Bsmall", "Csmall", "Dsmall", "Esmall", "Fsmall",
    # SID 280-289
    "Gsmall", "Hsmall", "Ismall", "Jsmall", "Ksmall",
    "Lsmall", "Msmall", "Nsmall", "Osmall", "Psmall",
    # SID 290-299
    "Qsmall", "Rsmall", "Ssmall", "Tsmall", "Usmall",
    "Vsmall", "Wsmall", "Xsmall", "Ysmall", "Zsmall",
    # SID 300-309
    "colonmonetary", "onefitted", "rupiah", "Tildesmall", "exclamdownsmall",
    "centoldstyle", "Lslashsmall", "Scaronsmall", "Zcaronsmall", "Dieresissmall",
    # SID 310-319
    "Brevesmall", "Caronsmall", "Dotaccentsmall", "Macronsmall", "figuredash",
    "hypheninferior", "Ogoneksmall", "Ringsmall", "Cedillasmall", "questiondownsmall",
    # SID 320-329
    "oneeighth", "threeeighths", "fiveeighths", "seveneighths", "onethird",
    "twothirds", "zerosuperior", "foursuperior", "fivesuperior", "sixsuperior",
    # SID 330-339
    "sevensuperior", "eightsuperior", "ninesuperior", "zeroinferior", "oneinferior",
    "twoinferior", "threeinferior", "fourinferior", "fiveinferior", "sixinferior",
    # SID 340-349
    "seveninferior", "eightinferior", "nineinferior", "centinferior", "dollarinferior",
    "periodinferior", "commainferior", "Agravesmall", "Aacutesmall", "Acircumflexsmall",
    # SID 350-359
    "Atildesmall", "Adieresissmall", "Aringsmall", "AEsmall", "Ccedillasmall",
    "Egravesmall", "Eacutesmall", "Ecircumflexsmall", "Edieresissmall", "Igravesmall",
    # SID 360-369
    "Iacutesmall", "Icircumflexsmall", "Idieresissmall", "Ethsmall", "Ntildesmall",
    "Ogravesmall", "Oacutesmall", "Ocircumflexsmall", "Otildesmall", "Odieresissmall",
    # SID 370-379
    "OEsmall", "Oslashsmall", "Ugravesmall", "Uacutesmall", "Ucircumflexsmall",
    "Udieresissmall", "Yacutesmall", "Thornsmall", "Ydieresissmall",
    "001.000", "001.001",
    # SID 380-390
    "001.002", "001.003", "Black", "Bold", "Book",
    "Light", "Medium", "Regular", "Roman", "Semibold",
    "001.004",  # SID 390 — last standard string
]

_NUM_STANDARD_STRINGS = 391


# ---------------------------------------------------------------------------
# Predefined Charsets
# ---------------------------------------------------------------------------

# ISOAdobe charset (charset ID 0) — SIDs for GID 1..228
_ISO_ADOBE_CHARSET = list(range(1, 229))

# Expert charset (charset ID 1) — SIDs for GID 1..165
_EXPERT_CHARSET = [
    1, 229, 230, 231, 232, 233, 234, 235, 236, 237,
    238, 13, 14, 15, 99, 239, 240, 241, 242, 243,
    244, 245, 246, 247, 248, 27, 28, 249, 250, 251,
    252, 253, 254, 255, 256, 257, 258, 259, 260, 261,
    262, 263, 264, 265, 266, 109, 110, 267, 268, 269,
    270, 271, 272, 273, 274, 275, 276, 277, 278, 279,
    280, 281, 282, 283, 284, 285, 286, 287, 288, 289,
    290, 291, 292, 293, 294, 295, 296, 297, 298, 299,
    300, 301, 302, 303, 304, 305, 306, 307, 308, 309,
    310, 311, 312, 313, 314, 315, 316, 317, 318, 158,
    155, 163, 319, 320, 321, 322, 323, 324, 325, 326,
    150, 164, 169, 327, 328, 329, 330, 331, 332, 333,
    334, 335, 336, 337, 338, 339, 340, 341, 342, 343,
    344, 345, 346, 347, 348, 349, 350, 351, 352, 353,
    354, 355, 356, 357, 358, 359, 360, 361, 362, 363,
    364, 365, 366, 367, 368, 369, 370, 371, 372, 373,
    374, 375, 376, 377, 378,
]

# ExpertSubset charset (charset ID 2) — SIDs for GID 1..86
_EXPERT_SUBSET_CHARSET = [
    1, 231, 232, 235, 236, 237, 238, 13, 14, 15,
    99, 239, 240, 241, 242, 243, 244, 245, 246, 247,
    248, 27, 28, 249, 250, 251, 253, 254, 255, 256,
    257, 258, 259, 260, 261, 262, 263, 264, 265, 266,
    109, 110, 267, 268, 269, 270, 272, 300, 301, 302,
    305, 314, 315, 158, 155, 163, 320, 321, 322, 323,
    324, 325, 326, 150, 164, 169, 327, 328, 329, 330,
    331, 332, 333, 334, 335, 336, 337, 338, 339, 340,
    341, 342, 343, 344, 345, 346,
]


# ---------------------------------------------------------------------------
# Predefined Encodings
# ---------------------------------------------------------------------------

# Standard Encoding — code -> SID (only non-zero entries)
_STANDARD_ENCODING_MAP = {
    32: 1, 33: 2, 34: 3, 35: 4, 36: 5, 37: 6, 38: 7, 39: 8,
    40: 9, 41: 10, 42: 11, 43: 12, 44: 13, 45: 14, 46: 15, 47: 16,
    48: 17, 49: 18, 50: 19, 51: 20, 52: 21, 53: 22, 54: 23, 55: 24,
    56: 25, 57: 26, 58: 27, 59: 28, 60: 29, 61: 30, 62: 31, 63: 32,
    64: 33, 65: 34, 66: 35, 67: 36, 68: 37, 69: 38, 70: 39, 71: 40,
    72: 41, 73: 42, 74: 43, 75: 44, 76: 45, 77: 46, 78: 47, 79: 48,
    80: 49, 81: 50, 82: 51, 83: 52, 84: 53, 85: 54, 86: 55, 87: 56,
    88: 57, 89: 58, 90: 59, 91: 60, 92: 61, 93: 62, 94: 63, 95: 64,
    96: 65, 97: 66, 98: 67, 99: 68, 100: 69, 101: 70, 102: 71,
    103: 72, 104: 73, 105: 74, 106: 75, 107: 76, 108: 77, 109: 78,
    110: 79, 111: 80, 112: 81, 113: 82, 114: 83, 115: 84, 116: 85,
    117: 86, 118: 87, 119: 88, 120: 89, 121: 90, 122: 91, 123: 92,
    124: 93, 125: 94, 126: 95,
    161: 96, 162: 97, 163: 98, 164: 99, 165: 100, 166: 101,
    167: 102, 168: 103, 169: 104, 170: 105, 171: 106, 172: 107,
    173: 108, 174: 109, 175: 110, 177: 111, 178: 112, 179: 113,
    180: 114, 182: 115, 183: 116, 184: 117, 185: 118, 186: 119,
    187: 120, 188: 121, 189: 122, 191: 123, 193: 124, 194: 125,
    195: 126, 196: 127, 197: 128, 198: 129, 199: 130, 200: 131,
    202: 132, 203: 133, 205: 134, 206: 135, 207: 136, 208: 137,
    225: 138, 227: 139, 232: 140, 233: 141, 234: 142, 235: 143,
    241: 144, 245: 145, 248: 146, 249: 147, 250: 148, 251: 149,
}

# Expert Encoding — code -> SID (only non-zero entries)
_EXPERT_ENCODING_MAP = {
    32: 1, 33: 229, 34: 230, 36: 231, 37: 232, 38: 233, 39: 234,
    40: 235, 41: 236, 42: 237, 43: 238, 44: 13, 45: 14, 46: 15,
    47: 99, 48: 239, 49: 240, 50: 241, 51: 242, 52: 243, 53: 244,
    54: 245, 55: 246, 56: 247, 57: 248, 58: 27, 59: 28, 60: 249,
    61: 250, 62: 251, 63: 252, 64: 253, 65: 254, 66: 255, 67: 256,
    68: 257, 69: 258, 70: 259, 71: 260, 72: 261, 73: 262, 74: 263,
    75: 264, 76: 265, 77: 266, 78: 109, 79: 110, 80: 267, 81: 268,
    82: 269, 83: 270, 84: 271, 85: 272, 86: 273, 87: 274, 88: 275,
    89: 276, 90: 277, 91: 278, 92: 279, 93: 280, 94: 281, 95: 282,
    96: 283, 97: 284, 98: 285, 99: 286, 100: 287, 101: 288, 102: 289,
    103: 290, 104: 291, 105: 292, 106: 293, 107: 294, 108: 295,
    109: 296, 110: 297, 111: 298, 112: 299, 113: 300, 114: 301,
    115: 302, 116: 303, 117: 304, 118: 305, 119: 306, 120: 307,
    121: 308, 122: 309, 123: 310, 124: 311, 125: 312, 126: 313,
    161: 314, 162: 315, 163: 316, 164: 317, 165: 318, 166: 158,
    167: 155, 168: 163, 169: 319, 170: 320, 171: 321, 172: 322,
    173: 323, 174: 324, 175: 325, 176: 326, 177: 150, 178: 164,
    179: 169, 180: 327, 181: 328, 182: 329, 183: 330, 184: 331,
    185: 332, 186: 333, 187: 334, 188: 335, 189: 336, 190: 337,
    191: 338, 192: 339, 193: 340, 194: 341, 195: 342, 196: 343,
    197: 344, 198: 345, 199: 346, 200: 347, 201: 348, 202: 349,
    203: 350, 204: 351, 205: 352, 206: 353, 207: 354, 208: 355,
    209: 356, 210: 357, 211: 358, 212: 359, 213: 360, 214: 361,
    215: 362, 216: 363, 217: 364, 218: 365, 219: 366, 220: 367,
    221: 368, 222: 369, 223: 370, 224: 371, 225: 372, 226: 373,
    227: 374, 228: 375, 229: 376, 230: 377, 231: 378,
}


# ---------------------------------------------------------------------------
# DICT Operator Names  (op_byte or (12, sub_byte)) -> name
# ---------------------------------------------------------------------------
_TOP_DICT_OPERATORS = {
    0: 'version', 1: 'Notice', 2: 'FullName', 3: 'FamilyName',
    4: 'Weight', 5: 'FontBBox', 13: 'UniqueID', 14: 'XUID',
    15: 'charset', 16: 'Encoding', 17: 'CharStrings', 18: 'Private',
    (12, 0): 'Copyright', (12, 1): 'isFixedPitch', (12, 2): 'ItalicAngle',
    (12, 3): 'UnderlinePosition', (12, 4): 'UnderlineThickness',
    (12, 5): 'PaintType', (12, 6): 'CharstringType', (12, 7): 'FontMatrix',
    (12, 8): 'StrokeWidth', (12, 20): 'SyntheticBase',
    (12, 21): 'PostScript', (12, 22): 'BaseFontName',
    (12, 23): 'BaseFontBlend',
    # CID-specific
    (12, 30): 'ROS', (12, 31): 'CIDFontVersion', (12, 32): 'CIDFontRevision',
    (12, 33): 'CIDFontType', (12, 34): 'CIDCount', (12, 35): 'UIDBase',
    (12, 36): 'FDArray', (12, 37): 'FDSelect', (12, 38): 'FontName',
}

_PRIVATE_DICT_OPERATORS = {
    6: 'BlueValues', 7: 'OtherBlues', 8: 'FamilyBlues',
    9: 'FamilyOtherBlues', 10: 'StdHW', 11: 'StdVW',
    19: 'Subrs', 20: 'defaultWidthX', 21: 'nominalWidthX',
    (12, 9): 'BlueScale', (12, 10): 'BlueShift', (12, 11): 'BlueFuzz',
    (12, 12): 'StemSnapH', (12, 13): 'StemSnapV', (12, 14): 'ForceBold',
    (12, 17): 'LanguageGroup', (12, 18): 'ExpansionFactor',
    (12, 19): 'initialRandomSeed',
}


# ---------------------------------------------------------------------------
# Low-Level Binary Helpers
# ---------------------------------------------------------------------------

def _read_card8(data, offset):
    """Read Card8 (unsigned byte)."""
    return data[offset], offset + 1


def _read_card16(data, offset):
    """Read Card16 (unsigned 16-bit big-endian)."""
    return struct.unpack_from('>H', data, offset)[0], offset + 2


def _read_offset(data, offset, off_size):
    """Read an offset of off_size bytes (1-4), big-endian unsigned."""
    if off_size == 1:
        return data[offset], offset + 1
    elif off_size == 2:
        return struct.unpack_from('>H', data, offset)[0], offset + 2
    elif off_size == 3:
        b1, b2, b3 = data[offset], data[offset + 1], data[offset + 2]
        return (b1 << 16) | (b2 << 8) | b3, offset + 3
    elif off_size == 4:
        return struct.unpack_from('>I', data, offset)[0], offset + 4
    else:
        raise CFFError(f"Invalid offSize: {off_size}")


# ---------------------------------------------------------------------------
# INDEX Parsing
# ---------------------------------------------------------------------------

def _parse_index(data, offset):
    """Parse a CFF INDEX structure.

    Returns (list_of_bytes_objects, offset_after_index).
    An empty INDEX (count=0) returns ([], offset+2).
    """
    count, offset = _read_card16(data, offset)
    if count == 0:
        return [], offset

    off_size, offset = _read_card8(data, offset)

    # Read count+1 offsets
    offsets = []
    for _ in range(count + 1):
        val, offset = _read_offset(data, offset, off_size)
        offsets.append(val)

    # Data starts at current offset; offsets are 1-based relative to byte before data
    data_start = offset - 1  # offsets[0] == 1 means first byte of data region
    items = []
    for i in range(count):
        start = data_start + offsets[i]
        end = data_start + offsets[i + 1]
        items.append(data[start:end])

    # Advance past all data
    end_offset = data_start + offsets[count]
    return items, end_offset


# ---------------------------------------------------------------------------
# DICT Parsing
# ---------------------------------------------------------------------------

def _parse_dict_data(data):
    """Parse a CFF DICT from raw bytes into {operator: operands} dict.

    Operands are accumulated before each operator. Delta arrays are NOT
    decoded here — the caller handles them per-key.
    """
    result = {}
    operands = []
    i = 0
    length = len(data)

    while i < length:
        b0 = data[i]

        if b0 <= 21:
            # Operator
            if b0 == 12:
                # Two-byte operator
                i += 1
                if i >= length:
                    break
                op = (12, data[i])
            else:
                op = b0
            result[op] = operands
            operands = []
            i += 1

        elif b0 == 28:
            # 3-byte integer
            if i + 2 >= length:
                break
            val = struct.unpack_from('>h', data, i + 1)[0]
            operands.append(val)
            i += 3

        elif b0 == 29:
            # 5-byte integer
            if i + 4 >= length:
                break
            val = struct.unpack_from('>i', data, i + 1)[0]
            operands.append(val)
            i += 5

        elif b0 == 30:
            # Real number (BCD nibble-encoded)
            i += 1
            nibbles = []
            while i < length:
                byte = data[i]
                i += 1
                n1 = (byte >> 4) & 0x0F
                n2 = byte & 0x0F
                nibbles.append(n1)
                if n1 == 0x0F:
                    break
                nibbles.append(n2)
                if n2 == 0x0F:
                    break

            # Convert BCD nibbles to float
            chars = []
            for n in nibbles:
                if n <= 9:
                    chars.append(str(n))
                elif n == 0x0A:
                    chars.append('.')
                elif n == 0x0B:
                    chars.append('E')
                elif n == 0x0C:
                    chars.append('E-')
                elif n == 0x0D:
                    # Reserved
                    pass
                elif n == 0x0E:
                    chars.append('-')
                elif n == 0x0F:
                    break  # End of number
            try:
                operands.append(float(''.join(chars)))
            except ValueError:
                operands.append(0.0)

        elif 32 <= b0 <= 246:
            operands.append(b0 - 139)
            i += 1

        elif 247 <= b0 <= 250:
            if i + 1 >= length:
                break
            b1 = data[i + 1]
            operands.append((b0 - 247) * 256 + b1 + 108)
            i += 2

        elif 251 <= b0 <= 254:
            if i + 1 >= length:
                break
            b1 = data[i + 1]
            operands.append(-(b0 - 251) * 256 - b1 - 108)
            i += 2

        else:
            # Skip unknown bytes (byte 255 is not used in DICT data)
            i += 1

    return result


def _decode_delta(values):
    """Decode a delta-encoded array: [a0, d1, d2, ...] -> [a0, a0+d1, a0+d1+d2, ...]"""
    result = []
    accum = 0
    for v in values:
        accum += v
        result.append(accum)
    return result


# ---------------------------------------------------------------------------
# SID Resolution
# ---------------------------------------------------------------------------

def _get_sid_string(sid, string_index):
    """Resolve a String ID (SID) to its string.

    SID 0-390 are predefined standard strings.
    SID >= 391 indexes into the String INDEX (offset by 391).
    """
    if sid < _NUM_STANDARD_STRINGS:
        return _STANDARD_STRINGS[sid]
    idx = sid - _NUM_STANDARD_STRINGS
    if idx < len(string_index):
        return string_index[idx].decode('latin-1', errors='replace')
    return f".sid{sid}"


# ---------------------------------------------------------------------------
# Charset Parsing
# ---------------------------------------------------------------------------

def _parse_charset(data, offset, n_glyphs, string_index):
    """Parse a charset structure. Returns list of glyph names (GID 0 = .notdef always)."""
    names = ['.notdef']

    if n_glyphs <= 1:
        return names

    fmt, offset = _read_card8(data, offset)

    if fmt == 0:
        # Format 0: array of SIDs
        for _ in range(n_glyphs - 1):
            sid, offset = _read_card16(data, offset)
            names.append(_get_sid_string(sid, string_index))

    elif fmt == 1:
        # Format 1: ranges with Card8 nLeft
        while len(names) < n_glyphs:
            first_sid, offset = _read_card16(data, offset)
            n_left, offset = _read_card8(data, offset)
            for sid in range(first_sid, first_sid + n_left + 1):
                if len(names) >= n_glyphs:
                    break
                names.append(_get_sid_string(sid, string_index))

    elif fmt == 2:
        # Format 2: ranges with Card16 nLeft
        while len(names) < n_glyphs:
            first_sid, offset = _read_card16(data, offset)
            n_left, offset = _read_card16(data, offset)
            for sid in range(first_sid, first_sid + n_left + 1):
                if len(names) >= n_glyphs:
                    break
                names.append(_get_sid_string(sid, string_index))

    return names


def _get_predefined_charset(charset_id, n_glyphs, string_index):
    """Return glyph names for a predefined charset ID."""
    if charset_id == 0:
        sids = _ISO_ADOBE_CHARSET
    elif charset_id == 1:
        sids = _EXPERT_CHARSET
    elif charset_id == 2:
        sids = _EXPERT_SUBSET_CHARSET
    else:
        return ['.notdef'] + [f'.gid{i}' for i in range(1, n_glyphs)]

    names = ['.notdef']
    for i, sid in enumerate(sids):
        if len(names) >= n_glyphs:
            break
        names.append(_get_sid_string(sid, string_index))

    # Pad if charset has fewer names than glyphs
    while len(names) < n_glyphs:
        names.append(f'.gid{len(names)}')

    return names


# ---------------------------------------------------------------------------
# Encoding Parsing
# ---------------------------------------------------------------------------

def _parse_encoding(data, offset, charset, string_index):
    """Parse an encoding structure. Returns 256-element list (code -> GID).

    The encoding maps character codes to GIDs. We build a name->GID lookup
    from the charset, then resolve encoding entries via SID->name->GID.
    """
    encoding = [0] * 256

    # Build name-to-GID mapping from charset
    name_to_gid = {}
    for gid, name in enumerate(charset):
        if name not in name_to_gid:
            name_to_gid[name] = gid

    raw_format = data[offset]
    fmt = raw_format & 0x7F
    has_supplement = (raw_format & 0x80) != 0
    offset += 1

    if fmt == 0:
        # Format 0: nCodes + code array
        n_codes = data[offset]
        offset += 1
        for gid_minus_1 in range(n_codes):
            code = data[offset]
            offset += 1
            gid = gid_minus_1 + 1
            if code < 256:
                encoding[code] = gid

    elif fmt == 1:
        # Format 1: nRanges + Range1 array
        n_ranges = data[offset]
        offset += 1
        gid = 1
        for _ in range(n_ranges):
            first_code = data[offset]
            n_left = data[offset + 1]
            offset += 2
            for code in range(first_code, first_code + n_left + 1):
                if code < 256:
                    encoding[code] = gid
                gid += 1

    # Supplemental encoding
    if has_supplement:
        n_sups = data[offset]
        offset += 1
        for _ in range(n_sups):
            code = data[offset]
            sid, _ = _read_card16(data, offset + 1)
            offset += 3
            # Resolve SID to name, then name to GID
            name = _get_sid_string(sid, string_index)
            gid = name_to_gid.get(name, 0)
            if code < 256:
                encoding[code] = gid

    return encoding


def _get_predefined_encoding(encoding_id, charset, string_index):
    """Build encoding for predefined encoding IDs (0=Standard, 1=Expert)."""
    encoding = [0] * 256

    if encoding_id == 0:
        enc_map = _STANDARD_ENCODING_MAP
    elif encoding_id == 1:
        enc_map = _EXPERT_ENCODING_MAP
    else:
        return encoding

    # Build name-to-GID from charset
    name_to_gid = {}
    for gid, name in enumerate(charset):
        if name not in name_to_gid:
            name_to_gid[name] = gid

    # Map: code -> SID -> name -> GID
    for code, sid in enc_map.items():
        name = _get_sid_string(sid, string_index)
        gid = name_to_gid.get(name, 0)
        if code < 256:
            encoding[code] = gid

    return encoding


# ---------------------------------------------------------------------------
# FDSelect Parsing (CID fonts)
# ---------------------------------------------------------------------------

def _parse_fd_select(data, offset, n_glyphs):
    """Parse FDSelect structure. Returns list of FD indices (GID-indexed)."""
    fmt, offset = _read_card8(data, offset)

    if fmt == 0:
        # Format 0: one byte per glyph
        fd_select = list(data[offset:offset + n_glyphs])
        return fd_select

    elif fmt == 3:
        # Format 3: ranges
        n_ranges, offset = _read_card16(data, offset)
        fd_select = [0] * n_glyphs
        for i in range(n_ranges):
            first_gid, offset = _read_card16(data, offset)
            fd, offset = _read_card8(data, offset)
            # Sentinel: next range's first GID (or n_glyphs at end)
            if i + 1 < n_ranges:
                next_first = struct.unpack_from('>H', data, offset)[0]
            else:
                # Read sentinel
                next_first, _ = _read_card16(data, offset)
            for gid in range(first_gid, min(next_first, n_glyphs)):
                fd_select[gid] = fd
        # Skip sentinel Card16 at end
        offset += 2
        return fd_select

    else:
        raise CFFError(f"Unknown FDSelect format: {fmt}")


# ---------------------------------------------------------------------------
# Main Parser
# ---------------------------------------------------------------------------

def parse_cff(data):
    """Parse CFF binary data into a list of CFFFont objects.

    Args:
        data: bytes — raw CFF binary data

    Returns:
        list of CFFFont objects (typically one per CFF FontSet)
    """
    if len(data) < 4:
        raise CFFError("CFF data too short for header")

    # --- Header ---
    major = data[0]
    minor = data[1]
    hdr_size = data[2]
    off_size = data[3]

    if major != 1:
        raise CFFError(f"Unsupported CFF major version: {major}")

    offset = hdr_size

    # --- Name INDEX ---
    name_index, offset = _parse_index(data, offset)

    # --- Top DICT INDEX ---
    top_dict_index, offset = _parse_index(data, offset)

    # --- String INDEX ---
    string_index, offset = _parse_index(data, offset)

    # --- Global Subr INDEX ---
    global_subr_index, offset = _parse_index(data, offset)

    # Parse each font
    fonts = []
    for font_idx in range(len(name_index)):
        font = CFFFont()
        font.raw_data = data

        # Font name
        font.name = name_index[font_idx].decode('latin-1', errors='replace')

        # Parse Top DICT
        if font_idx < len(top_dict_index):
            font.top_dict = _parse_dict_data(top_dict_index[font_idx])

        # Global subrs (shared across all fonts in FontSet)
        font.global_subrs = [bytes(s) for s in global_subr_index]

        # --- Extract Top DICT values ---

        # FontMatrix (default [0.001 0 0 0.001 0 0])
        if (12, 7) in font.top_dict:
            fm = font.top_dict[(12, 7)]
            if len(fm) == 6:
                font.font_matrix = [float(v) for v in fm]

        # FontBBox (default [0 0 0 0])
        if 5 in font.top_dict:
            bb = font.top_dict[5]
            if len(bb) == 4:
                font.font_bbox = [float(v) for v in bb]

        # CID detection (ROS operator)
        if (12, 30) in font.top_dict:
            font.is_cid = True
            ros_ops = font.top_dict[(12, 30)]
            if len(ros_ops) >= 3:
                registry = _get_sid_string(int(ros_ops[0]), string_index)
                ordering = _get_sid_string(int(ros_ops[1]), string_index)
                supplement = int(ros_ops[2])
                font.ros = (registry, ordering, supplement)

        # --- CharStrings INDEX ---
        cs_offset = int(font.top_dict.get(17, [0])[0]) if 17 in font.top_dict else 0
        if cs_offset > 0:
            cs_items, _ = _parse_index(data, cs_offset)
            font.char_strings = [bytes(s) for s in cs_items]

        n_glyphs = len(font.char_strings)

        # --- Charset ---
        charset_val = int(font.top_dict.get(15, [0])[0]) if 15 in font.top_dict else 0
        if charset_val <= 2:
            font.charset = _get_predefined_charset(charset_val, n_glyphs, string_index)
        else:
            font.charset = _parse_charset(data, charset_val, n_glyphs, string_index)

        # --- Encoding (only for name-keyed fonts) ---
        if not font.is_cid:
            enc_val = int(font.top_dict.get(16, [0])[0]) if 16 in font.top_dict else 0
            if enc_val <= 1:
                font.encoding = _get_predefined_encoding(enc_val, font.charset, string_index)
            else:
                font.encoding = _parse_encoding(data, enc_val, font.charset, string_index)

        # --- Private DICT ---
        if 18 in font.top_dict:
            priv_ops = font.top_dict[18]
            if len(priv_ops) >= 2:
                priv_size = int(priv_ops[0])
                priv_offset = int(priv_ops[1])
                if priv_size > 0 and priv_offset > 0:
                    priv_data = data[priv_offset:priv_offset + priv_size]
                    font.private_dict = _parse_dict_data(priv_data)

                    # defaultWidthX (op 20, default 0)
                    if 20 in font.private_dict:
                        vals = font.private_dict[20]
                        if vals:
                            font.default_width_x = float(vals[0])

                    # nominalWidthX (op 21, default 0)
                    if 21 in font.private_dict:
                        vals = font.private_dict[21]
                        if vals:
                            font.nominal_width_x = float(vals[0])

                    # Local Subr INDEX (offset relative to Private DICT start)
                    if 19 in font.private_dict:
                        subr_rel_offset = int(font.private_dict[19][0])
                        subr_abs_offset = priv_offset + subr_rel_offset
                        local_subrs, _ = _parse_index(data, subr_abs_offset)
                        font.local_subrs = [bytes(s) for s in local_subrs]

        # --- CID-specific: FDArray and FDSelect ---
        if font.is_cid:
            # FDArray
            if (12, 36) in font.top_dict:
                fda_offset = int(font.top_dict[(12, 36)][0])
                fd_dicts_raw, _ = _parse_index(data, fda_offset)
                for fd_raw in fd_dicts_raw:
                    fd_top = _parse_dict_data(fd_raw)
                    fd_entry = {'top_dict': fd_top, 'private_dict': {}, 'local_subrs': [],
                                'default_width_x': 0.0, 'nominal_width_x': 0.0}

                    # Each FD has its own Private DICT
                    if 18 in fd_top:
                        fd_priv_ops = fd_top[18]
                        if len(fd_priv_ops) >= 2:
                            fd_priv_size = int(fd_priv_ops[0])
                            fd_priv_offset = int(fd_priv_ops[1])
                            if fd_priv_size > 0 and fd_priv_offset > 0:
                                fd_priv_data = data[fd_priv_offset:fd_priv_offset + fd_priv_size]
                                fd_entry['private_dict'] = _parse_dict_data(fd_priv_data)

                                if 20 in fd_entry['private_dict']:
                                    vals = fd_entry['private_dict'][20]
                                    if vals:
                                        fd_entry['default_width_x'] = float(vals[0])
                                if 21 in fd_entry['private_dict']:
                                    vals = fd_entry['private_dict'][21]
                                    if vals:
                                        fd_entry['nominal_width_x'] = float(vals[0])

                                # FD-level local subrs
                                if 19 in fd_entry['private_dict']:
                                    subr_rel = int(fd_entry['private_dict'][19][0])
                                    subr_abs = fd_priv_offset + subr_rel
                                    fd_local, _ = _parse_index(data, subr_abs)
                                    fd_entry['local_subrs'] = [bytes(s) for s in fd_local]

                    font.fd_array.append(fd_entry)

            # FDSelect
            if (12, 37) in font.top_dict:
                fds_offset = int(font.top_dict[(12, 37)][0])
                font.fd_select = _parse_fd_select(data, fds_offset, n_glyphs)

        fonts.append(font)

    return fonts
