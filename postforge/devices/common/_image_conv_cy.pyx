# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

# cython: boundscheck=False, wraparound=False, cdivision=True
"""
Cython-accelerated 8-bit image sample conversion for Cairo BGRX format.

All functions accept bytes/bytearray input and return bytearray output
in Cairo's native BGRX (ARGB32) pixel order: [B, G, R, 0xFF] per pixel.
"""

from libc.stdlib cimport malloc, free
from libc.string cimport memset


def gray8_to_bgrx(const unsigned char[::1] src, int num_pixels):
    """Convert 8-bit grayscale with identity decode [0,1] to BGRX."""
    cdef int out_len = num_pixels * 4
    cdef bytearray result = bytearray(out_len)
    cdef unsigned char[::1] out = result
    cdef int i, o
    cdef unsigned char g

    with nogil:
        for i in range(num_pixels):
            g = src[i]
            o = i * 4
            out[o] = g
            out[o + 1] = g
            out[o + 2] = g
            out[o + 3] = 0xFF

    return result


def gray8_decode_to_bgrx(const unsigned char[::1] src, int num_pixels,
                          const unsigned char[::1] lut):
    """Convert 8-bit grayscale with custom decode via 256-byte LUT to BGRX."""
    cdef int out_len = num_pixels * 4
    cdef bytearray result = bytearray(out_len)
    cdef unsigned char[::1] out = result
    cdef int i, o
    cdef unsigned char g

    with nogil:
        for i in range(num_pixels):
            g = lut[src[i]]
            o = i * 4
            out[o] = g
            out[o + 1] = g
            out[o + 2] = g
            out[o + 3] = 0xFF

    return result


def rgb8_to_bgrx(const unsigned char[::1] src, int num_pixels):
    """Convert 8-bit RGB with identity decode [0,1,0,1,0,1] to BGRX (swap R<->B, pad alpha)."""
    cdef int out_len = num_pixels * 4
    cdef bytearray result = bytearray(out_len)
    cdef unsigned char[::1] out = result
    cdef int i, si, di

    with nogil:
        for i in range(num_pixels):
            si = i * 3
            di = i * 4
            out[di] = src[si + 2]      # B
            out[di + 1] = src[si + 1]  # G
            out[di + 2] = src[si]      # R
            out[di + 3] = 0xFF

    return result


def rgb8_decode_to_bgrx(const unsigned char[::1] src, int num_pixels,
                         const unsigned char[::1] r_lut,
                         const unsigned char[::1] g_lut,
                         const unsigned char[::1] b_lut):
    """Convert 8-bit RGB with custom decode via per-channel 256-byte LUTs to BGRX."""
    cdef int out_len = num_pixels * 4
    cdef bytearray result = bytearray(out_len)
    cdef unsigned char[::1] out = result
    cdef int i, si, di

    with nogil:
        for i in range(num_pixels):
            si = i * 3
            di = i * 4
            out[di] = b_lut[src[si + 2]]      # B
            out[di + 1] = g_lut[src[si + 1]]  # G
            out[di + 2] = r_lut[src[si]]      # R
            out[di + 3] = 0xFF

    return result


def cmyk8_to_bgrx(const unsigned char[::1] src, int num_pixels):
    """Convert 8-bit CMYK with identity decode [0,1,...] to BGRX using integer CMYK->RGB."""
    cdef int out_len = num_pixels * 4
    cdef bytearray result = bytearray(out_len)
    cdef unsigned char[::1] out = result
    cdef int i, si, di
    cdef int c, m, y, k, r, g, b

    with nogil:
        for i in range(num_pixels):
            si = i * 4
            di = i * 4
            c = src[si]
            m = src[si + 1]
            y = src[si + 2]
            k = src[si + 3]

            # R = 255 - min(255, C + K)
            r = 255 - c - k
            if r < 0:
                r = 0
            g = 255 - m - k
            if g < 0:
                g = 0
            b = 255 - y - k
            if b < 0:
                b = 0

            out[di] = <unsigned char>b
            out[di + 1] = <unsigned char>g
            out[di + 2] = <unsigned char>r
            out[di + 3] = 0xFF

    return result


