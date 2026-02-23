# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
Image processing and XObject building for native PDF output.

Section 1: Content stream image emission -- color space classification,
CIE/ICC conversion, indexed image expansion, and XObject references.

Section 2: ImageXObjectBuilder class -- PDF XObject construction with
compression selection, ICC profile embedding, and deduplication.
"""

import copy
import hashlib
import io
import zlib

from PIL import Image

from ...core import types as ps
from ...core import icc_profile
from ...core.color_space import (ColorSpaceEngine, _get_cie_float_array,
                                 is_identity_cie, preconvert_cie_def_table)
from ._common import _fmt, _cfmt, _GState
from .shading_ops import _pdf_number
from .text_ops import _emit_text_color

try:
    from pypdf.generic import (
        ArrayObject,
        BooleanObject,
        DictionaryObject,
        FloatObject,
        NameObject,
        NumberObject,
        StreamObject,
    )
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False


# ---------------------------------------------------------------------------
# Section 1: Content stream image emission
# ---------------------------------------------------------------------------


def _compute_image_cm(image_matrix: list | None, ctm: list | None,
                      width: int, height: int) -> bytes | None:
    """Compute the PDF cm operator that places an inline image correctly.

    Maps the PDF unit square (where image data fills top-to-bottom) to the
    correct position in device space, matching PostScript's image placement.

    Returns the cm operator as bytes, or None if matrices are missing/degenerate.
    """
    if not image_matrix or not ctm:
        return None

    im_a, im_b, im_c, im_d, im_tx, im_ty = image_matrix
    det_im = im_a * im_d - im_b * im_c
    if abs(det_im) < 1e-10:
        return None

    # Inverse of image_matrix (maps image pixel space -> user space)
    inv = 1.0 / det_im
    ii_a = im_d * inv
    ii_b = -im_b * inv
    ii_c = -im_c * inv
    ii_d = im_a * inv
    ii_tx = (im_c * im_ty - im_d * im_tx) * inv
    ii_ty = (im_b * im_tx - im_a * im_ty) * inv

    # T = inv(image_matrix) x CTM (image pixel space -> device space)
    ca, cb, cc, cd, ctx_, cty = ctm
    ta = ii_a * ca + ii_b * cc
    tb = ii_a * cb + ii_b * cd
    tc = ii_c * ca + ii_d * cc
    td = ii_c * cb + ii_d * cd
    ttx = ii_tx * ca + ii_ty * cc + ctx_
    tty = ii_tx * cb + ii_ty * cd + cty

    # PDF cm: maps unit square to device space
    # Unit (ux, uy) -> image pixel (ux*w, h - uy*h) -> device via T
    w, h = float(width), float(height)
    m_a = ta * w
    m_b = tb * w
    m_c = -tc * h
    m_d = -td * h
    m_tx = tc * h + ttx
    m_ty = td * h + tty

    return (f'{_fmt(m_a)} {_fmt(m_b)} {_fmt(m_c)} {_fmt(m_d)} '
            f'{_fmt(m_tx)} {_fmt(m_ty)} cm').encode()


def _get_indexed_info(color_space: list | str | None) -> tuple[str, bytes] | None:
    """Extract base color space and lookup table from an Indexed color space.

    Returns (base_cs_name, lookup_bytes) or None if not Indexed.
    """
    if not isinstance(color_space, list) or len(color_space) < 4:
        return None
    if color_space[0] != "Indexed":
        return None
    base = color_space[1]
    if isinstance(base, list):
        base = base[0]
    # Convert PS Name object to Python string
    if isinstance(base, ps.PSObject):
        base = base.val
        if isinstance(base, bytes):
            base = base.decode('latin-1')
    lookup = color_space[3]
    if isinstance(lookup, ps.PSObject):
        lookup = lookup.byte_string()
    if isinstance(lookup, str):
        lookup = lookup.encode('latin-1')
    return base, lookup


def _expand_indexed_image(image: ps.ImageElement, lookup: bytes,
                          base_ncomp: int) -> ps.ImageElement:
    """Expand indexed image data to base color space values.

    Replaces each index byte with the corresponding color from the lookup table.
    Returns a modified copy of the image with expanded sample_data.
    """
    data = image.sample_data
    bpc = image.bits_per_component
    w = image.width
    h = image.height

    # Unpack indices from packed bit data
    indices: list[int] = []
    if bpc == 8:
        indices = list(data)
    elif bpc == 4:
        for row in range(h):
            row_start = row * ((w * 4 + 7) // 8)
            col = 0
            byte_idx = row_start
            while col < w and byte_idx < len(data):
                b = data[byte_idx]
                indices.append((b >> 4) & 0x0F)
                col += 1
                if col < w:
                    indices.append(b & 0x0F)
                    col += 1
                byte_idx += 1
    elif bpc == 2:
        for row in range(h):
            row_start = row * ((w * 2 + 7) // 8)
            col = 0
            byte_idx = row_start
            while col < w and byte_idx < len(data):
                b = data[byte_idx]
                for shift in (6, 4, 2, 0):
                    if col < w:
                        indices.append((b >> shift) & 0x03)
                        col += 1
                byte_idx += 1
    elif bpc == 1:
        for row in range(h):
            row_start = row * ((w + 7) // 8)
            col = 0
            byte_idx = row_start
            while col < w and byte_idx < len(data):
                b = data[byte_idx]
                for bit in range(7, -1, -1):
                    if col < w:
                        indices.append((b >> bit) & 1)
                        col += 1
                byte_idx += 1
    else:
        indices = list(data)

    # Look up each index in the palette
    expanded = bytearray()
    for idx in indices:
        offset = idx * base_ncomp
        if offset + base_ncomp <= len(lookup):
            expanded.extend(lookup[offset:offset + base_ncomp])
        else:
            expanded.extend(b'\x00' * base_ncomp)

    # Create a shallow copy with expanded data
    result = copy.copy(image)
    result.sample_data = bytes(expanded)
    result.bits_per_component = 8
    result.components = base_ncomp
    result.decode_array = None  # Reset decode -- data is now direct color values
    return result


def _try_color_convert(color_space: list | str | None, sample_data: bytes,
                        width: int, height: int, ncomp: int,
                        decode_array: list | None) -> bytes | None:
    """Try to convert non-device color space image data to RGB.

    Handles ICCBased and CIE-based (CIEBasedABC, CIEBasedA, CIEBasedDEF,
    CIEBasedDEFG) color spaces by converting through the appropriate pipeline
    to produce sRGB output. Returns None if the color space is already a
    device space or conversion fails.
    """
    if (not color_space or not isinstance(color_space, list)
            or len(color_space) < 2):
        return None
    cs_name = color_space[0]
    if isinstance(cs_name, ps.PSObject):
        cs_name = cs_name.val
        if isinstance(cs_name, bytes):
            cs_name = cs_name.decode('latin-1')

    if cs_name == 'ICCBased':
        stream_obj = color_space[1]
        profile_hash = icc_profile.get_profile_hash(stream_obj)
        if profile_hash is not None:
            bgrx = icc_profile.icc_convert_image(
                profile_hash, ncomp, sample_data, width, height, 8,
                decode_array)
            if bgrx is not None:
                # Convert BGRX (Cairo format) -> RGB for PDF output
                n_pixels = width * height
                rgb = bytearray(n_pixels * 3)
                for i in range(n_pixels):
                    off = i * 4
                    rgb[i * 3] = bgrx[off + 2]      # R
                    rgb[i * 3 + 1] = bgrx[off + 1]  # G
                    rgb[i * 3 + 2] = bgrx[off]       # B
                return bytes(rgb)
        return None

    elif cs_name in ('CIEBasedABC', 'CIEBasedA'):
        return _convert_cie_to_rgb(sample_data, width, height,
                                   decode_array, color_space, ncomp)

    elif cs_name in ('CIEBasedDEF', 'CIEBasedDEFG'):
        return _convert_cie_def_to_rgb(sample_data, width, height,
                                       decode_array, color_space, ncomp)

    return None


def _convert_cie_to_rgb(sample_data: bytes | bytearray, width: int, height: int,
                        decode_array: list | None, color_space: list,
                        ncomp: int) -> bytes | None:
    """Convert CIEBasedABC or CIEBasedA 8-bit image samples to RGB bytes.

    For the common identity-CIE case (sRGB wrapped as CIEBasedABC), passes
    data through directly.  Otherwise converts each pixel through the full
    CIE pipeline.
    """
    try:
        space_name = color_space[0]
        dict_obj = color_space[1]
        cie_dict = (dict_obj.val
                    if hasattr(dict_obj, 'val') and isinstance(dict_obj.val, dict)
                    else {})

        # Fast path: identity CIE -- data is already RGB/gray
        if is_identity_cie(cie_dict, space_name):
            if space_name == "CIEBasedABC":
                return bytes(sample_data[:width * height * 3])
            # CIEBasedA identity: expand gray -> RGB
            n_pixels = width * height
            rgb = bytearray(n_pixels * 3)
            for i in range(n_pixels):
                if i < len(sample_data):
                    v = sample_data[i]
                else:
                    v = 0
                off = i * 3
                rgb[off] = v
                rgb[off + 1] = v
                rgb[off + 2] = v
            return bytes(rgb)

        if not decode_array:
            return None

        # Slow path: full CIE conversion per pixel
        if space_name == "CIEBasedABC":
            r_min, r_max = decode_array[0], decode_array[1]
            g_min, g_max = decode_array[2], decode_array[3]
            b_min, b_max = decode_array[4], decode_array[5]

            n_pixels = width * height
            rgb = bytearray(n_pixels * 3)

            for i in range(0, n_pixels * 3, 3):
                if i + 2 >= len(sample_data):
                    break
                a = r_min + (sample_data[i] / 255.0) * (r_max - r_min)
                b = g_min + (sample_data[i + 1] / 255.0) * (g_max - g_min)
                c = b_min + (sample_data[i + 2] / 255.0) * (b_max - b_min)

                r, g, b_ = ColorSpaceEngine.cie_abc_to_rgb([a, b, c], cie_dict)
                px = i  # output index matches input since both are 3-comp
                rgb[px] = max(0, min(255, int(r * 255 + 0.5)))
                rgb[px + 1] = max(0, min(255, int(g * 255 + 0.5)))
                rgb[px + 2] = max(0, min(255, int(b_ * 255 + 0.5)))
            return bytes(rgb)

        elif space_name == "CIEBasedA":
            d_min, d_max = decode_array[0], decode_array[1]

            n_pixels = width * height
            rgb = bytearray(n_pixels * 3)

            for i in range(n_pixels):
                if i >= len(sample_data):
                    break
                a = d_min + (sample_data[i] / 255.0) * (d_max - d_min)
                r, g, b_ = ColorSpaceEngine.cie_a_to_rgb(a, cie_dict)
                off = i * 3
                rgb[off] = max(0, min(255, int(r * 255 + 0.5)))
                rgb[off + 1] = max(0, min(255, int(g * 255 + 0.5)))
                rgb[off + 2] = max(0, min(255, int(b_ * 255 + 0.5)))
            return bytes(rgb)

        return None

    except (ValueError, TypeError, IndexError, KeyError, ZeroDivisionError):
        return None


def _convert_cie_def_to_rgb(sample_data: bytes | bytearray, width: int, height: int,
                             decode_array: list | None, color_space: list,
                             ncomp: int) -> bytes | None:
    """Convert CIEBasedDEF or CIEBasedDEFG 8-bit image samples to RGB bytes.

    For CIEBasedDEF: pre-converts the 3D lookup table through the full CIE
    pipeline once, then uses fast trilinear interpolation per pixel.
    For CIEBasedDEFG: per-pixel conversion through cie_defg_to_rgb.
    """
    try:
        space_name = color_space[0]
        dict_obj = color_space[1]
        cie_dict = (dict_obj.val
                    if hasattr(dict_obj, 'val') and isinstance(dict_obj.val, dict)
                    else {})

        if not decode_array:
            return None

        if space_name == "CIEBasedDEF":
            preconv = preconvert_cie_def_table(cie_dict)
            if preconv is None:
                # No Table -- pass through as RGB
                return bytes(sample_data[:width * height * 3])

            r_tab, g_tab, b_tab, m1, m2, m3 = preconv
            range_def = _get_cie_float_array(cie_dict, b"RangeDEF",
                                             [0, 1, 0, 1, 0, 1])

            d_min, d_max = decode_array[0], decode_array[1]
            e_min, e_max = decode_array[2], decode_array[3]
            f_min, f_max = decode_array[4], decode_array[5]

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

            n_pixels = width * height
            rgb = bytearray(n_pixels * 3)
            out_idx = 0

            for i in range(0, n_pixels * 3, 3):
                if i + 2 >= len(sample_data):
                    break

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

                i000 = di0 * stride_d + ei0 * stride_e + fi0
                i001 = di0 * stride_d + ei0 * stride_e + fi1
                i010 = di0 * stride_d + ei1 * stride_e + fi0
                i011 = di0 * stride_d + ei1 * stride_e + fi1
                i100 = di1 * stride_d + ei0 * stride_e + fi0
                i101 = di1 * stride_d + ei0 * stride_e + fi1
                i110 = di1 * stride_d + ei1 * stride_e + fi0
                i111 = di1 * stride_d + ei1 * stride_e + fi1

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

                rgb[out_idx] = max(0, min(255, int(r * 255 + 0.5)))
                rgb[out_idx + 1] = max(0, min(255, int(g * 255 + 0.5)))
                rgb[out_idx + 2] = max(0, min(255, int(b_ * 255 + 0.5)))
                out_idx += 3

            return bytes(rgb)

        elif space_name == "CIEBasedDEFG":
            n_pixels = width * height
            rgb = bytearray(n_pixels * 3)
            out_idx = 0

            for i in range(0, n_pixels * 4, 4):
                if i + 3 >= len(sample_data):
                    break
                d = decode_array[0] + (sample_data[i] / 255.0) * (decode_array[1] - decode_array[0])
                e = decode_array[2] + (sample_data[i + 1] / 255.0) * (decode_array[3] - decode_array[2])
                f = decode_array[4] + (sample_data[i + 2] / 255.0) * (decode_array[5] - decode_array[4])
                g = decode_array[6] + (sample_data[i + 3] / 255.0) * (decode_array[7] - decode_array[6])

                r, gv, b_ = ColorSpaceEngine.cie_defg_to_rgb([d, e, f, g], cie_dict)
                rgb[out_idx] = max(0, min(255, int(r * 255 + 0.5)))
                rgb[out_idx + 1] = max(0, min(255, int(gv * 255 + 0.5)))
                rgb[out_idx + 2] = max(0, min(255, int(b_ * 255 + 0.5)))
                out_idx += 3

            return bytes(rgb)

        return None

    except (ValueError, TypeError, IndexError, KeyError, ZeroDivisionError):
        return None


def _convert_12bit_to_8bit(data: bytes, width: int, height: int,
                            ncomp: int) -> bytes:
    """Convert 12-bit-per-component image data to 8-bit.

    PDF only supports BPC 1, 2, 4, 8, 16 -- PostScript also allows 12-bit.
    Extracts each 12-bit sample and scales from 0-4095 to 0-255.
    Input data is bit-packed with row padding to byte boundaries.
    """
    samples_per_row = width * ncomp
    bits_per_row = samples_per_row * 12
    bytes_per_row = (bits_per_row + 7) // 8

    out = bytearray(width * height * ncomp)
    out_idx = 0

    for row in range(height):
        row_start = row * bytes_per_row
        for s in range(samples_per_row):
            bit_pos = s * 12
            byte_idx = row_start + bit_pos // 8
            bit_offset = bit_pos % 8

            if bit_offset == 0:
                # Sample starts at byte boundary -- top 8 bits of byte + top 4 of next
                if byte_idx + 1 < len(data):
                    val = ((data[byte_idx] << 4) |
                           (data[byte_idx + 1] >> 4))
                elif byte_idx < len(data):
                    val = data[byte_idx] << 4
                else:
                    val = 0
            else:
                # bit_offset == 4 -- bottom 4 bits of byte + all 8 of next
                if byte_idx + 1 < len(data):
                    val = (((data[byte_idx] & 0x0F) << 8) |
                           data[byte_idx + 1])
                elif byte_idx < len(data):
                    val = (data[byte_idx] & 0x0F) << 8
                else:
                    val = 0

            # Scale 12-bit (0-4095) to 8-bit (0-255)
            out[out_idx] = (val * 255 + 2047) // 4095
            out_idx += 1

    return bytes(out)


def _classify_image_color_space(image: ps.ImageElement) -> tuple[str, dict]:
    """Determine how an image's color space should be embedded in PDF.

    Returns (strategy, params) where strategy is one of:
      'device'       -- DeviceGray/RGB/CMYK, params={'cs': '/G'|'/RGB'|'/CMYK'}
      'icc'          -- ICCBased with valid profile, params={'hash': bytes, 'n': int}
      'calrgb'       -- CIEBasedABC mappable to CalRGB, params={WhitePoint, ...}
      'calgray'      -- CIEBasedA mappable to CalGray, params={WhitePoint, ...}
      'convert_rgb'  -- must convert to RGB first
    """
    if isinstance(image, ps.ImageMaskElement):
        return ('device', {'cs': None})

    if isinstance(image, ps.ColorImageElement):
        cs_map = {'DeviceGray': '/G', 'DeviceRGB': '/RGB', 'DeviceCMYK': '/CMYK'}
        return ('device', {'cs': cs_map.get(image.color_space_name, '/RGB')})

    # Regular image -- examine color space
    color_space = image.color_space
    if not color_space or not isinstance(color_space, list) or len(color_space) < 2:
        # Simple device space -- infer from component count
        cs_map = {1: '/G', 3: '/RGB', 4: '/CMYK'}
        return ('device', {'cs': cs_map.get(image.components, '/RGB')})

    cs_name = color_space[0]
    if isinstance(cs_name, ps.PSObject):
        cs_name = cs_name.val
        if isinstance(cs_name, bytes):
            cs_name = cs_name.decode('latin-1')

    if cs_name == 'ICCBased':
        stream_obj = color_space[1]
        profile_hash = icc_profile.get_profile_hash(stream_obj)
        if profile_hash is not None and icc_profile.get_icc_bytes(profile_hash) is not None:
            return ('icc', {'hash': profile_hash, 'n': image.components})
        # ICC profile not available -- fall back to device space based on N
        cs_map = {1: '/G', 3: '/RGB', 4: '/CMYK'}
        return ('device', {'cs': cs_map.get(image.components, '/RGB')})

    if cs_name == 'CIEBasedABC':
        dict_obj = color_space[1]
        cie_dict = (dict_obj.val
                    if hasattr(dict_obj, 'val') and isinstance(dict_obj.val, dict)
                    else {})
        if is_identity_cie(cie_dict, 'CIEBasedABC'):
            return ('device', {'cs': '/RGB'})
        cal_params = _try_extract_calrgb(cie_dict)
        if cal_params is not None:
            return ('calrgb', cal_params)
        return ('convert_rgb', {})

    if cs_name == 'CIEBasedA':
        dict_obj = color_space[1]
        cie_dict = (dict_obj.val
                    if hasattr(dict_obj, 'val') and isinstance(dict_obj.val, dict)
                    else {})
        if is_identity_cie(cie_dict, 'CIEBasedA'):
            return ('device', {'cs': '/G'})
        cal_params = _try_extract_calgray(cie_dict)
        if cal_params is not None:
            return ('calgray', cal_params)
        return ('convert_rgb', {})

    if cs_name in ('CIEBasedDEF', 'CIEBasedDEFG'):
        return ('convert_rgb', {})

    # Unknown -- infer device from components
    cs_map = {1: '/G', 3: '/RGB', 4: '/CMYK'}
    return ('device', {'cs': cs_map.get(image.components, '/RGB')})


def _try_extract_calrgb(cie_dict: dict) -> dict | None:
    """Try to extract CalRGB parameters from a CIEBasedABC dict.

    Returns dict with WhitePoint and optional BlackPoint, Gamma, Matrix
    if the CIE space can be represented as PDF CalRGB. Returns None if
    decode procedures or non-identity MatrixABC are present.
    """
    # Can't map arbitrary PS procedures to PDF
    if b'DecodeABC' in cie_dict or b'DecodeLMN' in cie_dict:
        return None

    # MatrixABC must be identity or absent
    mat_abc = _get_cie_float_array(cie_dict, b'MatrixABC', None)
    if mat_abc is not None and mat_abc != [1, 0, 0, 0, 1, 0, 0, 0, 1]:
        return None

    white_point = _get_cie_float_array(cie_dict, b'WhitePoint', None)
    if white_point is None or len(white_point) < 3:
        return None

    params: dict = {'WhitePoint': white_point[:3]}

    black_point = _get_cie_float_array(cie_dict, b'BlackPoint', None)
    if black_point is not None and black_point != [0, 0, 0]:
        params['BlackPoint'] = black_point[:3]

    # RangeABC -> CalRGB Gamma (PDF CalRGB has per-component gamma)
    range_abc = _get_cie_float_array(cie_dict, b'RangeABC', [0, 1, 0, 1, 0, 1])
    # CalRGB doesn't support arbitrary ranges -- only standard [0,1]
    if range_abc != [0, 1, 0, 1, 0, 1]:
        return None

    # MatrixLMN -> CalRGB Matrix (same column-major 9-element format)
    mat_lmn = _get_cie_float_array(cie_dict, b'MatrixLMN', None)
    if mat_lmn is not None and mat_lmn != [1, 0, 0, 0, 1, 0, 0, 0, 1]:
        params['Matrix'] = mat_lmn[:9]

    return params


def _try_extract_calgray(cie_dict: dict) -> dict | None:
    """Try to extract CalGray parameters from a CIEBasedA dict.

    Returns dict with WhitePoint and optional BlackPoint, Gamma
    if the CIE space can be represented as PDF CalGray. Returns None if
    decode procedures are present.
    """
    if b'DecodeA' in cie_dict or b'DecodeLMN' in cie_dict:
        return None

    white_point = _get_cie_float_array(cie_dict, b'WhitePoint', None)
    if white_point is None or len(white_point) < 3:
        return None

    # MatrixA must be uniform [k k k] or absent
    mat_a = _get_cie_float_array(cie_dict, b'MatrixA', None)
    if mat_a is not None:
        if len(mat_a) < 3:
            return None
        if not (abs(mat_a[0] - mat_a[1]) < 1e-6 and
                abs(mat_a[1] - mat_a[2]) < 1e-6):
            return None

    params: dict = {'WhitePoint': white_point[:3]}

    black_point = _get_cie_float_array(cie_dict, b'BlackPoint', None)
    if black_point is not None and black_point != [0, 0, 0]:
        params['BlackPoint'] = black_point[:3]

    return params


def _emit_image_xobject(lines: list[bytes], image: ps.ImageElement,
                        image_defs: list[tuple[str, dict]],
                        image_counter: int,
                        gs: _GState) -> tuple[str | None, int]:
    """Build an image description dict and emit an XObject reference.

    All images (including device-color and imagemasks) become XObject
    references in the content stream. The actual XObject is built later
    by pdf_builder.

    Returns (image_resource_name, updated_image_counter).
    """
    if image.sample_data is None:
        return (None, image_counter)

    width = image.width
    height = image.height
    bpc = image.bits_per_component
    is_mask = isinstance(image, ps.ImageMaskElement)

    # Handle Indexed color spaces -- expand before classification
    if not is_mask and not isinstance(image, ps.ColorImageElement):
        indexed_info = _get_indexed_info(image.color_space)
        if indexed_info is not None:
            base_cs, lookup = indexed_info
            base_ncomp_map = {'DeviceGray': 1, 'DeviceRGB': 3, 'DeviceCMYK': 4}
            base_ncomp = base_ncomp_map.get(base_cs, 3)
            image = _expand_indexed_image(image, lookup, base_ncomp)
            width = image.width
            height = image.height
            bpc = image.bits_per_component

    # Convert 12-bit to 8-bit (PDF doesn't support 12-bit BPC)
    sample_data = image.sample_data
    ncomp = image.components
    original_bpc = bpc
    if bpc == 12 and not is_mask:
        sample_data = _convert_12bit_to_8bit(sample_data, width, height, ncomp)
        bpc = 8

    # Classify the image color space
    strategy, params = _classify_image_color_space(image)

    # For convert_rgb: run ICC/CIE conversion, then treat as device RGB
    icc_converted = False
    if strategy == 'convert_rgb' and bpc == 8:
        rgb_data = _try_color_convert(image.color_space, sample_data,
                                       width, height, ncomp,
                                       image.decode_array)
        if rgb_data is not None:
            sample_data = rgb_data
            ncomp = 3
            strategy = 'device'
            params = {'cs': '/RGB'}
            icc_converted = True
        else:
            # Conversion failed -- fall back to device space from components
            cs_map = {1: '/G', 3: '/RGB', 4: '/CMYK'}
            strategy = 'device'
            params = {'cs': cs_map.get(ncomp, '/RGB')}

    # Build image description dict
    desc: dict = {
        'width': width,
        'height': height,
        'bpc': bpc,
        'sample_data': sample_data,
        'color_space_type': strategy,
        'device_cs': params.get('cs'),
        'icc_hash': params.get('hash'),
        'icc_n': params.get('n'),
        'cal_params': params if strategy in ('calrgb', 'calgray') else None,
        'interpolate': image.interpolate,
        'is_mask': is_mask,
        'mask_polarity': getattr(image, 'polarity', None) if is_mask else None,
        'mask_color': tuple(image.color) if is_mask else None,
    }

    # Type 3 stencil mask data
    stencil_mask = getattr(image, 'stencil_mask', None)
    if stencil_mask is not None and not is_mask:
        desc['stencil_mask'] = stencil_mask
        desc['stencil_mask_width'] = getattr(
            image, 'stencil_mask_width', width)
        desc['stencil_mask_height'] = getattr(
            image, 'stencil_mask_height', height)
        desc['stencil_mask_polarity'] = getattr(
            image, 'stencil_mask_polarity', True)
    else:
        desc['stencil_mask'] = None

    # Type 4 color key mask (MaskColor) -> PDF /Mask array
    raw_mask = getattr(image, 'mask_color', None)
    if raw_mask is not None and not is_mask:
        mask_vals = [int(v) for v in raw_mask]
        # Scale mask values if 12-bit was converted to 8-bit
        if original_bpc == 12 and bpc == 8:
            mask_vals = [(v * 255 + 2047) // 4095 for v in mask_vals]
        # PDF /Mask is always 2n values (ranges).  Convert exact match
        # (n values) to min=max range pairs.
        if len(mask_vals) == ncomp:
            pdf_mask = []
            for v in mask_vals:
                pdf_mask.extend([v, v])
            mask_vals = pdf_mask
        desc['color_key_mask'] = mask_vals
    else:
        desc['color_key_mask'] = None

    # Decode array -- skip if ICC-converted (decode already applied)
    if is_mask:
        desc['decode_array'] = None  # Handled via mask_polarity
    elif icc_converted:
        desc['decode_array'] = None
    elif image.decode_array:
        desc['decode_array'] = list(image.decode_array)
    else:
        desc['decode_array'] = None

    # Assign resource name and register
    img_name = f'/Im{image_counter}'
    image_counter += 1
    image_defs.append((img_name, desc))

    # Emit content stream reference
    lines.append(b'q')

    # For imagemask, set fill color before the Do
    if is_mask:
        _emit_text_color(lines, image.color, gs)

    cm_line = _compute_image_cm(image.image_matrix, image.ctm, width, height)
    if cm_line:
        lines.append(cm_line)

    lines.append(f'{img_name} Do'.encode())
    lines.append(b'Q')

    return (img_name, image_counter)


# ---------------------------------------------------------------------------
# Section 2: ImageXObjectBuilder (extracted from PDFBuilder)
# ---------------------------------------------------------------------------


def _get_ncomp(img_desc: dict) -> int:
    """Derive the number of color components from an image description.

    Args:
        img_desc: Image description dict from content_stream.

    Returns:
        Number of components (1, 3, or 4).
    """
    cs_type = img_desc.get('color_space_type', '')

    if cs_type == 'device':
        device_cs = img_desc.get('device_cs', '/RGB')
        return {'/G': 1, '/RGB': 3, '/CMYK': 4}.get(device_cs, 3)

    if cs_type == 'icc':
        return img_desc.get('icc_n', 3)

    if cs_type == 'calrgb':
        return 3

    if cs_type == 'calgray':
        return 1

    return 3


class ImageXObjectBuilder:
    """Builds PDF Image XObjects with compression and deduplication."""

    def __init__(self, lossless_images: bool = False) -> None:
        self._lossless_images = lossless_images
        # ICC profile deduplication: hash -> indirect reference
        self._icc_profile_refs: dict[bytes, object] = {}
        # Image XObject deduplication: signature -> indirect reference
        self._image_xobj_refs: dict[bytes, object] = {}

    def _get_icc_profile_ref(self, writer: object,
                             profile_hash: bytes,
                             n: int) -> object | None:
        """Get or create an ICC profile stream as an indirect reference.

        Deduplicates across pages -- same profile hash reuses the same ref.

        Args:
            writer: PdfWriter to add the stream to.
            profile_hash: SHA-256 hash of the ICC profile bytes.
            n: Number of color components (1, 3, or 4).

        Returns:
            Indirect reference to the ICC stream, or None.
        """
        if profile_hash in self._icc_profile_refs:
            return self._icc_profile_refs[profile_hash]

        raw_bytes = icc_profile.get_icc_bytes(profile_hash)
        if raw_bytes is None:
            return None

        compressed = zlib.compress(raw_bytes)
        icc_stream = StreamObject()
        icc_stream._data = compressed
        icc_stream[NameObject('/Length')] = NumberObject(len(compressed))
        icc_stream[NameObject('/Filter')] = NameObject('/FlateDecode')
        icc_stream[NameObject('/N')] = NumberObject(n)

        # Add /Alternate for graceful fallback
        alt_map = {1: '/DeviceGray', 3: '/DeviceRGB', 4: '/DeviceCMYK'}
        alt_cs = alt_map.get(n)
        if alt_cs:
            icc_stream[NameObject('/Alternate')] = NameObject(alt_cs)

        ref = writer._add_object(icc_stream)
        self._icc_profile_refs[profile_hash] = ref
        return ref

    def _build_image_color_space(self, writer: object,
                                  img_desc: dict) -> object | None:
        """Build the PDF color space object for an image XObject.

        Args:
            writer: PdfWriter for creating indirect objects.
            img_desc: Image description dict from content_stream.

        Returns:
            PDF object for /ColorSpace, or None for imagemasks.
        """
        cs_type = img_desc['color_space_type']

        if img_desc['is_mask']:
            return None  # Imagemasks have no /ColorSpace

        if cs_type == 'device':
            device_cs = img_desc['device_cs']
            cs_map = {'/G': '/DeviceGray', '/RGB': '/DeviceRGB',
                      '/CMYK': '/DeviceCMYK'}
            return NameObject(cs_map.get(device_cs, '/DeviceRGB'))

        if cs_type == 'icc':
            profile_ref = self._get_icc_profile_ref(
                writer, img_desc['icc_hash'], img_desc['icc_n'])
            if profile_ref is not None:
                return ArrayObject([NameObject('/ICCBased'), profile_ref])
            # Fallback to device space
            alt_map = {1: '/DeviceGray', 3: '/DeviceRGB', 4: '/DeviceCMYK'}
            return NameObject(alt_map.get(img_desc['icc_n'], '/DeviceRGB'))

        if cs_type == 'calrgb':
            cal_params = img_desc['cal_params']
            cal_dict = DictionaryObject()
            cal_dict[NameObject('/WhitePoint')] = ArrayObject(
                [FloatObject(round(v, 6)) for v in cal_params['WhitePoint']])
            if 'BlackPoint' in cal_params:
                cal_dict[NameObject('/BlackPoint')] = ArrayObject(
                    [FloatObject(round(v, 6)) for v in cal_params['BlackPoint']])
            if 'Gamma' in cal_params:
                cal_dict[NameObject('/Gamma')] = ArrayObject(
                    [FloatObject(round(v, 6)) for v in cal_params['Gamma']])
            if 'Matrix' in cal_params:
                cal_dict[NameObject('/Matrix')] = ArrayObject(
                    [FloatObject(round(v, 6)) for v in cal_params['Matrix']])
            return ArrayObject([NameObject('/CalRGB'), cal_dict])

        if cs_type == 'calgray':
            cal_params = img_desc['cal_params']
            cal_dict = DictionaryObject()
            cal_dict[NameObject('/WhitePoint')] = ArrayObject(
                [FloatObject(round(v, 6)) for v in cal_params['WhitePoint']])
            if 'BlackPoint' in cal_params:
                cal_dict[NameObject('/BlackPoint')] = ArrayObject(
                    [FloatObject(round(v, 6)) for v in cal_params['BlackPoint']])
            if 'Gamma' in cal_params:
                cal_dict[NameObject('/Gamma')] = FloatObject(
                    round(cal_params['Gamma'], 6))
            return ArrayObject([NameObject('/CalGray'), cal_dict])

        # Fallback
        return NameObject('/DeviceRGB')

    def _compute_image_signature(self, img_desc: dict) -> bytes:
        """Compute a SHA-256 signature for image XObject deduplication.

        Hashes all fields that define the XObject identity including
        color key mask (which becomes a /Mask entry on the XObject).
        """
        h = hashlib.sha256()
        h.update(hashlib.sha256(img_desc['sample_data']).digest())
        h.update(str(img_desc['width']).encode())
        h.update(str(img_desc['height']).encode())
        h.update(str(img_desc.get('bpc', 1)).encode())
        h.update(str(img_desc.get('color_space_type', '')).encode())
        h.update(str(img_desc.get('device_cs', '')).encode())
        h.update(str(img_desc.get('icc_hash', b'')).encode())
        h.update(str(img_desc.get('icc_n', 0)).encode())
        cal_params = img_desc.get('cal_params')
        if cal_params and isinstance(cal_params, dict):
            h.update(str(sorted(cal_params.items())).encode())
        else:
            h.update(b'None')
        h.update(str(img_desc.get('is_mask', False)).encode())
        h.update(str(img_desc.get('mask_polarity', True)).encode())
        decode_array = img_desc.get('decode_array')
        h.update(str(tuple(decode_array) if decode_array else None).encode())
        h.update(str(img_desc.get('interpolate', False)).encode())
        color_key_mask = img_desc.get('color_key_mask')
        h.update(str(tuple(color_key_mask) if color_key_mask else None).encode())
        stencil_mask = img_desc.get('stencil_mask')
        if stencil_mask is not None:
            h.update(hashlib.sha256(stencil_mask).digest())
            h.update(str(img_desc.get('stencil_mask_width', 0)).encode())
            h.update(str(img_desc.get('stencil_mask_height', 0)).encode())
            h.update(str(img_desc.get('stencil_mask_polarity', True)).encode())
        else:
            h.update(b'no_stencil')
        return h.digest()

    def _try_dct_encode(self, sample_data: bytes, width: int,
                         height: int, ncomp: int,
                         bpc: int) -> bytes | None:
        """Try to encode image data as JPEG.

        Args:
            sample_data: Raw image sample bytes.
            width: Image width in pixels.
            height: Image height in pixels.
            ncomp: Number of color components (1, 3, or 4).
            bpc: Bits per component.

        Returns:
            JPEG-encoded bytes, or None if encoding is not possible.
        """
        if bpc != 8:
            return None

        mode_map = {1: 'L', 3: 'RGB', 4: 'CMYK'}
        mode = mode_map.get(ncomp)
        if mode is None:
            return None

        try:
            if mode == 'CMYK':
                # Pillow's CMYK JPEG encoder inverts channel values (for
                # YCCK colorspace convention).  Pre-invert so the double
                # inversion produces correct values in the JPEG stream.
                inv = bytes(255 - b for b in sample_data)
                img = Image.frombytes(mode, (width, height), inv)
            else:
                img = Image.frombytes(mode, (width, height), sample_data)
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=85)
            return buf.getvalue()
        except Exception:
            return None

    def build_image_xobject(self, writer: object,
                             img_desc: dict) -> object | None:
        """Build a PDF Image XObject from an image description dict.

        Args:
            writer: PdfWriter to add the XObject to.
            img_desc: Image description dict from content_stream.

        Returns:
            Indirect reference to the Image XObject, or None.
        """
        sample_data = img_desc['sample_data']
        if not sample_data:
            return None

        sig = self._compute_image_signature(img_desc)
        cached = self._image_xobj_refs.get(sig)
        if cached is not None:
            return cached

        compressed_flate = zlib.compress(sample_data)
        dct_data = None
        color_key_mask = img_desc.get('color_key_mask')

        if (not self._lossless_images
                and not img_desc['is_mask']
                and img_desc.get('bpc', 1) == 8):
            ncomp = _get_ncomp(img_desc)
            if ncomp in (1, 3, 4):
                dct_data = self._try_dct_encode(
                    sample_data, img_desc['width'], img_desc['height'],
                    ncomp, 8)

        # Pick best compression
        use_flate = len(compressed_flate) < len(sample_data)
        if dct_data is not None and len(dct_data) < len(compressed_flate):
            # DCT wins
            img_stream = StreamObject()
            img_stream._data = dct_data
            img_stream[NameObject('/Length')] = NumberObject(len(dct_data))
            img_stream[NameObject('/Filter')] = NameObject('/DCTDecode')
        else:
            # Flate wins (or raw if Flate is larger)
            img_stream = StreamObject()
            img_stream._data = compressed_flate if use_flate else sample_data
            img_stream[NameObject('/Length')] = NumberObject(
                len(compressed_flate) if use_flate else len(sample_data))
            if use_flate:
                img_stream[NameObject('/Filter')] = NameObject('/FlateDecode')

        img_stream[NameObject('/Type')] = NameObject('/XObject')
        img_stream[NameObject('/Subtype')] = NameObject('/Image')
        img_stream[NameObject('/Width')] = NumberObject(img_desc['width'])
        img_stream[NameObject('/Height')] = NumberObject(img_desc['height'])

        if img_desc['is_mask']:
            img_stream[NameObject('/ImageMask')] = BooleanObject(True)
            img_stream[NameObject('/BitsPerComponent')] = NumberObject(1)
            # PostScript: polarity=true means paint where bit=1
            # PDF: default Decode [0 1] paints where bit=0
            # So polarity=true needs /Decode [1 0] to invert
            polarity = img_desc.get('mask_polarity', True)
            if polarity:
                img_stream[NameObject('/Decode')] = ArrayObject([
                    NumberObject(1), NumberObject(0)])
        else:
            img_stream[NameObject('/BitsPerComponent')] = NumberObject(
                img_desc['bpc'])
            cs_obj = self._build_image_color_space(writer, img_desc)
            if cs_obj is not None:
                img_stream[NameObject('/ColorSpace')] = cs_obj

            decode_array = img_desc.get('decode_array')
            if decode_array:
                img_stream[NameObject('/Decode')] = ArrayObject(
                    [_pdf_number(v) for v in decode_array])

            # Type 4 color key mask -> PDF /Mask array
            if color_key_mask is not None:
                img_stream[NameObject('/Mask')] = ArrayObject(
                    [NumberObject(v) for v in color_key_mask])

            # Type 3 stencil mask -> PDF /Mask with Image XObject ref
            stencil_mask = img_desc.get('stencil_mask')
            if stencil_mask is not None:
                mask_ref = self._build_stencil_mask_xobject(
                    writer, img_desc)
                if mask_ref is not None:
                    img_stream[NameObject('/Mask')] = mask_ref

        if img_desc.get('interpolate'):
            img_stream[NameObject('/Interpolate')] = BooleanObject(True)

        ref = writer._add_object(img_stream)
        self._image_xobj_refs[sig] = ref
        return ref

    @staticmethod
    def _build_stencil_mask_xobject(writer: object,
                                     img_desc: dict) -> object | None:
        """Build a 1-bit mask Image XObject for Type 3 stencil masking.

        The mask XObject is referenced by the base image's /Mask entry.
        PDF stencil mask semantics: value 0 = paint base, value 1 = mask out.

        Args:
            writer: PdfWriter to add the XObject to.
            img_desc: Image description dict containing stencil_mask fields.

        Returns:
            Indirect reference to the mask XObject, or None.
        """
        mask_data = img_desc.get('stencil_mask')
        if mask_data is None:
            return None

        mask_width = img_desc.get('stencil_mask_width', img_desc['width'])
        mask_height = img_desc.get('stencil_mask_height', img_desc['height'])
        polarity = img_desc.get('stencil_mask_polarity', True)

        compressed = zlib.compress(mask_data)
        mask_stream = StreamObject()
        mask_stream._data = compressed
        mask_stream[NameObject('/Length')] = NumberObject(len(compressed))
        mask_stream[NameObject('/Filter')] = NameObject('/FlateDecode')
        mask_stream[NameObject('/Type')] = NameObject('/XObject')
        mask_stream[NameObject('/Subtype')] = NameObject('/Image')
        mask_stream[NameObject('/Width')] = NumberObject(mask_width)
        mask_stream[NameObject('/Height')] = NumberObject(mask_height)
        mask_stream[NameObject('/BitsPerComponent')] = NumberObject(1)
        mask_stream[NameObject('/ImageMask')] = BooleanObject(True)

        # polarity=True (PS Decode [0 1]): bit=0 -> paint base image
        #   Matches PDF default (value 0 = paint) -- no Decode needed
        # polarity=False (PS Decode [1 0]): bit=1 -> paint base image
        #   Need Decode [1 0] to invert
        if not polarity:
            mask_stream[NameObject('/Decode')] = ArrayObject([
                NumberObject(1), NumberObject(0)])

        return writer._add_object(mask_stream)
