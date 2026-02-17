# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Font Tracker Module

Tracks font usage during PDF rendering to determine which fonts need embedding.
Standard 14 PDF fonts are guaranteed available and don't need embedding,
unless they have been re-encoded with a custom encoding array.
Supports both Type 1 and CID (Type 0 composite) fonts.
"""

from ...core import types as ps


class FontUsage:
    """
    Track usage of a single font.

    Records which glyphs (character codes) are used from this font,
    enabling font subsetting for smaller PDF output.
    """

    __slots__ = ('font_dict', 'font_name', 'glyphs_used', 'order')

    def __init__(self, font_dict, font_name, order, glyphs_used=None):
        """
        Initialize font usage tracking.

        Args:
            font_dict: PostScript font dictionary
            font_name: Base font name (bytes) for PDF resource naming
            order: Order in which this font was first used (0-based)
            glyphs_used: Set of character codes used (or None to create empty set)
        """
        self.font_dict = font_dict
        self.font_name = font_name
        self.order = order
        self.glyphs_used = glyphs_used if glyphs_used is not None else set()


class FontTracker:
    """
    Track font usage during PDF rendering.

    Identifies which fonts are used and which glyphs from each font,
    enabling font embedding for non-Standard 14 fonts.

    Fonts are deduplicated by their CharStrings and Encoding identity.
    Multiple scaled instances of the same base font (via scalefont/makefont)
    share the same CharStrings and Encoding, so they produce identical
    embedded font data and are tracked as a single entry.
    """

    # Standard 14 PDF fonts - guaranteed available in all PDF viewers
    # These fonts don't need embedding
    STANDARD_14 = frozenset({
        b'Times-Roman',
        b'Times-Bold',
        b'Times-Italic',
        b'Times-BoldItalic',
        b'Helvetica',
        b'Helvetica-Bold',
        b'Helvetica-Oblique',
        b'Helvetica-BoldOblique',
        b'Courier',
        b'Courier-Bold',
        b'Courier-Oblique',
        b'Courier-BoldOblique',
        b'Symbol',
        b'ZapfDingbats',
    })

    def __init__(self):
        """Initialize empty font tracker."""
        # Key is a composite of (CharStrings identity, Encoding identity).
        # This deduplicates scaled instances of the same font that share
        # CharStrings and Encoding (e.g., Times-Roman at 8pt, 10pt, 12pt).
        self.fonts_used = {}  # font_key (tuple) -> FontUsage
        self._font_order = []  # List of font_keys in order of first use
        self._next_order = 0  # Counter for font ordering
        # Map from font_dict identity to font_key for fast lookup
        self._dict_to_key = {}  # id(font_dict.val) -> font_key

    def _get_font_key(self, font_dict):
        """
        Get a deduplication key for a font dictionary.

        For Type 1 fonts: key is (CharStrings_id, Encoding_id) — fonts that
        share the same CharStrings and Encoding produce identical embedded data.

        For Type 0 (CID) fonts: key is ('cid', sfnts_id, cmap_id) — a 3-tuple
        that prevents collisions with Type 1 2-tuples.

        Args:
            font_dict: PostScript font dictionary

        Returns:
            tuple: Composite key for deduplication
        """
        # Check if this is a Type 0 composite font
        font_type = font_dict.val.get(b'FontType')
        if font_type and font_type.val == 0:
            return self._get_cid_font_key(font_dict)

        char_strings = font_dict.val.get(b'CharStrings')
        encoding = font_dict.val.get(b'Encoding')

        cs_id = id(char_strings.val) if char_strings else 0

        if encoding is not None and encoding.TYPE in ps.ARRAY_TYPES:
            enc_id = id(encoding.val)
        elif encoding is not None and encoding.TYPE == ps.T_NAME:
            enc_id = encoding.val  # Use name bytes (e.g., b'StandardEncoding')
        else:
            enc_id = None

        return (cs_id, enc_id)

    def _get_cid_font_key(self, font_dict):
        """
        Get deduplication key for a Type 0 (CID) font.

        Uses a 3-tuple ('cid', sfnts_id, cmap_id) to distinguish from
        Type 1 2-tuple keys.

        Args:
            font_dict: PostScript Type 0 font dictionary

        Returns:
            tuple: ('cid', sfnts_identity, cmap_identity)
        """
        fdep_vector = font_dict.val.get(b'FDepVector')
        sfnts_id = 0
        if fdep_vector and fdep_vector.TYPE in ps.ARRAY_TYPES and fdep_vector.val:
            cidfont = fdep_vector.val[0]
            sfnts = cidfont.val.get(b'sfnts')
            if sfnts:
                sfnts_id = id(sfnts.val)

        cmap = font_dict.val.get(b'CMap')
        cmap_id = id(cmap.val) if cmap else 0

        return ('cid', sfnts_id, cmap_id)

    def track_text_obj(self, text_obj):
        """
        Record font and glyph usage from a TextObj.

        Standard 14 fonts are skipped since they don't need embedding,
        unless re-encoded with a custom encoding array.

        For Type 0 (CID) fonts, text bytes are decoded through the CMap to
        get CIDs which are tracked as the glyph identifiers.

        Fonts are deduplicated by CharStrings and Encoding identity, so
        multiple scaled instances of the same font are tracked as one entry.

        Args:
            text_obj: TextObj display list element
        """
        font_name = text_obj.font_name
        font_type = text_obj.font_dict.val.get(b'FontType')
        is_type0 = font_type and font_type.val == 0

        # Standard 14 fonts don't need embedding - unless re-encoded with a
        # custom encoding array (e.g., DiacriticEncoding in FrameMaker documents).
        # CID fonts always need embedding (skip Standard-14 check).
        if not is_type0 and font_name in self.STANDARD_14:
            encoding = text_obj.font_dict.val.get(b'Encoding')
            if encoding is None or encoding.TYPE == ps.T_NAME:
                return  # Standard encoding - no embedding needed
            # Custom encoding array - fall through to track for embedding

        font_key = self._get_font_key(text_obj.font_dict)

        # Map this font_dict to its key for later lookup
        self._dict_to_key[id(text_obj.font_dict.val)] = font_key

        # Track this font if not already seen
        if font_key not in self.fonts_used:
            self.fonts_used[font_key] = FontUsage(
                font_dict=text_obj.font_dict,
                font_name=font_name,
                order=self._next_order
            )
            self._font_order.append(font_key)
            self._next_order += 1

        # Track which glyphs are used
        if is_type0:
            # For CID fonts, decode text bytes through CMap to get CIDs
            self._track_cid_glyphs(text_obj)
        else:
            # For Type 1 fonts, each byte is a character code
            for char_code in text_obj.text:
                self.fonts_used[font_key].glyphs_used.add(char_code)

    def _track_cid_glyphs(self, text_obj):
        """
        Extract CIDs from Type 0 font TextObj and track them.

        TextObj.text for Type 0 fonts contains pre-encoded 2-byte big-endian
        CID values (already decoded through the CMap by _emit_text_obj).

        Args:
            text_obj: TextObj display list element
        """
        font_key = self._get_font_key(text_obj.font_dict)
        text = text_obj.text

        # Extract 2-byte CID values from the pre-encoded bytes
        for i in range(0, len(text) - 1, 2):
            cid = (text[i] << 8) | text[i + 1]
            self.fonts_used[font_key].glyphs_used.add(cid)

    def needs_embedding(self):
        """
        Return True if any non-Standard 14 fonts were used.

        Returns:
            bool: True if font embedding is needed
        """
        return len(self.fonts_used) > 0

    def get_fonts_to_embed(self):
        """
        Get dictionary of fonts that need embedding.

        Returns:
            dict: font_key (tuple) -> FontUsage
        """
        return self.fonts_used

    def get_fonts_in_order(self):
        """
        Get fonts in the order they were first used.

        This order corresponds to Cairo's font naming convention
        (f-0-0, f-1-0, f-2-0, etc.).

        Returns:
            list: List of (font_key, FontUsage) tuples in usage order
        """
        return [(font_key, self.fonts_used[font_key]) for font_key in self._font_order]

    def get_font_key_for_dict(self, font_dict):
        """
        Look up the font key for a font dictionary.

        Uses the cached mapping from font_dict identity to font_key,
        falling back to computing the key if not cached.

        Args:
            font_dict: PostScript font dictionary

        Returns:
            font_key tuple, or None if not tracked
        """
        dict_id = id(font_dict.val)
        font_key = self._dict_to_key.get(dict_id)
        if font_key is None:
            # Compute key and check if this font was tracked under a different
            # font_dict instance with the same CharStrings/Encoding
            font_key = self._get_font_key(font_dict)
            if font_key in self.fonts_used:
                self._dict_to_key[dict_id] = font_key
            else:
                return None
        return font_key

    @staticmethod
    def is_cid_font(font_key):
        """
        Check if a font key represents a CID (Type 0) font.

        CID font keys are 3-tuples starting with 'cid', while Type 1 keys
        are 2-tuples of (CharStrings_id, Encoding_id).

        Args:
            font_key: Font key tuple from _get_font_key()

        Returns:
            bool: True if this is a CID font key
        """
        return len(font_key) == 3 and font_key[0] == 'cid'

    def get_best_subrs(self):
        """
        Find the fullest Subrs array for each CharStrings group.

        DVIPS creates re-encoded font instances that may have truncated Subrs
        arrays in their Private dicts, even though they share the same base
        font's CharStrings. This method finds the Subrs with the most entries
        for each CharStrings identity, so the embedding code can use the
        complete Subrs regardless of which font instance it's processing.

        Keyed by CharStrings identity (not font name) because different fonts
        with the same name (e.g., CMR10 in the main document vs CMR10 embedded
        in an EPS) have different CharStrings and incompatible Subrs.

        CID font keys are skipped (no Subrs).

        Returns:
            dict: cs_id (int) -> Subrs PostScript array object
        """
        best = {}  # cs_id -> (subrs_count, subrs_obj)
        for font_key, usage in self.fonts_used.items():
            # Skip CID fonts - they don't have Type 1 Subrs
            if self.is_cid_font(font_key):
                continue
            cs_id = font_key[0]  # id(CharStrings.val)
            font_dict = usage.font_dict
            private = font_dict.val.get(b'Private')
            if not private or private.TYPE != ps.T_DICT:
                continue
            subrs = private.val.get(b'Subrs')
            if not subrs or subrs.TYPE not in ps.ARRAY_TYPES:
                continue
            count = len(subrs.val)
            if cs_id not in best or count > best[cs_id][0]:
                best[cs_id] = (count, subrs)
        return {cs_id: subrs for cs_id, (_, subrs) in best.items()}

    def reset(self):
        """Clear all tracked fonts."""
        self.fonts_used.clear()
        self._font_order.clear()
        self._dict_to_key.clear()
        self._next_order = 0


def is_standard_14_font(font_name):
    """
    Check if a font is one of the Standard 14 PDF fonts.

    Args:
        font_name: Font name as bytes

    Returns:
        bool: True if font is Standard 14 (no embedding needed)
    """
    return font_name in FontTracker.STANDARD_14
