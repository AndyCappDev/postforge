# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
Font Embedder Module

Reconstructs Type 1 fonts from parsed PostScript font dictionaries for PDF embedding.
This works for fonts loaded from files OR embedded in PostScript documents.
"""

from ...core import types as ps
from ...core.unicode_mapping import glyph_name_to_unicode

# StandardEncoding: index -> glyph name (bytes), non-.notdef entries only.
# Used to resolve seac bchar/achar indices to glyph names during subsetting.
# Derived from resources/Encoding/StandardEncoding.ps
_STANDARD_ENCODING = {
    32: b'space', 33: b'exclam', 34: b'quotedbl', 35: b'numbersign',
    36: b'dollar', 37: b'percent', 38: b'ampersand', 39: b'quoteright',
    40: b'parenleft', 41: b'parenright', 42: b'asterisk', 43: b'plus',
    44: b'comma', 45: b'hyphen', 46: b'period', 47: b'slash',
    48: b'zero', 49: b'one', 50: b'two', 51: b'three',
    52: b'four', 53: b'five', 54: b'six', 55: b'seven',
    56: b'eight', 57: b'nine', 58: b'colon', 59: b'semicolon',
    60: b'less', 61: b'equal', 62: b'greater', 63: b'question',
    64: b'at', 65: b'A', 66: b'B', 67: b'C',
    68: b'D', 69: b'E', 70: b'F', 71: b'G',
    72: b'H', 73: b'I', 74: b'J', 75: b'K',
    76: b'L', 77: b'M', 78: b'N', 79: b'O',
    80: b'P', 81: b'Q', 82: b'R', 83: b'S',
    84: b'T', 85: b'U', 86: b'V', 87: b'W',
    88: b'X', 89: b'Y', 90: b'Z', 91: b'bracketleft',
    92: b'backslash', 93: b'bracketright', 94: b'asciicircum', 95: b'underscore',
    96: b'quoteleft', 97: b'a', 98: b'b', 99: b'c',
    100: b'd', 101: b'e', 102: b'f', 103: b'g',
    104: b'h', 105: b'i', 106: b'j', 107: b'k',
    108: b'l', 109: b'm', 110: b'n', 111: b'o',
    112: b'p', 113: b'q', 114: b'r', 115: b's',
    116: b't', 117: b'u', 118: b'v', 119: b'w',
    120: b'x', 121: b'y', 122: b'z', 123: b'braceleft',
    124: b'bar', 125: b'braceright', 126: b'asciitilde',
    161: b'exclamdown', 162: b'cent', 163: b'sterling',
    164: b'fraction', 165: b'yen', 166: b'florin', 167: b'section',
    168: b'currency', 169: b'quotesingle', 170: b'quotedblleft',
    171: b'guillemotleft', 172: b'guilsinglleft', 173: b'guilsinglright',
    174: b'fi', 175: b'fl',
    177: b'endash', 178: b'dagger', 179: b'daggerdbl',
    180: b'periodcentered', 182: b'paragraph', 183: b'bullet',
    184: b'quotesinglbase', 185: b'quotedblbase', 186: b'quotedblright',
    187: b'guillemotright', 188: b'ellipsis', 189: b'perthousand',
    191: b'questiondown',
    193: b'grave', 194: b'acute', 195: b'circumflex',
    196: b'tilde', 197: b'macron', 198: b'breve', 199: b'dotaccent',
    200: b'dieresis', 202: b'ring', 203: b'cedilla',
    205: b'hungarumlaut', 206: b'ogonek', 207: b'caron',
    208: b'emdash',
    225: b'AE', 227: b'ordfeminine',
    232: b'Lslash', 233: b'Oslash', 234: b'OE', 235: b'ordmasculine',
    241: b'ae', 245: b'dotlessi',
    248: b'lslash', 249: b'oslash', 250: b'oe', 251: b'germandbls',
}


class FontEmbedder:
    """
    Reconstruct Type 1 fonts from PostScript font dictionaries for PDF embedding.
    """

    def __init__(self) -> None:
        """Initialize font embedder."""
        # eexec encryption constants
        self.EEXEC_R = 55665
        self.EEXEC_C1 = 52845
        self.EEXEC_C2 = 22719
        self.EEXEC_RANDOM_BYTES = 4

    def get_font_file_data(self, font_dict: ps.Dict,
                           unique_font_name: str | bytes | None = None,
                           glyphs_used: set[int] | None = None,
                           subrs_override: object = None) -> tuple[bytes, int, int, int] | None:
        """
        Reconstruct Type 1 font file data from font dictionary.

        Args:
            font_dict: PostScript font dictionary
            unique_font_name: Optional unique name for the font (bytes).
                              If not provided, uses FontName from font_dict.
                              Use this when embedding multiple instances of the
                              same base font to avoid name conflicts.
            glyphs_used: Optional set of character codes used from this font.
                         If provided, only CharStrings for these glyphs (plus
                         .notdef) are included, reducing font size.
            subrs_override: Optional Subrs array to use instead of the font's own.
                            Used when re-encoded font instances have truncated Subrs.

        Returns:
            tuple: (font_data, length1, length2, length3) where:
                   - font_data: Complete Type 1 font file data (bytes)
                   - length1: Length of clear text portion
                   - length2: Length of encrypted portion
                   - length3: Length of footer
                   Or None if reconstruction failed
        """
        font_type = font_dict.val.get(b'FontType', ps.Int(1)).val
        if font_type != 1:
            return None

        try:
            # Build the three sections of a Type 1 font
            clear_text = self._build_clear_text(font_dict, unique_font_name)
            encrypted = self._build_encrypted_section(font_dict, unique_font_name, glyphs_used, subrs_override)
            footer = self._build_footer()

            font_data = clear_text + encrypted + footer

            return (font_data, len(clear_text), len(encrypted), len(footer))
        except Exception as e:
            # Font reconstruction failed
            import traceback
            print(f"[FontEmbed] FAILED: {e}")
            traceback.print_exc()
            return None

    def _build_clear_text(self, font_dict: ps.Dict,
                           unique_font_name: str | bytes | None = None) -> bytes:
        """Build the clear text (unencrypted) portion of the font."""
        lines = []

        # Header - use unique name if provided, otherwise get from font_dict
        if unique_font_name:
            font_name = unique_font_name if isinstance(unique_font_name, bytes) else unique_font_name.encode('latin-1')
        else:
            font_name = self._get_font_name_bytes(font_dict)
        lines.append(b'%!PS-AdobeFont-1.0: ' + font_name + b' 001.000')

        # Font dictionary setup - must come BEFORE FontInfo
        lines.append(b'11 dict begin')

        # FontInfo dict (if available) - must be INSIDE the font dict
        font_info = font_dict.val.get(b'FontInfo')
        if font_info and font_info.TYPE == ps.T_DICT:
            lines.append(self._build_font_info(font_info))

        # FontName
        lines.append(b'/FontName /' + font_name + b' def')

        # FontType
        lines.append(b'/FontType 1 def')

        # FontMatrix - always use standard [0.001 0 0 0.001 0 0] for embedded fonts
        # The font dict may have a scaled FontMatrix from scalefont, but for embedding
        # we need the original unscaled matrix. PDF handles scaling via the text matrix.
        # CharString coordinates are in 1000-unit-per-em character space.
        lines.append(b'/FontMatrix [0.001 0 0 0.001 0 0] readonly def')

        # FontBBox
        bbox = font_dict.val.get(b'FontBBox')
        if bbox and bbox.TYPE in ps.ARRAY_TYPES:
            bbox_str = self._array_to_string(bbox)
            lines.append(b'/FontBBox ' + bbox_str + b' readonly def')
        else:
            lines.append(b'/FontBBox {0 0 1000 1000} readonly def')

        # PaintType
        paint_type = font_dict.val.get(b'PaintType', ps.Int(0))
        lines.append(b'/PaintType ' + str(paint_type.val).encode() + b' def')

        # Encoding
        encoding = font_dict.val.get(b'Encoding')
        if encoding:
            lines.append(self._build_encoding(encoding))
        else:
            lines.append(b'/Encoding StandardEncoding def')

        # End of clear text, start eexec
        lines.append(b'currentdict end')
        lines.append(b'currentfile eexec')

        return b'\n'.join(lines) + b'\n'

    def _build_encrypted_section(self, font_dict: ps.Dict,
                                   unique_font_name: str | bytes | None = None,
                                   glyphs_used: set[int] | None = None,
                                   subrs_override: object = None) -> bytes:
        """Build and encrypt the Private dict and CharStrings."""
        # Note: unique_font_name is not used here - the eexec section uses
        # "dup /FontName get exch definefont" which reads from the dict
        # built in the clear text section.
        # Build the plaintext content that goes inside eexec
        plaintext = self._build_private_and_charstrings(font_dict, glyphs_used, subrs_override)

        # Encrypt with eexec
        encrypted = self._eexec_encrypt(plaintext)

        # Return binary eexec data directly.
        # PDF FontFile streams expect PFB-style binary eexec when no
        # ASCIIHexDecode filter is declared on the stream.
        return encrypted

    def _build_private_and_charstrings(self, font_dict: ps.Dict,
                                        glyphs_used: set[int] | None = None,
                                        subrs_override: object = None) -> bytes:
        """Build the Private dict and CharStrings as plaintext (to be encrypted)."""
        lines = []

        # dup for definefont - must be on its own line per Type 1 convention
        lines.append(b'dup')
        lines.append(b'/Private 17 dict dup begin')

        # Required Type 1 font procedures - MUST be defined before Subrs/CharStrings
        # RD = read string data from currentfile
        # ND = noaccess def (for CharStrings)
        # NP = noaccess put (for Subrs)
        lines.append(b'/RD {string currentfile exch readstring pop} executeonly def')
        lines.append(b'/ND {noaccess def} executeonly def')
        lines.append(b'/NP {noaccess put} executeonly def')

        # Get Private dict
        private = font_dict.val.get(b'Private')
        if private and private.TYPE == ps.T_DICT:
            # Output ALL Private dict entries except those we handle specially
            skip_keys = {b'RD', b'ND', b'NP', b'Subrs', b'-|', b'|-', b'|', b'OtherSubrs'}
            for key, val in private.val.items():
                # Keys may be Name objects or bytes
                key_bytes = key.val if hasattr(key, 'val') else key
                if key_bytes not in skip_keys:
                    lines.append(b'/' + key_bytes + b' ' + self._value_to_bytes(val) + b' def')

            # Use standard minimal OtherSubrs - PostForge may corrupt the original during parsing
            # For PDF embedding, OtherSubrs aren't used (PDF viewers handle rendering)
            lines.append(b'/OtherSubrs [{}{}{}{systemdict /internaldict known not {pop 3} '
                        b'{1183615869 systemdict /internaldict get exec dup /startlock known '
                        b'{/startlock get exec} {dup /strtlck known {/strtlck get exec} '
                        b'{pop 3} ifelse} ifelse} ifelse}] def')

            # Subrs - use override if provided (fixes truncated Subrs in
            # DVIPS re-encoded font instances)
            subrs = subrs_override if subrs_override else private.val.get(b'Subrs')
            if subrs and subrs.TYPE in ps.ARRAY_TYPES:
                lines.append(self._build_subrs(subrs))

        else:
            # Minimal Private dict
            lines.append(b'/MinFeature {16 16} def')
            lines.append(b'/password 5839 def')

        # CharStrings - must be defined while still inside Private (so RD/ND/NP are in scope)
        # Use "2 index" to bring font dict to top of stack for the put operations
        char_strings = font_dict.val.get(b'CharStrings')
        if char_strings and char_strings.TYPE == ps.T_DICT:
            # Determine which glyph names to include (subsetting)
            needed_names = None
            if glyphs_used is not None:
                needed_names = self._get_needed_glyph_names(font_dict, glyphs_used)

            # Count actual entries that will be emitted (intersection of
            # needed_names and CharStrings keys that have string data)
            if needed_names is not None:
                count = sum(1 for name, cs in char_strings.val.items()
                            if cs.TYPE == ps.T_STRING and name in needed_names)
            else:
                count = sum(1 for cs in char_strings.val.values()
                            if cs.TYPE == ps.T_STRING)
            lines.append(b'2 index /CharStrings ' +
                        str(count).encode() + b' dict dup begin')
            lines.append(self._build_charstrings_entries(char_strings, needed_names))
            lines.append(b'end')  # end CharStrings dict

        lines.append(b'end')  # end Private dict
        lines.append(b'readonly put')  # Make CharStrings readonly and put in font dict
        lines.append(b'noaccess put')  # Make Private noaccess and put in font dict
        lines.append(b'dup /FontName get exch definefont pop')
        lines.append(b'mark currentfile closefile')

        return b'\n'.join(lines)

    def _build_subrs(self, subrs: object) -> bytes:
        """Build Subrs array."""
        lines = []
        count = len(subrs.val)
        lines.append(f'/Subrs {count} array'.encode())

        for i, subr in enumerate(subrs.val):
            if subr.TYPE == ps.T_STRING:
                # Subrs are charstring-encrypted, get the raw bytes
                data = self._get_string_bytes(subr)
                # Format: dup index length RD <space><binary>NP
                lines.append(f'dup {i} {len(data)} RD '.encode() + data + b'NP')

        lines.append(b'ND')
        return b'\n'.join(lines)

    def _build_charstrings_entries(self, char_strings: ps.Dict,
                                      needed_names: set[bytes] | None = None) -> bytes:
        """
        Build CharStrings entries (without dict wrapper).

        .notdef is always emitted first - this is required because certain
        CharStrings orderings produce binary eexec byte patterns that confuse
        some PDF parsers (notably Ghostscript).

        Args:
            char_strings: PostScript CharStrings dictionary
            needed_names: Optional set of glyph name bytes to include.
                          If None, all glyphs are included.
        """
        lines = []

        def _emit(name, cs):
            data = self._get_string_bytes(cs)
            name_str = name.decode('latin-1') if isinstance(name, bytes) else str(name)
            if name_str.startswith('/'):
                name_str = name_str[1:]
            lines.append(f'/{name_str} {len(data)} RD '.encode() + data + b'ND')

        # Emit .notdef first (required for reliable binary eexec parsing)
        notdef = char_strings.val.get(b'.notdef')
        if notdef and notdef.TYPE == ps.T_STRING:
            if needed_names is None or b'.notdef' in needed_names:
                _emit(b'.notdef', notdef)

        for name, cs in char_strings.val.items():
            if name == b'.notdef':
                continue  # Already emitted above
            if cs.TYPE == ps.T_STRING:
                if needed_names is not None and name not in needed_names:
                    continue
                _emit(name, cs)

        return b'\n'.join(lines)

    def _get_needed_glyph_names(self, font_dict: ps.Dict,
                                  glyphs_used: set[int]) -> set[bytes]:
        """
        Get the set of glyph names needed for subsetting.

        Maps used character codes through the font's Encoding to glyph names.
        Always includes .notdef (required by the Type 1 spec).

        Args:
            font_dict: PostScript font dictionary
            glyphs_used: Set of character codes used

        Returns:
            set: Glyph name bytes to include in the subsetted font
        """
        encoding = font_dict.val.get(b'Encoding')
        needed = {b'.notdef'}

        for char_code in glyphs_used:
            glyph_name = self._get_glyph_name_for_code(encoding, char_code)
            if glyph_name:
                needed.add(glyph_name)

        self._resolve_seac_dependencies(font_dict, needed)

        return needed

    def _resolve_seac_dependencies(self, font_dict: ps.Dict,
                                      needed_names: set[bytes]) -> None:
        """
        Add base and accent glyphs referenced by seac instructions to the subset.

        Iterates over CharStrings in the needed set, scanning each for seac
        instructions. Any new glyph names found are added. Loops until no new
        dependencies are found.

        Args:
            font_dict: PostScript font dictionary
            needed_names: Mutable set of glyph name bytes (modified in place)
        """
        char_strings = font_dict.val.get(b'CharStrings')
        if not char_strings or char_strings.TYPE != ps.T_DICT:
            return

        # Get lenIV from Private dict (default 4)
        private = font_dict.val.get(b'Private')
        len_iv = 4
        if private and private.TYPE == ps.T_DICT:
            len_iv_val = private.val.get(b'lenIV')
            if len_iv_val and len_iv_val.TYPE == ps.T_INT:
                len_iv = len_iv_val.val

        # Iterate until stable (handles transitive deps, though seac-in-seac
        # is forbidden by the Type 1 spec)
        while True:
            new_deps = set()
            for name in list(needed_names):
                cs = char_strings.val.get(name)
                if cs is None or cs.TYPE != ps.T_STRING:
                    continue
                raw = self._get_string_bytes(cs)
                deps = self._find_seac_dependencies(raw, len_iv)
                for dep in deps:
                    if dep not in needed_names and dep in char_strings.val:
                        new_deps.add(dep)
            if not new_deps:
                break
            needed_names.update(new_deps)

    def _find_seac_dependencies(self, encrypted_charstring: bytes,
                                  len_iv: int = 4) -> set[bytes]:
        """
        Find glyph dependencies from seac instructions in a CharString.

        Parses a decrypted CharString to find seac (opcode 12,6) and extract
        the bchar/achar StandardEncoding indices, returning glyph names.

        Args:
            encrypted_charstring: Encrypted CharString bytes
            len_iv: Number of random bytes to skip (default 4)

        Returns:
            set: Glyph name bytes referenced by seac instructions
        """
        data = self._decrypt_charstring(encrypted_charstring, len_iv)
        if data is None:
            return set()

        deps = set()
        stack = []
        i = 0
        while i < len(data):
            b = data[i]
            i += 1

            if b == 12 and i < len(data):  # Two-byte command
                b2 = data[i]
                i += 1
                if b2 == 6:  # seac: asb adx ady bchar achar
                    if len(stack) >= 5:
                        achar = int(stack[-1])
                        bchar = int(stack[-2])
                        aname = _STANDARD_ENCODING.get(achar)
                        bname = _STANDARD_ENCODING.get(bchar)
                        if aname:
                            deps.add(aname)
                        if bname:
                            deps.add(bname)
                    return deps  # seac ends the CharString
                elif b2 == 7:  # sbw: 4 args
                    del stack[-4:]
                elif b2 == 12:  # div: 2 -> 1
                    if len(stack) >= 2:
                        divisor = stack.pop()
                        dividend = stack.pop()
                        stack.append(dividend / divisor if divisor != 0 else 0)
                elif b2 == 16:  # callothersubr: variable
                    if len(stack) >= 2:
                        stack.pop()  # subr_num
                        n_args = int(stack.pop())
                        for _ in range(min(n_args, len(stack))):
                            stack.pop()
                elif b2 == 17:  # pop: 0 -> 1
                    stack.append(0)
                elif b2 in (1, 2):  # vstem3(6), hstem3(6)
                    del stack[-6:]
                elif b2 == 33:  # setcurrentpoint: 2 args
                    del stack[-2:]

            elif b == 14:  # endchar
                return deps
            elif b == 13:  # hsbw: 2 args
                del stack[-2:]
            elif b in (1, 3):  # hstem(2), vstem(2)
                del stack[-2:]
            elif b in (5,):  # rlineto: 2
                del stack[-2:]
            elif b in (4, 6, 7):  # vmoveto(1), hlineto(1), vlineto(1)
                del stack[-1:]
            elif b == 8:  # rrcurveto: 6
                del stack[-6:]
            elif b == 9:  # closepath: 0
                pass
            elif b == 10:  # callsubr - skip (we don't follow into subrs here)
                if stack:
                    stack.pop()
            elif b == 11:  # return
                return deps

            elif 32 <= b <= 246:
                stack.append(b - 139)
            elif 247 <= b <= 250:
                if i < len(data):
                    stack.append((b - 247) * 256 + data[i] + 108)
                    i += 1
            elif 251 <= b <= 254:
                if i < len(data):
                    stack.append(-(b - 251) * 256 - data[i] - 108)
                    i += 1
            elif b == 255:
                if i + 3 < len(data):
                    val = (data[i] << 24) | (data[i+1] << 16) | (data[i+2] << 8) | data[i+3]
                    if val >= 0x80000000:
                        val -= 0x100000000
                    stack.append(val)
                    i += 4

        return deps

    def _build_footer(self) -> bytes:
        """Build the font file footer."""
        # 512 ASCII zeros followed by cleartomark per PDF spec.
        # The zeros are the character '0' (0x30), not null bytes.
        return b'0' * 512 + b'\ncleartomark\n'

    def _build_font_info(self, font_info: ps.Dict) -> bytes:
        """Build FontInfo dictionary."""
        lines = [b'/FontInfo 10 dict dup begin']

        for key in [b'version', b'Notice', b'Copyright', b'FullName',
                    b'FamilyName', b'Weight', b'ItalicAngle', b'isFixedPitch',
                    b'UnderlinePosition', b'UnderlineThickness']:
            val = font_info.val.get(key)
            if val is not None:
                lines.append(b'/' + key + b' ' + self._value_to_bytes(val) + b' def')

        lines.append(b'end readonly def')
        return b'\n'.join(lines)

    def _build_encoding(self, encoding: object) -> bytes:
        """Build Encoding array."""
        if encoding.TYPE == ps.T_NAME:
            # Named encoding like StandardEncoding
            return b'/Encoding ' + encoding.val + b' def'

        if encoding.TYPE not in ps.ARRAY_TYPES:
            return b'/Encoding StandardEncoding def'

        # Custom encoding array
        lines = [b'/Encoding 256 array']
        lines.append(b'0 1 255 {1 index exch /.notdef put} for')

        for i, elem in enumerate(encoding.val):
            if elem.TYPE == ps.T_NAME and elem.val != b'.notdef':
                lines.append(f'dup {i} /{elem.val.decode("latin-1")} put'.encode())

        lines.append(b'readonly def')
        return b'\n'.join(lines)

    def _eexec_encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt data using Adobe eexec encryption."""
        # Use zeros for the prefix bytes (same as original URW fonts and Adobe examples).
        # Random bytes can cause parsing issues with some interpreters.
        prefix_bytes = bytes(self.EEXEC_RANDOM_BYTES)  # All zeros
        data = prefix_bytes + plaintext

        R = self.EEXEC_R
        encrypted = bytearray()

        for plain_byte in data:
            cipher_byte = plain_byte ^ (R >> 8)
            encrypted.append(cipher_byte)
            R = ((cipher_byte + R) * self.EEXEC_C1 + self.EEXEC_C2) & 0xFFFF

        return bytes(encrypted)

    def _get_font_name_bytes(self, font_dict: ps.Dict) -> bytes:
        """Get font name as bytes."""
        font_name = font_dict.val.get(b'FontName', ps.Name(b'Unknown'))
        if font_name.TYPE == ps.T_NAME:
            return font_name.val
        return b'Unknown'

    def _get_string_bytes(self, string_obj: object) -> bytes:
        """Get raw bytes from a PostScript string object."""
        # PostForge String objects use byte_string() method
        if hasattr(string_obj, 'byte_string'):
            try:
                return string_obj.byte_string()
            except Exception:
                pass

        # Fallback for simple mock objects (used in tests)
        if hasattr(string_obj, 'val'):
            val = string_obj.val
            if isinstance(val, bytes):
                return val
            elif isinstance(val, str):
                return val.encode('latin-1')
            elif isinstance(val, bytearray):
                return bytes(val)
        return b''

    def _value_to_bytes(self, val: object) -> bytes:
        """Convert a PostScript value to its string representation as bytes."""
        if val.TYPE == ps.T_INT:
            return str(val.val).encode()
        elif val.TYPE == ps.T_REAL:
            return str(val.val).encode()
        elif val.TYPE == ps.T_BOOL:
            return b'true' if val.val else b'false'
        elif val.TYPE == ps.T_STRING:
            s = self._get_string_bytes(val)
            return b'(' + s.replace(b'\\', b'\\\\').replace(b'(', b'\\(').replace(b')', b'\\)') + b')'
        elif val.TYPE == ps.T_NAME:
            # Executable names have no slash, literal names have /
            if hasattr(val, 'attrib') and val.attrib == ps.ATTRIB_EXEC:
                return val.val
            return b'/' + val.val
        elif val.TYPE in ps.ARRAY_TYPES:
            return self._array_to_string(val)
        return b''

    def _array_to_string(self, arr: object) -> bytes:
        """Convert a PostScript array to its string representation."""
        if not arr.TYPE in ps.ARRAY_TYPES:
            return b'[]'
        parts = []
        for elem in arr.val:
            parts.append(self._value_to_bytes(elem))
        # Use {} for executable arrays, [] for literal arrays
        if hasattr(arr, 'attrib') and arr.attrib == ps.ATTRIB_EXEC:
            return b'{' + b' '.join(parts) + b'}'
        return b'[' + b' '.join(parts) + b']'

    def get_glyph_widths(self, font_dict: ps.Dict,
                          glyphs_used: set[int]) -> dict[int, int]:
        """
        Get character widths for all used glyphs.

        Args:
            font_dict: PostScript font dictionary
            glyphs_used: Set of character codes used

        Returns:
            dict: char_code -> width (in PDF units, 1000 per em)
        """
        widths = {}
        if not glyphs_used:
            return widths

        # Get encoding and CharStrings
        encoding = font_dict.val.get(b'Encoding')
        char_strings = font_dict.val.get(b'CharStrings')
        if not char_strings or char_strings.TYPE != ps.T_DICT:
            return widths

        # Get lenIV from Private dict (default 4)
        private = font_dict.val.get(b'Private')
        len_iv = 4
        if private and private.TYPE == ps.T_DICT:
            len_iv_val = private.val.get(b'lenIV')
            if len_iv_val and len_iv_val.TYPE == ps.T_INT:
                len_iv = len_iv_val.val

        # Pre-decrypt Subrs for callsubr handling during width extraction.
        # Many fonts (especially Computer Modern) put hsbw inside a Subroutine,
        # so the width parser must be able to follow callsubr instructions.
        decrypted_subrs = None
        if private and private.TYPE == ps.T_DICT:
            subrs = private.val.get(b'Subrs')
            if subrs and subrs.TYPE in ps.ARRAY_TYPES:
                decrypted_subrs = []
                for subr in subrs.val:
                    if subr.TYPE == ps.T_STRING:
                        raw = self._get_string_bytes(subr)
                        decrypted_subrs.append(self._decrypt_charstring(raw, len_iv))
                    else:
                        decrypted_subrs.append(None)

        # CharString widths are in character space (1000 units = 1 em).
        # We always embed fonts with standard FontMatrix [0.001 0 0 0.001 0 0],
        # so CharString widths map directly to PDF width units (1/1000 em).
        # No scaling is needed regardless of the font_dict's FontMatrix,
        # which may have been modified by scalefont/makefont.

        for char_code in glyphs_used:
            glyph_name = self._get_glyph_name_for_code(encoding, char_code)
            if glyph_name and glyph_name in char_strings.val:
                charstring = char_strings.val[glyph_name]
                if charstring.TYPE == ps.T_STRING:
                    cs_data = self._get_string_bytes(charstring)
                    width = self._extract_charstring_width(cs_data, len_iv, decrypted_subrs)
                    if width is not None:
                        widths[char_code] = int(width)

        return widths

    def _get_glyph_name_for_code(self, encoding: object,
                                   char_code: int) -> bytes | None:
        """Get glyph name for a character code from encoding."""
        if encoding is None:
            return None

        if encoding.TYPE == ps.T_NAME:
            # Standard encoding - use ASCII mapping for printable chars
            if 32 <= char_code <= 126:
                return chr(char_code).encode('latin-1')
            return None

        if encoding.TYPE in ps.ARRAY_TYPES:
            if 0 <= char_code < len(encoding.val):
                elem = encoding.val[char_code]
                if elem.TYPE == ps.T_NAME:
                    return elem.val

        return None

    def _decrypt_charstring(self, encrypted_data: bytes,
                              len_iv: int = 4) -> bytes | None:
        """
        Decrypt a CharString and strip lenIV prefix bytes.

        Args:
            encrypted_data: Encrypted CharString bytes
            len_iv: Number of random bytes to skip (default 4)

        Returns:
            bytes: Decrypted CharString data (without lenIV prefix), or None if too short
        """
        if len(encrypted_data) < len_iv + 2:
            return None

        R = 4330
        C1 = 52845
        C2 = 22719

        decrypted = bytearray()
        for cipher_byte in encrypted_data:
            plain_byte = cipher_byte ^ (R >> 8)
            decrypted.append(plain_byte)
            R = ((cipher_byte + R) * C1 + C2) & 0xFFFF

        return bytes(decrypted[len_iv:])

    def _extract_charstring_width(self, encrypted_charstring: bytes,
                                    len_iv: int = 4,
                                    decrypted_subrs: list[bytes | None] | None = None) -> float | None:
        """
        Extract width from a CharString without full interpretation.

        The width is set by hsbw (opcode 13) or sbw (opcode 12,7) at the
        start of the CharString. We decrypt and parse just enough to find it.

        Handles callsubr (opcode 10) by following into pre-decrypted Subroutines,
        which is necessary for fonts (like Computer Modern) that place hsbw inside
        a Subroutine.

        Args:
            encrypted_charstring: Encrypted CharString bytes
            len_iv: Number of random bytes to skip (default 4)
            decrypted_subrs: List of pre-decrypted Subroutine data (or None)

        Returns:
            float: Character width in character space, or None if not found
        """
        data = self._decrypt_charstring(encrypted_charstring, len_iv)
        if data is None:
            return None

        return self._parse_width(data, decrypted_subrs)

    def _parse_width(self, data: bytes | None,
                      decrypted_subrs: list[bytes | None] | None,
                      stack: list | None = None,
                      ps_stack: list | None = None,
                      depth: int = 0) -> float | None:
        """
        Parse decrypted CharString data looking for hsbw/sbw width commands.

        Handles all Type 1 CharString opcodes with correct stack effects:

        One-byte opcodes:
            1=hstem(2), 3=vstem(2), 4=vmoveto(1), 5=rlineto(2), 6=hlineto(1),
            7=vlineto(1), 8=rrcurveto(6), 9=closepath(0), 10=callsubr, 11=return,
            13=hsbw(2), 14=endchar

        Two-byte opcodes (12,x):
            0=dotsection(0), 1=vstem3(6), 2=hstem3(6), 6=seac(5), 7=sbw(4),
            12=div(2->1), 16=callothersubr(variable), 17=pop(0->1), 33=setcurrentpoint(2)

        Args:
            data: Decrypted CharString bytes
            decrypted_subrs: List of pre-decrypted Subroutine data (or None)
            stack: Shared operand stack (created if None)
            ps_stack: PostScript interpreter stack for callothersubr/pop (created if None)
            depth: Recursion depth for callsubr (safety limit)

        Returns:
            float: Character width, or None if not found
        """
        if depth > 10 or data is None:
            return None

        if stack is None:
            stack = []
        if ps_stack is None:
            ps_stack = []

        i = 0
        while i < len(data):
            b = data[i]
            i += 1

            if b == 13:  # hsbw: sbx wx
                if len(stack) >= 2:
                    return stack[-1]  # wx is top of stack
                return None

            elif b == 12 and i < len(data):  # Two-byte command
                b2 = data[i]
                i += 1
                if b2 == 7:  # sbw: sbx sby wx wy
                    if len(stack) >= 4:
                        return stack[-2]  # wx is second from top
                    return None
                elif b2 == 12:  # div: num1 num2 -> num1/num2
                    if len(stack) >= 2:
                        divisor = stack.pop()
                        dividend = stack.pop()
                        stack.append(dividend / divisor if divisor != 0 else 0)
                elif b2 == 16:  # callothersubr: arg1...argN N subr# callothersubr
                    if len(stack) >= 2:
                        _subr_num = int(stack.pop())
                        n_args = int(stack.pop())
                        args = []
                        for _ in range(min(n_args, len(stack))):
                            args.append(stack.pop())
                        # Args popped in reverse; store so pop retrieves in correct order
                        ps_stack.extend(reversed(args))
                elif b2 == 17:  # pop: move value from PS interpreter stack to CharString stack
                    stack.append(ps_stack.pop() if ps_stack else 0)
                elif b2 in (1, 2):  # vstem3(6 args), hstem3(6 args)
                    del stack[-6:]
                elif b2 == 6:  # seac: 5 args
                    del stack[-5:]
                elif b2 == 33:  # setcurrentpoint: 2 args
                    del stack[-2:]
                # 12,0 (dotsection): no stack effect

            elif b == 10:  # callsubr
                if stack and decrypted_subrs:
                    subr_idx = int(stack.pop())
                    if 0 <= subr_idx < len(decrypted_subrs):
                        result = self._parse_width(
                            decrypted_subrs[subr_idx], decrypted_subrs,
                            stack, ps_stack, depth + 1
                        )
                        if result is not None:
                            return result
                        # Subr returned without hsbw - continue parsing main charstring

            elif b == 11:  # return - back to caller
                return None

            elif b == 14:  # endchar - stop parsing
                return None

            # One-byte opcodes that consume stack args
            elif b in (1, 3):  # hstem(2 args), vstem(2 args)
                del stack[-2:]
            elif b in (5,):  # rlineto: 2 args
                del stack[-2:]
            elif b in (4, 6, 7):  # vmoveto(1), hlineto(1), vlineto(1)
                del stack[-1:]
            elif b == 8:  # rrcurveto: 6 args
                del stack[-6:]
            elif b == 9:  # closepath: 0 args
                pass

            elif 32 <= b <= 246:  # Single-byte number
                stack.append(b - 139)

            elif 247 <= b <= 250:  # Two-byte positive
                if i < len(data):
                    stack.append((b - 247) * 256 + data[i] + 108)
                    i += 1

            elif 251 <= b <= 254:  # Two-byte negative
                if i < len(data):
                    stack.append(-(b - 251) * 256 - data[i] - 108)
                    i += 1

            elif b == 255:  # Five-byte number (4-byte signed int)
                if i + 3 < len(data):
                    val = (data[i] << 24) | (data[i+1] << 16) | (data[i+2] << 8) | data[i+3]
                    if val >= 0x80000000:
                        val -= 0x100000000
                    stack.append(val)
                    i += 4

            # Opcodes 0, 2, 15-31: reserved/unused - ignore

        return None


