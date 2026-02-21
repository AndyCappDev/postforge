# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
Default ICC Color Management — Tier 3

Provides ICC-based DeviceCMYK→sRGB conversion using a system-installed CMYK
ICC profile. This matches GhostScript's behavior of applying ICC color
management to Device* color spaces for print-accurate on-screen rendering.

Key design: ICC transforms are applied only at the device rendering pipeline
(display list building, image rasterization, shading interpolation).
PostScript operators (currentrgbcolor, currentgray, etc.) remain PLRM-compliant.

Profile sourcing: Searches for system-installed CMYK ICC profiles. Falls back
gracefully to naive PLRM formulas if no profile is found or ImageCms is
unavailable.
"""

import glob
import hashlib
import io
import os
import sys

from . import icc_profile

try:
    from PIL import ImageCms
    _IMAGECMS_AVAILABLE = True
except ImportError:
    _IMAGECMS_AVAILABLE = False

# Module-level state
_default_cmyk_hash = None    # SHA-256 hash once loaded
_initialized = False          # Lazy init flag
_disabled = False             # Set by --no-icc
_custom_profile_path = None   # Set by --cmyk-profile


def disable() -> None:
    """Disable ICC color management. Called from CLI --no-icc."""
    global _disabled
    _disabled = True


def set_custom_profile(path: str) -> None:
    """Set a custom CMYK ICC profile path. Called from CLI --cmyk-profile."""
    global _custom_profile_path
    _custom_profile_path = path


def _is_cmyk_profile(path: str) -> bool:
    """Check whether an ICC profile file has a CMYK color space.

    Reads the color space signature at byte offset 16 in the ICC header.
    'CMYK' means the profile accepts CMYK input.
    """
    try:
        with open(path, 'rb') as f:
            f.seek(16)
            sig = f.read(4)
            return sig == b'CMYK'
    except (OSError, IOError):
        return False


def _find_cmyk_in_dir(directory: str) -> str | None:
    """Search a directory for the first .icc/.icm file with CMYK color space."""
    if not os.path.isdir(directory):
        return None
    for f in sorted(os.listdir(directory)):
        if f.lower().endswith(('.icc', '.icm')):
            path = os.path.join(directory, f)
            if _is_cmyk_profile(path):
                return path
    return None


def _find_cmyk_profile() -> str | None:
    """Search system paths for a CMYK ICC profile.

    Returns:
        File path string, or None if no profile found.
    """
    # Custom path from --cmyk-profile
    if _custom_profile_path:
        if os.path.isfile(_custom_profile_path):
            return _custom_profile_path
        return None

    # Linux paths — check well-known filenames first
    linux_paths = [
        '/usr/share/color/icc/ghostscript/default_cmyk.icc',
        '/usr/share/color/icc/ghostscript/ps_cmyk.icc',
    ]
    for p in linux_paths:
        if os.path.isfile(p):
            return p

    # Linux colord paths (glob for SWOP profiles)
    swop_matches = sorted(glob.glob('/usr/share/color/icc/colord/SWOP*.icc'))
    if swop_matches:
        return swop_matches[0]

    fogra_path = '/usr/share/color/icc/colord/FOGRA39L_coated.icc'
    if os.path.isfile(fogra_path):
        return fogra_path

    # macOS paths — scan directories for any CMYK profile
    if sys.platform == 'darwin':
        mac_dirs = [
            '/Library/ColorSync/Profiles',
            os.path.expanduser('~/Library/ColorSync/Profiles'),
            '/System/Library/ColorSync/Profiles',
        ]
        for d in mac_dirs:
            result = _find_cmyk_in_dir(d)
            if result:
                return result

    # Windows paths — scan system color directory for any CMYK profile
    if sys.platform == 'win32':
        sysroot = os.environ.get('SYSTEMROOT', r'C:\Windows')
        win_dir = os.path.join(sysroot, 'System32', 'spool', 'drivers', 'color')
        result = _find_cmyk_in_dir(win_dir)
        if result:
            return result

    return None


def initialize() -> None:
    """Eagerly initialize ICC profile search.

    Called from CLI at startup so the profile message prints before
    job execution. Safe to call multiple times (idempotent).
    """
    _ensure_initialized()


def _ensure_initialized() -> None:
    """Lazy initialization: find profile, load, build transform.

    Called on first conversion attempt. Sets _default_cmyk_hash on success.
    """
    global _initialized, _default_cmyk_hash

    if _initialized:
        return
    _initialized = True

    if _disabled:
        return

    if not icc_profile.is_available():
        return

    profile_path = _find_cmyk_profile()
    if profile_path is None:
        print("ICC: No CMYK profile found, using PLRM conversion formulas")
        return

    try:
        with open(profile_path, 'rb') as f:
            icc_bytes = f.read()
    except (OSError, IOError):
        return

    if not icc_bytes or len(icc_bytes) < 128:
        return

    profile_hash = hashlib.sha256(icc_bytes).digest()

    # Register in icc_profile's cache if not already present
    if profile_hash not in icc_profile._profile_cache:
        try:
            profile = ImageCms.getOpenProfile(io.BytesIO(icc_bytes))
            icc_profile._profile_cache[profile_hash] = profile
        except Exception:
            return

    # Verify we can build the transform
    transform = icc_profile.get_transform(profile_hash, 4)
    if transform is None:
        return

    _default_cmyk_hash = profile_hash


def get_cmyk_profile_hash() -> bytes | None:
    """Return the default CMYK profile hash, or None if disabled/unavailable.

    Triggers lazy initialization on first call.
    """
    _ensure_initialized()
    return _default_cmyk_hash


def convert_cmyk_color(c: float, m: float, y: float, k: float) -> tuple[float, float, float] | None:
    """Convert a single CMYK color to sRGB using the default ICC profile.

    Args:
        c, m, y, k: CMYK component values (0.0-1.0)

    Returns:
        (r, g, b) tuple of floats (0.0-1.0), or None if ICC unavailable.
    """
    profile_hash = get_cmyk_profile_hash()
    if profile_hash is None:
        return None

    return icc_profile.icc_convert_color(profile_hash, 4, [c, m, y, k])


def convert_cmyk_image(sample_data: bytes, width: int, height: int,
                       bits_per_component: int,
                       decode_array: list[float] | None) -> bytearray | None:
    """Bulk CMYK image to Cairo BGRX conversion using the default ICC profile.

    Only handles 8-bit samples. Returns None for other bit depths or if ICC
    is unavailable, so callers fall through to existing pixel loops.

    Args:
        sample_data: Raw CMYK sample bytes
        width, height: Image dimensions
        bits_per_component: Bits per sample component (must be 8)
        decode_array: Decode array for sample mapping

    Returns:
        bytearray of Cairo BGRX pixel data, or None on failure.
    """
    if bits_per_component != 8:
        return None

    profile_hash = get_cmyk_profile_hash()
    if profile_hash is None:
        return None

    return icc_profile.icc_convert_image(
        profile_hash, 4, sample_data, width, height,
        bits_per_component, decode_array)
