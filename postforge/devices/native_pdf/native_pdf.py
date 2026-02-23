# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
Native PDF Output Device

Generates PDF content streams directly from the PostScript display list,
preserving original color space information (CMYK, Gray, RGB). Unlike the
Cairo-based PDF device, this device does not convert everything to RGB.

The device accumulates pages and assembles the final PDF at document end,
embedding fonts using the same infrastructure as the Cairo PDF device.
"""

import math
import os

from ...core import types as ps
from ..pdf.font_tracker import FontTracker
from ..pdf.font_embedder import FontEmbedder
from ..pdf.cff_font_embedder import CFFEmbedder
from .content_stream import generate_content_stream
from .pdf_builder import PDFBuilder, PageData


class NativePDFDocumentState:
    """Maintains state for a multi-page native PDF document."""

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self.font_tracker = FontTracker()
        self.pages: list[PageData] = []
        self.pages_written = 0
        self.lossless_images = False
        # Document-level Type 3 font accumulation (shared across pages)
        self.type3_fonts: dict = {}
        self.type3_font_counter: int = 0

    def finalize(self) -> None:
        """Assemble all pages into final PDF with embedded fonts."""
        if not self.pages:
            return

        try:
            builder = PDFBuilder(lossless_images=self.lossless_images)

            # Pre-compute glyph widths for TJ kern calculations
            font_widths_cache: dict[tuple, dict[int, int]] = {}
            font_embedder = FontEmbedder()
            cff_embedder = CFFEmbedder()
            for font_key, usage in self.font_tracker.get_fonts_in_order():
                if not FontTracker.is_cid_font(font_key):
                    if builder._is_cff_font(usage.font_dict):
                        widths = cff_embedder.get_glyph_widths(
                            usage.font_dict, usage.glyphs_used)
                    elif builder._is_type42_font(usage.font_dict):
                        widths = builder._get_type42_glyph_widths(
                            usage.font_dict, usage.glyphs_used)
                    else:
                        widths = font_embedder.get_glyph_widths(
                            usage.font_dict, usage.glyphs_used)
                    if widths:
                        font_widths_cache[font_key] = widths

            # Build and write PDF
            builder.build_pdf(self.pages, self.font_tracker, self.file_path,
                              self.type3_fonts)
            print(f"   Output: {self.file_path} ({self.pages_written} page(s))")
        except Exception as e:
            import traceback
            print(f"[native_pdf] Failed to build PDF: {e}")
            traceback.print_exc()


# Key used to store state in page device
_STATE_KEY = b'_NativePDFDocumentState'


def showpage(ctxt: ps.Context, pd: dict) -> None:
    """Render the current display list to the native PDF document.

    Args:
        ctxt: PostScript context containing the display list.
        pd: Page device dictionary with rendering parameters.
    """
    # The native PDF device works at 72 DPI (1:1 with PDF points)
    # so device coordinates ARE PDF coordinates.
    hw_res_x = pd[b"HWResolution"].get(ps.Int(0))[1].val
    hw_res_y = pd[b"HWResolution"].get(ps.Int(1))[1].val

    # Page dimensions in device space
    width_device = pd[b"MediaSize"].get(ps.Int(0))[1].val
    height_device = pd[b"MediaSize"].get(ps.Int(1))[1].val

    # Convert to PDF points
    scale_x = 72.0 / hw_res_x
    scale_y = 72.0 / hw_res_y
    width_pts = width_device * scale_x
    height_pts = height_device * scale_y

    # Get or create document state
    state = pd.get(_STATE_KEY)
    if state is None:
        if b"OutputBaseName" in pd:
            base_name = pd[b"OutputBaseName"].python_string()
        else:
            base_name = "page"

        if b"OutputDirectory" in pd:
            output_dir = pd[b"OutputDirectory"].python_string()
        else:
            output_dir = ps.OUTPUT_DIRECTORY

        file_path = os.path.join(os.getcwd(), output_dir, f"{base_name}.pdf")
        state = NativePDFDocumentState(file_path)
        lossless = pd.get(b'LosslessImages')
        if lossless is not None and lossless.val:
            state.lossless_images = True
        pd[_STATE_KEY] = state

    # Track fonts used in display list
    for item in ctxt.display_list:
        if isinstance(item, ps.TextObj):
            state.font_tracker.track_text_obj(item)

    # Build embedded_fonts dict for content stream generation
    # (We need this to know which font resource names to use.)
    # For now, build a temporary mapping from font_key to resource name
    # that will match what pdf_builder uses.
    embedded_fonts = _build_temp_font_mapping(state.font_tracker)

    # Pre-compute glyph widths for text rendering
    font_widths_cache: dict[tuple, dict[int, int]] = {}
    font_embedder = FontEmbedder()
    cff_embedder = CFFEmbedder()
    for font_key, usage in state.font_tracker.get_fonts_in_order():
        if not FontTracker.is_cid_font(font_key):
            if _is_cff_font(usage.font_dict):
                widths = cff_embedder.get_glyph_widths(
                    usage.font_dict, usage.glyphs_used)
            elif _is_type42_font(usage.font_dict):
                widths = {}  # Type 42 widths handled differently
            else:
                widths = font_embedder.get_glyph_widths(
                    usage.font_dict, usage.glyphs_used)
            if widths:
                font_widths_cache[font_key] = widths

    # Generate PDF content stream from display list
    device_scale = 72.0 / hw_res_x  # device units → PDF points
    (content_stream, shading_defs, image_defs, type3_font_defs,
     state.type3_font_counter, type3_page_keys) = generate_content_stream(
        ctxt.display_list, height_device,
        state.font_tracker, embedded_fonts, font_widths_cache,
        device_scale, state.type3_fonts, state.type3_font_counter)
    state.type3_fonts = type3_font_defs

    # Collect fonts used on this page
    page_font_keys: set[tuple] = set()
    standard14_used: set[str] = set()
    has_actual_text = False
    for item in ctxt.display_list:
        if isinstance(item, ps.TextObj):
            font_key = state.font_tracker.get_font_key_for_dict(item.font_dict)
            if font_key is not None:
                page_font_keys.add(font_key)
            elif item.font_name in FontTracker.STANDARD_14:
                standard14_used.add(item.font_name.decode('latin-1'))
        elif isinstance(item, ps.ActualTextStart):
            has_actual_text = True

    # Page rotation: prefer DSC %%Orientation (set by cli_runner),
    # fall back to display list CTM analysis for files without DSC.
    # Only apply rotation on portrait pages — if the MediaBox is already
    # landscape (e.g., via setpagedevice), rotation would be wrong.
    page_rotate = 0
    if width_pts < height_pts:
        dsc_orient = pd.get(b'DSCOrientation')
        if dsc_orient is not None and dsc_orient.val == b'Landscape':
            page_rotate = 90
        else:
            page_rotate = _detect_landscape(
                ctxt.display_list, width_pts, height_pts)

    # Store page data
    page_data = PageData(content_stream, width_pts, height_pts)
    page_data.font_keys_used = page_font_keys
    page_data.standard14_fonts = standard14_used
    page_data.needs_invisible_font = has_actual_text
    page_data.rotate = page_rotate
    page_data.shading_defs = shading_defs
    page_data.image_defs = image_defs
    page_data.type3_page_keys = type3_page_keys
    state.pages.append(page_data)
    state.pages_written += 1


def finalize(pd: dict) -> None:
    """Finalize the native PDF document at job end.

    Args:
        pd: Page device dictionary containing the document state.
    """
    state = pd.get(_STATE_KEY)
    if state is not None:
        state.finalize()
        del pd[_STATE_KEY]


def _build_temp_font_mapping(font_tracker: FontTracker) -> dict[tuple, tuple[str, None]]:
    """Build a temporary font mapping for content stream generation.

    The actual font refs are created later by pdf_builder, but the resource
    names must match. This builds the same naming scheme.

    Args:
        font_tracker: Font tracker with usage data.

    Returns:
        Dict mapping font_key -> (resource_name, None).
    """
    mapping: dict[tuple, tuple[str, None]] = {}
    font_name_counts: dict[bytes, int] = {}

    for font_key, usage in font_tracker.get_fonts_in_order():
        font_name = usage.font_name

        if font_name not in font_name_counts:
            font_name_counts[font_name] = 0
        instance_num = font_name_counts[font_name]
        font_name_counts[font_name] += 1

        font_name_str = font_name.decode('latin-1') if isinstance(font_name, bytes) else str(font_name)
        base_name = font_name_str.replace('-', '').replace(' ', '')
        resource_name = f'/{base_name}_{instance_num}' if instance_num > 0 else f'/{base_name}'
        mapping[font_key] = (resource_name, None)

    return mapping


def _is_cff_font(font_dict: ps.Dict) -> bool:
    """Check if a font dictionary is a CFF (Type 2) font."""
    font_type = font_dict.val.get(b'FontType')
    return font_type is not None and font_type.val == 2


def _is_type42_font(font_dict: ps.Dict) -> bool:
    """Check if a font dictionary is a Type 42 (TrueType) font."""
    font_type = font_dict.val.get(b'FontType')
    return font_type is not None and font_type.val == 42


def _detect_landscape(display_list: list, width_pts: float,
                       height_pts: float) -> int:
    """Heuristic fallback for landscape detection when DSC is absent.

    Examines CTMs of display list elements to determine the dominant content
    rotation.  Each element's x-axis direction ``(a, b)`` is classified to
    the nearest 90-degree multiple.  If the vast majority agree on a non-zero
    rotation, that value is returned as the page ``/Rotate``.

    Args:
        display_list: Display list elements from the context.
        width_pts: Page width in PDF points.
        height_pts: Page height in PDF points.

    Returns:
        Rotation angle (0, 90, or 270).
    """
    # Only consider portrait pages
    if width_pts >= height_pts:
        return 0

    votes: dict[int, int] = {0: 0, 90: 0, 180: 0, 270: 0}
    for item in display_list:
        ctm = getattr(item, 'ctm', None)
        if ctm is None or len(ctm) < 4:
            continue
        a, b = ctm[0], ctm[1]
        if a * a + b * b < 1e-10:
            continue
        # Classify x-axis direction to nearest 90°.
        # The content stream's initial cm has a negative y-scale (y-flip)
        # which mirrors the rotation direction: a PS +90° CCW rotation
        # appears as -90° in the rendered PDF.  Swap 90↔270 to produce
        # the correct /Rotate value that compensates.
        if abs(a) >= abs(b):
            nearest = 0 if a >= 0 else 180
        else:
            nearest = 270 if b >= 0 else 90
        votes[nearest] += 1

    total = sum(votes.values())
    if total < 5:
        return 0

    best = max(votes, key=votes.get)
    # Only apply rotation for landscape orientations (90/270)
    if best in (90, 270) and votes[best] > total * 0.8:
        return best
    return 0


