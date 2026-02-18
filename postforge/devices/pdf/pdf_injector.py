# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PDF Injector Module

Uses pypdf to inject text content and embedded fonts into Cairo-generated PDF files.

Cairo renders graphics and Standard 14 font text. Non-Standard 14 fonts are skipped
during Cairo rendering and added here by:
1. Embedding the actual Type 1 font data (reconstructed from PostScript font dicts)
2. Writing PDF text operators (BT/Tf/Td/Tj/ET) directly to the content stream

This bypasses Cairo's font substitution to ensure correct rendering with the
actual PostScript fonts.
"""

import logging
import math
import struct
import unicodedata
import zlib

from ...core import types as ps
from ...core.unicode_mapping import glyph_name_to_unicode
from .font_embedder import FontEmbedder, generate_tounicode_cmap
from .cid_font_embedder import CIDFontEmbedder, generate_cid_tounicode_cmap
from .cff_font_embedder import CFFEmbedder
from .font_tracker import FontTracker

# pypdf is optional - gracefully handle if not installed
try:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import (
        ArrayObject,
        DictionaryObject,
        NameObject,
        NumberObject,
        StreamObject,
    )
    PYPDF_AVAILABLE = True
    # Suppress noisy "Multiple definitions in dictionary" warnings from pypdf
    logging.getLogger('pypdf').setLevel(logging.ERROR)
except ImportError:
    PYPDF_AVAILABLE = False


class PDFInjector:
    """
    Inject text content and embedded fonts into Cairo-generated PDF files.

    For non-Standard 14 fonts, this class:
    1. Embeds Type 1 font data reconstructed from PostScript font dictionaries
    2. Writes PDF text operators for each TextObj that was deferred during
       Cairo rendering
    """

    def __init__(self):
        """Initialize PDF injector."""
        if not PYPDF_AVAILABLE:
            raise ImportError(
                "pypdf is required for font embedding. "
                "Install with: pip install pypdf"
            )
        self.font_embedder = FontEmbedder()
        self.cid_font_embedder = CIDFontEmbedder()
        self.cff_embedder = CFFEmbedder()

    def inject_text_and_fonts(self, pdf_path, deferred_text_objs, font_tracker,
                               scale_x, scale_y, page_height_pdf):
        """
        Add deferred text and embedded fonts to Cairo-generated PDF.

        Args:
            pdf_path: Path to the PDF file to modify
            deferred_text_objs: List of TextObj elements to render
            font_tracker: FontTracker with font usage data
            scale_x: Device to PDF scale factor (X)
            scale_y: Device to PDF scale factor (Y)
            page_height_pdf: Page height in PDF points
        """
        if not deferred_text_objs:
            return

        try:
            # Read the existing PDF (strict=False suppresses duplicate key warnings)
            with open(pdf_path, 'rb') as f:
                reader = PdfReader(f, strict=False)
                writer = PdfWriter()

                # Clone all pages
                for page in reader.pages:
                    writer.add_page(page)

                # Track font instances we've embedded (font_key -> PDF resource name)
                # Key is (CharStrings id, Encoding id) composite from FontTracker,
                # which deduplicates scaled instances of the same base font.
                embedded_fonts = {}

                # Track how many times each base font name is used (for unique resource names)
                font_name_counts = {}

                # Find the fullest Subrs for each CharStrings group.
                # DVIPS re-encoded font instances may have truncated Subrs.
                best_subrs = font_tracker.get_best_subrs()

                # Embed all fonts that need embedding
                for font_key, usage in font_tracker.get_fonts_in_order():
                    font_name = usage.font_name

                    # Generate unique resource name for this font instance
                    if font_name not in font_name_counts:
                        font_name_counts[font_name] = 0
                    instance_num = font_name_counts[font_name]
                    font_name_counts[font_name] += 1

                    # Dispatch: CID fonts vs CFF fonts vs Type 42 vs Type 1 fonts
                    if FontTracker.is_cid_font(font_key):
                        pdf_font_name = self._embed_cid_font(
                            writer, font_name, usage, instance_num
                        )
                    elif self._is_cff_font(usage.font_dict):
                        pdf_font_name = self._embed_cff_font(
                            writer, font_name, usage, instance_num
                        )
                    elif self._is_type42_font(usage.font_dict):
                        pdf_font_name = self._embed_type42_font(
                            writer, font_name, usage, instance_num
                        )
                    else:
                        # Look up the best Subrs for this CharStrings group
                        # (keyed by CharStrings identity, not font name)
                        subrs_override = best_subrs.get(font_key[0])

                        pdf_font_name = self._embed_font(
                            writer, font_name, usage, instance_num, subrs_override
                        )
                    if pdf_font_name:
                        embedded_fonts[font_key] = pdf_font_name

                # Group deferred TextObjs by page number
                # Each entry is a tuple: (page_num, text_obj, clip_info)
                text_objs_by_page = {}
                for page_num, text_obj, clip_info in deferred_text_objs:
                    if page_num not in text_objs_by_page:
                        text_objs_by_page[page_num] = []
                    text_objs_by_page[page_num].append((text_obj, clip_info))

                # Generate text content for each page and inject
                for page_num, page_text_objs in text_objs_by_page.items():
                    if page_num >= len(writer.pages):
                        continue  # Safety check

                    text_content = self._generate_text_content(
                        page_text_objs, embedded_fonts, font_tracker,
                        scale_x, scale_y, page_height_pdf
                    )

                    if text_content:
                        page = writer.pages[page_num]
                        self._append_content_to_page(writer, page, text_content)
                        self._add_fonts_to_page(page, embedded_fonts)
                        # Add Courier for invisible text if any ActualTextStart on this page
                        if any(isinstance(entry, ps.ActualTextStart) for entry, _ in page_text_objs):
                            self._add_invisible_text_fonts(page, writer)

                # Compress all content streams (Cairo writes uncompressed)
                for page in writer.pages:
                    page.compress_content_streams()

                # Write modified PDF back
                with open(pdf_path, 'wb') as f:
                    writer.write(f)

        except Exception as e:
            # Font embedding is best-effort - don't fail the entire render
            import traceback
            print(f"[PDF] Font injection failed: {e}")
            traceback.print_exc()

    def _embed_font(self, writer, font_name, usage, instance_num=0,
                    subrs_override=None):
        """
        Embed a Type 1 font and return its PDF resource name.

        Args:
            writer: PdfWriter instance
            font_name: Font name (bytes)
            usage: FontUsage object with font_dict and glyphs_used
            instance_num: Instance number for unique resource naming (0-based)
            subrs_override: Optional Subrs array to use instead of the font's own.
                            Used when re-encoded font instances have truncated Subrs.

        Returns:
            PDF resource name (e.g., '/F1') or None if embedding failed
        """
        font_dict = usage.font_dict
        glyphs_used = usage.glyphs_used

        font_name_str = font_name.decode('latin-1') if isinstance(font_name, bytes) else str(font_name)

        # Create unique font name for PDF (BaseFont/FontName) to avoid conflicts
        # when multiple instances of the same base font are embedded
        if instance_num > 0:
            unique_font_name = f"{font_name_str}_{instance_num}"
        else:
            unique_font_name = font_name_str

        # Reconstruct the Type 1 font file data with unique name, subset to used glyphs
        result = self.font_embedder.get_font_file_data(
            font_dict, unique_font_name, glyphs_used, subrs_override
        )
        if result is None:
            return None

        font_file_data, length1, length2, length3 = result

        # Get font metrics from PostScript font
        font_bbox = self._get_font_bbox(font_dict)
        first_char, last_char = self._get_char_range(glyphs_used)
        widths = self._get_widths(font_dict, glyphs_used, first_char, last_char)

        # Convert to PFB format — Poppler/FreeType needs PFB segment markers
        # to distinguish binary eexec from the PFA-style ASCII header
        pfb_data = self._to_pfb(font_file_data, length1, length2, length3)

        # Create FontFile stream (FlateDecode compressed)
        compressed = zlib.compress(pfb_data)
        font_file_stream = StreamObject()
        font_file_stream._data = compressed
        font_file_stream[NameObject('/Length')] = NumberObject(len(compressed))
        font_file_stream[NameObject('/Length1')] = NumberObject(length1)
        font_file_stream[NameObject('/Length2')] = NumberObject(length2)
        font_file_stream[NameObject('/Length3')] = NumberObject(length3)
        font_file_stream[NameObject('/Filter')] = NameObject('/FlateDecode')

        font_file_ref = writer._add_object(font_file_stream)

        # Create Font Descriptor
        font_descriptor = DictionaryObject()
        font_descriptor[NameObject('/Type')] = NameObject('/FontDescriptor')
        font_descriptor[NameObject('/FontName')] = NameObject('/' + unique_font_name)
        font_descriptor[NameObject('/Flags')] = NumberObject(
            self._get_font_flags(font_dict)
        )
        font_descriptor[NameObject('/FontBBox')] = ArrayObject([
            NumberObject(int(font_bbox[0])),
            NumberObject(int(font_bbox[1])),
            NumberObject(int(font_bbox[2])),
            NumberObject(int(font_bbox[3])),
        ])
        font_descriptor[NameObject('/ItalicAngle')] = NumberObject(0)
        font_descriptor[NameObject('/Ascent')] = NumberObject(int(font_bbox[3]))
        font_descriptor[NameObject('/Descent')] = NumberObject(int(font_bbox[1]))
        font_descriptor[NameObject('/CapHeight')] = NumberObject(int(font_bbox[3] * 0.7))
        font_descriptor[NameObject('/StemV')] = NumberObject(80)
        font_descriptor[NameObject('/FontFile')] = font_file_ref

        font_descriptor_ref = writer._add_object(font_descriptor)

        # Create ToUnicode CMap for searchability
        tounicode_map = self._build_tounicode_map(font_dict, glyphs_used)
        tounicode_ref = None
        if tounicode_map:
            cmap_data = generate_tounicode_cmap(tounicode_map, font_name_str)
            cmap_stream = StreamObject()
            cmap_stream._data = cmap_data
            cmap_stream[NameObject('/Length')] = NumberObject(len(cmap_data))
            tounicode_ref = writer._add_object(cmap_stream)

        # Create the Font dictionary
        font_obj = DictionaryObject()
        font_obj[NameObject('/Type')] = NameObject('/Font')
        font_obj[NameObject('/Subtype')] = NameObject('/Type1')
        font_obj[NameObject('/BaseFont')] = NameObject('/' + unique_font_name)
        font_obj[NameObject('/FirstChar')] = NumberObject(first_char)
        font_obj[NameObject('/LastChar')] = NumberObject(last_char)
        font_obj[NameObject('/Widths')] = widths
        font_obj[NameObject('/FontDescriptor')] = font_descriptor_ref

        # Add encoding - build differences array from font's encoding
        encoding_obj = self._build_pdf_encoding(font_dict, first_char, last_char)
        if encoding_obj:
            font_obj[NameObject('/Encoding')] = encoding_obj

        if tounicode_ref:
            font_obj[NameObject('/ToUnicode')] = tounicode_ref

        font_ref = writer._add_object(font_obj)

        # Store the font reference for later
        # Use a sanitized version of font name for PDF resource name
        # Include instance number to distinguish different scaled/re-encoded versions
        base_name = font_name_str.replace('-', '').replace(' ', '')
        if instance_num > 0:
            pdf_resource_name = f'/{base_name}_{instance_num}'
        else:
            pdf_resource_name = f'/{base_name}'

        # Store as tuple: (resource_name, font_ref)
        return (pdf_resource_name, font_ref)

    def _embed_cid_font(self, writer, font_name, usage, instance_num=0):
        """
        Embed a CID/TrueType font as a PDF Type 0 font.

        Builds the PDF Type 0 structure:
          Type 0 Font -> /Subtype /Type0, /Encoding /Identity-H,
                         /DescendantFonts [CIDFont]
          CIDFont -> /Subtype /CIDFontType2, /CIDSystemInfo, /W, /DW,
                     /CIDToGIDMap /Identity
          FontDescriptor -> /FontFile2 (raw TrueType binary)
          ToUnicode -> CMap stream with 2-byte codespace

        Args:
            writer: PdfWriter instance
            font_name: Font name (bytes)
            usage: FontUsage object with font_dict and glyphs_used
            instance_num: Instance number for unique resource naming

        Returns:
            Tuple (pdf_resource_name, font_ref) or None if embedding failed
        """
        font_dict = usage.font_dict
        glyphs_used = usage.glyphs_used

        font_name_str = font_name.decode('latin-1') if isinstance(font_name, bytes) else str(font_name)

        # Create unique font name for PDF
        if instance_num > 0:
            unique_font_name = f"{font_name_str}_{instance_num}"
        else:
            unique_font_name = font_name_str

        # Get TrueType font data from sfnts array
        font_file_data = self.cid_font_embedder.get_sfnts_data(font_dict)
        if font_file_data is None:
            return None

        # Create FontFile2 stream (FlateDecode compressed TrueType binary)
        # Length1 refers to uncompressed size per PDF spec
        compressed = zlib.compress(font_file_data)
        font_file_stream = StreamObject()
        font_file_stream._data = compressed
        font_file_stream[NameObject('/Length')] = NumberObject(len(compressed))
        font_file_stream[NameObject('/Length1')] = NumberObject(len(font_file_data))
        font_file_stream[NameObject('/Filter')] = NameObject('/FlateDecode')
        font_file_ref = writer._add_object(font_file_stream)

        # Get font metrics for FontDescriptor
        metrics = self.cid_font_embedder.get_font_metrics(font_dict)
        bbox = metrics['bbox']

        # Create Font Descriptor
        font_descriptor = DictionaryObject()
        font_descriptor[NameObject('/Type')] = NameObject('/FontDescriptor')
        font_descriptor[NameObject('/FontName')] = NameObject('/' + unique_font_name)
        font_descriptor[NameObject('/Flags')] = NumberObject(4)  # Symbolic
        font_descriptor[NameObject('/FontBBox')] = ArrayObject([
            NumberObject(bbox[0]), NumberObject(bbox[1]),
            NumberObject(bbox[2]), NumberObject(bbox[3]),
        ])
        font_descriptor[NameObject('/ItalicAngle')] = NumberObject(0)
        font_descriptor[NameObject('/Ascent')] = NumberObject(metrics['ascent'])
        font_descriptor[NameObject('/Descent')] = NumberObject(metrics['descent'])
        font_descriptor[NameObject('/CapHeight')] = NumberObject(metrics['cap_height'])
        font_descriptor[NameObject('/StemV')] = NumberObject(metrics['stem_v'])
        font_descriptor[NameObject('/FontFile2')] = font_file_ref
        font_descriptor_ref = writer._add_object(font_descriptor)

        # Compute CID -> GID mapping once, reuse for widths, CIDToGIDMap, ToUnicode
        cid_to_gid = self.cid_font_embedder.get_cid_to_gid_dict(font_dict, glyphs_used)

        # Build /W array for glyph widths (using CID->GID mapping for correct hmtx lookup)
        glyph_widths = self.cid_font_embedder.get_glyph_widths(
            font_dict, glyphs_used, cid_to_gid)
        w_array_data = self.cid_font_embedder.build_w_array(glyph_widths)
        w_array = self._build_pdf_w_array(w_array_data)

        # Get default width
        default_width = self.cid_font_embedder.get_default_width(font_dict)

        # Get CIDSystemInfo
        registry, ordering, supplement = self.cid_font_embedder.get_cid_system_info(font_dict)

        # Build CIDSystemInfo dictionary
        cid_system_info = DictionaryObject()
        cid_system_info[NameObject('/Registry')] = StreamObject()
        # Use a simple string approach for Registry/Ordering
        cid_system_info[NameObject('/Registry')] = self._make_pdf_string(registry)
        cid_system_info[NameObject('/Ordering')] = self._make_pdf_string(ordering)
        cid_system_info[NameObject('/Supplement')] = NumberObject(supplement)

        # Create CIDFont dictionary (descendant)
        cid_font_dict = DictionaryObject()
        cid_font_dict[NameObject('/Type')] = NameObject('/Font')
        cid_font_dict[NameObject('/Subtype')] = NameObject('/CIDFontType2')
        cid_font_dict[NameObject('/BaseFont')] = NameObject('/' + unique_font_name)
        cid_font_dict[NameObject('/CIDSystemInfo')] = cid_system_info
        cid_font_dict[NameObject('/FontDescriptor')] = font_descriptor_ref
        cid_font_dict[NameObject('/DW')] = NumberObject(default_width)
        if w_array:
            cid_font_dict[NameObject('/W')] = w_array
        # Build CIDToGIDMap: maps CIDs to TrueType GIDs
        cid_to_gid_data = self.cid_font_embedder.build_cid_to_gid_map(
            font_dict, glyphs_used, cid_to_gid)
        if cid_to_gid_data:
            cid_to_gid_stream = StreamObject()
            cid_to_gid_stream._data = cid_to_gid_data
            cid_to_gid_stream[NameObject('/Length')] = NumberObject(len(cid_to_gid_data))
            cid_font_dict[NameObject('/CIDToGIDMap')] = writer._add_object(cid_to_gid_stream)
        else:
            cid_font_dict[NameObject('/CIDToGIDMap')] = NameObject('/Identity')
        cid_font_ref = writer._add_object(cid_font_dict)

        # Create ToUnicode CMap
        tounicode_ref = None
        tounicode_map = self.cid_font_embedder.build_tounicode_map(
            font_dict, glyphs_used, cid_to_gid)
        if tounicode_map:
            cmap_data = generate_cid_tounicode_cmap(tounicode_map, unique_font_name)
            cmap_stream = StreamObject()
            cmap_stream._data = cmap_data
            cmap_stream[NameObject('/Length')] = NumberObject(len(cmap_data))
            tounicode_ref = writer._add_object(cmap_stream)

        # Create the top-level Type 0 font dictionary
        font_obj = DictionaryObject()
        font_obj[NameObject('/Type')] = NameObject('/Font')
        font_obj[NameObject('/Subtype')] = NameObject('/Type0')
        font_obj[NameObject('/BaseFont')] = NameObject('/' + unique_font_name)
        font_obj[NameObject('/Encoding')] = NameObject('/Identity-H')
        font_obj[NameObject('/DescendantFonts')] = ArrayObject([cid_font_ref])
        if tounicode_ref:
            font_obj[NameObject('/ToUnicode')] = tounicode_ref

        font_ref = writer._add_object(font_obj)

        # Generate PDF resource name
        base_name = font_name_str.replace('-', '').replace(' ', '')
        if instance_num > 0:
            pdf_resource_name = f'/{base_name}_{instance_num}'
        else:
            pdf_resource_name = f'/{base_name}'

        return (pdf_resource_name, font_ref)

    @staticmethod
    def _is_cff_font(font_dict):
        """Check if a font dictionary is a CFF (Type 2) font.

        Args:
            font_dict: PostScript font dictionary

        Returns:
            bool: True if this is a CFF font
        """
        font_type = font_dict.val.get(b'FontType')
        return font_type is not None and font_type.val == 2

    @staticmethod
    def _is_type42_font(font_dict):
        """Check if a font dictionary is a Type 42 (TrueType) font.

        Args:
            font_dict: PostScript font dictionary

        Returns:
            bool: True if this is a Type 42 font
        """
        font_type = font_dict.val.get(b'FontType')
        return font_type is not None and font_type.val == 42

    def _embed_cff_font(self, writer, font_name, usage, instance_num=0):
        """
        Embed a CFF font as /FontFile3 /Type1C and return its PDF resource name.

        CFF fonts are embedded with:
        - /FontFile3 stream containing raw CFF binary (with /Subtype /Type1C)
        - Font dictionary with /Subtype /Type1 (CFF-backed Type 1 in PDF)
        - Widths extracted via Type 2 charstring execution

        Args:
            writer: PdfWriter instance
            font_name: Font name (bytes)
            usage: FontUsage object with font_dict and glyphs_used
            instance_num: Instance number for unique resource naming (0-based)

        Returns:
            Tuple (pdf_resource_name, font_ref) or None if embedding failed
        """
        font_dict = usage.font_dict
        glyphs_used = usage.glyphs_used

        font_name_str = (font_name.decode('latin-1') if isinstance(font_name, bytes)
                         else str(font_name))

        if instance_num > 0:
            unique_font_name = f"{font_name_str}_{instance_num}"
        else:
            unique_font_name = font_name_str

        # Get raw CFF binary
        cff_data = self.cff_embedder.get_font_file_data(font_dict)
        if cff_data is None:
            return None

        # Get font metrics
        font_bbox = self._get_font_bbox(font_dict)
        first_char, last_char = self._get_char_range(glyphs_used)

        # Get glyph widths via Type 2 charstring execution
        glyph_widths = self.cff_embedder.get_glyph_widths(font_dict, glyphs_used)
        default_width = 600
        widths = ArrayObject([
            NumberObject(glyph_widths.get(cc, default_width))
            for cc in range(first_char, last_char + 1)
        ])

        # Create FontFile3 stream (FlateDecode compressed CFF binary)
        compressed = zlib.compress(cff_data)
        font_file_stream = StreamObject()
        font_file_stream._data = compressed
        font_file_stream[NameObject('/Length')] = NumberObject(len(compressed))
        font_file_stream[NameObject('/Subtype')] = NameObject('/Type1C')
        font_file_stream[NameObject('/Filter')] = NameObject('/FlateDecode')
        font_file_ref = writer._add_object(font_file_stream)

        # Create Font Descriptor
        font_descriptor = DictionaryObject()
        font_descriptor[NameObject('/Type')] = NameObject('/FontDescriptor')
        font_descriptor[NameObject('/FontName')] = NameObject('/' + unique_font_name)
        font_descriptor[NameObject('/Flags')] = NumberObject(
            self._get_font_flags(font_dict)
        )
        font_descriptor[NameObject('/FontBBox')] = ArrayObject([
            NumberObject(int(font_bbox[0])),
            NumberObject(int(font_bbox[1])),
            NumberObject(int(font_bbox[2])),
            NumberObject(int(font_bbox[3])),
        ])
        font_descriptor[NameObject('/ItalicAngle')] = NumberObject(0)
        font_descriptor[NameObject('/Ascent')] = NumberObject(int(font_bbox[3]))
        font_descriptor[NameObject('/Descent')] = NumberObject(int(font_bbox[1]))
        font_descriptor[NameObject('/CapHeight')] = NumberObject(int(font_bbox[3] * 0.7))
        font_descriptor[NameObject('/StemV')] = NumberObject(80)
        font_descriptor[NameObject('/FontFile3')] = font_file_ref
        font_descriptor_ref = writer._add_object(font_descriptor)

        # Create ToUnicode CMap
        tounicode_map = self._build_tounicode_map(font_dict, glyphs_used)
        tounicode_ref = None
        if tounicode_map:
            cmap_data = generate_tounicode_cmap(tounicode_map, font_name_str)
            cmap_stream = StreamObject()
            cmap_stream._data = cmap_data
            cmap_stream[NameObject('/Length')] = NumberObject(len(cmap_data))
            tounicode_ref = writer._add_object(cmap_stream)

        # Create the Font dictionary
        font_obj = DictionaryObject()
        font_obj[NameObject('/Type')] = NameObject('/Font')
        font_obj[NameObject('/Subtype')] = NameObject('/Type1')
        font_obj[NameObject('/BaseFont')] = NameObject('/' + unique_font_name)
        font_obj[NameObject('/FirstChar')] = NumberObject(first_char)
        font_obj[NameObject('/LastChar')] = NumberObject(last_char)
        font_obj[NameObject('/Widths')] = widths
        font_obj[NameObject('/FontDescriptor')] = font_descriptor_ref

        encoding_obj = self._build_pdf_encoding(font_dict, first_char, last_char)
        if encoding_obj:
            font_obj[NameObject('/Encoding')] = encoding_obj

        if tounicode_ref:
            font_obj[NameObject('/ToUnicode')] = tounicode_ref

        font_ref = writer._add_object(font_obj)

        base_name = font_name_str.replace('-', '').replace(' ', '')
        if instance_num > 0:
            pdf_resource_name = f'/{base_name}_{instance_num}'
        else:
            pdf_resource_name = f'/{base_name}'

        return (pdf_resource_name, font_ref)

    def _embed_type42_font(self, writer, font_name, usage, instance_num=0):
        """
        Embed a Type 42 (TrueType) font as a simple TrueType font in PDF.

        Type 42 fonts have TrueType outlines in an sfnts array and use a
        CharStrings dict mapping glyph names to GIDs. They are embedded as:
        - /Subtype /TrueType with /FontFile2 (raw TrueType binary)
        - Standard 1-byte char code encoding (same as Type 1)

        Args:
            writer: PdfWriter instance
            font_name: Font name (bytes)
            usage: FontUsage object with font_dict and glyphs_used
            instance_num: Instance number for unique resource naming

        Returns:
            Tuple (pdf_resource_name, font_ref) or None if embedding failed
        """
        font_dict = usage.font_dict
        glyphs_used = usage.glyphs_used

        font_name_str = (font_name.decode('latin-1') if isinstance(font_name, bytes)
                         else str(font_name))

        if instance_num > 0:
            unique_font_name = f"{font_name_str}_{instance_num}"
        else:
            unique_font_name = font_name_str

        # Get raw TrueType binary from sfnts array
        font_file_data = self._get_type42_sfnts_data(font_dict)
        if font_file_data is None:
            return None

        # Parse TrueType tables for metrics
        tables = self.cid_font_embedder._parse_table_directory(font_file_data)

        # Rewrite cmap to map char codes -> GIDs per PostScript encoding.
        # Symbolic TrueType fonts in PDF use cmap directly (ignoring /Encoding).
        font_file_data = self._rewrite_type42_cmap(
            font_file_data, tables, font_dict, glyphs_used)
        # Re-parse tables after reassembly (offsets changed)
        tables = self.cid_font_embedder._parse_table_directory(font_file_data)

        units_per_em = self.cid_font_embedder._get_units_per_em(
            font_file_data, tables)
        scale = 1000.0 / units_per_em if units_per_em > 0 else 1.0

        # Get font metrics
        font_bbox = self._get_type42_bbox(font_file_data, tables, scale)
        first_char, last_char = self._get_char_range(glyphs_used)
        widths = self._get_type42_widths(
            font_dict, font_file_data, tables, units_per_em,
            glyphs_used, first_char, last_char)

        # Get ascent/descent/capHeight from TrueType tables
        ascent = int(font_bbox[3])
        descent = int(font_bbox[1])
        cap_height = int(font_bbox[3] * 0.7)

        hhea_info = tables.get(b'hhea')
        if hhea_info:
            ho = hhea_info[0]
            if ho + 8 <= len(font_file_data):
                ascent = int(round(
                    struct.unpack('>h', font_file_data[ho + 4:ho + 6])[0] * scale))
                descent = int(round(
                    struct.unpack('>h', font_file_data[ho + 6:ho + 8])[0] * scale))

        os2_info = tables.get(b'OS/2')
        if os2_info:
            ho = os2_info[0]
            tbl_len = os2_info[1]
            if tbl_len >= 90 and ho + 90 <= len(font_file_data):
                ch = struct.unpack('>h', font_file_data[ho + 88:ho + 90])[0]
                if ch > 0:
                    cap_height = int(round(ch * scale))

        # Create FontFile2 stream (FlateDecode compressed TrueType binary)
        compressed = zlib.compress(font_file_data)
        font_file_stream = StreamObject()
        font_file_stream._data = compressed
        font_file_stream[NameObject('/Length')] = NumberObject(len(compressed))
        font_file_stream[NameObject('/Length1')] = NumberObject(len(font_file_data))
        font_file_stream[NameObject('/Filter')] = NameObject('/FlateDecode')
        font_file_ref = writer._add_object(font_file_stream)

        # Create Font Descriptor
        font_descriptor = DictionaryObject()
        font_descriptor[NameObject('/Type')] = NameObject('/FontDescriptor')
        font_descriptor[NameObject('/FontName')] = NameObject('/' + unique_font_name)
        font_descriptor[NameObject('/Flags')] = NumberObject(
            self._get_font_flags(font_dict)
        )
        font_descriptor[NameObject('/FontBBox')] = ArrayObject([
            NumberObject(int(font_bbox[0])),
            NumberObject(int(font_bbox[1])),
            NumberObject(int(font_bbox[2])),
            NumberObject(int(font_bbox[3])),
        ])
        font_descriptor[NameObject('/ItalicAngle')] = NumberObject(0)
        font_descriptor[NameObject('/Ascent')] = NumberObject(ascent)
        font_descriptor[NameObject('/Descent')] = NumberObject(descent)
        font_descriptor[NameObject('/CapHeight')] = NumberObject(cap_height)
        font_descriptor[NameObject('/StemV')] = NumberObject(80)
        font_descriptor[NameObject('/FontFile2')] = font_file_ref
        font_descriptor_ref = writer._add_object(font_descriptor)

        # Create ToUnicode CMap for searchability
        tounicode_map = self._build_tounicode_map(font_dict, glyphs_used)
        tounicode_ref = None
        if tounicode_map:
            cmap_data = generate_tounicode_cmap(tounicode_map, font_name_str)
            cmap_stream = StreamObject()
            cmap_stream._data = cmap_data
            cmap_stream[NameObject('/Length')] = NumberObject(len(cmap_data))
            tounicode_ref = writer._add_object(cmap_stream)

        # Create the Font dictionary
        font_obj = DictionaryObject()
        font_obj[NameObject('/Type')] = NameObject('/Font')
        font_obj[NameObject('/Subtype')] = NameObject('/TrueType')
        font_obj[NameObject('/BaseFont')] = NameObject('/' + unique_font_name)
        font_obj[NameObject('/FirstChar')] = NumberObject(first_char)
        font_obj[NameObject('/LastChar')] = NumberObject(last_char)
        font_obj[NameObject('/Widths')] = widths
        font_obj[NameObject('/FontDescriptor')] = font_descriptor_ref

        # Add encoding
        encoding_obj = self._build_pdf_encoding(font_dict, first_char, last_char)
        if encoding_obj:
            font_obj[NameObject('/Encoding')] = encoding_obj

        if tounicode_ref:
            font_obj[NameObject('/ToUnicode')] = tounicode_ref

        font_ref = writer._add_object(font_obj)

        base_name = font_name_str.replace('-', '').replace(' ', '')
        if instance_num > 0:
            pdf_resource_name = f'/{base_name}_{instance_num}'
        else:
            pdf_resource_name = f'/{base_name}'

        return (pdf_resource_name, font_ref)

    def _get_type42_sfnts_data(self, font_dict):
        """
        Extract raw TrueType binary from a Type 42 font dict's sfnts array.

        Unlike CID fonts which navigate through FDepVector, Type 42 fonts
        have the sfnts array directly in the font dictionary.

        Args:
            font_dict: PostScript Type 42 font dictionary

        Returns:
            bytes: TrueType font binary, or None if unavailable
        """
        sfnts = font_dict.val.get(b'sfnts')
        if not sfnts or sfnts.TYPE not in ps.ARRAY_TYPES:
            return None

        font_data = bytearray()
        for s in sfnts.val:
            if s.TYPE == ps.T_STRING:
                b = s.byte_string()
                if isinstance(b, str):
                    b = b.encode('latin-1')
                font_data.extend(b)

        return bytes(font_data) if len(font_data) >= 12 else None

    def _rewrite_type42_cmap(self, font_data, tables, font_dict, glyphs_used):
        """
        Rewrite cmap table in TrueType font data to match PostScript encoding.

        For Symbolic TrueType fonts in PDF, viewers ignore the /Encoding dict
        and look up character codes directly in the font's cmap table. We must
        rewrite the cmap so char codes map to the correct GIDs as specified by
        the PostScript Encoding + CharStrings.

        Builds a cmap with:
        - Platform (1,0) format 0: direct char_code -> GID (for Mac/generic)
        - Platform (3,0) format 4: 0xF000+char_code -> GID (for Windows Symbol)

        Args:
            font_data: TrueType font binary
            tables: Parsed table directory
            font_dict: PostScript Type 42 font dictionary
            glyphs_used: Set of character codes used

        Returns:
            bytes: Modified TrueType font binary with rewritten cmap
        """
        encoding = font_dict.val.get(b'Encoding')
        char_strings = font_dict.val.get(b'CharStrings')
        if not char_strings or char_strings.TYPE != ps.T_DICT:
            return font_data

        # Build char_code -> GID mapping from Encoding + CharStrings
        code_to_gid = {}
        for char_code in range(256):
            glyph_name = self._get_glyph_name(encoding, char_code)
            if not glyph_name:
                continue
            cs_entry = char_strings.val.get(glyph_name)
            if cs_entry is None:
                continue
            gid = cs_entry.val if hasattr(cs_entry, 'val') else int(cs_entry)
            code_to_gid[char_code] = gid

        if not code_to_gid:
            return font_data

        # Build format 0 subtable for platform (1, 0) - Macintosh Roman
        # 256-byte array: GID for each char code (GID clamped to 0-255)
        fmt0_glyph_array = bytearray(256)
        for cc, gid in code_to_gid.items():
            if 0 <= cc < 256 and gid <= 255:
                fmt0_glyph_array[cc] = gid

        fmt0_data = struct.pack('>HHH', 0, 262, 0) + bytes(fmt0_glyph_array)

        # Build format 4 subtable for platform (3, 0) - Windows Symbol
        # Map 0xF000+char_code -> GID for each used char code
        segments = []
        for cc in sorted(code_to_gid.keys()):
            if cc not in glyphs_used:
                continue
            sym_code = 0xF000 + cc
            gid = code_to_gid[cc]
            segments.append((sym_code, sym_code, gid - sym_code, 0))

        # Add terminating segment
        segments.append((0xFFFF, 0xFFFF, 1, 0))

        seg_count = len(segments)
        search_range = 1
        entry_selector = 0
        while search_range * 2 <= seg_count:
            search_range *= 2
            entry_selector += 1
        search_range *= 2
        range_shift = seg_count * 2 - search_range

        fmt4 = bytearray()
        fmt4.extend(struct.pack('>H', 4))        # format
        length_pos = len(fmt4)
        fmt4.extend(struct.pack('>H', 0))         # length (fill later)
        fmt4.extend(struct.pack('>H', 0))         # language
        fmt4.extend(struct.pack('>H', seg_count * 2))
        fmt4.extend(struct.pack('>H', search_range))
        fmt4.extend(struct.pack('>H', entry_selector))
        fmt4.extend(struct.pack('>H', range_shift))

        # endCode array
        for _, end, _, _ in segments:
            fmt4.extend(struct.pack('>H', end))
        fmt4.extend(struct.pack('>H', 0))  # reservedPad
        # startCode array
        for start, _, _, _ in segments:
            fmt4.extend(struct.pack('>H', start))
        # idDelta array (mod 65536 arithmetic, packed as unsigned)
        for _, _, delta, _ in segments:
            fmt4.extend(struct.pack('>H', delta & 0xFFFF))
        # idRangeOffset array
        for _, _, _, ro in segments:
            fmt4.extend(struct.pack('>H', ro))

        fmt4_len = len(fmt4)
        struct.pack_into('>H', fmt4, length_pos, fmt4_len)
        fmt4_data = bytes(fmt4)

        # Assemble cmap table
        # Header: version(2) + numTables(2) + records(2 * 8)
        num_subtables = 2
        cmap_header = struct.pack('>HH', 0, num_subtables)

        # Subtable offsets: header(4) + records(num_subtables * 8)
        records_size = 4 + num_subtables * 8
        fmt0_offset = records_size
        fmt4_offset = fmt0_offset + len(fmt0_data)

        # Platform (1, 0) -> format 0
        cmap_header += struct.pack('>HHI', 1, 0, fmt0_offset)
        # Platform (3, 0) -> format 4
        cmap_header += struct.pack('>HHI', 3, 0, fmt4_offset)

        new_cmap = cmap_header + fmt0_data + fmt4_data

        # Replace cmap table in font data and reassemble
        keep_tables = {}
        for tag, (tbl_offset, tbl_length) in tables.items():
            keep_tables[tag] = font_data[tbl_offset:tbl_offset + tbl_length]
        keep_tables[b'cmap'] = new_cmap

        return self.cid_font_embedder._assemble_truetype(keep_tables)

    def _get_type42_bbox(self, font_data, tables, scale):
        """
        Get font bounding box from TrueType head table.

        Args:
            font_data: TrueType font binary
            tables: Parsed table directory
            scale: Scale factor (1000 / unitsPerEm)

        Returns:
            list: [xMin, yMin, xMax, yMax] in 1000-unit space
        """
        head_info = tables.get(b'head')
        if head_info:
            ho = head_info[0]
            if ho + 54 <= len(font_data):
                x_min = struct.unpack('>h', font_data[ho + 36:ho + 38])[0]
                y_min = struct.unpack('>h', font_data[ho + 38:ho + 40])[0]
                x_max = struct.unpack('>h', font_data[ho + 40:ho + 42])[0]
                y_max = struct.unpack('>h', font_data[ho + 42:ho + 44])[0]
                return [
                    int(round(x_min * scale)),
                    int(round(y_min * scale)),
                    int(round(x_max * scale)),
                    int(round(y_max * scale)),
                ]
        return [0, -200, 1000, 800]

    def _get_type42_widths(self, font_dict, font_data, tables, units_per_em,
                           glyphs_used, first_char, last_char):
        """
        Get character widths for a Type 42 font via Encoding -> CharStrings -> hmtx.

        For each char_code in the range: looks up glyph name from Encoding,
        gets GID from CharStrings, reads advance width from hmtx table.

        Args:
            font_dict: PostScript Type 42 font dictionary
            font_data: TrueType font binary
            tables: Parsed table directory
            units_per_em: Font units per em
            glyphs_used: Set of character codes used
            first_char: First character code
            last_char: Last character code

        Returns:
            ArrayObject of widths for the character range
        """
        glyph_widths = self._get_type42_glyph_widths(font_dict, glyphs_used,
                                                      font_data, tables,
                                                      units_per_em)
        default_width = 600

        widths = []
        for char_code in range(first_char, last_char + 1):
            width = glyph_widths.get(char_code, default_width)
            widths.append(NumberObject(width))

        return ArrayObject(widths)

    def _get_type42_glyph_widths(self, font_dict, glyphs_used,
                                  font_data=None, tables=None,
                                  units_per_em=None):
        """
        Get glyph widths for Type 42 font via Encoding -> CharStrings -> hmtx.

        Maps char_code -> glyph_name (Encoding) -> GID (CharStrings) -> width (hmtx).

        Args:
            font_dict: PostScript Type 42 font dictionary
            glyphs_used: Set of character codes used
            font_data: Optional pre-extracted TrueType binary
            tables: Optional pre-parsed table directory
            units_per_em: Optional pre-extracted unitsPerEm

        Returns:
            dict: {char_code: width_in_1000_units}
        """
        if font_data is None:
            font_data = self._get_type42_sfnts_data(font_dict)
        if font_data is None:
            return {}

        if tables is None:
            tables = self.cid_font_embedder._parse_table_directory(font_data)
        if units_per_em is None:
            units_per_em = self.cid_font_embedder._get_units_per_em(
                font_data, tables)

        scale = 1000.0 / units_per_em if units_per_em > 0 else 1.0

        # Get hmtx table info
        hhea_info = tables.get(b'hhea')
        hmtx_info = tables.get(b'hmtx')
        if not hhea_info or not hmtx_info:
            return {}

        hhea_offset = hhea_info[0]
        hmtx_offset = hmtx_info[0]

        if hhea_offset + 36 > len(font_data):
            return {}
        num_hmetrics = int.from_bytes(
            font_data[hhea_offset + 34:hhea_offset + 36], 'big')

        # Get Encoding and CharStrings from font dict
        encoding = font_dict.val.get(b'Encoding')
        char_strings = font_dict.val.get(b'CharStrings')
        if not char_strings or char_strings.TYPE != ps.T_DICT:
            return {}

        widths = {}
        for char_code in glyphs_used:
            # Look up glyph name from Encoding
            glyph_name = self._get_glyph_name(encoding, char_code)
            if not glyph_name:
                continue

            # Look up GID from CharStrings
            cs_entry = char_strings.val.get(glyph_name)
            if cs_entry is None:
                continue
            gid = cs_entry.val if hasattr(cs_entry, 'val') else int(cs_entry)

            # Read advance width from hmtx
            if gid < num_hmetrics:
                offset = hmtx_offset + gid * 4
                if offset + 2 <= len(font_data):
                    w = int.from_bytes(font_data[offset:offset + 2], 'big')
                    widths[char_code] = int(round(w * scale))
            elif num_hmetrics > 0:
                # Use last full hmetric width
                offset = hmtx_offset + (num_hmetrics - 1) * 4
                if offset + 2 <= len(font_data):
                    w = int.from_bytes(font_data[offset:offset + 2], 'big')
                    widths[char_code] = int(round(w * scale))

        return widths

    def _build_pdf_w_array(self, w_array_data):
        """
        Convert W array data to pypdf ArrayObject.

        Args:
            w_array_data: List of alternating start_cid (int) and width lists

        Returns:
            ArrayObject or None if empty
        """
        if not w_array_data:
            return None

        result = ArrayObject()
        i = 0
        while i < len(w_array_data):
            start_cid = w_array_data[i]
            widths = w_array_data[i + 1]
            result.append(NumberObject(start_cid))
            width_array = ArrayObject([NumberObject(w) for w in widths])
            result.append(width_array)
            i += 2

        return result

    def _make_pdf_string(self, text):
        """
        Create a PDF string object (ByteStringObject) from a Python string.

        Args:
            text: String to encode

        Returns:
            pypdf string object
        """
        from pypdf.generic import ByteStringObject
        return ByteStringObject(text.encode('latin-1'))

    def _generate_text_content(self, text_objs, embedded_fonts, font_tracker, scale_x, scale_y, page_height_pdf):
        """
        Generate PDF content stream operators for text objects.

        For invisible text (ActualTextStart / Type 3 fonts), consecutive
        same-baseline fragments are concatenated into a single Tj with spaces
        detected from position gaps.  This prevents PDF viewers from treating
        each fragment as a separate line when copying text.

        For visible text (TextObj / embedded Type 1 fonts), consecutive
        same-baseline, same-font, same-color fragments are merged into a
        single BT block using TJ arrays with kern adjustments.  This prevents
        copy-paste fragmentation while maintaining precise visual positioning.

        Args:
            text_objs: List of (TextObj|ActualTextStart, clip_info) tuples
            embedded_fonts: Dict mapping font_key (tuple) -> (pdf_resource_name, font_ref)
            font_tracker: FontTracker for looking up font keys
            scale_x, scale_y: Device to PDF scale factors
            page_height_pdf: Page height in PDF points

        Returns:
            bytes: PDF content stream data
        """
        if not text_objs:
            return b''

        lines = []
        current_font = None
        active_clip_id = None

        # Pre-compute glyph widths for TJ kern calculations (Type 1 and CFF).
        # Maps font_key -> {char_code: width_in_1000ths_of_em}
        font_widths_cache = {}
        for font_key_iter, usage in font_tracker.get_fonts_in_order():
            if not FontTracker.is_cid_font(font_key_iter) and font_key_iter in embedded_fonts:
                if self._is_cff_font(usage.font_dict):
                    widths = self.cff_embedder.get_glyph_widths(
                        usage.font_dict, usage.glyphs_used)
                elif self._is_type42_font(usage.font_dict):
                    widths = self._get_type42_glyph_widths(
                        usage.font_dict, usage.glyphs_used)
                else:
                    widths = self.font_embedder.get_glyph_widths(
                        usage.font_dict, usage.glyphs_used)
                if widths:
                    font_widths_cache[font_key_iter] = widths

        # -- Invisible text batch (ActualTextStart) --
        # Each item: (x_pdf, y_pdf, tm_b, tm_c, tm_d, text_bytes,
        #             adv_x_pdf, advance_width_pdf)
        invis_batch = []

        def flush_invis_batch():
            """Emit accumulated invisible text as a single Tj.

            Concatenates same-baseline fragments with spaces inserted where
            the gap between advance endpoint and next start exceeds a
            threshold (detected from PS advance_width vs position gap).
            """
            nonlocal current_font
            if not invis_batch:
                return

            first = invis_batch[0]
            # x_pdf=0, y_pdf=1, tm_b=2, tm_c=3, tm_d=4, text_bytes=5,
            # adv_x_pdf=6, advance_width_pdf=7
            first_x = first[0]
            tm_b, tm_c, tm_d = first[2], first[3], first[4]
            y_pdf = first[1]

            # Space detection threshold: fraction of font height.
            # Inter-word gaps are typically 25-33% of em, micro-kerning < 2%.
            font_height = abs(tm_d) if abs(tm_d) > 0.01 else 10.0
            space_threshold = font_height * 0.1

            # Build concatenated text with accurate space detection
            combined_text = bytearray()
            for i, (x_pdf_i, y_pdf_i, tm_b_i, tm_c_i, tm_d_i,
                     text_bytes_i, adv_x_i, adv_w_i) in enumerate(invis_batch):
                if i > 0:
                    # Gap = distance from previous fragment's advance endpoint
                    #        to this fragment's advance start position.
                    # advance_width is the PS character advance (font metrics),
                    # so any excess gap beyond that is inter-word spacing.
                    prev = invis_batch[i - 1]
                    prev_adv_end = prev[6] + prev[7]  # adv_x + advance_width
                    gap = adv_x_i - prev_adv_end
                    if gap > space_threshold:
                        combined_text.extend(b' ')
                combined_text.extend(text_bytes_i)

            if not combined_text:
                invis_batch.clear()
                return

            # Compute tm_a so Courier glyphs (600/1000 em-width each) span
            # the same total width as the visible text.  Use advance-based
            # coordinates (adv_x + advance_width) which are always accurate,
            # unlike visual_start_x which may not span multiple entries.
            first_adv_x = invis_batch[0][6]
            last = invis_batch[-1]
            total_width = (last[6] + last[7]) - first_adv_x
            total_chars = len(combined_text)
            if total_chars > 0 and total_width > 0:
                combined_tm_a = total_width / (total_chars * 0.6)
            else:
                combined_tm_a = font_height

            lines.append(b'BT')
            lines.append(b'3 Tr')
            lines.append(b'/PFCour 1 Tf')
            lines.append(
                f'{combined_tm_a:.4f} {tm_b:.4f} {tm_c:.4f} {tm_d:.4f} '
                f'{first_x:.4f} {y_pdf:.4f} Tm'.encode())
            text_hex = bytes(combined_text).hex().upper()
            lines.append(f'<{text_hex}> Tj'.encode())
            lines.append(b'0 Tr')
            lines.append(b'ET')
            current_font = '/PFCour'

            invis_batch.clear()

        # -- Visible text batch (TextObj) --
        # Each item: (text_bytes, x_pdf, y_pdf, tm_a, tm_b, tm_c, tm_d)
        text_batch = []
        text_batch_clip_id = None
        text_batch_font_key = None
        text_batch_color = None
        text_batch_resource = None

        def flush_text_batch():
            """Emit accumulated visible text entries.

            Single entries use simple Tj.  Multiple entries use TJ arrays
            with kern adjustments computed from the difference between actual
            fragment positions and expected glyph-advance positions.
            """
            nonlocal current_font
            if not text_batch:
                return

            first = text_batch[0]
            # text_bytes=0, x_pdf=1, y_pdf=2, tm_a=3, tm_b=4, tm_c=5, tm_d=6

            # Set color
            color = text_batch_color
            if len(color) >= 3:
                lines.append(f'{color[0]:.4f} {color[1]:.4f} {color[2]:.4f} rg'.encode())
            elif len(color) == 1:
                lines.append(f'{color[0]:.4f} g'.encode())
            else:
                lines.append(b'0 g')

            lines.append(b'BT')

            if text_batch_resource != current_font:
                lines.append(f'{text_batch_resource} 1 Tf'.encode())
                current_font = text_batch_resource

            # Text matrix from first entry
            tm_a = first[3]
            lines.append(
                f'{tm_a:.4f} {first[4]:.4f} {first[5]:.4f} {first[6]:.4f} '
                f'{first[1]:.4f} {first[2]:.4f} Tm'.encode())

            if len(text_batch) == 1:
                # Single entry — simple Tj
                text_hex = first[0].hex().upper()
                lines.append(f'<{text_hex}> Tj'.encode())
            else:
                # Multiple entries — TJ with kern adjustments.
                # Kern values compensate for the gap between where the PDF
                # text cursor IS (after rendering glyph widths from /Widths)
                # and where the next fragment SHOULD start.
                glyph_widths = font_widths_cache.get(text_batch_font_key, {})
                tj_parts = []
                # Track cursor in 1000ths of text space (includes both
                # glyph advances and kern displacements for accuracy)
                cursor_thousandths = 0.0

                for i, (text_bytes_i, x_i, y_i, _, _, _, _) in enumerate(text_batch):
                    if i > 0:
                        # Target position in 1000ths of text space
                        target = (x_i - first[1]) * 1000.0 / tm_a
                        # TJ kern: positive = move left, negative = move right
                        kern = -(target - cursor_thousandths)
                        if abs(kern) > 0.5:
                            kern_int = round(kern)
                            tj_parts.append(str(kern_int))
                            # Track the rounded kern in cursor position
                            cursor_thousandths -= kern_int

                    text_hex = text_bytes_i.hex().upper()
                    tj_parts.append(f'<{text_hex}>')

                    # Advance cursor by sum of glyph widths for this fragment
                    for byte_val in text_bytes_i:
                        cursor_thousandths += glyph_widths.get(byte_val, 600)

                tj_str = ' '.join(tj_parts)
                lines.append(f'[{tj_str}] TJ'.encode())

            lines.append(b'ET')
            text_batch.clear()

        for entry, clip_info in text_objs:
            clip_id = id(clip_info[0]) if clip_info else None

            # ActualTextStart: invisible text for Type 3 font searchability
            if isinstance(entry, ps.ActualTextStart):
                # Flush visible text batch before processing invisible text
                flush_text_batch()

                params = self._compute_invisible_text_params(
                    entry, scale_x, scale_y)
                if params is None:
                    continue

                x_pdf, y_pdf, tm_b, tm_c, tm_d, text_bytes, adv_x_pdf, \
                    adv_w_pdf = params

                # Check if this can join the current batch.
                # For invisible text, only check y-proximity (same visual line).
                # Different font sizes on the same line should merge since
                # the text is invisible — only copy-paste matters.
                can_batch = (invis_batch
                             and clip_id == active_clip_id
                             and abs(y_pdf - invis_batch[0][1]) < 1.0)

                if not can_batch:
                    # Flush previous batch and handle clip state
                    flush_invis_batch()

                    if clip_id != active_clip_id:
                        if active_clip_id is not None:
                            lines.append(b'Q')
                            current_font = None
                        if clip_info is not None:
                            clip_path, clip_winding = clip_info
                            lines.append(b'q')
                            self._emit_clip_path(lines, clip_path,
                                                 clip_winding, scale_x,
                                                 scale_y)
                        active_clip_id = clip_id

                invis_batch.append(params)
                continue

            # --- TextObj: visible embedded font text ---
            # Flush any pending invisible batch first
            flush_invis_batch()

            text_obj = entry

            font_key = font_tracker.get_font_key_for_dict(text_obj.font_dict)
            font_info = embedded_fonts.get(font_key) if font_key else None
            if not font_info:
                continue

            pdf_resource_name, font_ref = font_info

            # Compute text matrix parameters
            x_pdf = text_obj.start_x * scale_x
            y_pdf = text_obj.start_y * scale_y

            ctm = text_obj.ctm
            ca, cb, cc, cd = ctm[0], ctm[1], ctm[2], ctm[3]

            fm = text_obj.font_matrix

            # The PDF content stream is in Y-down space (Cairo's initial cm
            # provides the Y-flip from PDF's native Y-up).  The CTM already
            # encodes the correct Y direction for this space — d < 0 means
            # text is right-side up, d > 0 means upside down.  No adjustment
            # is needed.  (The Cairo renderer DOES adjust because it applies
            # the CTM as a coordinate transform where the semantics differ.)

            # Compose font matrix with CTM for the PDF text matrix (Tm).
            # FontMatrix provides per-axis scaling (e.g., [40 0 0 15 0 0] for
            # non-uniform makefont), CTM provides page-level transforms.
            if fm:
                # Tm = FontMatrix × CTM × device_scale
                tm_a = (fm[0] * ca + fm[1] * cc) * scale_x
                tm_b = (fm[0] * cb + fm[1] * cd) * scale_y
                tm_c = (fm[2] * ca + fm[3] * cc) * scale_x
                tm_d = (fm[2] * cb + fm[3] * cd) * scale_y
            else:
                sx = math.sqrt(ca * ca + cb * cb)
                sy = math.sqrt(cc * cc + cd * cd)
                ctm_scale = math.sqrt(sx * sy)
                point_size = text_obj.font_size / ctm_scale if ctm_scale > 0 else text_obj.font_size
                tm_a = point_size * ca * scale_x
                tm_b = point_size * cb * scale_y
                tm_c = point_size * cc * scale_x
                tm_d = point_size * cd * scale_y

            # Check if this can batch with previous TextObj entries.
            # Requirements: same clip, font, color, font size, and baseline.
            # Also requires glyph widths for TJ kern computation.
            can_batch = False
            if (text_batch
                    and clip_id == text_batch_clip_id
                    and font_key == text_batch_font_key
                    and text_obj.color == text_batch_color
                    and font_key in font_widths_cache
                    and abs(tm_a) > 0.1):
                prev = text_batch[-1]
                if (abs(prev[3] - tm_a) < 0.01
                        and self._same_baseline(
                            prev[4], prev[5], prev[6], prev[1], prev[2],
                            tm_b, tm_c, tm_d, x_pdf, y_pdf)):
                    can_batch = True

            if not can_batch:
                flush_text_batch()

                # Handle clip state transitions
                if clip_id != active_clip_id:
                    if active_clip_id is not None:
                        lines.append(b'Q')
                        current_font = None
                    if clip_info is not None:
                        clip_path, clip_winding = clip_info
                        lines.append(b'q')
                        self._emit_clip_path(lines, clip_path, clip_winding,
                                             scale_x, scale_y)
                    active_clip_id = clip_id

                text_batch_clip_id = clip_id
                text_batch_font_key = font_key
                text_batch_color = text_obj.color
                text_batch_resource = pdf_resource_name

            text_batch.append(
                (text_obj.text, x_pdf, y_pdf, tm_a, tm_b, tm_c, tm_d))

        # Flush remaining batches
        flush_invis_batch()
        flush_text_batch()

        # Close final clip group if any
        if active_clip_id is not None:
            lines.append(b'Q')

        return b'\n'.join(lines)

    def _compute_invisible_text_params(self, actual_text, scale_x, scale_y):
        """
        Compute positioning parameters for invisible text (Type 3 searchability).

        Returns:
            Tuple (x_pdf, y_pdf, tm_b, tm_c, tm_d, text_bytes, adv_x_pdf,
                   advance_width_pdf) or None if text cannot be encoded.

            x_pdf: Visual start position (for rendering alignment)
            adv_x_pdf: PS advance-based start position (for space detection)
            advance_width_pdf: PS character advance width in PDF coords
        """
        try:
            # NFKD decomposition converts ligatures to component chars
            # (e.g., ffi→ffi, fi→fi, ff→ff) before cp1252 encoding
            normalized = unicodedata.normalize('NFKD', actual_text.unicode_text)
            text_bytes = normalized.encode('cp1252', errors='replace')
        except Exception:
            return None

        if not text_bytes:
            return None

        # Advance-based position (original PS currentpoint at show start)
        adv_x_pdf = actual_text.start_x * scale_x
        x_pdf = adv_x_pdf
        y_pdf = actual_text.start_y * scale_y

        # PS character advance width (from font metrics, set during show)
        advance_width_pdf = actual_text.advance_width * scale_x

        ctm = actual_text.ctm
        ca, cb, cc, cd = ctm[0], ctm[1], ctm[2], ctm[3]

        font_size = actual_text.font_size
        bbox = actual_text.font_bbox
        if bbox:
            bbox_height = abs(bbox[3] - bbox[1])
            if bbox_height > 0:
                font_size = font_size * bbox_height / 1000.0
            else:
                font_size = font_size / 1000.0
        else:
            font_size = font_size / 1000.0

        sx = math.sqrt(ca * ca + cb * cb)
        sy = math.sqrt(cc * cc + cd * cd)
        ctm_scale = math.sqrt(sx * sy)
        point_size = font_size / ctm_scale if ctm_scale > 0 else font_size

        tm_b = point_size * cb * scale_y
        tm_c = point_size * cc * scale_x
        tm_d = point_size * cd * scale_y

        if actual_text.visual_start_x is not None:
            x_pdf = actual_text.visual_start_x * scale_x

        return (x_pdf, y_pdf, tm_b, tm_c, tm_d, text_bytes, adv_x_pdf,
                advance_width_pdf)

    def _same_baseline(self, tm_b1, tm_c1, tm_d1, x1, y1,
                             tm_b2, tm_c2, tm_d2, x2, y2):
        """
        Check if two text entries share the same baseline (rotation-aware).

        Uses the perpendicular direction (tm_c, tm_d) of the text matrix to
        compute the cross-baseline distance. For non-rotated text this
        simplifies to |dy| < 0.5.

        Returns True if the entries are on the same baseline.
        """
        if abs(tm_b1 - tm_b2) > 0.01 or abs(tm_c1 - tm_c2) > 0.01 or abs(tm_d1 - tm_d2) > 0.01:
            return False
        dx, dy = x2 - x1, y2 - y1
        perp_len = math.sqrt(tm_c1 ** 2 + tm_d1 ** 2)
        if perp_len > 0:
            perp_dist = abs(dx * tm_c1 + dy * tm_d1) / perp_len
        else:
            perp_dist = abs(dy)
        return perp_dist < 0.5

    def _emit_clip_path(self, lines, clip_path, clip_winding, scale_x, scale_y):
        """
        Emit PDF clipping path operators.

        Converts device-coordinate clip path to PDF coordinates and emits
        path construction operators followed by the clip operator.

        Args:
            lines: List to append PDF operator bytes to
            clip_path: Path object (list of SubPaths) in device coordinates
            clip_winding: Winding rule (WINDING_NON_ZERO or WINDING_EVEN_ODD)
            scale_x, scale_y: Device to PDF scale factors
        """
        for subpath in clip_path:
            for element in subpath:
                if isinstance(element, ps.MoveTo):
                    x = element.p.x * scale_x
                    y = element.p.y * scale_y
                    lines.append(f'{x:.4f} {y:.4f} m'.encode())
                elif isinstance(element, ps.LineTo):
                    x = element.p.x * scale_x
                    y = element.p.y * scale_y
                    lines.append(f'{x:.4f} {y:.4f} l'.encode())
                elif isinstance(element, ps.CurveTo):
                    x1 = element.p1.x * scale_x
                    y1 = element.p1.y * scale_y
                    x2 = element.p2.x * scale_x
                    y2 = element.p2.y * scale_y
                    x3 = element.p3.x * scale_x
                    y3 = element.p3.y * scale_y
                    lines.append(f'{x1:.4f} {y1:.4f} {x2:.4f} {y2:.4f} {x3:.4f} {y3:.4f} c'.encode())
                elif isinstance(element, ps.ClosePath):
                    lines.append(b'h')

        # Apply clipping
        if clip_winding == ps.WINDING_EVEN_ODD:
            lines.append(b'W* n')
        else:
            lines.append(b'W n')

    def _append_content_to_page(self, writer, page, new_content):
        """
        Append content stream data to a page.

        Args:
            writer: PdfWriter instance
            page: Page object to modify
            new_content: bytes to append to content stream
        """
        # Get existing content stream
        if '/Contents' in page:
            contents = page['/Contents']
            if hasattr(contents, 'get_object'):
                contents = contents.get_object()

            # Handle array of content streams
            if isinstance(contents, ArrayObject):
                # Create new stream for our content
                new_stream = StreamObject()
                new_stream._data = new_content
                new_stream[NameObject('/Length')] = NumberObject(len(new_content))
                new_stream_ref = writer._add_object(new_stream)

                # Append to array
                contents.append(new_stream_ref)
            else:
                # Single content stream - read existing and append
                existing_data = b''
                if hasattr(contents, 'get_data'):
                    existing_data = contents.get_data()
                elif hasattr(contents, '_data'):
                    existing_data = contents._data

                # Combine content
                combined = existing_data + b'\n' + new_content

                # Create new stream
                new_stream = StreamObject()
                new_stream._data = combined
                new_stream[NameObject('/Length')] = NumberObject(len(combined))

                # Replace page content
                page[NameObject('/Contents')] = writer._add_object(new_stream)
        else:
            # No existing content - create new
            new_stream = StreamObject()
            new_stream._data = new_content
            new_stream[NameObject('/Length')] = NumberObject(len(new_content))
            page[NameObject('/Contents')] = writer._add_object(new_stream)

    def _add_fonts_to_page(self, page, embedded_fonts):
        """
        Add font resources to page's resource dictionary.

        Args:
            page: Page object
            embedded_fonts: Dict mapping font_key (tuple) -> (pdf_resource_name, font_ref)
        """
        # Ensure Resources dict exists
        if '/Resources' not in page:
            page[NameObject('/Resources')] = DictionaryObject()

        resources = page['/Resources']
        if hasattr(resources, 'get_object'):
            resources = resources.get_object()

        # Ensure Font dict exists
        if '/Font' not in resources:
            resources[NameObject('/Font')] = DictionaryObject()

        fonts = resources['/Font']
        if hasattr(fonts, 'get_object'):
            fonts = fonts.get_object()

        # Add each embedded font
        for font_dict_id, (pdf_resource_name, font_ref) in embedded_fonts.items():
            fonts[NameObject(pdf_resource_name)] = font_ref

    def _add_invisible_text_fonts(self, page, writer):
        """
        Add Courier font resource (/PFCour) for invisible text rendering.

        Used by ActualTextStart entries for Type 3 font searchability.
        Courier is a Standard 14 monospaced font (600 units/char) requiring
        no embedding. Its fixed width enables exact selection box sizing.
        """
        # Ensure Resources/Font dict exists
        if '/Resources' not in page:
            page[NameObject('/Resources')] = DictionaryObject()
        resources = page['/Resources']
        if hasattr(resources, 'get_object'):
            resources = resources.get_object()
        if '/Font' not in resources:
            resources[NameObject('/Font')] = DictionaryObject()
        fonts = resources['/Font']
        if hasattr(fonts, 'get_object'):
            fonts = fonts.get_object()

        # Skip if already added
        if '/PFCour' in fonts:
            return

        # Create minimal Courier font dictionary with WinAnsiEncoding
        cour_dict = DictionaryObject({
            NameObject('/Type'): NameObject('/Font'),
            NameObject('/Subtype'): NameObject('/Type1'),
            NameObject('/BaseFont'): NameObject('/Courier'),
            NameObject('/Encoding'): NameObject('/WinAnsiEncoding'),
        })
        fonts[NameObject('/PFCour')] = writer._add_object(cour_dict)

    @staticmethod
    def _to_pfb(raw_data, length1, length2, length3):
        """
        Convert raw Type 1 font segments to PFB (Printer Font Binary) format.

        Raw Type 1 data has a PFA-style header (%!PS-AdobeFont) but binary
        eexec, which some PDF viewers (Poppler/FreeType) cannot parse.
        PFB format wraps each segment with type/length markers that FreeType
        uses to correctly identify ASCII vs binary sections.

        PFB segment format: 0x80 type(1=ASCII,2=binary) + 4-byte LE length + data
        Terminated by: 0x80 0x03

        Args:
            raw_data: Concatenated clear-text + binary eexec + footer
            length1: Clear-text segment length
            length2: Binary eexec segment length
            length3: Footer segment length

        Returns:
            bytes: PFB-formatted font data
        """
        pfb = bytearray()
        # Segment 1: ASCII (clear text)
        pfb.extend(b'\x80\x01')
        pfb.extend(struct.pack('<I', length1))
        pfb.extend(raw_data[:length1])
        # Segment 2: Binary (eexec encrypted)
        pfb.extend(b'\x80\x02')
        pfb.extend(struct.pack('<I', length2))
        pfb.extend(raw_data[length1:length1 + length2])
        # Segment 3: ASCII (footer — 512 zeros + cleartomark)
        pfb.extend(b'\x80\x01')
        pfb.extend(struct.pack('<I', length3))
        pfb.extend(raw_data[length1 + length2:])
        # EOF marker
        pfb.extend(b'\x80\x03')
        return bytes(pfb)

    # Standard PDF-compatible named encodings — fonts using these are NonSymbolic
    _STANDARD_ENCODINGS = {
        b'StandardEncoding', b'ISOLatin1Encoding',
        b'WinAnsiEncoding', b'MacRomanEncoding', b'MacExpertEncoding',
    }

    def _get_font_flags(self, font_dict):
        """
        Determine PDF font descriptor Flags based on font encoding.

        Fonts with standard named encodings get NonSymbolic (32), which tells
        PDF viewers the encoding can be mapped through standard encoding tables.

        Fonts with custom array encodings (e.g., TeX CM fonts) get Symbolic (4),
        which tells PDF viewers to use the encoding built into the font program
        without attempting standard encoding remapping.

        Args:
            font_dict: PostScript font dictionary

        Returns:
            int: PDF font descriptor Flags value (4 or 32)
        """
        encoding = font_dict.val.get(b'Encoding')
        if encoding is not None and encoding.TYPE == ps.T_NAME:
            if encoding.val in self._STANDARD_ENCODINGS:
                return 32  # NonSymbolic — standard encoding
        # Custom array encoding, unknown named encoding, or no encoding → Symbolic
        return 4  # Symbolic — encoding built into font program

    def _build_pdf_encoding(self, font_dict, first_char, last_char):
        """
        Build PDF encoding dictionary from PostScript font encoding.

        For fonts with custom encodings (like TeX fonts), we need to specify
        the encoding in the PDF so the viewer knows how to map character codes
        to glyph names in the embedded font.

        Args:
            font_dict: PostScript font dictionary
            first_char: First character code used
            last_char: Last character code used

        Returns:
            DictionaryObject with encoding, or None if standard encoding
        """
        encoding = font_dict.val.get(b'Encoding')
        if not encoding:
            return None

        # If it's a named encoding like StandardEncoding, use it directly
        if encoding.TYPE == ps.T_NAME:
            enc_name = encoding.val.decode('latin-1') if isinstance(encoding.val, bytes) else str(encoding.val)
            if enc_name in ['StandardEncoding', 'ISOLatin1Encoding', 'WinAnsiEncoding']:
                return NameObject('/' + enc_name)
            # For other named encodings, we need to build differences
            return None

        # For array encodings, build a Differences array
        if encoding.TYPE not in ps.ARRAY_TYPES:
            return None

        # Build differences array
        # Format: [code1 /name1 /name2 ... code2 /nameN ...]
        differences = []
        current_run_start = None

        for char_code in range(first_char, last_char + 1):
            if char_code < len(encoding.val):
                elem = encoding.val[char_code]
                if elem.TYPE == ps.T_NAME:
                    glyph_name = elem.val.decode('latin-1') if isinstance(elem.val, bytes) else str(elem.val)
                    if glyph_name != '.notdef':
                        if current_run_start != char_code:
                            # Start new run with character code
                            differences.append(NumberObject(char_code))
                        differences.append(NameObject('/' + glyph_name))
                        current_run_start = char_code + 1

        if not differences:
            return None

        # Build encoding dictionary
        enc_dict = DictionaryObject()
        enc_dict[NameObject('/Type')] = NameObject('/Encoding')
        enc_dict[NameObject('/Differences')] = ArrayObject(differences)

        return enc_dict

    def _build_tounicode_map(self, font_dict, glyphs_used):
        """Build ToUnicode mapping from char codes to Unicode."""
        tounicode_map = {}
        encoding = font_dict.val.get(b'Encoding')

        for char_code in glyphs_used:
            glyph_name = self._get_glyph_name(encoding, char_code)
            if glyph_name:
                unicode_val = glyph_name_to_unicode(glyph_name)
                if unicode_val:
                    tounicode_map[char_code] = unicode_val

        return tounicode_map

    def _get_glyph_name(self, encoding, char_code):
        """Get glyph name for a character code."""
        if encoding is None:
            return None

        if encoding.TYPE == ps.T_NAME:
            if 32 <= char_code <= 126:
                return chr(char_code).encode('latin-1')
            return None

        if encoding.TYPE in ps.ARRAY_TYPES:
            if 0 <= char_code < len(encoding.val):
                elem = encoding.val[char_code]
                if elem.TYPE == ps.T_NAME:
                    return elem.val

        return None

    def _get_font_bbox(self, font_dict):
        """Get font bounding box."""
        bbox = font_dict.val.get(b'FontBBox')
        if bbox and bbox.TYPE in ps.ARRAY_TYPES and len(bbox.val) >= 4:
            return [elem.val for elem in bbox.val[:4]]
        return [0, -200, 1000, 800]

    def _get_char_range(self, glyphs_used):
        """Get first and last character codes."""
        if not glyphs_used:
            return 0, 255
        return min(glyphs_used), max(glyphs_used)

    def _get_widths(self, font_dict, glyphs_used, first_char, last_char):
        """Get character widths for the used character range."""
        if not glyphs_used:
            return ArrayObject([NumberObject(600)])

        default_width = 600

        # Extract actual widths from CharStrings
        glyph_widths = self.font_embedder.get_glyph_widths(font_dict, glyphs_used)

        widths = []
        for char_code in range(first_char, last_char + 1):
            width = glyph_widths.get(char_code, default_width)
            widths.append(NumberObject(width))

        return ArrayObject(widths)


def is_pypdf_available():
    """Check if pypdf is available for font embedding."""
    return PYPDF_AVAILABLE
