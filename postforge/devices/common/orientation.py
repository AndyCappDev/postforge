# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
Orientation detection for PostScript pages.

Provides a device-agnostic heuristic to detect landscape content rendered
onto portrait pages (e.g., PostScript files that use ``90 rotate``).
"""


def detect_landscape(display_list: list, width_pts: float,
                     height_pts: float) -> int:
    """Heuristic fallback for landscape detection when DSC is absent.

    Reads pre-tallied CTM rotation votes from the display list to determine
    the dominant content rotation.  Each painting operation's CTM x-axis
    direction was classified to the nearest 90° during display list
    construction.  If the vast majority agree on a non-zero rotation, that
    value is returned as the page rotation angle.

    Args:
        display_list: Display list from the context (with ``rotation_votes``).
        width_pts: Page width in points.
        height_pts: Page height in points.

    Returns:
        Rotation angle (0, 90, or 270).
    """
    # Only consider portrait pages
    if width_pts >= height_pts:
        return 0

    votes = getattr(display_list, 'rotation_votes', None)
    if not votes:
        return 0

    # votes layout: [0°, 90°, 180°, 270°]
    total = sum(votes)
    if total < 5:
        return 0

    angle_map = {0: 0, 1: 90, 2: 180, 3: 270}
    best_idx = max(range(4), key=lambda i: votes[i])
    best_angle = angle_map[best_idx]

    # Only apply rotation for landscape orientations (90/270)
    if best_angle in (90, 270) and votes[best_idx] > total * 0.8:
        return best_angle
    return 0
