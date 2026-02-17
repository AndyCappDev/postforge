# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
ICC Profile Processing — Tier 2

Extracts ICC profile binary data from PostScript ICCBased color space streams,
builds lcms2 transforms via Pillow's ImageCms, and provides color/image conversion.
Falls back gracefully to Tier 1 (alternate/N-based device space) when profile
extraction fails or ImageCms is unavailable.

Key design: Store raw N-component values in gstate.color (unchanged from Tier 1).
Apply ICC transforms at render time via the functions in this module.

Stream identity approach: Maintain id(stream_obj) → profile_hash mapping so that
the same stream object in gstate.color_space or shading dictionaries resolves to
the same cached profile.
"""

import hashlib
import io

try:
    from PIL import ImageCms, Image
    _IMAGECMS_AVAILABLE = True
except ImportError:
    _IMAGECMS_AVAILABLE = False

# Profile cache: sha256(icc_bytes) → CmsProfile
_profile_cache = {}

# Transform cache: (profile_hash, in_mode, out_mode) → CmsTransform
_transform_cache = {}

# Stream identity → profile hash: id(stream_obj) → profile_hash bytes
_stream_to_hash = {}

# Single-color conversion cache: (profile_hash, quantized_components) → (r, g, b)
_color_cache = {}
_COLOR_CACHE_MAX = 4096

# PIL mode mapping by number of components
_N_TO_PIL_MODE = {1: 'L', 3: 'RGB', 4: 'CMYK'}


def is_available():
    """Return True if Pillow ImageCms (lcms2) is available."""
    return _IMAGECMS_AVAILABLE


def extract_icc_bytes(ctxt, stream_obj):
    """Extract ICC profile binary data from a PostScript stream object.

    Tries multiple access patterns in order:
    1. DataSource key in the stream dict (FilterFile with read_bulk)
    2. DataSource as a String object (byte_string)
    3. Stream object itself if it has read_bulk
    4. Returns None (Tier 1 fallback)

    Args:
        ctxt: PostScript context
        stream_obj: The stream object from [/ICCBased stream]

    Returns:
        bytes containing ICC profile data, or None
    """
    max_size = 10 * 1024 * 1024  # 10 MB limit

    # The stream_obj is typically a PS Dict with DataSource holding the data
    if hasattr(stream_obj, 'val') and isinstance(stream_obj.val, dict):
        data_source = stream_obj.val.get(b'DataSource')
        if data_source is not None:
            # Try read_bulk on DataSource (FilterFile or File)
            if hasattr(data_source, 'read_bulk'):
                try:
                    data = bytearray()
                    remaining = max_size
                    while remaining > 0:
                        chunk = data_source.read_bulk(ctxt, min(remaining, 65536))
                        if not chunk:
                            break
                        data.extend(chunk)
                        remaining -= len(chunk)
                    if data:
                        return bytes(data)
                except Exception:
                    pass

            # Try byte_string on DataSource (String object)
            if hasattr(data_source, 'byte_string'):
                try:
                    data = data_source.byte_string()
                    if data:
                        return bytes(data)
                except Exception:
                    pass

    # Try read_bulk directly on stream_obj (less common)
    if hasattr(stream_obj, 'read_bulk') and not isinstance(getattr(stream_obj, 'val', None), dict):
        try:
            data = bytearray()
            remaining = max_size
            while remaining > 0:
                chunk = stream_obj.read_bulk(ctxt, min(remaining, 65536))
                if not chunk:
                    break
                data.extend(chunk)
                remaining -= len(chunk)
            if data:
                return bytes(data)
        except Exception:
            pass

    return None


def register_stream(ctxt, stream_obj):
    """Extract ICC bytes from stream, build profile, register in cache.

    Called at setcolorspace time.

    Args:
        ctxt: PostScript context
        stream_obj: The stream object from [/ICCBased stream]

    Returns:
        profile_hash (bytes) on success, None on failure (Tier 1 fallback)
    """
    if not _IMAGECMS_AVAILABLE:
        return None

    icc_bytes = extract_icc_bytes(ctxt, stream_obj)
    if not icc_bytes or len(icc_bytes) < 128:
        return None

    profile_hash = hashlib.sha256(icc_bytes).digest()

    if profile_hash not in _profile_cache:
        try:
            profile = ImageCms.getOpenProfile(io.BytesIO(icc_bytes))
            _profile_cache[profile_hash] = profile
        except Exception:
            return None

    _stream_to_hash[id(stream_obj)] = profile_hash
    return profile_hash


def get_profile_hash(stream_obj):
    """Look up cached profile hash for a stream object.

    Args:
        stream_obj: The stream object from color_space[1]

    Returns:
        profile_hash (bytes) or None if not registered
    """
    if stream_obj is None:
        return None
    return _stream_to_hash.get(id(stream_obj))


def get_transform(profile_hash, n_components):
    """Get or build a cached CMS transform from ICC profile → sRGB.

    Args:
        profile_hash: SHA-256 hash of the ICC profile bytes
        n_components: Number of input components (1, 3, or 4)

    Returns:
        ImageCms transform object, or None on failure
    """
    if not _IMAGECMS_AVAILABLE or profile_hash is None:
        return None

    in_mode = _N_TO_PIL_MODE.get(n_components)
    if in_mode is None:
        return None

    cache_key = (profile_hash, in_mode, 'RGB')
    if cache_key in _transform_cache:
        return _transform_cache[cache_key]

    profile = _profile_cache.get(profile_hash)
    if profile is None:
        return None

    try:
        srgb = ImageCms.createProfile('sRGB')
        transform = ImageCms.buildTransform(
            profile, srgb, in_mode, 'RGB',
            renderingIntent=ImageCms.Intent.PERCEPTUAL
        )
        _transform_cache[cache_key] = transform
        return transform
    except Exception:
        return None


def icc_convert_color(profile_hash, n_components, components):
    """Convert a single color from ICC profile space to sRGB.

    Uses a 1×1 Pillow image for the transform, with LRU color cache.

    Args:
        profile_hash: SHA-256 hash of the ICC profile bytes
        n_components: Number of input components (1, 3, or 4)
        components: List of float color values (0.0–1.0)

    Returns:
        (r, g, b) tuple of floats (0.0–1.0), or None on failure
    """
    if not _IMAGECMS_AVAILABLE or profile_hash is None:
        return None

    # Quantize components to 8-bit for cache lookup
    quantized = tuple(min(255, max(0, int(c * 255.0 + 0.5))) for c in components[:n_components])
    cache_key = (profile_hash, quantized)

    cached = _color_cache.get(cache_key)
    if cached is not None:
        return cached

    transform = get_transform(profile_hash, n_components)
    if transform is None:
        return None

    in_mode = _N_TO_PIL_MODE.get(n_components)
    if in_mode is None:
        return None

    try:
        # Build 1×1 image with the quantized pixel values
        pixel_bytes = bytes(quantized)
        img = Image.frombytes(in_mode, (1, 1), pixel_bytes)
        # Cannot use inPlace when input/output modes differ (e.g. CMYK→RGB)
        out = ImageCms.applyTransform(img, transform)
        r_byte, g_byte, b_byte = out.getpixel((0, 0))[:3]
        result = (r_byte / 255.0, g_byte / 255.0, b_byte / 255.0)

        # Cache with eviction
        if len(_color_cache) >= _COLOR_CACHE_MAX:
            # Evict oldest quarter
            keys = list(_color_cache.keys())
            for k in keys[:_COLOR_CACHE_MAX // 4]:
                del _color_cache[k]

        _color_cache[cache_key] = result
        return result
    except Exception:
        return None


def icc_convert_image(profile_hash, n_components, sample_data, width, height,
                      bits_per_component, decode_array):
    """Bulk image conversion from ICC profile space to Cairo BGRX format.

    Only handles 8-bit samples initially. Returns None for other bit depths
    so callers fall through to Tier 1.

    Args:
        profile_hash: SHA-256 hash of the ICC profile bytes
        n_components: Number of input components (1, 3, or 4)
        sample_data: Raw sample bytes
        width, height: Image dimensions
        bits_per_component: Bits per sample component (must be 8)
        decode_array: Decode array for sample mapping

    Returns:
        bytearray of Cairo BGRX pixel data, or None on failure
    """
    if not _IMAGECMS_AVAILABLE or profile_hash is None:
        return None

    # Only handle 8-bit for now
    if bits_per_component != 8:
        return None

    in_mode = _N_TO_PIL_MODE.get(n_components)
    if in_mode is None:
        return None

    transform = get_transform(profile_hash, n_components)
    if transform is None:
        return None

    try:
        expected_bytes = width * height * n_components
        if len(sample_data) < expected_bytes:
            return None

        raw = bytes(sample_data[:expected_bytes])

        # Apply decode LUT if non-identity
        if decode_array and not _is_identity_decode(decode_array, n_components):
            luts = _build_decode_lut(decode_array, n_components)
            raw = _apply_decode_luts(raw, luts, n_components)

        img = Image.frombytes(in_mode, (width, height), raw)
        # Cannot use inPlace when input/output modes differ (e.g. CMYK→RGB)
        rgb_img = ImageCms.applyTransform(img, transform)
        rgba_img = rgb_img.convert('RGBA')
        rgba_bytes = rgba_img.tobytes('raw', 'BGRA')
        return bytearray(rgba_bytes)
    except Exception:
        return None


def _is_identity_decode(decode_array, n_components):
    """Check if decode array is identity ([0 1] per component, or [0 1 0 1 ...] for CMYK)."""
    if not decode_array:
        return True
    expected = []
    for _ in range(n_components):
        expected.extend([0.0, 1.0])
    if len(decode_array) < len(expected):
        return True  # Too short → treat as identity
    for i, val in enumerate(expected):
        actual = decode_array[i]
        if hasattr(actual, 'val'):
            actual = float(actual.val)
        if abs(float(actual) - val) > 1e-6:
            return False
    return True


def _build_decode_lut(decode_array, n_components):
    """Build per-component 256-entry byte lookup tables for decode mapping.

    Returns a list of bytearrays, one per component. Each maps input byte value
    through its decode range back to a 0-255 output byte.
    """
    ranges = []
    for c in range(n_components):
        d_min = decode_array[c * 2]
        d_max = decode_array[c * 2 + 1]
        if hasattr(d_min, 'val'):
            d_min = float(d_min.val)
        if hasattr(d_max, 'val'):
            d_max = float(d_max.val)
        ranges.append((float(d_min), float(d_max)))

    luts = []
    for d_min, d_max in ranges:
        comp_lut = bytearray(256)
        for i in range(256):
            val = d_min + (i / 255.0) * (d_max - d_min)
            comp_lut[i] = min(255, max(0, int(val * 255.0 + 0.5)))
        luts.append(comp_lut)

    return luts


def _apply_decode_luts(raw_data, luts, n_components):
    """Apply per-component decode LUTs to interleaved sample data."""
    result = bytearray(len(raw_data))
    for i, b in enumerate(raw_data):
        comp = i % n_components
        result[i] = luts[comp][b]
    return bytes(result)


def clear_caches():
    """Clear all ICC profile caches."""
    _profile_cache.clear()
    _transform_cache.clear()
    _stream_to_hash.clear()
    _color_cache.clear()
