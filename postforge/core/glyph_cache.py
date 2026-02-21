# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
Glyph Cache Infrastructure

This module implements an LRU cache for rendered glyph paths and bitmaps.
Caching avoids re-interpreting font data for repeated glyph renderings,
providing significant performance improvements for documents with repeated
characters. All scalable font types are cached: Type 1, Type 2 (CFF),
Type 3, Type 42, and CID fonts.

Architecture:
- GlyphCacheKey: Unique identifier combining font, character, and scale/rotation
- CachedGlyph: Stores path data and character metrics
- GlyphCache: LRU path cache with configurable size limit
- GlyphBitmapCache: LRU bitmap cache with configurable size and memory limits

Cache Key Design:
- font_id: Python id() of font dict - unique per font instance
- char_selector: Glyph name bytes or character code bytes
- ctm_scale: (a,b,c,d) quantized to 3 decimals - captures scale/rotation only
- Translation excluded because same glyph at different positions shares cache

Cacheability (per PLRM, Type 3 fonts only):
- setcachedevice: Cacheable - graphics state restricted, path is complete
- setcharwidth: Not cacheable - color operations allowed
"""

import copy
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GlyphCacheKey:
    """Unique identifier for a cached glyph.

    Uses frozen dataclass for automatic __hash__ and __eq__ generation,
    making it suitable as a dictionary key.

    Includes color because imagemask glyphs render with the current color.
    Same glyph with different colors must be cached separately.

    Includes sub-pixel Y offset (quantized to 0.5 increments) because Cairo's
    antialiasing produces slightly different ink extents at different sub-pixel
    positions. Without this, glyphs rendered at integer Y (e.g., 2766.0) would
    have different origins than those at fractional Y (e.g., 731.5), causing
    1-pixel baseline drift when both appear on the same visual line.

    font_id is FontName bytes (or (FontName, FID) tuple) for stable identity
    across GC cycles.  Falls back to id() only for fonts without FontName.
    """
    font_id: object        # FontName bytes, (FontName, FID) tuple, or id() fallback
    char_selector: bytes   # Glyph name or chr(code) as bytes
    ctm_scale: tuple       # (a, b, c, d) rounded to 3 decimals - scale/rotation only
    color: tuple           # Current color (rounded) - needed for imagemask glyphs
    font_matrix: tuple     # FontMatrix values - helps distinguish font sizes
    subpixel_y: float      # Sub-pixel Y offset: 0.0 or 0.5 (quantized)


@dataclass
class CachedGlyph:
    """Cached glyph rendering data.

    Stores the display list elements generated during BuildGlyph/BuildChar
    execution along with character metrics for position advancement.

    Display list elements include Path objects and Fill/Stroke operations
    which together capture the complete rendered glyph.

    Also stores a reference to the font_dict to prevent garbage collection
    and enable validation that the cached glyph is for the correct font.
    """
    display_elements: list[Any]  # Display list elements (Path, Fill, Stroke, etc.)
    char_width: tuple[float, float]  # (wx, wy) in character space
    char_bbox: tuple[float, float, float, float] | None  # (llx, lly, urx, ury) or None
    font_dict: Any  # Reference to font dict - prevents GC and enables validation


class GlyphCache:
    """LRU cache for rendered glyph paths.

    Uses OrderedDict for O(1) LRU operations. When a glyph is accessed,
    it moves to the end (most recently used). When capacity is exceeded,
    the first item (least recently used) is evicted.

    Thread Safety: This implementation is NOT thread-safe. PostScript
    execution is single-threaded per context, so this is acceptable.
    """
    DEFAULT_MAX_ENTRIES = 2048

    def __init__(self, max_entries: int | None = None) -> None:
        """Initialize glyph cache with optional size limit.

        Args:
            max_entries: Maximum cached glyphs before LRU eviction.
                        Defaults to DEFAULT_MAX_ENTRIES (2048).
        """
        self._cache: OrderedDict[GlyphCacheKey, CachedGlyph] = OrderedDict()
        self._max_entries = max_entries or self.DEFAULT_MAX_ENTRIES
        self._hits = 0
        self._misses = 0

    def get(self, key: GlyphCacheKey) -> CachedGlyph | None:
        """Retrieve cached glyph, updating LRU order.

        Args:
            key: Cache key identifying the glyph

        Returns:
            CachedGlyph if found, None otherwise
        """
        if key in self._cache:
            self._hits += 1
            self._cache.move_to_end(key)  # Update LRU position
            return self._cache[key]
        self._misses += 1
        return None

    def put(self, key: GlyphCacheKey, glyph: CachedGlyph) -> None:
        """Cache a glyph with LRU eviction.

        If the key already exists, updates value and LRU position.
        If cache is full, evicts least recently used entry.

        Args:
            key: Cache key for the glyph
            glyph: Glyph data to cache
        """
        if key in self._cache:
            # Update existing entry and move to end
            self._cache.move_to_end(key)
            self._cache[key] = glyph
        else:
            # Check capacity and evict if needed
            if len(self._cache) >= self._max_entries:
                self._cache.popitem(last=False)  # Evict oldest (first) item
            self._cache[key] = glyph

    def clear(self) -> None:
        """Clear entire cache and reset statistics."""
        self._cache.clear()
        self._hits = 0
        self._misses = 0

    def stats(self) -> dict:
        """Return cache statistics for debugging/profiling.

        Returns:
            Dictionary with entries count, max_entries, hits, misses, and hit_rate
        """
        total = self._hits + self._misses
        return {
            'entries': len(self._cache),
            'max_entries': self._max_entries,
            'hits': self._hits,
            'misses': self._misses,
            'hit_rate': self._hits / total if total > 0 else 0.0
        }

    def __len__(self) -> int:
        """Return number of cached entries."""
        return len(self._cache)


@dataclass
class CachedBitmap:
    """Cached glyph rendered as a Cairo surface.

    Each glyph gets its own small ARGB32 surface sized to its bounding box.
    The origin_x/origin_y offset positions the surface relative to the glyph's
    drawing position.
    """
    surface: Any            # cairo.ImageSurface (ARGB32)
    width: int              # surface width in pixels
    height: int             # surface height in pixels
    origin_x: float         # offset from glyph position to surface top-left X
    origin_y: float         # offset from glyph position to surface top-left Y
    backing_data: Any       # bytearray - prevents GC of surface backing memory


class GlyphBitmapCache:
    """LRU cache for rendered glyph bitmaps (Cairo surfaces).

    Shared across all fonts. Keyed by GlyphCacheKey. Enforces both
    entry count and memory limits to prevent unbounded growth.
    """
    DEFAULT_MAX_ENTRIES = 4096
    FALLBACK_MAX_BYTES = 64 * 1024 * 1024  # 64 MB - only used if system params not wired

    def __init__(self, max_entries: int | None = None, max_bytes: int | None = None) -> None:
        self._cache: OrderedDict[GlyphCacheKey, CachedBitmap] = OrderedDict()
        self._width_cache: dict[GlyphCacheKey, tuple] = {}  # char_width per key
        self._max_entries = max_entries or self.DEFAULT_MAX_ENTRIES
        self._max_bytes = max_bytes or self.FALLBACK_MAX_BYTES
        self._current_bytes = 0
        self._hits = 0
        self._misses = 0

    def has(self, key: GlyphCacheKey) -> bool:
        """Check if a bitmap is cached for this key."""
        if key in self._cache:
            self._hits += 1
            self._cache.move_to_end(key)
            return True
        self._misses += 1
        return False

    def get(self, key: GlyphCacheKey) -> CachedBitmap | None:
        """Retrieve cached bitmap, updating LRU order and statistics."""
        entry = self._cache.get(key)
        if entry is not None:
            self._hits += 1
            self._cache.move_to_end(key)
        else:
            self._misses += 1
        return entry

    def put(self, key: GlyphCacheKey, bitmap: CachedBitmap) -> None:
        """Cache a bitmap with LRU eviction by count and memory."""
        entry_bytes = bitmap.width * bitmap.height * 4  # ARGB32

        # Evict if over limits
        while (len(self._cache) >= self._max_entries or
               self._current_bytes + entry_bytes > self._max_bytes) and self._cache:
            _, evicted = self._cache.popitem(last=False)
            self._current_bytes -= evicted.width * evicted.height * 4
            evicted_key_to_remove = None
            # Also remove from width cache (we don't track key->evicted mapping,
            # so width cache entries are cleaned lazily)

        if key in self._cache:
            old = self._cache[key]
            self._current_bytes -= old.width * old.height * 4
            self._cache.move_to_end(key)

        self._cache[key] = bitmap
        self._current_bytes += entry_bytes

    def get_width(self, key: GlyphCacheKey) -> tuple | None:
        """Get cached char_width for a glyph."""
        return self._width_cache.get(key)

    def put_width(self, key: GlyphCacheKey, width: tuple) -> None:
        """Cache char_width for a glyph."""
        self._width_cache[key] = width

    def clear(self) -> None:
        """Clear entire cache and reset statistics."""
        self._cache.clear()
        self._width_cache.clear()
        self._current_bytes = 0
        self._hits = 0
        self._misses = 0

    def stats(self) -> dict:
        """Return cache statistics."""
        total = self._hits + self._misses
        return {
            'entries': len(self._cache),
            'max_entries': self._max_entries,
            'hits': self._hits,
            'misses': self._misses,
            'hit_rate': self._hits / total if total > 0 else 0.0,
            'memory_bytes': self._current_bytes,
            'width_entries': len(self._width_cache),
        }

    def __len__(self) -> int:
        return len(self._cache)


def make_cache_key(font_dict: Any, char_selector: bytes, ctm: Any, color: list[float], position_y: float = 0.0) -> GlyphCacheKey:
    """Create cache key from font, character, transformation, color, and sub-pixel Y.

    The cache key captures everything that affects glyph appearance:
    - font identity: FontName (+ FID when available) uniquely identifies the font
    - char_selector: Different characters produce different glyphs
    - CTM scale/rotation: Different scales/rotations produce different paths
    - color: Different colors produce different output (for imagemask glyphs)
    - font_matrix: Different font sizes from scalefont
    - subpixel_y: Sub-pixel Y offset (0.0 or 0.5) - Cairo's antialiasing produces
      different ink extents at different sub-pixel positions

    Translation (tx, ty) is intentionally excluded because the same glyph
    drawn at different positions should use the same cached data. However,
    sub-pixel Y is included because it affects antialiasing and ink extents.

    FontName is used instead of id() because id() values can be reused after
    garbage collection, causing cache collisions between different fonts.
    FID disambiguates fonts that are redefined with the same name.

    Args:
        font_dict: PostScript font dictionary object
        char_selector: Glyph name or character code as bytes
        ctm: Current Transformation Matrix (Array with 6 numeric elements)
        color: Current color as list of floats
        position_y: Y position in device space (used for sub-pixel quantization)

    Returns:
        GlyphCacheKey suitable for cache lookup
    """
    # Use a stable font identifier that survives scalefont, font copies,
    # and garbage collection.  Previous code used id() of internal objects
    # (base, BitMaps, CharStrings) but id() values can be reused after GC,
    # causing cache collisions between different fonts (e.g. roman vs italic).
    #
    # Now we use FontName (a stable bytes value) combined with FID when
    # available.  FontName uniquely identifies a font definition and is
    # not affected by memory allocation patterns.

    font_name_obj = font_dict.val.get(b'FontName')
    fid_obj = font_dict.val.get(b'FID')

    if font_name_obj is not None and hasattr(font_name_obj, 'val'):
        font_id = font_name_obj.val  # bytes — stable across GC
        # Include FID to disambiguate re-defined fonts with the same name
        if fid_obj is not None and hasattr(fid_obj, 'val'):
            font_id = (font_name_obj.val, fid_obj.val)
    else:
        # Fallback for fonts without FontName: use id() of the most
        # stable internal structure (least likely to be GC'd).
        # CharProcs is checked first because Type 3 fonts from makefont/scalefont
        # share CharProcs via shallow copy, making id(CharProcs) stable across copies.
        char_procs = font_dict.val.get(b'CharProcs')
        base = font_dict.val.get(b'base')
        bitmaps = font_dict.val.get(b'BitMaps')
        char_strings = font_dict.val.get(b'CharStrings')
        private = font_dict.val.get(b'Private')

        if char_procs is not None:
            font_id = id(char_procs)
        elif base is not None:
            font_id = id(base)
        elif bitmaps is not None:
            font_id = id(bitmaps)
        elif char_strings is not None:
            font_id = id(char_strings)
        elif private is not None:
            font_id = id(private)
        else:
            font_id = id(font_dict)

    # Extract and quantize scale/rotation components (ignore translation)
    # ctm.val is a list of PostScript numeric objects
    # Translation is handled at render time, so exclude tx (ctm[4]) and ty (ctm[5])
    a = round(ctm.val[0].val, 3)
    b = round(ctm.val[1].val, 3)
    c = round(ctm.val[2].val, 3)
    d = round(ctm.val[3].val, 3)

    # Quantize color to avoid floating-point comparison issues
    color_tuple = tuple(round(c, 3) for c in color)

    # Extract FontMatrix - helps distinguish font sizes from scalefont
    font_matrix_tuple = (0.001, 0, 0, 0.001, 0, 0)  # Default
    font_matrix = font_dict.val.get(b'FontMatrix')
    if font_matrix is not None and hasattr(font_matrix, 'val'):
        try:
            font_matrix_tuple = tuple(
                round(elem.val, 6) for elem in font_matrix.val[:6]
            )
        except (AttributeError, TypeError):
            pass

    # Quantize sub-pixel Y to 0.5 increments (2 buckets: 0.0 and 0.5)
    # This separates glyphs rendered at integer Y from fractional Y,
    # which have different ink extents due to Cairo's antialiasing
    frac_y = position_y - int(position_y)
    subpixel_y = 0.5 if frac_y >= 0.25 and frac_y < 0.75 else 0.0

    return GlyphCacheKey(font_id, char_selector, (a, b, c, d), color_tuple, font_matrix_tuple, subpixel_y)
