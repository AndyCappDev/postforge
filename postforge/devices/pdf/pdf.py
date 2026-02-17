# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PDF Output Device

This module renders PostScript display lists to PDF files using Cairo's PDFSurface.
It uses the common Cairo renderer, applying a transformation matrix to convert
from device coordinates to PDF coordinates (points, 72 DPI).

Multi-Page Support:
    PDF output creates a single file with all pages. The PDFDocumentState class
    maintains the Cairo surface across showpage calls. The document is finalized
    when finalize_document() is called (at job end).

Font Embedding Architecture:
- Standard 14 fonts: Rendered by Cairo, guaranteed available in all PDF viewers
- Non-Standard fonts: Deferred during Cairo rendering, then written directly to PDF
  using pypdf with embedded Type 1 font data reconstructed from PostScript font
  dictionaries. This bypasses Cairo's font substitution to ensure correct rendering.
"""

import os

import cairo

from ...core import types as ps
from ..common.cairo_renderer import render_display_list
from .font_tracker import FontTracker
from .pdf_injector import is_pypdf_available


class PDFDocumentState:
    """
    Maintains state for a multi-page PDF document.

    This class is stored in the page device dictionary and persists across
    showpage calls. It accumulates font usage and deferred text objects
    across all pages for final injection at document end.
    """

    def __init__(self, file_path, width_pdf, height_pdf):
        """
        Initialize PDF document state.

        Args:
            file_path: Output file path
            width_pdf: Page width in PDF points
            height_pdf: Page height in PDF points
        """
        self.file_path = file_path
        self.width_pdf = width_pdf
        self.height_pdf = height_pdf

        # Create PDF surface
        self.surface = cairo.PDFSurface(file_path, width_pdf, height_pdf)
        self.context = cairo.Context(self.surface)

        # Font tracking across all pages
        self.font_tracker = FontTracker()

        # Deferred text objects from all pages (for font embedding)
        self.all_deferred_text_objs = []

        # Page counter
        self.pages_written = 0

        # Scaling factors (set per page, should be consistent)
        self.scale_x = None
        self.scale_y = None

    def start_new_page(self, width_pdf, height_pdf, scale_x, scale_y):
        """
        Start a new page in the document.

        For pages after the first, this calls show_page() on the surface
        to advance to a new page.

        Args:
            width_pdf: Page width in PDF points
            height_pdf: Page height in PDF points
            scale_x: Device to PDF scale factor (X)
            scale_y: Device to PDF scale factor (Y)
        """
        if self.pages_written > 0:
            # Advance to next page
            self.surface.show_page()

            # Update page size if different
            if width_pdf != self.width_pdf or height_pdf != self.height_pdf:
                self.surface.set_size(width_pdf, height_pdf)
                self.width_pdf = width_pdf
                self.height_pdf = height_pdf

        # Store scaling factors
        self.scale_x = scale_x
        self.scale_y = scale_y

        # Reset context transformation for new page
        self.context.set_matrix(cairo.Matrix(scale_x, 0, 0, scale_y, 0, 0))

    def finish_page(self):
        """Mark the current page as complete."""
        self.pages_written += 1

    def finalize(self):
        """
        Finalize the PDF document.

        This closes the Cairo surface, injects embedded fonts,
        and compresses all streams to minimize file size.
        """
        # Finalize the last page (show_page is normally called by the NEXT
        # page's start_new_page, but the last page has no successor)
        if self.pages_written > 0:
            self.surface.show_page()

        # Finish the Cairo surface
        self.surface.finish()

        # Inject fonts if needed
        if self.all_deferred_text_objs and is_pypdf_available():
            from .pdf_injector import PDFInjector
            pdf_injector = PDFInjector()
            pdf_injector.inject_text_and_fonts(
                self.file_path,
                self.all_deferred_text_objs,
                self.font_tracker,
                self.scale_x,
                self.scale_y,
                self.height_pdf
            )
        elif is_pypdf_available():
            # No font injection needed, but still compress Cairo's
            # uncompressed content streams to reduce file size
            _compress_pdf(self.file_path)


# Key used to store PDF state in page device
PDF_STATE_KEY = b'_PDFDocumentState'


def showpage(ctxt: ps.Context, pd: dict) -> None:
    """
    Render the current display list to the PDF document.

    Args:
        ctxt: PostScript context containing the display list
        pd: Page device dictionary with rendering parameters
    """
    # Compute transformation from device space to PDF space
    # PDF uses points (72 DPI), device may use different resolution
    hw_res_x = pd[b"HWResolution"].get(ps.Int(0))[1].val
    hw_res_y = pd[b"HWResolution"].get(ps.Int(1))[1].val

    # Scale factors: device pixels to PDF points
    scale_x = 72.0 / hw_res_x
    scale_y = 72.0 / hw_res_y

    min_line_width = pd[b"LineWidthMin"].val

    # Get page dimensions in device space
    WIDTH_device = pd[b"MediaSize"].get(ps.Int(0))[1].val
    HEIGHT_device = pd[b"MediaSize"].get(ps.Int(1))[1].val

    # Convert to PDF points
    WIDTH_pdf = WIDTH_device * scale_x
    HEIGHT_pdf = HEIGHT_device * scale_y

    # Get or create PDF document state
    pdf_state = pd.get(PDF_STATE_KEY)

    if pdf_state is None:
        # First page - create new document
        # Get base name from page device, default to "page"
        if b"OutputBaseName" in pd:
            base_name = pd[b"OutputBaseName"].python_string()
        else:
            base_name = "page"

        # Get output directory from page device, default to OUTPUT_DIRECTORY
        if b"OutputDirectory" in pd:
            output_dir = pd[b"OutputDirectory"].python_string()
        else:
            output_dir = ps.OUTPUT_DIRECTORY

        file_name = os.path.join(os.getcwd(), output_dir, f"{base_name}.pdf")

        # Create PDF document state
        pdf_state = PDFDocumentState(file_name, WIDTH_pdf, HEIGHT_pdf)
        pd[PDF_STATE_KEY] = pdf_state

    # Start new page
    pdf_state.start_new_page(WIDTH_pdf, HEIGHT_pdf, scale_x, scale_y)

    # Set Cairo context properties
    cc = pdf_state.context
    cc.set_tolerance(ctxt.gstate.flatness / 10.0)

    # Track fonts used in the display list and detect ActualText markers
    has_actual_text = False
    for item in ctxt.display_list:
        if isinstance(item, ps.TextObj):
            pdf_state.font_tracker.track_text_obj(item)
        elif isinstance(item, ps.ActualTextStart):
            has_actual_text = True

    # Collect deferred text: non-Standard 14 fonts and Type 3 ActualText entries
    needs_deferred = pdf_state.font_tracker.needs_embedding() or has_actual_text
    deferred_text_objs = [] if needs_deferred else None

    # Use common renderer
    render_display_list(ctxt, cc, HEIGHT_device, min_line_width, deferred_text_objs)

    # Accumulate deferred text objects for later font injection
    # Tag each with current page number (0-indexed)
    # Each entry in deferred_text_objs is (text_obj, clip_info)
    if deferred_text_objs:
        page_num = pdf_state.pages_written  # Current page before finish_page increments
        for text_obj, clip_info in deferred_text_objs:
            pdf_state.all_deferred_text_objs.append((page_num, text_obj, clip_info))

    # Mark page complete
    pdf_state.finish_page()


def _compress_pdf(file_path):
    """Compress content streams in a Cairo-generated PDF.

    Cairo writes uncompressed content streams. This reads the PDF with pypdf,
    applies FlateDecode compression to all page content streams, and writes
    it back. Typically reduces file size by ~70%.
    """
    try:
        from pypdf import PdfReader, PdfWriter
        with open(file_path, 'rb') as f:
            reader = PdfReader(f, strict=False)
            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)
            for page in writer.pages:
                page.compress_content_streams()
            with open(file_path, 'wb') as f:
                writer.write(f)
    except Exception:
        pass  # Compression is best-effort — uncompressed PDF still works


def finalize_document(pd: dict) -> None:
    """
    Finalize the PDF document at job end.

    This should be called when the job completes to close the PDF surface
    and inject any embedded fonts.

    Args:
        pd: Page device dictionary containing the PDF state
    """
    pdf_state = pd.get(PDF_STATE_KEY)

    if pdf_state is not None:
        pdf_state.finalize()
        # Remove state from page device
        del pd[PDF_STATE_KEY]