def cmyk8_decode_to_bgrx(const unsigned char[::1] src, int num_pixels,
                          const unsigned char[::1] c_lut,
                          const unsigned char[::1] m_lut,
                          const unsigned char[::1] y_lut,
                          const unsigned char[::1] k_lut):
    """Convert 8-bit CMYK with custom decode via per-channel 256-byte LUTs to BGRX.

    LUTs map raw byte -> decoded 0-255 value. The function then applies
    R = 255 - min(255, C_decoded + K_decoded), etc.
    """
    cdef int out_len = num_pixels * 4
    cdef bytearray result = bytearray(out_len)
    cdef unsigned char[::1] out = result
    cdef int i, si, di
    cdef int c, m, y, k, r, g, b

    with nogil:
        for i in range(num_pixels):
            si = i * 4
            di = i * 4
            c = c_lut[src[si]]
            m = m_lut[src[si + 1]]
            y = y_lut[src[si + 2]]
            k = k_lut[src[si + 3]]

            r = 255 - c - k
            if r < 0:
                r = 0
            g = 255 - m - k
            if g < 0:
                g = 0
            b = 255 - y - k
            if b < 0:
                b = 0

            out[di] = <unsigned char>b
            out[di + 1] = <unsigned char>g
            out[di + 2] = <unsigned char>r
            out[di + 3] = 0xFF

    return result


# ---------------------------------------------------------------------------
# Sub-byte grayscale conversions (1, 2, 4-bit) — output BGRX directly
# ---------------------------------------------------------------------------

def gray1_to_bgrx(const unsigned char[::1] src, int width, int height,
                   double decode_min_val, double decode_max_val):
    """Convert 1-bit grayscale to BGRX. No row padding, MSB-first."""
    cdef int total = width * height
    cdef int out_len = total * 4
    cdef bytearray result = bytearray(out_len)
    cdef unsigned char[::1] out = result
    cdef unsigned char lut[2]
    cdef int v0, v1
    cdef int bit_index, o, byte_idx, bit, bv
    cdef unsigned char g

    # Pre-compute 2-entry LUT
    v0 = <int>(decode_min_val * 255.0)
    if v0 < 0: v0 = 0
    if v0 > 255: v0 = 255
    v1 = <int>(decode_max_val * 255.0)
    if v1 < 0: v1 = 0
    if v1 > 255: v1 = 255
    lut[0] = <unsigned char>v0
    lut[1] = <unsigned char>v1

    bit_index = 0
    byte_idx = 0
    with nogil:
        while bit_index < total:
            bv = src[byte_idx]
            for bit in range(8):
                if bit_index >= total:
                    break
                g = lut[(bv >> (7 - bit)) & 1]
                o = bit_index * 4
                out[o] = g
                out[o + 1] = g
                out[o + 2] = g
                out[o + 3] = 0xFF
                bit_index += 1
            byte_idx += 1

    return result


def gray2_to_bgrx(const unsigned char[::1] src, int width, int height,
                   double decode_min_val, double decode_max_val):
    """Convert 2-bit grayscale to BGRX. No row padding."""
    cdef int total = width * height
    cdef int out_len = total * 4
    cdef bytearray result = bytearray(out_len)
    cdef unsigned char[::1] out = result
    cdef unsigned char lut[4]
    cdef int i, o, sample_index, byte_idx, shift, val
    cdef unsigned char g
    cdef double scale

    # Pre-compute 4-entry LUT
    scale = (decode_max_val - decode_min_val) / 3.0
    for i in range(4):
        val = <int>((decode_min_val + i * scale) * 255.0)
        if val < 0: val = 0
        if val > 255: val = 255
        lut[i] = <unsigned char>val

    cdef int shifts[4]
    shifts[0] = 6
    shifts[1] = 4
    shifts[2] = 2
    shifts[3] = 0

    sample_index = 0
    byte_idx = 0
    with nogil:
        while sample_index < total:
            for i in range(4):
                if sample_index >= total:
                    break
                g = lut[(src[byte_idx] >> shifts[i]) & 0x03]
                o = sample_index * 4
                out[o] = g
                out[o + 1] = g
                out[o + 2] = g
                out[o + 3] = 0xFF
                sample_index += 1
            byte_idx += 1

    return result


