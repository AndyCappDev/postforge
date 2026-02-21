# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Cairo Image Rendering Module

Image rendering and pixel format conversion for Cairo-based output devices.
Handles grayscale, RGB, CMYK, and imagemask conversions from PostScript
sample data to Cairo surface formats.

Performance:
- Imagemask glyphs are cached as pre-rendered Cairo ARGB32 surfaces
- Cache key uses content of sample_data to avoid id() reuse after garbage collection
- Optional Cython accelerators for common pixel format conversions
"""

import math
from collections import OrderedDict

from .cairo_utils import _safe_rgb

import cairo

from ...core import icc_default
from ...core import icc_profile
from ...core import types as ps
from ...core.color_space import ColorSpaceEngine, _get_cie_float_array, _apply_decode_array

try:
    from ._image_conv_cy import (
        gray8_to_bgrx, gray8_decode_to_bgrx,
        gray1_to_bgrx, gray2_to_bgrx, gray4_to_bgrx, gray12_to_bgrx,
        rgb8_to_bgrx, rgb8_decode_to_bgrx,
        rgb4_to_bgrx, rgb12_to_bgrx,
        cmyk8_to_bgrx, cmyk8_decode_to_bgrx,
        cmyk4_to_bgrx, cmyk12_to_bgrx,
        apply_color_key_mask_8, apply_color_key_mask_4, apply_color_key_mask_12,
    )
    _CYTHON_IMAGE_CONV = True
except ImportError:
    _CYTHON_IMAGE_CONV = False


# Cairo surface cache for imagemask glyphs (LRU with size limit)
# Key: (sample_data_bytes, width, height, polarity, color_key)
# Value: (cairo.ImageSurface, backing_bytearray, pattern, matrix)
_IMAGEMASK_CACHE_MAX_ENTRIES = 2048
_imagemask_surface_cache = OrderedDict()
_imagemask_cache_hits = 0
_imagemask_cache_misses = 0


def get_imagemask_cache_stats():
    """Return imagemask surface cache statistics."""
    total = _imagemask_cache_hits + _imagemask_cache_misses
    return {
        'entries': len(_imagemask_surface_cache),
        'max_entries': _IMAGEMASK_CACHE_MAX_ENTRIES,
        'hits': _imagemask_cache_hits,
        'misses': _imagemask_cache_misses,
        'hit_rate': _imagemask_cache_hits / total if total > 0 else 0.0
    }


def clear_imagemask_cache():
    """Clear the imagemask surface cache."""
    global _imagemask_surface_cache, _imagemask_cache_hits, _imagemask_cache_misses
    _imagemask_surface_cache.clear()
    _imagemask_cache_hits = 0
    _imagemask_cache_misses = 0


def _render_image_element(image_element: ps.ImageElement, cairo_ctx, page_height):
    """Render PostScript image using Cairo with correct matrix handling"""

    try:
        # Get mask_color for ImageType 4 (color key masking)
        mask_color = getattr(image_element, 'mask_color', None)

        # 1. Convert PostScript samples to Cairo pixel data with color space transformation
        pixel_data = _convert_samples_to_cairo_format(
            image_element.sample_data,
            image_element.bits_per_component,
            image_element.width,
            image_element.height,
            image_element.decode_array,
            image_element.components,
            getattr(image_element, 'color_space', None),
            mask_color
        )

        if not pixel_data:
            return

        # 1b. Apply ImageType 3 stencil mask if present
        stencil_mask = getattr(image_element, 'stencil_mask', None)
        if stencil_mask is not None:
            _apply_stencil_mask(
                pixel_data,
                image_element.width, image_element.height,
                stencil_mask,
                getattr(image_element, 'stencil_mask_width', image_element.width),
                getattr(image_element, 'stencil_mask_height', image_element.height),
                getattr(image_element, 'stencil_mask_polarity', True)
            )

        # 2. Create Cairo image surface with appropriate format and stride handling
        cairo_format = _select_cairo_format(
            image_element.bits_per_component,
            image_element.components
        )

        # Ensure pixel_data is a mutable bytearray for Cairo
        if isinstance(pixel_data, bytes):
            pixel_data = bytearray(pixel_data)

        # Calculate required stride for this format and width
        required_stride = cairo.ImageSurface.format_stride_for_width(cairo_format, image_element.width)
        bytes_per_pixel = 4 if cairo_format == cairo.FORMAT_ARGB32 else (3 if cairo_format == cairo.FORMAT_RGB24 else 1)
        actual_stride = image_element.width * bytes_per_pixel

        # Check if we need to add padding to meet Cairo's stride requirements
        if required_stride > actual_stride:
            padded_data = bytearray()
            for row in range(image_element.height):
                row_start = row * actual_stride
                row_end = row_start + actual_stride
                row_data = pixel_data[row_start:row_end]
                padded_data.extend(row_data)
                padding_needed = required_stride - actual_stride
                padded_data.extend([0] * padding_needed)
            pixel_data = padded_data

        surface = cairo.ImageSurface.create_for_data(
            pixel_data, cairo_format,
            image_element.width, image_element.height
        )

        # 3. Apply device space image matrix (CTM)
        cairo_ctx.save()
        try:
            # Apply PostScript CTM at time of image creation
            cairo_ctx.transform(cairo.Matrix(*image_element.ctm))

            # 4. Apply interpolation setting from PostScript
            pattern = cairo.SurfacePattern(surface)

            # Use the original PostScript image matrix as-is
            ps_matrix = image_element.image_matrix
            pattern.set_matrix(cairo.Matrix(*ps_matrix))

            if image_element.interpolate:
                pattern.set_filter(cairo.FILTER_BILINEAR)
            else:
                pattern.set_filter(cairo.FILTER_NEAREST)

            # 5. Render image
            cairo_ctx.set_source(pattern)
            cairo_ctx.paint()

        except Exception as e:
            print(f"Image rendering error: {e}")
        finally:
            cairo_ctx.restore()

    except Exception as e:
        print(f"Image element rendering failed: {e}")


def _apply_stencil_mask(pixel_data, img_width, img_height, mask_data, mask_width, mask_height, polarity):
    """Apply a 1-bit stencil mask to ARGB32 pixel data, setting alpha=0 for masked pixels.

    The mask is 1-bit per sample, row-padded to byte boundaries.
    polarity=True (Decode [0 1]): bit=1 means paint, bit=0 means mask out.
    polarity=False (Decode [1 0]): bit=0 means paint, bit=1 means mask out.

    If mask dimensions differ from image dimensions, scale the mask lookup.
    """
    mask_row_bytes = (mask_width + 7) // 8

    # Scale factors for mask lookup when dimensions differ
    x_scale = mask_width / img_width if img_width != mask_width else 1.0
    y_scale = mask_height / img_height if img_height != mask_height else 1.0

    for row in range(img_height):
        mask_row = int(row * y_scale)
        if mask_row >= mask_height:
            mask_row = mask_height - 1
        for col in range(img_width):
            mask_col = int(col * x_scale)
            if mask_col >= mask_width:
                mask_col = mask_width - 1

            # Get mask bit
            byte_idx = mask_row * mask_row_bytes + mask_col // 8
            bit_idx = 7 - (mask_col % 8)
            if byte_idx < len(mask_data):
                bit_val = (mask_data[byte_idx] >> bit_idx) & 1
            else:
                bit_val = 0

            # Determine if pixel should be painted
            # PLRM: decoded value 0 = paint, decoded value 1 = mask out
            # polarity=True (Decode [0 1]): raw bit 0 → paint, raw bit 1 → mask out
            # polarity=False (Decode [1 0]): raw bit 0 → mask out, raw bit 1 → paint
            paint = (bit_val == 0) if polarity else (bit_val == 1)

            if not paint:
                # Set all BGRA bytes to 0 (Cairo uses pre-multiplied alpha)
                pixel_offset = (row * img_width + col) * 4
                if pixel_offset + 3 < len(pixel_data):
                    pixel_data[pixel_offset] = 0
                    pixel_data[pixel_offset + 1] = 0
                    pixel_data[pixel_offset + 2] = 0
                    pixel_data[pixel_offset + 3] = 0


def _render_imagemask_element(mask_element: ps.ImageMaskElement, cairo_ctx, page_height):
    """Render PostScript imagemask with pre-baked color surface caching.

    Caches ARGB32 surfaces with color already applied. This eliminates:
    - Bit-to-alpha conversion on cache hit
    - Separate color/mask composition (just paint the pre-colored surface)

    Cache key includes color since color is baked into the surface.
    """
    global _imagemask_surface_cache, _imagemask_cache_hits, _imagemask_cache_misses

    try:
        mask_data = mask_element.sample_data
        if not mask_data:
            return

        width = mask_element.width
        height = mask_element.height
        polarity = mask_element.polarity

        # Normalize color to RGB triple (grayscale may be single-element)
        r_f, g_f, b_f = _safe_rgb(mask_element.color)

        # Quantize color for cache key (same as glyph cache)
        color_key = (round(r_f, 3), round(g_f, 3), round(b_f, 3))

        # Cache key uses content bytes (not id()) to avoid stale hits after GC
        cache_key = (mask_data, width, height, polarity, color_key)

        cached = _imagemask_surface_cache.get(cache_key)
        if cached is not None:
            _imagemask_cache_hits += 1
            _imagemask_surface_cache.move_to_end(cache_key)  # LRU: mark as recently used
            colored_surface, argb_data, cached_pattern, cached_matrix = cached
        else:
            _imagemask_cache_misses += 1

            # Convert color to bytes (Cairo ARGB32 is BGRA in memory on little-endian)
            r = int(r_f * 255)
            g = int(g_f * 255)
            b = int(b_f * 255)

            # Calculate stride for ARGB32 format
            cairo_stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_ARGB32, width)
            argb_data = bytearray(cairo_stride * height)

            ps_bytes_per_row = (width + 7) // 8
            byte_offset = 0

            for row in range(height):
                row_start = row * cairo_stride
                for col in range(width):
                    byte_idx = col // 8
                    bit_idx = 7 - (col % 8)
                    should_paint = False

                    if byte_offset + byte_idx < len(mask_data):
                        byte_val = mask_data[byte_offset + byte_idx]
                        bit_val = (byte_val >> bit_idx) & 1
                        if polarity:
                            should_paint = (bit_val == 1)
                        else:
                            should_paint = (bit_val == 0)

                    if should_paint:
                        pixel_offset = row_start + col * 4
                        argb_data[pixel_offset] = b      # Blue
                        argb_data[pixel_offset + 1] = g  # Green
                        argb_data[pixel_offset + 2] = r  # Red
                        argb_data[pixel_offset + 3] = 255  # Alpha (opaque)
                    # else: leave as 0,0,0,0 (fully transparent)

                byte_offset += ps_bytes_per_row

            colored_surface = cairo.ImageSurface.create_for_data(
                argb_data, cairo.FORMAT_ARGB32,
                width, height, cairo_stride
            )

            # Pre-create pattern and matrix (reused on every render)
            cached_pattern = cairo.SurfacePattern(colored_surface)
            # Use BEST for smooth scaling of bitmap fonts (e.g., DVIPS 300dpi
            # fonts rendered at screen resolution). NEAREST causes jagged text.
            cached_pattern.set_filter(cairo.FILTER_BEST)
            cached_matrix = cairo.Matrix(*mask_element.image_matrix)
            cached_pattern.set_matrix(cached_matrix)

            # LRU eviction: remove oldest entry if at capacity
            if len(_imagemask_surface_cache) >= _IMAGEMASK_CACHE_MAX_ENTRIES:
                _imagemask_surface_cache.popitem(last=False)
            _imagemask_surface_cache[cache_key] = (colored_surface, argb_data, cached_pattern, cached_matrix)

        # Render - just transform and paint the pre-colored surface
        cairo_ctx.save()
        try:
            cairo_ctx.transform(cairo.Matrix(*mask_element.ctm))
            cairo_ctx.set_source(cached_pattern)
            cairo_ctx.paint()
        except Exception as e:
            print(f"Imagemask rendering error: {e}")
        finally:
            cairo_ctx.restore()

    except Exception as e:
        print(f"Imagemask element rendering failed: {e}")


def _render_colorimage_element(color_element: ps.ColorImageElement, cairo_ctx, page_height):
    """Render PostScript colorimage using Cairo color formats"""

    try:
        # 1. Convert color samples based on component count

        if color_element.components == 1:  # Grayscale
            # Cython fast paths: return BGRX directly
            if _CYTHON_IMAGE_CONV and color_element.bits_per_component == 8:
                num_pixels = color_element.width * color_element.height
                if _is_identity_decode(color_element.decode_array, 1):
                    pixel_data = gray8_to_bgrx(color_element.sample_data, num_pixels)
                else:
                    lut = _build_decode_lut(color_element.decode_array[0], color_element.decode_array[1])
                    pixel_data = gray8_decode_to_bgrx(color_element.sample_data, num_pixels, lut)
                cairo_format = cairo.FORMAT_ARGB32
            elif _CYTHON_IMAGE_CONV and color_element.bits_per_component in (1, 2, 4, 12):
                bpc = color_element.bits_per_component
                da = color_element.decode_array
                if bpc == 1:
                    pixel_data = gray1_to_bgrx(color_element.sample_data, color_element.width, color_element.height, da[0], da[1])
                elif bpc == 2:
                    pixel_data = gray2_to_bgrx(color_element.sample_data, color_element.width, color_element.height, da[0], da[1])
                elif bpc == 4:
                    pixel_data = gray4_to_bgrx(color_element.sample_data, color_element.width, color_element.height, da[0], da[1])
                else:
                    pixel_data = gray12_to_bgrx(color_element.sample_data, color_element.width, color_element.height, da)
                cairo_format = cairo.FORMAT_ARGB32
            else:
                grayscale_data = _convert_grayscale_samples(
                    color_element.sample_data, color_element.bits_per_component,
                    color_element.width, color_element.height,
                    color_element.decode_array,
                    getattr(color_element, 'color_space', None)
                )

                # Convert single grayscale values to ARGB32
                pixel_data = bytearray()
                for gray_val in grayscale_data:
                    pixel_data.extend([gray_val, gray_val, gray_val, 255])

                cairo_format = cairo.FORMAT_ARGB32

        elif color_element.components == 3:  # RGB
            pixel_data = _convert_rgb_samples(
                color_element.sample_data, color_element.bits_per_component,
                color_element.width, color_element.height,
                color_element.decode_array
            )
            cairo_format = cairo.FORMAT_ARGB32

        elif color_element.components == 4:  # CMYK
            pixel_data = _convert_cmyk_to_rgb(
                color_element.sample_data, color_element.bits_per_component,
                color_element.width, color_element.height,
                color_element.decode_array
            )
            cairo_format = cairo.FORMAT_RGB24

        else:
            return

        if not pixel_data:
            return

        # 2. Create Cairo surface with proper stride handling
        if isinstance(pixel_data, bytes):
            pixel_data = bytearray(pixel_data)

        required_stride = cairo.ImageSurface.format_stride_for_width(cairo_format, color_element.width)
        bytes_per_pixel = 4 if cairo_format == cairo.FORMAT_RGB24 else 4
        actual_stride = color_element.width * bytes_per_pixel

        if required_stride > actual_stride:
            padded_data = bytearray()
            for row in range(color_element.height):
                row_start = row * actual_stride
                row_end = row_start + actual_stride
                padded_data.extend(pixel_data[row_start:row_end])
                padding_needed = required_stride - actual_stride
                padded_data.extend([0] * padding_needed)
            pixel_data = padded_data

        surface = cairo.ImageSurface.create_for_data(
            pixel_data, cairo_format,
            color_element.width, color_element.height
        )

        # 3. Apply transformation and render
        cairo_ctx.save()
        try:
            cairo_ctx.transform(cairo.Matrix(*color_element.ctm))

            pattern = cairo.SurfacePattern(surface)

            ps_matrix = color_element.image_matrix
            pattern.set_matrix(cairo.Matrix(*ps_matrix))

            if color_element.interpolate:
                pattern.set_filter(cairo.FILTER_BILINEAR)
            else:
                pattern.set_filter(cairo.FILTER_NEAREST)

            cairo_ctx.set_source(pattern)
            cairo_ctx.paint()

        except Exception as e:
            print(f"Colorimage rendering error: {e}")
        finally:
            cairo_ctx.restore()

    except Exception as e:
        print(f"Colorimage element rendering failed: {e}")


# Sample Data Conversion Helper Functions

def _select_cairo_format(bits_per_component, components):
    """Select appropriate Cairo format based on PostScript image parameters

    Note: For grayscale images (components=1), we always use ARGB32 because
    _convert_samples_to_cairo_format() converts all grayscale samples to
    ARGB32 format (4 bytes per pixel). FORMAT_A1 is only appropriate for
    imagemask operations which use separate rendering path.
    """
    # All image types use ARGB32 since the sample conversion always produces ARGB32 data
    return cairo.FORMAT_ARGB32


def _convert_samples_to_cairo_format(sample_data, bits_per_component, width, height, decode_array, components, color_space=None, mask_color=None):
    """Convert PostScript sample data to Cairo pixel format with color space transformation

    mask_color: For ImageType 4 color key masking. Array of n integers (exact match) or
                2n integers (range match) where n=components. Matching pixels get alpha=0.
                Comparison is done BEFORE decode mapping per PLRM.
    """
    try:
        # Check for Indexed color space - needs palette expansion before component dispatch
        if (color_space and isinstance(color_space, list) and len(color_space) >= 4
                and isinstance(color_space[0], str) and color_space[0] == "Indexed"):
            return _convert_indexed_image(sample_data, bits_per_component,
                                          width, height, decode_array,
                                          color_space, mask_color)

        # Check for CIE-based color spaces - convert samples through CIE pipeline
        if (color_space and isinstance(color_space, list) and len(color_space) >= 2
                and isinstance(color_space[0], str)
                and color_space[0] in ("CIEBasedABC", "CIEBasedA")):
            return _convert_cie_image(sample_data, bits_per_component,
                                      width, height, decode_array,
                                      color_space, components, mask_color)

        # CIEBasedDEF/DEFG: convert through Table → ABC → XYZ → sRGB pipeline
        if (color_space and isinstance(color_space, list) and len(color_space) >= 2
                and isinstance(color_space[0], str)
                and color_space[0] in ("CIEBasedDEF", "CIEBasedDEFG")):
            return _convert_cie_def_image(sample_data, bits_per_component,
                                          width, height, decode_array,
                                          color_space, components, mask_color)

        # ICCBased: try ICC Tier 2 bulk image transform, fall back to Tier 1
        if (color_space and isinstance(color_space, list) and len(color_space) >= 2
                and isinstance(color_space[0], str) and color_space[0] == "ICCBased"):
            stream_obj = color_space[1]
            profile_hash = icc_profile.get_profile_hash(stream_obj) if stream_obj else None
            n = ColorSpaceEngine.get_component_count(color_space)

            if profile_hash is not None:
                result = icc_profile.icc_convert_image(
                    profile_hash, n, sample_data, width, height,
                    bits_per_component, decode_array)
                if result is not None:
                    if mask_color is not None:
                        _apply_color_key_mask(result, sample_data, bits_per_component,
                                              width, height, n, mask_color)
                    return result

            # Tier 1 fallback: resolve to device space
            device_space = ColorSpaceEngine.resolve_iccbased_space(color_space)
            components = ColorSpaceEngine.COMPONENT_COUNTS.get(device_space, 3)

        if components == 1:  # Grayscale
            # Cython fast paths: return BGRX directly
            if _CYTHON_IMAGE_CONV and mask_color is None:
                if bits_per_component == 8:
                    num_pixels = width * height
                    if _is_identity_decode(decode_array, 1):
                        return gray8_to_bgrx(sample_data, num_pixels)
                    else:
                        lut = _build_decode_lut(decode_array[0], decode_array[1])
                        return gray8_decode_to_bgrx(sample_data, num_pixels, lut)
                elif bits_per_component == 1:
                    return gray1_to_bgrx(sample_data, width, height, decode_array[0], decode_array[1])
                elif bits_per_component == 2:
                    return gray2_to_bgrx(sample_data, width, height, decode_array[0], decode_array[1])
                elif bits_per_component == 4:
                    return gray4_to_bgrx(sample_data, width, height, decode_array[0], decode_array[1])
                elif bits_per_component == 12:
                    return gray12_to_bgrx(sample_data, width, height, decode_array)

            grayscale_data = _convert_grayscale_samples(sample_data, bits_per_component, width, height, decode_array, color_space)

            pixel_data = bytearray()
            for gray_val in grayscale_data:
                pixel_data.extend([gray_val, gray_val, gray_val, 255])

            # Apply mask_color if specified (for grayscale, we need raw samples)
            if mask_color is not None:
                _apply_color_key_mask(pixel_data, sample_data, bits_per_component, width, height, components, mask_color)

            return pixel_data
        elif components == 3:  # RGB
            pixel_data = _convert_rgb_samples(sample_data, bits_per_component, width, height, decode_array)

            # Apply mask_color if specified
            if mask_color is not None:
                _apply_color_key_mask(pixel_data, sample_data, bits_per_component, width, height, components, mask_color)

            return pixel_data
        elif components == 4:  # CMYK
            pixel_data = _convert_cmyk_to_rgb(sample_data, bits_per_component, width, height, decode_array)

            # Apply mask_color if specified
            if mask_color is not None:
                _apply_color_key_mask(pixel_data, sample_data, bits_per_component, width, height, components, mask_color)

            return pixel_data
        else:
            return None
    except Exception as e:
        print(f"Sample conversion error: {e}")
        return None


def _apply_color_key_mask(pixel_data, sample_data, bits_per_component, width, height, components, mask_color):
    """Apply ImageType 4 color key masking - set alpha=0 for matching pixels.

    PLRM: Comparison occurs BEFORE decode mapping, using raw sample values.
    mask_color can be:
      - n integers: exact match (mask if sample equals these values)
      - 2n integers: range match (pairs of min/max for each component)
    """
    try:
        n = components
        is_range = len(mask_color) == 2 * n

        # Cython fast paths
        if _CYTHON_IMAGE_CONV:
            if bits_per_component == 8:
                apply_color_key_mask_8(pixel_data, sample_data, width, height, n, mask_color, is_range)
                return
            elif bits_per_component == 4:
                apply_color_key_mask_4(pixel_data, sample_data, width, height, n, mask_color, is_range)
                return
            elif bits_per_component == 12:
                apply_color_key_mask_12(pixel_data, sample_data, width, height, n, mask_color, is_range)
                return

        if is_range:
            # Range match: mask_color = [min0, max0, min1, max1, ...]
            ranges = [(mask_color[i*2], mask_color[i*2+1]) for i in range(n)]
        else:
            # Exact match: mask_color = [val0, val1, ...]
            exact_values = mask_color[:n]

        # Calculate bytes per sample based on bits_per_component
        if bits_per_component == 8:
            bytes_per_sample = n
            total_pixels = width * height

            for pixel_idx in range(total_pixels):
                sample_offset = pixel_idx * bytes_per_sample

                # Extract raw sample values for this pixel
                if sample_offset + n <= len(sample_data):
                    raw_values = [sample_data[sample_offset + i] for i in range(n)]

                    # Check if pixel matches mask
                    matches = False
                    if is_range:
                        # Range match: each component must be within its range
                        matches = all(ranges[i][0] <= raw_values[i] <= ranges[i][1] for i in range(n))
                    else:
                        # Exact match
                        matches = raw_values == exact_values

                    if matches:
                        # Set alpha to 0 (transparent) - pixel_data is BGRA format, 4 bytes per pixel
                        alpha_offset = pixel_idx * 4 + 3
                        if alpha_offset < len(pixel_data):
                            pixel_data[alpha_offset] = 0

        elif bits_per_component == 12:
            # 12-bit samples - need to extract each 12-bit value
            # IMPORTANT: PostScript pads each ROW to byte boundary
            samples_per_row = width * n
            bits_per_row = samples_per_row * 12
            bytes_per_row = (bits_per_row + 7) // 8

            def get_12bit_sample_from_row(row_data, sample_in_row):
                """Extract 12-bit sample at given index within a row."""
                bit_pos = sample_in_row * 12
                byte_idx = bit_pos // 8
                bit_offset = bit_pos % 8

                if bit_offset == 0:
                    if byte_idx + 1 < len(row_data):
                        return ((row_data[byte_idx] << 4) | (row_data[byte_idx + 1] >> 4))
                    elif byte_idx < len(row_data):
                        return row_data[byte_idx] << 4
                    return 0
                else:  # bit_offset == 4
                    if byte_idx + 1 < len(row_data):
                        return ((row_data[byte_idx] & 0x0F) << 8) | row_data[byte_idx + 1]
                    elif byte_idx < len(row_data):
                        return (row_data[byte_idx] & 0x0F) << 8
                    return 0

            for row in range(height):
                row_start = row * bytes_per_row
                row_data = sample_data[row_start:row_start + bytes_per_row]

                for col in range(width):
                    pixel_idx = row * width + col
                    sample_base = col * n
                    raw_values = [get_12bit_sample_from_row(row_data, sample_base + i) for i in range(n)]

                    # Check if pixel matches mask
                    matches = False
                    if is_range:
                        matches = all(ranges[i][0] <= raw_values[i] <= ranges[i][1] for i in range(n))
                    else:
                        matches = raw_values == exact_values

                    if matches:
                        alpha_offset = pixel_idx * 4 + 3
                        if alpha_offset < len(pixel_data):
                            pixel_data[alpha_offset] = 0

        elif bits_per_component == 4:
            # 4-bit samples - need nibble unpacking, row-padded
            samples_per_row = width * n
            bits_per_row = samples_per_row * 4
            bytes_per_row = (bits_per_row + 7) // 8

            def get_4bit_sample_from_row(row_data, sample_in_row):
                byte_idx = sample_in_row // 2
                if byte_idx >= len(row_data):
                    return 0
                if sample_in_row % 2 == 0:
                    return (row_data[byte_idx] >> 4) & 0x0F
                else:
                    return row_data[byte_idx] & 0x0F

            for row in range(height):
                row_start = row * bytes_per_row
                row_data = sample_data[row_start:row_start + bytes_per_row]

                for col in range(width):
                    pixel_idx = row * width + col
                    sample_base = col * n
                    raw_values = [get_4bit_sample_from_row(row_data, sample_base + i) for i in range(n)]

                    matches = False
                    if is_range:
                        matches = all(ranges[i][0] <= raw_values[i] <= ranges[i][1] for i in range(n))
                    else:
                        matches = raw_values == exact_values

                    if matches:
                        alpha_offset = pixel_idx * 4 + 3
                        if alpha_offset < len(pixel_data):
                            pixel_data[alpha_offset] = 0

    except Exception as e:
        print(f"Color key mask error: {e}")


def _is_identity_decode(decode_array, components):
    """Check if decode array is the identity mapping [0,1] repeated per component."""
    expected = [0, 1] * components
    if len(decode_array) != len(expected):
        return False
    return all(decode_array[i] == expected[i] for i in range(len(expected)))


def _build_decode_lut(d_min, d_max):
    """Build a 256-byte LUT for an 8-bit decode mapping."""
    lut = bytearray(256)
    for i in range(256):
        decoded = d_min + (i / 255.0) * (d_max - d_min)
        lut[i] = max(0, min(255, int(decoded * 255 + 0.5)))
    return lut


def _convert_indexed_image(sample_data, bits_per_component, width, height, decode_array, color_space, mask_color):
    """Convert Indexed color space image to Cairo BGRX format.

    Indexed images have single-component samples that are indices into a color palette.
    The palette maps indices to colors in the base color space (DeviceGray/RGB/CMYK).
    Uses a pre-computed palette array for efficient per-pixel lookup.
    """
    try:
        base_space_obj = color_space[1]
        hival_obj = color_space[2]
        lookup_obj = color_space[3]

        # Get base space name
        if hasattr(base_space_obj, 'TYPE'):
            if hasattr(base_space_obj, 'val'):
                val = base_space_obj.val
                base_space_name = val.decode('ascii') if isinstance(val, bytes) else str(val)
            else:
                base_space_name = "DeviceRGB"
        elif isinstance(base_space_obj, str):
            base_space_name = base_space_obj
        else:
            base_space_name = "DeviceRGB"

        base_component_counts = {"DeviceGray": 1, "DeviceRGB": 3, "DeviceCMYK": 4}
        base_components = base_component_counts.get(base_space_name, 3)

        hival = hival_obj.val if hasattr(hival_obj, 'val') else int(hival_obj)

        # Get lookup data (string only for images; procedure lookup not practical here)
        if hasattr(lookup_obj, 'byte_string'):
            lookup_bytes = lookup_obj.byte_string()
        elif isinstance(lookup_obj, (bytes, bytearray)):
            lookup_bytes = lookup_obj
        else:
            return None

        # Build pre-computed palette: index → (B, G, R, A) tuple
        palette = []
        for idx in range(hival + 1):
            offset = idx * base_components
            if base_space_name == "DeviceGray":
                g = lookup_bytes[offset] if offset < len(lookup_bytes) else 0
                if not isinstance(g, int):
                    g = ord(g)
                palette.append((g, g, g, 255))
            elif base_space_name == "DeviceRGB":
                r = lookup_bytes[offset] if offset < len(lookup_bytes) else 0
                g = lookup_bytes[offset + 1] if offset + 1 < len(lookup_bytes) else 0
                b = lookup_bytes[offset + 2] if offset + 2 < len(lookup_bytes) else 0
                if not isinstance(r, int):
                    r = ord(r)
                if not isinstance(g, int):
                    g = ord(g)
                if not isinstance(b, int):
                    b = ord(b)
                palette.append((b, g, r, 255))
            elif base_space_name == "DeviceCMYK":
                c = lookup_bytes[offset] if offset < len(lookup_bytes) else 0
                m = lookup_bytes[offset + 1] if offset + 1 < len(lookup_bytes) else 0
                y = lookup_bytes[offset + 2] if offset + 2 < len(lookup_bytes) else 0
                k = lookup_bytes[offset + 3] if offset + 3 < len(lookup_bytes) else 0
                if not isinstance(c, int):
                    c = ord(c)
                if not isinstance(m, int):
                    m = ord(m)
                if not isinstance(y, int):
                    y = ord(y)
                if not isinstance(k, int):
                    k = ord(k)
                icc_rgb = icc_default.convert_cmyk_color(
                    c / 255.0, m / 255.0, y / 255.0, k / 255.0)
                if icc_rgb is not None:
                    r = max(0, min(255, int(icc_rgb[0] * 255 + 0.5)))
                    g = max(0, min(255, int(icc_rgb[1] * 255 + 0.5)))
                    b = max(0, min(255, int(icc_rgb[2] * 255 + 0.5)))
                else:
                    r = max(0, 255 - c - k)
                    g = max(0, 255 - m - k)
                    b = max(0, 255 - y - k)
                palette.append((b, g, r, 255))
            else:
                palette.append((0, 0, 0, 255))

        # Fallback entry for out-of-range indices
        fallback = (0, 0, 0, 255)

        # Get decode parameters (maps raw sample to index)
        d_min = decode_array[0] if len(decode_array) >= 1 else 0.0
        d_max = decode_array[1] if len(decode_array) >= 2 else float(hival)
        max_sample = (1 << bits_per_component) - 1

        # Pre-check for identity decode (common case: Decode [0 hival])
        identity_decode = (d_min == 0.0 and d_max == float(hival)
                           and max_sample >= hival)

        pixel_data = bytearray(width * height * 4)
        pixel_idx = 0

        if bits_per_component == 8:
            for row in range(height):
                row_offset = row * width
                for col in range(width):
                    sample_pos = row_offset + col
                    raw = sample_data[sample_pos] if sample_pos < len(sample_data) else 0
                    if not isinstance(raw, int):
                        raw = ord(raw)
                    if identity_decode:
                        index = min(raw, hival)
                    else:
                        index = max(0, min(hival, round(d_min + (raw / max_sample) * (d_max - d_min))))
                    entry = palette[index] if index < len(palette) else fallback
                    pixel_data[pixel_idx] = entry[0]
                    pixel_data[pixel_idx + 1] = entry[1]
                    pixel_data[pixel_idx + 2] = entry[2]
                    pixel_data[pixel_idx + 3] = entry[3]
                    pixel_idx += 4

        elif bits_per_component == 4:
            bytes_per_row = (width * 4 + 7) // 8
            for row in range(height):
                row_start = row * bytes_per_row
                for col in range(width):
                    byte_idx = row_start + col // 2
                    if byte_idx < len(sample_data):
                        byte_val = sample_data[byte_idx]
                        if not isinstance(byte_val, int):
                            byte_val = ord(byte_val)
                        raw = (byte_val >> 4) & 0x0F if col % 2 == 0 else byte_val & 0x0F
                    else:
                        raw = 0
                    if identity_decode:
                        index = min(raw, hival)
                    else:
                        index = max(0, min(hival, round(d_min + (raw / max_sample) * (d_max - d_min))))
                    entry = palette[index] if index < len(palette) else fallback
                    pixel_data[pixel_idx] = entry[0]
                    pixel_data[pixel_idx + 1] = entry[1]
                    pixel_data[pixel_idx + 2] = entry[2]
                    pixel_data[pixel_idx + 3] = entry[3]
                    pixel_idx += 4

        elif bits_per_component == 2:
            bytes_per_row = (width * 2 + 7) // 8
            for row in range(height):
                row_start = row * bytes_per_row
                for col in range(width):
                    bit_offset = col * 2
                    byte_idx = row_start + bit_offset // 8
                    shift = 6 - (bit_offset % 8)
                    if byte_idx < len(sample_data):
                        byte_val = sample_data[byte_idx]
                        if not isinstance(byte_val, int):
                            byte_val = ord(byte_val)
                        raw = (byte_val >> shift) & 0x03
                    else:
                        raw = 0
                    if identity_decode:
                        index = min(raw, hival)
                    else:
                        index = max(0, min(hival, round(d_min + (raw / max_sample) * (d_max - d_min))))
                    entry = palette[index] if index < len(palette) else fallback
                    pixel_data[pixel_idx] = entry[0]
                    pixel_data[pixel_idx + 1] = entry[1]
                    pixel_data[pixel_idx + 2] = entry[2]
                    pixel_data[pixel_idx + 3] = entry[3]
                    pixel_idx += 4

        elif bits_per_component == 1:
            bytes_per_row = (width + 7) // 8
            for row in range(height):
                row_start = row * bytes_per_row
                for col in range(width):
                    byte_idx = row_start + col // 8
                    bit_idx = 7 - (col % 8)
                    if byte_idx < len(sample_data):
                        byte_val = sample_data[byte_idx]
                        if not isinstance(byte_val, int):
                            byte_val = ord(byte_val)
                        raw = (byte_val >> bit_idx) & 1
                    else:
                        raw = 0
                    if identity_decode:
                        index = min(raw, hival)
                    else:
                        index = max(0, min(hival, round(d_min + raw * (d_max - d_min))))
                    entry = palette[index] if index < len(palette) else fallback
                    pixel_data[pixel_idx] = entry[0]
                    pixel_data[pixel_idx + 1] = entry[1]
                    pixel_data[pixel_idx + 2] = entry[2]
                    pixel_data[pixel_idx + 3] = entry[3]
                    pixel_idx += 4

        elif bits_per_component == 12:
            bits_per_row = width * 12
            bytes_per_row = (bits_per_row + 7) // 8
            for row in range(height):
                row_start = row * bytes_per_row
                row_data = sample_data[row_start:row_start + bytes_per_row]
                for col in range(width):
                    bit_pos = col * 12
                    byte_idx = bit_pos // 8
                    bit_offset = bit_pos % 8
                    if bit_offset == 0:
                        if byte_idx + 1 < len(row_data):
                            raw = (row_data[byte_idx] << 4) | (row_data[byte_idx + 1] >> 4)
                        elif byte_idx < len(row_data):
                            raw = row_data[byte_idx] << 4
                        else:
                            raw = 0
                    else:  # bit_offset == 4
                        if byte_idx + 1 < len(row_data):
                            raw = ((row_data[byte_idx] & 0x0F) << 8) | row_data[byte_idx + 1]
                        elif byte_idx < len(row_data):
                            raw = (row_data[byte_idx] & 0x0F) << 8
                        else:
                            raw = 0
                    if identity_decode:
                        index = min(raw, hival)
                    else:
                        index = max(0, min(hival, round(d_min + (raw / max_sample) * (d_max - d_min))))
                    entry = palette[index] if index < len(palette) else fallback
                    pixel_data[pixel_idx] = entry[0]
                    pixel_data[pixel_idx + 1] = entry[1]
                    pixel_data[pixel_idx + 2] = entry[2]
                    pixel_data[pixel_idx + 3] = entry[3]
                    pixel_idx += 4

        # Apply mask_color if specified (ImageType 4)
        if mask_color is not None:
            _apply_color_key_mask(pixel_data, sample_data, bits_per_component, width, height, 1, mask_color)

        return pixel_data

    except Exception as e:
        print(f"Indexed image conversion error: {e}")
        return None


def _is_identity_cie(cie_dict, space_name):
    """Check if a CIE color space is effectively identity sRGB (no transforms).

    For CIEBasedABC: identity MatrixABC, identity MatrixLMN, no decode procedures,
    standard RangeABC [0,1,0,1,0,1]. This covers the common case of documents
    wrapping sRGB as CIEBasedABC for "device independent" labeling.

    For CIEBasedA: identity MatrixA=[1,1,1], identity MatrixLMN, no decode procedures.
    """
    if space_name == "CIEBasedABC":
        # Check for non-identity MatrixABC
        mat_abc = _get_cie_float_array(cie_dict, b"MatrixABC", None)
        if mat_abc is not None and mat_abc != [1, 0, 0, 0, 1, 0, 0, 0, 1]:
            return False
        # Check for non-identity MatrixLMN
        mat_lmn = _get_cie_float_array(cie_dict, b"MatrixLMN", None)
        if mat_lmn is not None and mat_lmn != [1, 0, 0, 0, 1, 0, 0, 0, 1]:
            return False
        # Check for decode procedures (can't shortcut if present)
        if b"DecodeABC" in cie_dict or b"DecodeLMN" in cie_dict:
            return False
        # Check RangeABC
        range_abc = _get_cie_float_array(cie_dict, b"RangeABC", [0, 1, 0, 1, 0, 1])
        if range_abc != [0, 1, 0, 1, 0, 1]:
            return False
        return True
    elif space_name == "CIEBasedA":
        # Check for non-identity MatrixA
        mat_a = _get_cie_float_array(cie_dict, b"MatrixA", None)
        if mat_a is not None and mat_a != [1, 1, 1]:
            return False
        # Check for non-identity MatrixLMN
        mat_lmn = _get_cie_float_array(cie_dict, b"MatrixLMN", None)
        if mat_lmn is not None and mat_lmn != [1, 0, 0, 0, 1, 0, 0, 0, 1]:
            return False
        if b"DecodeA" in cie_dict or b"DecodeLMN" in cie_dict:
            return False
        return True
    return False


def _convert_cie_image(sample_data, bits_per_component, width, height, decode_array,
                       color_space, components, mask_color):
    """Convert CIEBasedABC or CIEBasedA image samples to Cairo BGRX format.

    For the common case where CIE wraps identity sRGB, delegates to the fast
    RGB or grayscale path. Otherwise, converts each pixel through the CIE pipeline.
    """
    try:
        space_name = color_space[0]
        dict_obj = color_space[1]
        cie_dict = dict_obj.val if hasattr(dict_obj, 'val') and isinstance(dict_obj.val, dict) else {}

        # Fast path: identity CIE → use standard RGB/grayscale conversion
        if _is_identity_cie(cie_dict, space_name):
            if space_name == "CIEBasedABC":
                pixel_data = _convert_rgb_samples(sample_data, bits_per_component,
                                                  width, height, decode_array)
                if mask_color is not None:
                    _apply_color_key_mask(pixel_data, sample_data, bits_per_component,
                                          width, height, components, mask_color)
                return pixel_data
            elif space_name == "CIEBasedA":
                grayscale_data = _convert_grayscale_samples(sample_data, bits_per_component,
                                                            width, height, decode_array, None)
                pixel_data = bytearray()
                for gray_val in grayscale_data:
                    pixel_data.extend([gray_val, gray_val, gray_val, 255])
                if mask_color is not None:
                    _apply_color_key_mask(pixel_data, sample_data, bits_per_component,
                                          width, height, components, mask_color)
                return pixel_data

        # Slow path: full CIE conversion per pixel
        if space_name == "CIEBasedABC" and bits_per_component == 8:
            # 8-bit 3-component CIE image
            r_min, r_max = decode_array[0], decode_array[1]
            g_min, g_max = decode_array[2], decode_array[3]
            b_min, b_max = decode_array[4], decode_array[5]

            pixel_data = bytearray(width * height * 4)
            pixel_idx = 0

            for i in range(0, width * height * 3, 3):
                if i + 2 >= len(sample_data):
                    break
                raw_a = sample_data[i]
                raw_b = sample_data[i + 1]
                raw_c = sample_data[i + 2]

                a = r_min + (raw_a / 255.0) * (r_max - r_min)
                b = g_min + (raw_b / 255.0) * (g_max - g_min)
                c = b_min + (raw_c / 255.0) * (b_max - b_min)

                r, g, b_ = ColorSpaceEngine.cie_abc_to_rgb([a, b, c], cie_dict)
                pixel_data[pixel_idx] = max(0, min(255, int(b_ * 255 + 0.5)))
                pixel_data[pixel_idx + 1] = max(0, min(255, int(g * 255 + 0.5)))
                pixel_data[pixel_idx + 2] = max(0, min(255, int(r * 255 + 0.5)))
                pixel_data[pixel_idx + 3] = 255
                pixel_idx += 4

            if mask_color is not None:
                _apply_color_key_mask(pixel_data, sample_data, bits_per_component,
                                      width, height, components, mask_color)
            return pixel_data

        elif space_name == "CIEBasedA" and bits_per_component == 8:
            # 8-bit 1-component CIE grayscale image
            d_min, d_max = decode_array[0], decode_array[1]

            pixel_data = bytearray(width * height * 4)
            pixel_idx = 0

            for i in range(width * height):
                if i >= len(sample_data):
                    break
                raw = sample_data[i]
                a = d_min + (raw / 255.0) * (d_max - d_min)

                r, g, b_ = ColorSpaceEngine.cie_a_to_rgb(a, cie_dict)
                pixel_data[pixel_idx] = max(0, min(255, int(b_ * 255 + 0.5)))
                pixel_data[pixel_idx + 1] = max(0, min(255, int(g * 255 + 0.5)))
                pixel_data[pixel_idx + 2] = max(0, min(255, int(r * 255 + 0.5)))
                pixel_data[pixel_idx + 3] = 255
                pixel_idx += 4

            if mask_color is not None:
                _apply_color_key_mask(pixel_data, sample_data, bits_per_component,
                                      width, height, components, mask_color)
            return pixel_data

        else:
            # For other bit depths, decode samples first then convert through CIE
            if space_name == "CIEBasedABC":
                # Use RGB sample extraction, then convert each pixel
                # First get decoded float RGB-like values by treating as generic 3-component
                pixel_data = _convert_rgb_samples(sample_data, bits_per_component,
                                                  width, height, decode_array)
                if pixel_data is None:
                    return None

                # pixel_data is BGRX format — extract RGB, run through CIE, write back
                for px in range(0, len(pixel_data), 4):
                    if px + 3 >= len(pixel_data):
                        break
                    # Extract from BGRX
                    b_val = pixel_data[px] / 255.0
                    g_val = pixel_data[px + 1] / 255.0
                    r_val = pixel_data[px + 2] / 255.0

                    r, g, b_ = ColorSpaceEngine.cie_abc_to_rgb([r_val, g_val, b_val], cie_dict)
                    pixel_data[px] = max(0, min(255, int(b_ * 255 + 0.5)))
                    pixel_data[px + 1] = max(0, min(255, int(g * 255 + 0.5)))
                    pixel_data[px + 2] = max(0, min(255, int(r * 255 + 0.5)))

                if mask_color is not None:
                    _apply_color_key_mask(pixel_data, sample_data, bits_per_component,
                                          width, height, components, mask_color)
                return pixel_data

            else:  # CIEBasedA
                grayscale_data = _convert_grayscale_samples(sample_data, bits_per_component,
                                                            width, height, decode_array, None)
                if grayscale_data is None:
                    return None

                pixel_data = bytearray(width * height * 4)
                pixel_idx = 0
                for gray_byte in grayscale_data:
                    a = gray_byte / 255.0
                    r, g, b_ = ColorSpaceEngine.cie_a_to_rgb(a, cie_dict)
                    pixel_data[pixel_idx] = max(0, min(255, int(b_ * 255 + 0.5)))
                    pixel_data[pixel_idx + 1] = max(0, min(255, int(g * 255 + 0.5)))
                    pixel_data[pixel_idx + 2] = max(0, min(255, int(r * 255 + 0.5)))
                    pixel_data[pixel_idx + 3] = 255
                    pixel_idx += 4

                if mask_color is not None:
                    _apply_color_key_mask(pixel_data, sample_data, bits_per_component,
                                          width, height, components, mask_color)
                return pixel_data

    except Exception as e:
        return None


def _preconvert_cie_def_table(cie_dict):
    """Pre-convert all CIEBasedDEF Table entries through the full CIE→sRGB pipeline.

    Evaluates DecodeABC, MatrixABC, DecodeLMN, MatrixLMN, XYZ→sRGB once per table
    entry (~36K entries for 33×33×33) instead of per pixel. Returns flat arrays of
    pre-converted R, G, B float values for trilinear interpolation.

    Returns (r_table, g_table, b_table, m1, m2, m3) or None if no Table.
    """
    table_obj = cie_dict.get(b"Table")
    if not table_obj or not hasattr(table_obj, 'val') or len(table_obj.val) < 4:
        return None

    tv = table_obj.val
    m1 = int(tv[0].val) if hasattr(tv[0], 'val') else int(tv[0])
    m2 = int(tv[1].val) if hasattr(tv[1], 'val') else int(tv[1])
    m3 = int(tv[2].val) if hasattr(tv[2], 'val') else int(tv[2])
    strings_obj = tv[3]

    if hasattr(strings_obj, 'val'):
        strings = strings_obj.val
        s_start = getattr(strings_obj, 'start', 0)
    else:
        strings = strings_obj
        s_start = 0

    range_abc = _get_cie_float_array(cie_dict, b"RangeABC", [0, 1, 0, 1, 0, 1])
    abc_min = [range_abc[0], range_abc[2], range_abc[4]]
    abc_scale = [(range_abc[1] - range_abc[0]) / 255.0,
                 (range_abc[3] - range_abc[2]) / 255.0,
                 (range_abc[5] - range_abc[4]) / 255.0]

    total = m1 * m2 * m3
    r_table = [0.0] * total
    g_table = [0.0] * total
    b_table = [0.0] * total

    idx = 0
    for di in range(m1):
        string_obj = strings[s_start + di]
        if hasattr(string_obj, 'byte_string'):
            data = string_obj.byte_string()
        elif hasattr(string_obj, 'val') and isinstance(string_obj.val, (bytes, bytearray)):
            data = string_obj.val
        else:
            data = bytes(string_obj) if not isinstance(string_obj, (bytes, bytearray)) else string_obj

        for ei in range(m2):
            for fi in range(m3):
                offset = (ei * m3 + fi) * 3
                if offset + 2 < len(data):
                    a = abc_min[0] + data[offset] * abc_scale[0]
                    b = abc_min[1] + data[offset + 1] * abc_scale[1]
                    c = abc_min[2] + data[offset + 2] * abc_scale[2]
                else:
                    a, b, c = abc_min[0], abc_min[1], abc_min[2]

                r, g, b_ = ColorSpaceEngine.cie_abc_to_rgb([a, b, c], cie_dict)
                r_table[idx] = r
                g_table[idx] = g
                b_table[idx] = b_
                idx += 1

    return (r_table, g_table, b_table, m1, m2, m3)


def _convert_cie_def_image(sample_data, bits_per_component, width, height, decode_array,
                           color_space, components, mask_color):
    """Convert CIEBasedDEF or CIEBasedDEFG image samples to Cairo BGRX format.

    Pre-converts the CIE Table to sRGB once, then uses fast trilinear interpolation
    per pixel in the pre-converted table. This avoids evaluating Decode procedures
    (which may be complex Lab→XYZ functions) for every pixel.
    """
    try:
        space_name = color_space[0]
        dict_obj = color_space[1]
        cie_dict = dict_obj.val if hasattr(dict_obj, 'val') and isinstance(dict_obj.val, dict) else {}

        if space_name == "CIEBasedDEF" and bits_per_component == 8:
            preconv = _preconvert_cie_def_table(cie_dict)
            if preconv is None:
                # No Table — fallback to treating as RGB
                return _convert_rgb_samples(sample_data, bits_per_component,
                                            width, height, decode_array)

            r_tab, g_tab, b_tab, m1, m2, m3 = preconv
            range_def = _get_cie_float_array(cie_dict, b"RangeDEF", [0, 1, 0, 1, 0, 1])

            d_min, d_max = decode_array[0], decode_array[1]
            e_min, e_max = decode_array[2], decode_array[3]
            f_min, f_max = decode_array[4], decode_array[5]

            # Precompute normalization: pixel byte → table index
            d_range = range_def[1] - range_def[0] if range_def[1] != range_def[0] else 1.0
            e_range = range_def[3] - range_def[2] if range_def[3] != range_def[2] else 1.0
            f_range = range_def[5] - range_def[4] if range_def[5] != range_def[4] else 1.0
            d_scale = (d_max - d_min) / 255.0
            e_scale = (e_max - e_min) / 255.0
            f_scale = (f_max - f_min) / 255.0
            m1_f = float(m1 - 1)
            m2_f = float(m2 - 1)
            m3_f = float(m3 - 1)
            stride_e = m3
            stride_d = m2 * m3

            pixel_data = bytearray(width * height * 4)
            pixel_idx = 0
            num_pixels = width * height

            for i in range(0, num_pixels * 3, 3):
                if i + 2 >= len(sample_data):
                    break

                # Decode bytes to DEF range, then normalize to table indices
                d = d_min + sample_data[i] * d_scale
                e = e_min + sample_data[i + 1] * e_scale
                f = f_min + sample_data[i + 2] * f_scale

                di = max(0.0, min(m1_f, (d - range_def[0]) / d_range * m1_f))
                ei = max(0.0, min(m2_f, (e - range_def[2]) / e_range * m2_f))
                fi = max(0.0, min(m3_f, (f - range_def[4]) / f_range * m3_f))

                di0 = int(di); ei0 = int(ei); fi0 = int(fi)
                di1 = min(di0 + 1, m1 - 1)
                ei1 = min(ei0 + 1, m2 - 1)
                fi1 = min(fi0 + 1, m3 - 1)
                dd = di - di0; de = ei - ei0; df = fi - fi0
                dd1 = 1.0 - dd; de1 = 1.0 - de; df1 = 1.0 - df

                # 8 corner indices
                i000 = di0 * stride_d + ei0 * stride_e + fi0
                i001 = di0 * stride_d + ei0 * stride_e + fi1
                i010 = di0 * stride_d + ei1 * stride_e + fi0
                i011 = di0 * stride_d + ei1 * stride_e + fi1
                i100 = di1 * stride_d + ei0 * stride_e + fi0
                i101 = di1 * stride_d + ei0 * stride_e + fi1
                i110 = di1 * stride_d + ei1 * stride_e + fi0
                i111 = di1 * stride_d + ei1 * stride_e + fi1

                # Trilinear interpolation for each channel
                r = (((r_tab[i000] * df1 + r_tab[i001] * df) * de1 +
                      (r_tab[i010] * df1 + r_tab[i011] * df) * de) * dd1 +
                     ((r_tab[i100] * df1 + r_tab[i101] * df) * de1 +
                      (r_tab[i110] * df1 + r_tab[i111] * df) * de) * dd)
                g = (((g_tab[i000] * df1 + g_tab[i001] * df) * de1 +
                      (g_tab[i010] * df1 + g_tab[i011] * df) * de) * dd1 +
                     ((g_tab[i100] * df1 + g_tab[i101] * df) * de1 +
                      (g_tab[i110] * df1 + g_tab[i111] * df) * de) * dd)
                b_ = (((b_tab[i000] * df1 + b_tab[i001] * df) * de1 +
                       (b_tab[i010] * df1 + b_tab[i011] * df) * de) * dd1 +
                      ((b_tab[i100] * df1 + b_tab[i101] * df) * de1 +
                       (b_tab[i110] * df1 + b_tab[i111] * df) * de) * dd)

                pixel_data[pixel_idx] = max(0, min(255, int(b_ * 255 + 0.5)))
                pixel_data[pixel_idx + 1] = max(0, min(255, int(g * 255 + 0.5)))
                pixel_data[pixel_idx + 2] = max(0, min(255, int(r * 255 + 0.5)))
                pixel_data[pixel_idx + 3] = 255
                pixel_idx += 4

            if mask_color is not None:
                _apply_color_key_mask(pixel_data, sample_data, bits_per_component,
                                      width, height, components, mask_color)
            return pixel_data

        elif space_name == "CIEBasedDEFG" and bits_per_component == 8:
            # 4-component: per-pixel DEFG conversion (DEFG Tables are less common)
            pixel_data = bytearray(width * height * 4)
            pixel_idx = 0

            for i in range(0, width * height * 4, 4):
                if i + 3 >= len(sample_data):
                    break
                d = decode_array[0] + (sample_data[i] / 255.0) * (decode_array[1] - decode_array[0])
                e = decode_array[2] + (sample_data[i+1] / 255.0) * (decode_array[3] - decode_array[2])
                f = decode_array[4] + (sample_data[i+2] / 255.0) * (decode_array[5] - decode_array[4])
                g = decode_array[6] + (sample_data[i+3] / 255.0) * (decode_array[7] - decode_array[6])

                r, gv, b_ = ColorSpaceEngine.cie_defg_to_rgb([d, e, f, g], cie_dict)
                pixel_data[pixel_idx] = max(0, min(255, int(b_ * 255 + 0.5)))
                pixel_data[pixel_idx + 1] = max(0, min(255, int(gv * 255 + 0.5)))
                pixel_data[pixel_idx + 2] = max(0, min(255, int(r * 255 + 0.5)))
                pixel_data[pixel_idx + 3] = 255
                pixel_idx += 4

            if mask_color is not None:
                _apply_color_key_mask(pixel_data, sample_data, bits_per_component,
                                      width, height, components, mask_color)
            return pixel_data

        else:
            # Non-8-bit fallback
            if components == 3:
                return _convert_rgb_samples(sample_data, bits_per_component,
                                            width, height, decode_array)
            return None

    except Exception as e:
        print(f"CIE DEF image conversion error: {e}")
        return None


def _convert_grayscale_samples(sample_data, bits_per_component, width, height, decode_array, color_space=None):
    """Convert grayscale PostScript samples to Cairo format with color space transformation"""
    try:
        if bits_per_component == 8:
            decode_min, decode_max = decode_array[0], decode_array[1]

            # Identity decode shortcut — avoids floating-point rounding errors
            if decode_min == 0 and decode_max == 1:
                return bytearray(sample_data)

            output = bytearray()
            scale = (decode_max - decode_min) / 255.0

            for byte_val in sample_data:
                decoded_val = decode_min + byte_val * scale
                final_val = max(0, min(255, int(decoded_val * 255 + 0.5)))
                output.append(final_val)

            return output

        elif bits_per_component == 1:
            output = bytearray()
            decode_min, decode_max = decode_array[0], decode_array[1]

            bit_index = 0
            for byte_val in sample_data:
                for bit in range(8):
                    if bit_index >= width * height:
                        break

                    bit_val = (byte_val >> (7 - bit)) & 1

                    if bit_val == 0:
                        decoded_val = decode_min
                    else:
                        decoded_val = decode_max

                    final_val = max(0, min(255, int(decoded_val * 255)))
                    output.append(final_val)
                    bit_index += 1

                if bit_index >= width * height:
                    break

            return output

        elif bits_per_component == 4:
            output = bytearray()
            decode_min, decode_max = decode_array[0], decode_array[1]
            scale = (decode_max - decode_min) / 15.0

            sample_index = 0
            total_samples = width * height

            for byte_val in sample_data:
                if sample_index >= total_samples:
                    break

                high_nibble = (byte_val >> 4) & 0x0F
                decoded_val = decode_min + high_nibble * scale
                final_val = max(0, min(255, int(decoded_val * 255)))
                output.append(final_val)
                sample_index += 1

                if sample_index >= total_samples:
                    break

                low_nibble = byte_val & 0x0F
                decoded_val = decode_min + low_nibble * scale
                final_val = max(0, min(255, int(decoded_val * 255)))
                output.append(final_val)
                sample_index += 1

            return output

        elif bits_per_component == 2:
            output = bytearray()
            decode_min, decode_max = decode_array[0], decode_array[1]
            scale = (decode_max - decode_min) / 3.0

            sample_index = 0
            total_samples = width * height

            for byte_val in sample_data:
                if sample_index >= total_samples:
                    break

                for shift in [6, 4, 2, 0]:
                    if sample_index >= total_samples:
                        break

                    two_bit_val = (byte_val >> shift) & 0x03
                    decoded_val = decode_min + two_bit_val * scale
                    final_val = max(0, min(255, int(decoded_val * 255)))

                    output.append(final_val)
                    sample_index += 1

            return output

        elif bits_per_component == 12:
            # 12-bit grayscale: each sample is 12 bits (0-4095 range)
            # IMPORTANT: PostScript pads each ROW to byte boundary
            output = bytearray()
            decode_min, decode_max = decode_array[0], decode_array[1]
            scale = (decode_max - decode_min) / 4095.0

            # Calculate row stride with padding
            bits_per_row = width * 12
            bytes_per_row = (bits_per_row + 7) // 8  # Pad to byte boundary

            def get_12bit_sample_from_row(row_data, sample_in_row):
                """Extract 12-bit sample at given index within a row."""
                bit_pos = sample_in_row * 12
                byte_idx = bit_pos // 8
                bit_offset = bit_pos % 8

                if bit_offset == 0:
                    if byte_idx + 1 < len(row_data):
                        return ((row_data[byte_idx] << 4) | (row_data[byte_idx + 1] >> 4))
                    elif byte_idx < len(row_data):
                        return row_data[byte_idx] << 4
                    return 0
                else:  # bit_offset == 4
                    if byte_idx + 1 < len(row_data):
                        return ((row_data[byte_idx] & 0x0F) << 8) | row_data[byte_idx + 1]
                    elif byte_idx < len(row_data):
                        return (row_data[byte_idx] & 0x0F) << 8
                    return 0

            for row in range(height):
                row_start = row * bytes_per_row
                row_data = sample_data[row_start:row_start + bytes_per_row]

                for col in range(width):
                    sample_val = get_12bit_sample_from_row(row_data, col)
                    decoded_val = decode_min + sample_val * scale
                    final_val = max(0, min(255, int(decoded_val * 255)))
                    output.append(final_val)

            return output

        else:
            return None

    except Exception as e:
        print(f"Grayscale conversion error: {e}")
        return None


def _convert_mask_to_cairo_a1(mask_data, width, height, polarity):
    """Convert 1-bit PostScript mask data to Cairo A1 format"""
    try:
        if polarity:
            return bytearray(mask_data)
        else:
            inverted = bytearray()
            for byte in mask_data:
                inverted.append(byte ^ 0xFF)
            return inverted

    except Exception as e:
        print(f"Mask conversion error: {e}")
        return None


def _convert_rgb_samples(sample_data, bits_per_component, width, height, decode_array):
    """Convert RGB PostScript samples to Cairo RGB24 format"""
    try:
        if bits_per_component == 8:
            # Cython fast path
            if _CYTHON_IMAGE_CONV:
                num_pixels = width * height
                if _is_identity_decode(decode_array, 3):
                    return rgb8_to_bgrx(sample_data, num_pixels)
                else:
                    r_lut = _build_decode_lut(decode_array[0], decode_array[1])
                    g_lut = _build_decode_lut(decode_array[2], decode_array[3])
                    b_lut = _build_decode_lut(decode_array[4], decode_array[5])
                    return rgb8_decode_to_bgrx(sample_data, num_pixels, r_lut, g_lut, b_lut)

            r_min, r_max = decode_array[0], decode_array[1]
            g_min, g_max = decode_array[2], decode_array[3]
            b_min, b_max = decode_array[4], decode_array[5]

            # Identity decode shortcut
            if (r_min == 0 and r_max == 1 and g_min == 0 and
                    g_max == 1 and b_min == 0 and b_max == 1):
                output = bytearray(width * height * 4)
                for i in range(0, len(sample_data), 3):
                    if i + 2 >= len(sample_data):
                        break
                    j = (i // 3) * 4
                    output[j] = sample_data[i + 2]      # B
                    output[j + 1] = sample_data[i + 1]  # G
                    output[j + 2] = sample_data[i]      # R
                    output[j + 3] = 255
                return output

            output = bytearray()

            for i in range(0, len(sample_data), 3):
                if i + 2 >= len(sample_data):
                    break

                r_val = sample_data[i]
                g_val = sample_data[i + 1]
                b_val = sample_data[i + 2]

                r_decoded = r_min + (r_val / 255.0) * (r_max - r_min)
                g_decoded = g_min + (g_val / 255.0) * (g_max - g_min)
                b_decoded = b_min + (b_val / 255.0) * (b_max - b_min)

                final_r = max(0, min(255, int(r_decoded * 255 + 0.5)))
                final_g = max(0, min(255, int(g_decoded * 255 + 0.5)))
                final_b = max(0, min(255, int(b_decoded * 255 + 0.5)))

                output.append(final_b)
                output.append(final_g)
                output.append(final_r)
                output.append(255)

            return output

        elif bits_per_component == 4:
            # Cython fast path
            if _CYTHON_IMAGE_CONV:
                return rgb4_to_bgrx(sample_data, width, height, decode_array)

            # 4-bit RGB: each sample is 4 bits, 3 components = 12 bits per pixel
            # IMPORTANT: PostScript pads each ROW to byte boundary
            output = bytearray()

            r_min, r_max = decode_array[0], decode_array[1]
            g_min, g_max = decode_array[2], decode_array[3]
            b_min, b_max = decode_array[4], decode_array[5]

            r_scale = (r_max - r_min) / 15.0
            g_scale = (g_max - g_min) / 15.0
            b_scale = (b_max - b_min) / 15.0

            # Calculate row stride with padding
            samples_per_row = width * 3  # R, G, B for each pixel
            bits_per_row = samples_per_row * 4
            bytes_per_row = (bits_per_row + 7) // 8  # Pad to byte boundary

            def get_4bit_sample_from_row(row_data, sample_in_row):
                """Extract 4-bit sample at given index within a row."""
                byte_idx = sample_in_row // 2
                if byte_idx >= len(row_data):
                    return 0
                if sample_in_row % 2 == 0:
                    return (row_data[byte_idx] >> 4) & 0x0F
                else:
                    return row_data[byte_idx] & 0x0F

            for row in range(height):
                row_start = row * bytes_per_row
                row_data = sample_data[row_start:row_start + bytes_per_row]

                for col in range(width):
                    sample_base = col * 3
                    r_val = get_4bit_sample_from_row(row_data, sample_base)
                    g_val = get_4bit_sample_from_row(row_data, sample_base + 1)
                    b_val = get_4bit_sample_from_row(row_data, sample_base + 2)

                    r_decoded = r_min + r_val * r_scale
                    g_decoded = g_min + g_val * g_scale
                    b_decoded = b_min + b_val * b_scale

                    output.append(max(0, min(255, int(b_decoded * 255))))
                    output.append(max(0, min(255, int(g_decoded * 255))))
                    output.append(max(0, min(255, int(r_decoded * 255))))
                    output.append(255)

            return output

        elif bits_per_component == 12:
            # Cython fast path
            if _CYTHON_IMAGE_CONV:
                return rgb12_to_bgrx(sample_data, width, height, decode_array)

            # 12-bit RGB: each sample is 12 bits (0-4095 range)
            # 3 components = 36 bits per pixel
            # IMPORTANT: PostScript pads each ROW to byte boundary
            output = bytearray()

            r_min, r_max = decode_array[0], decode_array[1]
            g_min, g_max = decode_array[2], decode_array[3]
            b_min, b_max = decode_array[4], decode_array[5]

            r_scale = (r_max - r_min) / 4095.0
            g_scale = (g_max - g_min) / 4095.0
            b_scale = (b_max - b_min) / 4095.0

            # Calculate row stride with padding
            samples_per_row = width * 3  # R, G, B for each pixel
            bits_per_row = samples_per_row * 12
            bytes_per_row = (bits_per_row + 7) // 8  # Pad to byte boundary

            def get_12bit_sample_from_row(row_data, sample_in_row):
                """Extract 12-bit sample at given index within a row."""
                bit_pos = sample_in_row * 12
                byte_idx = bit_pos // 8
                bit_offset = bit_pos % 8

                if bit_offset == 0:
                    # Sample at byte boundary
                    if byte_idx + 1 < len(row_data):
                        return ((row_data[byte_idx] << 4) | (row_data[byte_idx + 1] >> 4))
                    elif byte_idx < len(row_data):
                        return row_data[byte_idx] << 4
                    return 0
                else:  # bit_offset == 4
                    # Sample at nibble boundary
                    if byte_idx + 1 < len(row_data):
                        return ((row_data[byte_idx] & 0x0F) << 8) | row_data[byte_idx + 1]
                    elif byte_idx < len(row_data):
                        return (row_data[byte_idx] & 0x0F) << 8
                    return 0

            for row in range(height):
                # Extract this row's data (accounting for row padding)
                row_start = row * bytes_per_row
                row_data = sample_data[row_start:row_start + bytes_per_row]

                for col in range(width):
                    sample_base = col * 3  # 3 samples (R, G, B) per pixel

                    r_val = get_12bit_sample_from_row(row_data, sample_base)
                    g_val = get_12bit_sample_from_row(row_data, sample_base + 1)
                    b_val = get_12bit_sample_from_row(row_data, sample_base + 2)

                    r_decoded = r_min + r_val * r_scale
                    g_decoded = g_min + g_val * g_scale
                    b_decoded = b_min + b_val * b_scale

                    final_r = max(0, min(255, int(r_decoded * 255)))
                    final_g = max(0, min(255, int(g_decoded * 255)))
                    final_b = max(0, min(255, int(b_decoded * 255)))

                    # Cairo ARGB32 format: B, G, R, A
                    output.append(final_b)
                    output.append(final_g)
                    output.append(final_r)
                    output.append(255)

            return output

        else:
            return None

    except Exception as e:
        return None


def _convert_cmyk_to_rgb(sample_data, bits_per_component, width, height, decode_array):
    """Convert CMYK PostScript samples to RGB for Cairo rendering"""
    try:
        # Try default ICC bulk conversion (8-bit only)
        if bits_per_component == 8:
            icc_result = icc_default.convert_cmyk_image(
                sample_data, width, height, bits_per_component, decode_array)
            if icc_result is not None:
                return icc_result

        if bits_per_component == 8:
            # Cython fast path
            if _CYTHON_IMAGE_CONV:
                num_pixels = width * height
                if _is_identity_decode(decode_array, 4):
                    return cmyk8_to_bgrx(sample_data, num_pixels)
                else:
                    c_lut = _build_decode_lut(decode_array[0], decode_array[1])
                    m_lut = _build_decode_lut(decode_array[2], decode_array[3])
                    y_lut = _build_decode_lut(decode_array[4], decode_array[5])
                    k_lut = _build_decode_lut(decode_array[6], decode_array[7])
                    return cmyk8_decode_to_bgrx(sample_data, num_pixels, c_lut, m_lut, y_lut, k_lut)

            c_min, c_max = decode_array[0], decode_array[1]
            m_min, m_max = decode_array[2], decode_array[3]
            y_min, y_max = decode_array[4], decode_array[5]
            k_min, k_max = decode_array[6], decode_array[7]

            # Identity decode shortcut — use integer math matching Cython path
            if (c_min == 0 and c_max == 1 and m_min == 0 and m_max == 1 and
                    y_min == 0 and y_max == 1 and k_min == 0 and k_max == 1):
                output = bytearray()
                for i in range(0, len(sample_data), 4):
                    if i + 3 >= len(sample_data):
                        break
                    c_val = sample_data[i]
                    m_val = sample_data[i + 1]
                    y_val = sample_data[i + 2]
                    k_val = sample_data[i + 3]
                    r = 255 - c_val - k_val
                    if r < 0: r = 0
                    g = 255 - m_val - k_val
                    if g < 0: g = 0
                    b = 255 - y_val - k_val
                    if b < 0: b = 0
                    output.append(b)
                    output.append(g)
                    output.append(r)
                    output.append(255)
                return output

            output = bytearray()

            for i in range(0, len(sample_data), 4):
                if i + 3 >= len(sample_data):
                    break

                c_val = sample_data[i]
                m_val = sample_data[i + 1]
                y_val = sample_data[i + 2]
                k_val = sample_data[i + 3]

                c = c_min + (c_val / 255.0) * (c_max - c_min)
                m = m_min + (m_val / 255.0) * (m_max - m_min)
                y = y_min + (y_val / 255.0) * (y_max - y_min)
                k = k_min + (k_val / 255.0) * (k_max - k_min)

                r = 1.0 - min(1.0, c + k)
                g = 1.0 - min(1.0, m + k)
                b = 1.0 - min(1.0, y + k)

                output.append(max(0, min(255, int(b * 255 + 0.5))))
                output.append(max(0, min(255, int(g * 255 + 0.5))))
                output.append(max(0, min(255, int(r * 255 + 0.5))))
                output.append(255)

            return output

        elif bits_per_component == 4:
            # Cython fast path
            if _CYTHON_IMAGE_CONV:
                return cmyk4_to_bgrx(sample_data, width, height, decode_array)

            output = bytearray()

            c_min, c_max = decode_array[0], decode_array[1]
            m_min, m_max = decode_array[2], decode_array[3]
            y_min, y_max = decode_array[4], decode_array[5]
            k_min, k_max = decode_array[6], decode_array[7]

            c_scale = (c_max - c_min) / 15.0
            m_scale = (m_max - m_min) / 15.0
            y_scale = (y_max - y_min) / 15.0
            k_scale = (k_max - k_min) / 15.0

            byte_index = 0
            pixel_count = 0
            total_pixels = width * height

            while pixel_count < total_pixels and byte_index + 1 < len(sample_data):
                byte1 = sample_data[byte_index]
                byte2 = sample_data[byte_index + 1]

                c_val = (byte1 >> 4) & 0x0F
                m_val = byte1 & 0x0F
                y_val = (byte2 >> 4) & 0x0F
                k_val = byte2 & 0x0F

                c = c_min + c_val * c_scale
                m = m_min + m_val * m_scale
                y = y_min + y_val * y_scale
                k = k_min + k_val * k_scale

                r = 1.0 - min(1.0, c + k)
                g = 1.0 - min(1.0, m + k)
                b = 1.0 - min(1.0, y + k)

                output.append(max(0, min(255, int(b * 255))))
                output.append(max(0, min(255, int(g * 255))))
                output.append(max(0, min(255, int(r * 255))))
                output.append(255)

                pixel_count += 1
                byte_index += 2

            return output

        elif bits_per_component == 12:
            # Cython fast path
            if _CYTHON_IMAGE_CONV:
                return cmyk12_to_bgrx(sample_data, width, height, decode_array)

            # 12-bit CMYK: each sample is 12 bits (0-4095 range)
            # IMPORTANT: PostScript pads each ROW to byte boundary
            output = bytearray()

            c_min, c_max = decode_array[0], decode_array[1]
            m_min, m_max = decode_array[2], decode_array[3]
            y_min, y_max = decode_array[4], decode_array[5]
            k_min, k_max = decode_array[6], decode_array[7]

            c_scale = (c_max - c_min) / 4095.0
            m_scale = (m_max - m_min) / 4095.0
            y_scale = (y_max - y_min) / 4095.0
            k_scale = (k_max - k_min) / 4095.0

            # Calculate row stride with padding
            samples_per_row = width * 4  # C, M, Y, K for each pixel
            bits_per_row = samples_per_row * 12
            bytes_per_row = (bits_per_row + 7) // 8  # Pad to byte boundary

            def get_12bit_sample_from_row(row_data, sample_in_row):
                """Extract 12-bit sample at given index within a row."""
                bit_pos = sample_in_row * 12
                byte_idx = bit_pos // 8
                bit_offset = bit_pos % 8

                if bit_offset == 0:
                    if byte_idx + 1 < len(row_data):
                        return ((row_data[byte_idx] << 4) | (row_data[byte_idx + 1] >> 4))
                    elif byte_idx < len(row_data):
                        return row_data[byte_idx] << 4
                    return 0
                else:  # bit_offset == 4
                    if byte_idx + 1 < len(row_data):
                        return ((row_data[byte_idx] & 0x0F) << 8) | row_data[byte_idx + 1]
                    elif byte_idx < len(row_data):
                        return (row_data[byte_idx] & 0x0F) << 8
                    return 0

            for row in range(height):
                row_start = row * bytes_per_row
                row_data = sample_data[row_start:row_start + bytes_per_row]

                for col in range(width):
                    sample_base = col * 4  # 4 samples (C, M, Y, K) per pixel

                    c_val = get_12bit_sample_from_row(row_data, sample_base)
                    m_val = get_12bit_sample_from_row(row_data, sample_base + 1)
                    y_val = get_12bit_sample_from_row(row_data, sample_base + 2)
                    k_val = get_12bit_sample_from_row(row_data, sample_base + 3)

                    c = c_min + c_val * c_scale
                    m = m_min + m_val * m_scale
                    y = y_min + y_val * y_scale
                    k = k_min + k_val * k_scale

                    r = (1.0 - c) * (1.0 - k)
                    g = (1.0 - m) * (1.0 - k)
                    b = (1.0 - y) * (1.0 - k)

                    output.append(max(0, min(255, int(b * 255))))
                    output.append(max(0, min(255, int(g * 255))))
                    output.append(max(0, min(255, int(r * 255))))
                    output.append(255)

            return output

        else:
            return None

    except Exception as e:
        return None
