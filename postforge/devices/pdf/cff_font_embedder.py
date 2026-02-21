# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
CFF Font Embedder Module

Extracts CFF binary data and glyph widths from PostScript font dictionaries
containing CFF (Type 2) fonts, for PDF embedding as /FontFile3 /Type1C.

Unlike Type 1 font embedding which requires reconstructing the font file from
parsed PostScript data, CFF embedding is simpler: the original CFF binary is
stored in a registry keyed by CharStrings identity and can be embedded directly.
Width extraction uses the Type 2 charstring interpreter in width-only mode.
"""

from ...core import types as ps
from ...core.type2_charstring import Type2CharStringInterpreter
from ...operators.cff_ops import _cff_registry


class CFFEmbedder:
    """Extract CFF font data and glyph widths for PDF embedding."""

    def get_font_file_data(self, font_dict: ps.Dict) -> bytes | None:
        """
        Get the raw CFF binary data for embedding.

        Looks up the CFF binary in the registry using the font's CharStrings
        identity. This survives scalefont/makefont copies since they share
        the same CharStrings dict.

        Args:
            font_dict: PostScript font dictionary (FontType 2)

        Returns:
            bytes: Raw CFF binary data, or None if not available
        """
        char_strings = font_dict.val.get(b'CharStrings')
        if char_strings and char_strings.TYPE == ps.T_DICT:
            return _cff_registry.get(id(char_strings.val))
        return None

    def get_glyph_widths(self, font_dict: ps.Dict, glyphs_used: set[int]) -> dict[int, int]:
        """
        Get character widths for all used glyphs via Type 2 charstring execution.

        Widths are returned in character space (typically 1000 units/em),
        matching the PDF /Widths convention for fonts embedded with standard
        FontMatrix [0.001 0 0 0.001 0 0].

        Args:
            font_dict: PostScript font dictionary (FontType 2)
            glyphs_used: Set of character codes used

        Returns:
            dict: char_code -> width (int, in character space units)
        """
        widths = {}
        if not glyphs_used:
            return widths

        encoding = font_dict.val.get(b'Encoding')
        char_strings = font_dict.val.get(b'CharStrings')
        if not char_strings or char_strings.TYPE != ps.T_DICT:
            return widths

        # Extract CFF-specific parameters from Private dict
        private = font_dict.val.get(b'Private')
        default_width_x = 0.0
        nominal_width_x = 0.0
        local_subrs = []

        if private and private.TYPE == ps.T_DICT:
            dwx = private.val.get(b'defaultWidthX')
            if dwx and dwx.TYPE in ps.NUMERIC_TYPES:
                default_width_x = float(dwx.val)
            nwx = private.val.get(b'nominalWidthX')
            if nwx and nwx.TYPE in ps.NUMERIC_TYPES:
                nominal_width_x = float(nwx.val)

            subrs_obj = private.val.get(b'Subrs')
            if subrs_obj and subrs_obj.TYPE in ps.ARRAY_TYPES:
                local_subrs = [
                    s.byte_string() if hasattr(s, 'byte_string') else bytes(s.val)
                    for s in subrs_obj.val
                ]

        # Global subrs
        global_subrs = []
        gsubrs_obj = font_dict.val.get(b'_cff_global_subrs')
        if gsubrs_obj and gsubrs_obj.TYPE in ps.ARRAY_TYPES:
            global_subrs = [
                s.byte_string() if hasattr(s, 'byte_string') else bytes(s.val)
                for s in gsubrs_obj.val
            ]

        for char_code in glyphs_used:
            glyph_name = _get_glyph_name_for_code(encoding, char_code)
            if glyph_name and glyph_name in char_strings.val:
                cs_obj = char_strings.val[glyph_name]
                if cs_obj.TYPE == ps.T_STRING:
                    cs_data = (cs_obj.byte_string() if hasattr(cs_obj, 'byte_string')
                               else bytes(cs_obj.val))
                    interpreter = Type2CharStringInterpreter(
                        None, font_dict, default_width_x, nominal_width_x,
                        local_subrs, global_subrs, width_only_mode=True)
                    interpreter.execute(cs_data)
                    if interpreter.advance_width is not None:
                        widths[char_code] = int(interpreter.advance_width)

        return widths


def _get_glyph_name_for_code(encoding: object, char_code: int) -> bytes | None:
    """Get glyph name bytes for a character code from encoding.

    Args:
        encoding: PostScript Encoding object (Name or Array)
        char_code: Integer character code

    Returns:
        bytes: Glyph name, or None if not found
    """
    if encoding is None:
        return None

    if encoding.TYPE == ps.T_NAME:
        if 32 <= char_code <= 126:
            return chr(char_code).encode('latin-1')
        return None

    if encoding.TYPE in ps.ARRAY_TYPES:
        if 0 <= char_code < len(encoding.val):
            elem = encoding.val[char_code]
            if elem.TYPE == ps.T_NAME:
                return elem.val

    return None
