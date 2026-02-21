# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared Cairo rendering utilities."""


def _safe_rgb(color):
    """Normalize a color list to an (r, g, b) tuple, defaulting to black."""
    if not color:
        return (0, 0, 0)
    if len(color) >= 3:
        return (color[0], color[1], color[2])
    if len(color) == 1:
        return (color[0], color[0], color[0])
    return (0, 0, 0)