def gray4_to_bgrx(const unsigned char[::1] src, int width, int height,
                   double decode_min_val, double decode_max_val):
    """Convert 4-bit grayscale to BGRX. No row padding."""
    cdef int total = width * height
    cdef int out_len = total * 4
    cdef bytearray result = bytearray(out_len)
    cdef unsigned char[::1] out = result
    cdef unsigned char lut[16]
    cdef int i, o, sample_index, byte_idx, val
    cdef unsigned char g, bv
    cdef double scale

    # Pre-compute 16-entry LUT
    scale = (decode_max_val - decode_min_val) / 15.0
    for i in range(16):
        val = <int>((decode_min_val + i * scale) * 255.0)
        if val < 0: val = 0
        if val > 255: val = 255
        lut[i] = <unsigned char>val

    sample_index = 0
    byte_idx = 0
    with nogil:
        while sample_index < total:
            bv = src[byte_idx]
            # High nibble
            g = lut[(bv >> 4) & 0x0F]
            o = sample_index * 4
            out[o] = g
            out[o + 1] = g
            out[o + 2] = g
            out[o + 3] = 0xFF
            sample_index += 1
            if sample_index >= total:
                break
            # Low nibble
            g = lut[bv & 0x0F]
            o = sample_index * 4
            out[o] = g
            out[o + 1] = g
            out[o + 2] = g
            out[o + 3] = 0xFF
            sample_index += 1
            byte_idx += 1

    return result


def gray12_to_bgrx(const unsigned char[::1] src, int width, int height,
                    object decode_array):
    """Convert 12-bit grayscale to BGRX. Row-padded to byte boundary."""
    cdef int total = width * height
    cdef int out_len = total * 4
    cdef bytearray result = bytearray(out_len)
    cdef unsigned char[::1] out = result
    cdef double decode_min = decode_array[0]
    cdef double decode_max = decode_array[1]
    cdef double scale = (decode_max - decode_min) / 4095.0
    cdef int bits_per_row = width * 12
    cdef int bytes_per_row = (bits_per_row + 7) // 8
    cdef int row, col, row_start, bit_pos, byte_idx, bit_offset, sample_val, val, o, pixel_idx

    with nogil:
        for row in range(height):
            row_start = row * bytes_per_row
            for col in range(width):
                bit_pos = col * 12
                byte_idx = row_start + bit_pos // 8
                bit_offset = bit_pos % 8
                if bit_offset == 0:
                    sample_val = (src[byte_idx] << 4) | (src[byte_idx + 1] >> 4)
                else:
                    sample_val = ((src[byte_idx] & 0x0F) << 8) | src[byte_idx + 1]
                val = <int>((decode_min + sample_val * scale) * 255.0)
                if val < 0: val = 0
                if val > 255: val = 255
                pixel_idx = row * width + col
                o = pixel_idx * 4
                out[o] = <unsigned char>val
                out[o + 1] = <unsigned char>val
                out[o + 2] = <unsigned char>val
                out[o + 3] = 0xFF

    return result


# ---------------------------------------------------------------------------
# Sub-byte RGB conversions (4-bit, 12-bit) — output BGRX directly
# ---------------------------------------------------------------------------

def rgb4_to_bgrx(const unsigned char[::1] src, int width, int height,
                  object decode_array):
    """Convert 4-bit RGB to BGRX. Row-padded to byte boundary."""
    cdef int out_len = width * height * 4
    cdef bytearray result = bytearray(out_len)
    cdef unsigned char[::1] out = result
    cdef double r_min = decode_array[0], r_max = decode_array[1]
    cdef double g_min = decode_array[2], g_max = decode_array[3]
    cdef double b_min = decode_array[4], b_max = decode_array[5]
    cdef double r_scale = (r_max - r_min) / 15.0
    cdef double g_scale = (g_max - g_min) / 15.0
    cdef double b_scale = (b_max - b_min) / 15.0
    cdef int samples_per_row = width * 3
    cdef int bits_per_row = samples_per_row * 4
    cdef int bytes_per_row = (bits_per_row + 7) // 8
    cdef int row, col, row_start, sample_base, si, byte_idx, rv, gv, bv, o, val
    cdef int pixel_idx

    with nogil:
        for row in range(height):
            row_start = row * bytes_per_row
            for col in range(width):
                sample_base = col * 3
                pixel_idx = row * width + col
                o = pixel_idx * 4

                # Extract 3 nibbles for R, G, B
                si = sample_base
                byte_idx = si // 2
                if si % 2 == 0:
                    rv = (src[row_start + byte_idx] >> 4) & 0x0F
                else:
                    rv = src[row_start + byte_idx] & 0x0F

                si = sample_base + 1
                byte_idx = si // 2
                if si % 2 == 0:
                    gv = (src[row_start + byte_idx] >> 4) & 0x0F
                else:
                    gv = src[row_start + byte_idx] & 0x0F

                si = sample_base + 2
                byte_idx = si // 2
                if si % 2 == 0:
                    bv = (src[row_start + byte_idx] >> 4) & 0x0F
                else:
                    bv = src[row_start + byte_idx] & 0x0F

                val = <int>((b_min + bv * b_scale) * 255.0)
                if val < 0: val = 0
                if val > 255: val = 255
                out[o] = <unsigned char>val

                val = <int>((g_min + gv * g_scale) * 255.0)
                if val < 0: val = 0
                if val > 255: val = 255
                out[o + 1] = <unsigned char>val

                val = <int>((r_min + rv * r_scale) * 255.0)
                if val < 0: val = 0
                if val > 255: val = 255
                out[o + 2] = <unsigned char>val

                out[o + 3] = 0xFF

    return result