def generate_tounicode_cmap(tounicode_map: dict[int, str],
                            font_name: str = 'Unknown') -> bytes:
    """
    Generate a ToUnicode CMap stream for PDF embedding.

    Args:
        tounicode_map: dict mapping char_code (int) -> unicode_string (str)
        font_name: Font name for the CMap

    Returns:
        bytes: ToUnicode CMap stream content
    """
    lines = [
        b'/CIDInit /ProcSet findresource begin',
        b'12 dict begin',
        b'begincmap',
        b'/CIDSystemInfo <<',
        b'  /Registry (Adobe)',
        b'  /Ordering (UCS)',
        b'  /Supplement 0',
        b'>> def',
        f'/CMapName /{font_name}-UCS def'.encode('latin-1'),
        b'/CMapType 2 def',
        b'1 begincodespacerange',
        b'<00> <FF>',
        b'endcodespacerange',
    ]

    # Build character mappings
    mappings = []
    for char_code, unicode_str in sorted(tounicode_map.items()):
        hex_code = f'{char_code:02X}'
        unicode_hex = ''.join(f'{ord(c):04X}' for c in unicode_str)
        mappings.append((hex_code, unicode_hex))

    # Emit in batches of 100 (PDF limit per block)
    for i in range(0, len(mappings), 100):
        batch = mappings[i:i + 100]
        lines.append(f'{len(batch)} beginbfchar'.encode())
        for hex_code, unicode_hex in batch:
            lines.append(f'<{hex_code}> <{unicode_hex}>'.encode())
        lines.append(b'endbfchar')

    lines.extend([
        b'endcmap',
        b'CMapName currentdict /CMap defineresource pop',
        b'end',
        b'end',
    ])

    return b'\n'.join(lines)
