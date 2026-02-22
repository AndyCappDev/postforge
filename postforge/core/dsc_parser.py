# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
DSC (Document Structuring Conventions) Header Parser

Parses structured comments from PostScript file headers per Adobe's
Document Structuring Conventions specification.  DSC comments start
with %% and appear at the top of the file before %%EndComments or the
first non-comment line.
"""


class DSCHeader:
    """Parsed DSC header information from a PostScript file."""

    __slots__ = ('orientation',)

    def __init__(self) -> None:
        self.orientation: str | None = None  # 'Portrait' or 'Landscape'


def parse_dsc_header(file_path: str) -> DSCHeader:
    """Parse DSC header comments from a PostScript file.

    Reads the header section looking for structured comments.  The header
    starts with %!PS-Adobe and ends at %%EndComments or the first line
    that isn't a comment.

    Args:
        file_path: Path to the PostScript file.

    Returns:
        DSCHeader with parsed values.
    """
    header = DSCHeader()
    try:
        with open(file_path, 'rb') as f:
            for _ in range(50):  # DSC header is always near the top
                line = f.readline(512)
                if not line:
                    break
                # DSC comments are ASCII — decode leniently
                try:
                    text = line.decode('ascii', errors='ignore').rstrip()
                except Exception:
                    break
                if text.startswith('%%EndComments'):
                    break
                # First non-comment, non-blank line ends the header
                if text and not text.startswith('%'):
                    break
                if text.startswith('%%Orientation:'):
                    value = text[len('%%Orientation:'):].strip()
                    if value in ('Portrait', 'Landscape'):
                        header.orientation = value
    except (OSError, IOError):
        pass
    return header