def rgb12_to_bgrx(const unsigned char[::1] src, int width, int height,
                   object decode_array):
    """Convert 12-bit RGB to BGRX. Row-padded to byte boundary."""
    cdef int out_len = width * height * 4
    cdef bytearray result = bytearray(out_len)
    cdef unsigned char[::1] out = result
    cdef double r_min = decode_array[0], r_max = decode_array[1]
    cdef double g_min = decode_array[2], g_max = decode_array[3]
    cdef double b_min = decode_array[4], b_max = decode_array[5]
    cdef double r_scale = (r_max - r_min) / 4095.0
    cdef double g_scale = (g_max - g_min) / 4095.0
    cdef double b_scale = (b_max - b_min) / 4095.0
    cdef int samples_per_row = width * 3
    cdef int bits_per_row = samples_per_row * 12
    cdef int bytes_per_row = (bits_per_row + 7) // 8
    cdef int row, col, row_start, sample_base, bit_pos, byte_idx, bit_offset
    cdef int rv, gv, bv, o, val, pixel_idx, si

    with nogil:
        for row in range(height):
            row_start = row * bytes_per_row
            for col in range(width):
                sample_base = col * 3
                pixel_idx = row * width + col
                o = pixel_idx * 4

                # Extract 3 x 12-bit samples
                for si in range(3):
                    bit_pos = (sample_base + si) * 12
                    byte_idx = row_start + bit_pos // 8
                    bit_offset = bit_pos % 8
                    if bit_offset == 0:
                        val = (src[byte_idx] << 4) | (src[byte_idx + 1] >> 4)
                    else:
                        val = ((src[byte_idx] & 0x0F) << 8) | src[byte_idx + 1]
                    if si == 0:
                        rv = val
                    elif si == 1:
                        gv = val
                    else:
                        bv = val

                val = <int>((b_min + bv * b_scale) * 255.0)
                if val < 0: val = 0
                if val > 255: val = 255
                out[o] = <unsigned char>val

                val = <int>((g_min + gv * g_scale) * 255.0)
                if val < 0: val = 0
                if val > 255: val = 255
                out[o + 1] = <unsigned char>val

                val = <int>((r_min + rv * r_scale) * 255.0)
                if val < 0: val = 0
                if val > 255: val = 255
                out[o + 2] = <unsigned char>val

                out[o + 3] = 0xFF

    return result


# ---------------------------------------------------------------------------
# Sub-byte CMYK conversions (4-bit, 12-bit) — output BGRX directly
# ---------------------------------------------------------------------------

