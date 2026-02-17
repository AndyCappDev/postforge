# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unicode Mapping Module

Maps PostScript glyph names to Unicode characters using the Adobe Glyph List (AGL).
Used by PDF device to generate searchable text from glyph names.

Reference: https://github.com/adobe-type-tools/agl-aglfn
"""

# Adobe Glyph List (AGL) mapping - subset of commonly used glyph names
# Format: glyph_name (bytes) -> Unicode character (str)
GLYPH_TO_UNICODE = {
    # Basic punctuation and symbols
    b'space': ' ',
    b'exclam': '!',
    b'quotedbl': '"',
    b'numbersign': '#',
    b'dollar': '$',
    b'percent': '%',
    b'ampersand': '&',
    b'quotesingle': "'",
    b'parenleft': '(',
    b'parenright': ')',
    b'asterisk': '*',
    b'plus': '+',
    b'comma': ',',
    b'hyphen': '-',
    b'period': '.',
    b'slash': '/',

    # Digits
    b'zero': '0',
    b'one': '1',
    b'two': '2',
    b'three': '3',
    b'four': '4',
    b'five': '5',
    b'six': '6',
    b'seven': '7',
    b'eight': '8',
    b'nine': '9',

    # More punctuation
    b'colon': ':',
    b'semicolon': ';',
    b'less': '<',
    b'equal': '=',
    b'greater': '>',
    b'question': '?',
    b'at': '@',

    # Uppercase letters
    b'A': 'A', b'B': 'B', b'C': 'C', b'D': 'D', b'E': 'E',
    b'F': 'F', b'G': 'G', b'H': 'H', b'I': 'I', b'J': 'J',
    b'K': 'K', b'L': 'L', b'M': 'M', b'N': 'N', b'O': 'O',
    b'P': 'P', b'Q': 'Q', b'R': 'R', b'S': 'S', b'T': 'T',
    b'U': 'U', b'V': 'V', b'W': 'W', b'X': 'X', b'Y': 'Y',
    b'Z': 'Z',

    # More punctuation
    b'bracketleft': '[',
    b'backslash': '\\',
    b'bracketright': ']',
    b'asciicircum': '^',
    b'underscore': '_',
    b'grave': '`',

    # Lowercase letters
    b'a': 'a', b'b': 'b', b'c': 'c', b'd': 'd', b'e': 'e',
    b'f': 'f', b'g': 'g', b'h': 'h', b'i': 'i', b'j': 'j',
    b'k': 'k', b'l': 'l', b'm': 'm', b'n': 'n', b'o': 'o',
    b'p': 'p', b'q': 'q', b'r': 'r', b's': 's', b't': 't',
    b'u': 'u', b'v': 'v', b'w': 'w', b'x': 'x', b'y': 'y',
    b'z': 'z',

    # More punctuation
    b'braceleft': '{',
    b'bar': '|',
    b'braceright': '}',
    b'asciitilde': '~',

    # Extended Latin characters
    b'Agrave': '\u00c0',
    b'Aacute': '\u00c1',
    b'Acircumflex': '\u00c2',
    b'Atilde': '\u00c3',
    b'Adieresis': '\u00c4',
    b'Aring': '\u00c5',
    b'AE': '\u00c6',
    b'Ccedilla': '\u00c7',
    b'Egrave': '\u00c8',
    b'Eacute': '\u00c9',
    b'Ecircumflex': '\u00ca',
    b'Edieresis': '\u00cb',
    b'Igrave': '\u00cc',
    b'Iacute': '\u00cd',
    b'Icircumflex': '\u00ce',
    b'Idieresis': '\u00cf',
    b'Eth': '\u00d0',
    b'Ntilde': '\u00d1',
    b'Ograve': '\u00d2',
    b'Oacute': '\u00d3',
    b'Ocircumflex': '\u00d4',
    b'Otilde': '\u00d5',
    b'Odieresis': '\u00d6',
    b'multiply': '\u00d7',
    b'Oslash': '\u00d8',
    b'Ugrave': '\u00d9',
    b'Uacute': '\u00da',
    b'Ucircumflex': '\u00db',
    b'Udieresis': '\u00dc',
    b'Yacute': '\u00dd',
    b'Thorn': '\u00de',
    b'germandbls': '\u00df',

    b'agrave': '\u00e0',
    b'aacute': '\u00e1',
    b'acircumflex': '\u00e2',
    b'atilde': '\u00e3',
    b'adieresis': '\u00e4',
    b'aring': '\u00e5',
    b'ae': '\u00e6',
    b'ccedilla': '\u00e7',
    b'egrave': '\u00e8',
    b'eacute': '\u00e9',
    b'ecircumflex': '\u00ea',
    b'edieresis': '\u00eb',
    b'igrave': '\u00ec',
    b'iacute': '\u00ed',
    b'icircumflex': '\u00ee',
    b'idieresis': '\u00ef',
    b'eth': '\u00f0',
    b'ntilde': '\u00f1',
    b'ograve': '\u00f2',
    b'oacute': '\u00f3',
    b'ocircumflex': '\u00f4',
    b'otilde': '\u00f5',
    b'odieresis': '\u00f6',
    b'divide': '\u00f7',
    b'oslash': '\u00f8',
    b'ugrave': '\u00f9',
    b'uacute': '\u00fa',
    b'ucircumflex': '\u00fb',
    b'udieresis': '\u00fc',
    b'yacute': '\u00fd',
    b'thorn': '\u00fe',
    b'ydieresis': '\u00ff',

    # Common typographic characters
    b'bullet': '\u2022',
    b'ellipsis': '\u2026',
    b'endash': '\u2013',
    b'emdash': '\u2014',
    b'quoteleft': '\u2018',
    b'quoteright': '\u2019',
    b'quotedblleft': '\u201c',
    b'quotedblright': '\u201d',
    b'dagger': '\u2020',
    b'daggerdbl': '\u2021',
    b'perthousand': '\u2030',
    b'guilsinglleft': '\u2039',
    b'guilsinglright': '\u203a',
    b'fraction': '\u2044',
    b'trademark': '\u2122',
    b'ff': '\ufb00',
    b'fi': '\ufb01',
    b'fl': '\ufb02',
    b'ffi': '\ufb03',
    b'ffl': '\ufb04',

    # Currency symbols
    b'cent': '\u00a2',
    b'sterling': '\u00a3',
    b'currency': '\u00a4',
    b'yen': '\u00a5',
    b'Euro': '\u20ac',

    # Other common symbols
    b'section': '\u00a7',
    b'copyright': '\u00a9',
    b'registered': '\u00ae',
    b'degree': '\u00b0',
    b'plusminus': '\u00b1',
    b'paragraph': '\u00b6',
    b'periodcentered': '\u00b7',
    b'mu': '\u00b5',
    b'onequarter': '\u00bc',
    b'onehalf': '\u00bd',
    b'threequarters': '\u00be',
    b'questiondown': '\u00bf',
    b'exclamdown': '\u00a1',
    b'guillemotleft': '\u00ab',
    b'guillemotright': '\u00bb',
    b'ordfeminine': '\u00aa',
    b'ordmasculine': '\u00ba',
    b'logicalnot': '\u00ac',
    b'brokenbar': '\u00a6',
    b'acute': '\u00b4',
    b'dieresis': '\u00a8',
    b'macron': '\u00af',
    b'cedilla': '\u00b8',

    # Superscripts
    b'onesuperior': '\u00b9',
    b'twosuperior': '\u00b2',
    b'threesuperior': '\u00b3',

    # Math symbols
    b'minus': '\u2212',
    b'infinity': '\u221e',
    b'notequal': '\u2260',
    b'lessequal': '\u2264',
    b'greaterequal': '\u2265',
    b'approxequal': '\u2248',
    b'summation': '\u2211',
    b'product': '\u220f',
    b'radical': '\u221a',
    b'partialdiff': '\u2202',
    b'integral': '\u222b',

    # Greek letters (commonly used in math/science)
    b'Alpha': '\u0391',
    b'Beta': '\u0392',
    b'Gamma': '\u0393',
    b'Delta': '\u0394',
    b'Epsilon': '\u0395',
    b'Zeta': '\u0396',
    b'Eta': '\u0397',
    b'Theta': '\u0398',
    b'Iota': '\u0399',
    b'Kappa': '\u039a',
    b'Lambda': '\u039b',
    b'Mu': '\u039c',
    b'Nu': '\u039d',
    b'Xi': '\u039e',
    b'Omicron': '\u039f',
    b'Pi': '\u03a0',
    b'Rho': '\u03a1',
    b'Sigma': '\u03a3',
    b'Tau': '\u03a4',
    b'Upsilon': '\u03a5',
    b'Phi': '\u03a6',
    b'Chi': '\u03a7',
    b'Psi': '\u03a8',
    b'Omega': '\u03a9',

    b'alpha': '\u03b1',
    b'beta': '\u03b2',
    b'gamma': '\u03b3',
    b'delta': '\u03b4',
    b'epsilon': '\u03b5',
    b'zeta': '\u03b6',
    b'eta': '\u03b7',
    b'theta': '\u03b8',
    b'iota': '\u03b9',
    b'kappa': '\u03ba',
    b'lambda': '\u03bb',
    # b'mu': already defined above as micro sign
    b'nu': '\u03bd',
    b'xi': '\u03be',
    b'omicron': '\u03bf',
    b'pi': '\u03c0',
    b'rho': '\u03c1',
    b'sigma': '\u03c3',
    b'tau': '\u03c4',
    b'upsilon': '\u03c5',
    b'phi': '\u03c6',
    b'chi': '\u03c7',
    b'psi': '\u03c8',
    b'omega': '\u03c9',

    # Special notdef
    b'.notdef': '\ufffd',
}


def glyph_name_to_unicode(glyph_name: bytes) -> str:
    """
    Map PostScript glyph name to Unicode character.

    Args:
        glyph_name: Glyph name as bytes (e.g., b'A', b'space', b'Agrave')

    Returns:
        Unicode character string. Returns replacement character for unknown glyphs.

    Supports:
    - Direct AGL lookup
    - uniXXXX format (e.g., uni0041 = 'A')
    - Single-character names (treated as the character itself)
    """
    # Ensure we have bytes
    if isinstance(glyph_name, str):
        glyph_name = glyph_name.encode('latin-1')

    # Direct lookup in AGL
    if glyph_name in GLYPH_TO_UNICODE:
        return GLYPH_TO_UNICODE[glyph_name]

    # uniXXXX format (e.g., uni0041 = 'A')
    if glyph_name.startswith(b'uni') and len(glyph_name) == 7:
        try:
            code_point = int(glyph_name[3:], 16)
            return chr(code_point)
        except ValueError:
            pass

    # uXXXX or uXXXXX format (e.g., u0041, u1F600)
    if glyph_name.startswith(b'u') and len(glyph_name) in (5, 6):
        try:
            code_point = int(glyph_name[1:], 16)
            return chr(code_point)
        except ValueError:
            pass

    # GhostScript cXX format (e.g., c2f = character at 0x2F)
    if glyph_name.startswith(b'c') and len(glyph_name) == 3:
        try:
            code_point = int(glyph_name[1:], 16)
            return chr(code_point)
        except ValueError:
            pass

    # Single character name is often the character itself
    if len(glyph_name) == 1:
        return glyph_name.decode('latin-1')

    # Unknown - return replacement character
    return '\ufffd'


def text_to_unicode(text_bytes: bytes, font_dict) -> str:
    """
    Convert PostScript text bytes to Unicode string using font's encoding.

    Args:
        text_bytes: Character codes from show operation
        font_dict: PostScript font dictionary containing Encoding array

    Returns:
        Unicode string for searchable text
    """
    from ..operators.font_ops import _get_glyph_name

    result = []
    for char_code in text_bytes:
        glyph_name = _get_glyph_name(font_dict, char_code)
        unicode_char = glyph_name_to_unicode(glyph_name)
        result.append(unicode_char)

    return ''.join(result)