def cmyk4_to_bgrx(const unsigned char[::1] src, int width, int height,
                   object decode_array):
    """Convert 4-bit CMYK to BGRX. NO row padding (2 bytes per pixel exact)."""
    cdef int total = width * height
    cdef int out_len = total * 4
    cdef bytearray result = bytearray(out_len)
    cdef unsigned char[::1] out = result
    cdef double c_min = decode_array[0], c_max = decode_array[1]
    cdef double m_min = decode_array[2], m_max = decode_array[3]
    cdef double y_min = decode_array[4], y_max = decode_array[5]
    cdef double k_min = decode_array[6], k_max = decode_array[7]
    cdef double c_scale = (c_max - c_min) / 15.0
    cdef double m_scale = (m_max - m_min) / 15.0
    cdef double y_scale = (y_max - y_min) / 15.0
    cdef double k_scale = (k_max - k_min) / 15.0
    cdef int pixel, byte_idx, o, val
    cdef unsigned char b1, b2
    cdef double c, m, y, k, r, g, b

    byte_idx = 0
    with nogil:
        for pixel in range(total):
            b1 = src[byte_idx]
            b2 = src[byte_idx + 1]

            c = c_min + ((b1 >> 4) & 0x0F) * c_scale
            m = m_min + (b1 & 0x0F) * m_scale
            y = y_min + ((b2 >> 4) & 0x0F) * y_scale
            k = k_min + (b2 & 0x0F) * k_scale

            r = 1.0 - c - k
            if r < 0.0: r = 0.0
            if r > 1.0: r = 1.0
            g = 1.0 - m - k
            if g < 0.0: g = 0.0
            if g > 1.0: g = 1.0
            b = 1.0 - y - k
            if b < 0.0: b = 0.0
            if b > 1.0: b = 1.0

            o = pixel * 4
            val = <int>(b * 255.0)
            if val < 0: val = 0
            if val > 255: val = 255
            out[o] = <unsigned char>val

            val = <int>(g * 255.0)
            if val < 0: val = 0
            if val > 255: val = 255
            out[o + 1] = <unsigned char>val

            val = <int>(r * 255.0)
            if val < 0: val = 0
            if val > 255: val = 255
            out[o + 2] = <unsigned char>val

            out[o + 3] = 0xFF
            byte_idx += 2

    return result


def cmyk12_to_bgrx(const unsigned char[::1] src, int width, int height,
                    object decode_array):
    """Convert 12-bit CMYK to BGRX. Row-padded. Uses multiplicative formula R=(1-C)*(1-K)."""
    cdef int out_len = width * height * 4
    cdef bytearray result = bytearray(out_len)
    cdef unsigned char[::1] out = result
    cdef double c_min = decode_array[0], c_max = decode_array[1]
    cdef double m_min = decode_array[2], m_max = decode_array[3]
    cdef double y_min = decode_array[4], y_max = decode_array[5]
    cdef double k_min = decode_array[6], k_max = decode_array[7]
    cdef double c_scale = (c_max - c_min) / 4095.0
    cdef double m_scale = (m_max - m_min) / 4095.0
    cdef double y_scale = (y_max - y_min) / 4095.0
    cdef double k_scale = (k_max - k_min) / 4095.0
    cdef int samples_per_row = width * 4
    cdef int bits_per_row = samples_per_row * 12
    cdef int bytes_per_row = (bits_per_row + 7) // 8
    cdef int row, col, row_start, sample_base, bit_pos, byte_idx, bit_offset
    cdef int cv, mv, yv, kv, o, val, pixel_idx, si, sv
    cdef double c, m, y, k, r, g, b

    with nogil:
        for row in range(height):
            row_start = row * bytes_per_row
            for col in range(width):
                sample_base = col * 4
                pixel_idx = row * width + col
                o = pixel_idx * 4

                # Extract 4 x 12-bit samples
                for si in range(4):
                    bit_pos = (sample_base + si) * 12
                    byte_idx = row_start + bit_pos // 8
                    bit_offset = bit_pos % 8
                    if bit_offset == 0:
                        sv = (src[byte_idx] << 4) | (src[byte_idx + 1] >> 4)
                    else:
                        sv = ((src[byte_idx] & 0x0F) << 8) | src[byte_idx + 1]
                    if si == 0:
                        cv = sv
                    elif si == 1:
                        mv = sv
                    elif si == 2:
                        yv = sv
                    else:
                        kv = sv

                c = c_min + cv * c_scale
                m = m_min + mv * m_scale
                y = y_min + yv * y_scale
                k = k_min + kv * k_scale

                r = (1.0 - c) * (1.0 - k)
                g = (1.0 - m) * (1.0 - k)
                b = (1.0 - y) * (1.0 - k)

                val = <int>(b * 255.0)
                if val < 0: val = 0
                if val > 255: val = 255
                out[o] = <unsigned char>val

                val = <int>(g * 255.0)
                if val < 0: val = 0
                if val > 255: val = 255
                out[o + 1] = <unsigned char>val

                val = <int>(r * 255.0)
                if val < 0: val = 0
                if val > 255: val = 255
                out[o + 2] = <unsigned char>val

                out[o + 3] = 0xFF

    return result


# ---------------------------------------------------------------------------
# Color key masking
# ---------------------------------------------------------------------------

def apply_color_key_mask_8(unsigned char[::1] pixel_data,
                            const unsigned char[::1] sample_data,
                            int width, int height, int components,
                            object mask_color, bint is_range):
    """Apply color key masking for 8-bit samples. Modifies pixel_data in place."""
    cdef int total = width * height
    cdef int n = components
    cdef int pixel_idx, sample_offset, i, alpha_offset
    cdef bint matches
    cdef int raw_val, lo, hi

    for pixel_idx in range(total):
        sample_offset = pixel_idx * n
        if sample_offset + n > <int>sample_data.shape[0]:
            break

        matches = True
        if is_range:
            for i in range(n):
                raw_val = sample_data[sample_offset + i]
                lo = mask_color[i * 2]
                hi = mask_color[i * 2 + 1]
                if raw_val < lo or raw_val > hi:
                    matches = False
                    break
        else:
            for i in range(n):
                if sample_data[sample_offset + i] != <int>mask_color[i]:
                    matches = False
                    break

        if matches:
            alpha_offset = pixel_idx * 4 + 3
            if alpha_offset < <int>pixel_data.shape[0]:
                pixel_data[alpha_offset] = 0


def apply_color_key_mask_4(unsigned char[::1] pixel_data,
                            const unsigned char[::1] sample_data,
                            int width, int height, int components,
                            object mask_color, bint is_range):
    """Apply color key masking for 4-bit samples. Row-padded."""
    cdef int n = components
    cdef int samples_per_row = width * n
    cdef int bits_per_row = samples_per_row * 4
    cdef int bytes_per_row = (bits_per_row + 7) // 8
    cdef int row, col, pixel_idx, sample_base, si, byte_idx, alpha_offset
    cdef int raw_val, lo, hi
    cdef bint matches

    for row in range(height):
        for col in range(width):
            pixel_idx = row * width + col
            sample_base = col * n
            matches = True

            for si in range(n):
                byte_idx = (sample_base + si) // 2
                if (sample_base + si) % 2 == 0:
                    raw_val = (sample_data[row * bytes_per_row + byte_idx] >> 4) & 0x0F
                else:
                    raw_val = sample_data[row * bytes_per_row + byte_idx] & 0x0F

                if is_range:
                    lo = mask_color[si * 2]
                    hi = mask_color[si * 2 + 1]
                    if raw_val < lo or raw_val > hi:
                        matches = False
                        break
                else:
                    if raw_val != <int>mask_color[si]:
                        matches = False
                        break

            if matches:
                alpha_offset = pixel_idx * 4 + 3
                if alpha_offset < <int>pixel_data.shape[0]:
                    pixel_data[alpha_offset] = 0


def apply_color_key_mask_12(unsigned char[::1] pixel_data,
                             const unsigned char[::1] sample_data,
                             int width, int height, int components,
                             object mask_color, bint is_range):
    """Apply color key masking for 12-bit samples. Row-padded."""
    cdef int n = components
    cdef int samples_per_row = width * n
    cdef int bits_per_row = samples_per_row * 12
    cdef int bytes_per_row = (bits_per_row + 7) // 8
    cdef int row, col, pixel_idx, sample_base, si, bit_pos, byte_idx, bit_offset
    cdef int raw_val, lo, hi, alpha_offset, row_start
    cdef bint matches

    for row in range(height):
        row_start = row * bytes_per_row
        for col in range(width):
            pixel_idx = row * width + col
            sample_base = col * n
            matches = True

            for si in range(n):
                bit_pos = (sample_base + si) * 12
                byte_idx = row_start + bit_pos // 8
                bit_offset = bit_pos % 8
                if bit_offset == 0:
                    raw_val = (sample_data[byte_idx] << 4) | (sample_data[byte_idx + 1] >> 4)
                else:
                    raw_val = ((sample_data[byte_idx] & 0x0F) << 8) | sample_data[byte_idx + 1]

                if is_range:
                    lo = mask_color[si * 2]
                    hi = mask_color[si * 2 + 1]
                    if raw_val < lo or raw_val > hi:
                        matches = False
                        break
                else:
                    if raw_val != <int>mask_color[si]:
                        matches = False
                        break

            if matches:
                alpha_offset = pixel_idx * 4 + 3
                if alpha_offset < <int>pixel_data.shape[0]:
                    pixel_data[alpha_offset] = 0
