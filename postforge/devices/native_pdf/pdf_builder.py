# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
PDF Document Builder

Assembles PDF files from content streams, fonts, and page metadata.
Uses pypdf for PDF object management and serialization.
"""

import hashlib
import io
import logging
import struct
import zlib

from PIL import Image

from ...core import types as ps
from ...core import icc_profile
from ...core.unicode_mapping import glyph_name_to_unicode
from ..pdf.font_tracker import FontTracker, FontUsage
from ..pdf.font_embedder import FontEmbedder, generate_tounicode_cmap
from ..pdf.cid_font_embedder import CIDFontEmbedder, generate_cid_tounicode_cmap
from ..pdf.cff_font_embedder import CFFEmbedder

try:
    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject,
        BooleanObject,
        DictionaryObject,
        FloatObject,
        NameObject,
        NumberObject,
        RectangleObject,
        StreamObject,
    )
    PYPDF_AVAILABLE = True
    logging.getLogger('pypdf').setLevel(logging.ERROR)
except ImportError:
    PYPDF_AVAILABLE = False


class PageData:
    """Stores data for a single PDF page."""

    __slots__ = ('content_stream', 'width_pts', 'height_pts',
                 'font_resources', 'standard14_fonts', 'font_keys_used',
                 'needs_invisible_font', 'rotate', 'shading_defs',
                 'image_defs')

    def __init__(self, content_stream: bytes, width_pts: float,
                 height_pts: float) -> None:
        self.content_stream = content_stream
        self.width_pts = width_pts
        self.height_pts = height_pts
        self.font_resources: dict[str, object] = {}  # name -> font_ref
        self.standard14_fonts: set[str] = set()  # Standard 14 font names used
        self.font_keys_used: set[tuple] = set()  # font keys used on this page
        self.needs_invisible_font: bool = False  # Courier for ActualText overlay
        self.rotate: int = 0  # PDF page rotation (0, 90, 180, 270)
        self.shading_defs: list[tuple[str, dict]] = []  # (name, shading_desc)
        self.image_defs: list[tuple[str, dict]] = []  # (name, image_desc)


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


class PDFBuilder:
    """Build a complete PDF document from pages and fonts."""

    def __init__(self, lossless_images: bool = False) -> None:
        if not PYPDF_AVAILABLE:
            raise ImportError(
                "pypdf is required for native_pdf device. "
                "Install with: pip install pypdf"
            )
        self.font_embedder = FontEmbedder()
        self.cid_font_embedder = CIDFontEmbedder()
        self.cff_embedder = CFFEmbedder()
        self._lossless_images = lossless_images
        # ICC profile deduplication: hash → indirect reference
        self._icc_profile_refs: dict[bytes, object] = {}
        # Image XObject deduplication: signature → indirect reference
        self._image_xobj_refs: dict[bytes, object] = {}

    def build_pdf(self, pages: list[PageData], font_tracker: FontTracker,
                  output_path: str) -> dict[tuple, tuple[str, object]]:
        """Build and write a PDF file.

        Args:
            pages: List of PageData objects (one per page).
            font_tracker: Font usage tracker with all fonts used.
            output_path: Path to write the PDF file.

        Returns:
            Dict of embedded_fonts: font_key -> (resource_name, font_ref).
        """
        writer = PdfWriter()

        # Embed all required fonts
        embedded_fonts = self._embed_all_fonts(writer, font_tracker)

        # Create pages
        for page_data in pages:
            self._add_page(writer, page_data, embedded_fonts)

        # Write output
        with open(output_path, 'wb') as f:
            writer.write(f)

        return embedded_fonts

    def _embed_all_fonts(self, writer: PdfWriter,
                         font_tracker: FontTracker) -> dict[tuple, tuple[str, object]]:
        """Embed all tracked fonts into the PDF.

        Args:
            writer: PdfWriter to add font objects to.
            font_tracker: Font usage tracker.

        Returns:
            Dict mapping font_key -> (pdf_resource_name, font_ref).
        """
        embedded_fonts: dict[tuple, tuple[str, object]] = {}
        font_name_counts: dict[bytes, int] = {}
        best_subrs = font_tracker.get_best_subrs()

        for font_key, usage in font_tracker.get_fonts_in_order():
            font_name = usage.font_name

            if font_name not in font_name_counts:
                font_name_counts[font_name] = 0
            instance_num = font_name_counts[font_name]
            font_name_counts[font_name] += 1

            result = None
            if FontTracker.is_cid_font(font_key):
                result = self._embed_cid_font(writer, font_name, usage,
                                              instance_num)
            elif self._is_cff_font(usage.font_dict):
                result = self._embed_cff_font(writer, font_name, usage,
                                              instance_num)
            elif self._is_type42_font(usage.font_dict):
                result = self._embed_type42_font(writer, font_name, usage,
                                                 instance_num)
            else:
                subrs_override = best_subrs.get(font_key[0])
                result = self._embed_type1_font(writer, font_name, usage,
                                                instance_num, subrs_override)

            if result:
                embedded_fonts[font_key] = result

        return embedded_fonts

    def _add_page(self, writer: PdfWriter, page_data: PageData,
                  embedded_fonts: dict[tuple, tuple[str, object]]) -> None:
        """Add a page to the PDF writer.

        Args:
            writer: PdfWriter to add the page to.
            page_data: Page content and dimensions.
            embedded_fonts: Dict of embedded fonts.
        """
        # Compress content stream
        compressed = zlib.compress(page_data.content_stream)
        content_stream = StreamObject()
        content_stream._data = compressed
        content_stream[NameObject('/Length')] = NumberObject(len(compressed))
        content_stream[NameObject('/Filter')] = NameObject('/FlateDecode')

        content_ref = writer._add_object(content_stream)

        # Build font resources dictionary — only fonts used on this page
        font_dict = DictionaryObject()

        # Add embedded fonts used on this page
        for font_key, (resource_name, font_ref) in embedded_fonts.items():
            if font_key in page_data.font_keys_used:
                font_dict[NameObject(resource_name)] = font_ref

        # Add Standard 14 fonts used on this page
        for std14_name in page_data.standard14_fonts:
            resource_name = '/' + std14_name
            if NameObject(resource_name) not in font_dict:
                std_font = DictionaryObject()
                std_font[NameObject('/Type')] = NameObject('/Font')
                std_font[NameObject('/Subtype')] = NameObject('/Type1')
                std_font[NameObject('/BaseFont')] = NameObject('/' + std14_name)
                font_ref = writer._add_object(std_font)
                font_dict[NameObject(resource_name)] = font_ref

        # Add Courier for invisible text overlay (ActualText / Type 3 fonts)
        if page_data.needs_invisible_font:
            cour_dict = DictionaryObject()
            cour_dict[NameObject('/Type')] = NameObject('/Font')
            cour_dict[NameObject('/Subtype')] = NameObject('/Type1')
            cour_dict[NameObject('/BaseFont')] = NameObject('/Courier')
            cour_dict[NameObject('/Encoding')] = NameObject('/WinAnsiEncoding')
            font_dict[NameObject('/PFCour')] = writer._add_object(cour_dict)

        # Build shading resources
        shading_dict = DictionaryObject()
        for sh_name, sh_desc in page_data.shading_defs:
            sh_ref = _build_pdf_shading(writer, sh_desc)
            if sh_ref is not None:
                shading_dict[NameObject(sh_name)] = sh_ref

        # Build XObject image resources
        xobject_dict = DictionaryObject()
        for img_name, img_desc in page_data.image_defs:
            img_ref = self._build_image_xobject(writer, img_desc)
            if img_ref is not None:
                xobject_dict[NameObject(img_name)] = img_ref

        # Build resources
        resources = DictionaryObject()
        if font_dict:
            resources[NameObject('/Font')] = font_dict
        if shading_dict:
            resources[NameObject('/Shading')] = shading_dict
        if xobject_dict:
            resources[NameObject('/XObject')] = xobject_dict

        # Create blank page and configure it
        page = writer.add_blank_page(
            width=page_data.width_pts, height=page_data.height_pts)
        page[NameObject('/Contents')] = content_ref
        page[NameObject('/Resources')] = resources
        if page_data.rotate:
            page[NameObject('/Rotate')] = NumberObject(page_data.rotate)

    def _embed_type1_font(self, writer: PdfWriter, font_name: bytes,
                          usage: FontUsage, instance_num: int,
                          subrs_override: object = None) -> tuple[str, object] | None:
        """Embed a Type 1 font. Reuses logic from pdf_injector."""
        font_dict = usage.font_dict
        glyphs_used = usage.glyphs_used

        font_name_str = font_name.decode('latin-1') if isinstance(font_name, bytes) else str(font_name)
        unique_font_name = f"{font_name_str}_{instance_num}" if instance_num > 0 else font_name_str

        result = self.font_embedder.get_font_file_data(
            font_dict, unique_font_name, glyphs_used, subrs_override)
        if result is None:
            return None

        font_file_data, length1, length2, length3 = result
        font_bbox = _get_font_bbox(font_dict)
        first_char, last_char = _get_char_range(glyphs_used)
        widths = _get_widths(font_dict, glyphs_used, first_char, last_char,
                             self.font_embedder)

        # Convert to PFB format
        pfb_data = _to_pfb(font_file_data, length1, length2, length3)

        # FontFile stream
        compressed = zlib.compress(pfb_data)
        font_file_stream = StreamObject()
        font_file_stream._data = compressed
        font_file_stream[NameObject('/Length')] = NumberObject(len(compressed))
        font_file_stream[NameObject('/Length1')] = NumberObject(length1)
        font_file_stream[NameObject('/Length2')] = NumberObject(length2)
        font_file_stream[NameObject('/Length3')] = NumberObject(length3)
        font_file_stream[NameObject('/Filter')] = NameObject('/FlateDecode')
        font_file_ref = writer._add_object(font_file_stream)

        # FontDescriptor
        font_descriptor = DictionaryObject()
        font_descriptor[NameObject('/Type')] = NameObject('/FontDescriptor')
        font_descriptor[NameObject('/FontName')] = NameObject('/' + unique_font_name)
        font_descriptor[NameObject('/Flags')] = NumberObject(
            _get_font_flags(font_dict))
        font_descriptor[NameObject('/FontBBox')] = ArrayObject([
            NumberObject(int(font_bbox[0])), NumberObject(int(font_bbox[1])),
            NumberObject(int(font_bbox[2])), NumberObject(int(font_bbox[3])),
        ])
        font_descriptor[NameObject('/ItalicAngle')] = NumberObject(0)
        font_descriptor[NameObject('/Ascent')] = NumberObject(int(font_bbox[3]))
        font_descriptor[NameObject('/Descent')] = NumberObject(int(font_bbox[1]))
        font_descriptor[NameObject('/CapHeight')] = NumberObject(int(font_bbox[3] * 0.7))
        font_descriptor[NameObject('/StemV')] = NumberObject(80)
        font_descriptor[NameObject('/FontFile')] = font_file_ref
        font_descriptor_ref = writer._add_object(font_descriptor)

        # ToUnicode CMap
        tounicode_map = _build_tounicode_map(font_dict, glyphs_used)
        tounicode_ref = None
        if tounicode_map:
            cmap_data = generate_tounicode_cmap(tounicode_map, font_name_str)
            cmap_stream = StreamObject()
            cmap_stream._data = cmap_data
            cmap_stream[NameObject('/Length')] = NumberObject(len(cmap_data))
            tounicode_ref = writer._add_object(cmap_stream)

        # Font dictionary
        font_obj = DictionaryObject()
        font_obj[NameObject('/Type')] = NameObject('/Font')
        font_obj[NameObject('/Subtype')] = NameObject('/Type1')
        font_obj[NameObject('/BaseFont')] = NameObject('/' + unique_font_name)
        font_obj[NameObject('/FirstChar')] = NumberObject(first_char)
        font_obj[NameObject('/LastChar')] = NumberObject(last_char)
        font_obj[NameObject('/Widths')] = widths
        font_obj[NameObject('/FontDescriptor')] = font_descriptor_ref

        encoding_obj = _build_pdf_encoding(font_dict, first_char, last_char)
        if encoding_obj:
            font_obj[NameObject('/Encoding')] = encoding_obj
        if tounicode_ref:
            font_obj[NameObject('/ToUnicode')] = tounicode_ref

        font_ref = writer._add_object(font_obj)

        base_name = font_name_str.replace('-', '').replace(' ', '')
        resource_name = f'/{base_name}_{instance_num}' if instance_num > 0 else f'/{base_name}'
        return (resource_name, font_ref)

    def _embed_cid_font(self, writer: PdfWriter, font_name: bytes,
                        usage: FontUsage,
                        instance_num: int) -> tuple[str, object] | None:
        """Embed a CID/TrueType font as PDF Type 0."""
        font_dict = usage.font_dict
        glyphs_used = usage.glyphs_used

        font_name_str = font_name.decode('latin-1') if isinstance(font_name, bytes) else str(font_name)
        unique_font_name = f"{font_name_str}_{instance_num}" if instance_num > 0 else font_name_str

        font_file_data = self.cid_font_embedder.get_sfnts_data(font_dict)
        if font_file_data is None:
            return None

        compressed = zlib.compress(font_file_data)
        font_file_stream = StreamObject()
        font_file_stream._data = compressed
        font_file_stream[NameObject('/Length')] = NumberObject(len(compressed))
        font_file_stream[NameObject('/Length1')] = NumberObject(len(font_file_data))
        font_file_stream[NameObject('/Filter')] = NameObject('/FlateDecode')
        font_file_ref = writer._add_object(font_file_stream)

        metrics = self.cid_font_embedder.get_font_metrics(font_dict)
        bbox = metrics['bbox']

        font_descriptor = DictionaryObject()
        font_descriptor[NameObject('/Type')] = NameObject('/FontDescriptor')
        font_descriptor[NameObject('/FontName')] = NameObject('/' + unique_font_name)
        font_descriptor[NameObject('/Flags')] = NumberObject(4)
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

        cid_to_gid = self.cid_font_embedder.get_cid_to_gid_dict(font_dict, glyphs_used)
        glyph_widths = self.cid_font_embedder.get_glyph_widths(
            font_dict, glyphs_used, cid_to_gid)
        w_array_data = self.cid_font_embedder.build_w_array(glyph_widths)

        default_width = self.cid_font_embedder.get_default_width(font_dict)
        registry, ordering, supplement = self.cid_font_embedder.get_cid_system_info(font_dict)

        cid_system_info = DictionaryObject()
        cid_system_info[NameObject('/Registry')] = _make_pdf_string(registry)
        cid_system_info[NameObject('/Ordering')] = _make_pdf_string(ordering)
        cid_system_info[NameObject('/Supplement')] = NumberObject(supplement)

        cid_font_dict_obj = DictionaryObject()
        cid_font_dict_obj[NameObject('/Type')] = NameObject('/Font')
        cid_font_dict_obj[NameObject('/Subtype')] = NameObject('/CIDFontType2')
        cid_font_dict_obj[NameObject('/BaseFont')] = NameObject('/' + unique_font_name)
        cid_font_dict_obj[NameObject('/CIDSystemInfo')] = cid_system_info
        cid_font_dict_obj[NameObject('/FontDescriptor')] = font_descriptor_ref
        cid_font_dict_obj[NameObject('/DW')] = NumberObject(default_width)

        if w_array_data:
            w_array = _build_pdf_w_array(w_array_data)
            if w_array:
                cid_font_dict_obj[NameObject('/W')] = w_array

        cid_to_gid_data = self.cid_font_embedder.build_cid_to_gid_map(
            font_dict, glyphs_used, cid_to_gid)
        if cid_to_gid_data:
            cid_to_gid_stream = StreamObject()
            cid_to_gid_stream._data = cid_to_gid_data
            cid_to_gid_stream[NameObject('/Length')] = NumberObject(len(cid_to_gid_data))
            cid_font_dict_obj[NameObject('/CIDToGIDMap')] = writer._add_object(cid_to_gid_stream)
        else:
            cid_font_dict_obj[NameObject('/CIDToGIDMap')] = NameObject('/Identity')

        cid_font_ref = writer._add_object(cid_font_dict_obj)

        tounicode_ref = None
        tounicode_map = self.cid_font_embedder.build_tounicode_map(
            font_dict, glyphs_used, cid_to_gid)
        if tounicode_map:
            cmap_data = generate_cid_tounicode_cmap(tounicode_map, unique_font_name)
            cmap_stream = StreamObject()
            cmap_stream._data = cmap_data
            cmap_stream[NameObject('/Length')] = NumberObject(len(cmap_data))
            tounicode_ref = writer._add_object(cmap_stream)

        font_obj = DictionaryObject()
        font_obj[NameObject('/Type')] = NameObject('/Font')
        font_obj[NameObject('/Subtype')] = NameObject('/Type0')
        font_obj[NameObject('/BaseFont')] = NameObject('/' + unique_font_name)
        font_obj[NameObject('/Encoding')] = NameObject('/Identity-H')
        font_obj[NameObject('/DescendantFonts')] = ArrayObject([cid_font_ref])
        if tounicode_ref:
            font_obj[NameObject('/ToUnicode')] = tounicode_ref

        font_ref = writer._add_object(font_obj)

        base_name = font_name_str.replace('-', '').replace(' ', '')
        resource_name = f'/{base_name}_{instance_num}' if instance_num > 0 else f'/{base_name}'
        return (resource_name, font_ref)

    def _embed_cff_font(self, writer: PdfWriter, font_name: bytes,
                        usage: FontUsage,
                        instance_num: int) -> tuple[str, object] | None:
        """Embed a CFF font as /FontFile3 /Type1C."""
        font_dict = usage.font_dict
        glyphs_used = usage.glyphs_used

        font_name_str = font_name.decode('latin-1') if isinstance(font_name, bytes) else str(font_name)
        unique_font_name = f"{font_name_str}_{instance_num}" if instance_num > 0 else font_name_str

        cff_data = self.cff_embedder.get_font_file_data(font_dict)
        if cff_data is None:
            return None

        font_bbox = _get_font_bbox(font_dict)
        first_char, last_char = _get_char_range(glyphs_used)

        glyph_widths = self.cff_embedder.get_glyph_widths(font_dict, glyphs_used)
        default_width = 600
        widths = ArrayObject([
            NumberObject(glyph_widths.get(cc, default_width))
            for cc in range(first_char, last_char + 1)
        ])

        compressed = zlib.compress(cff_data)
        font_file_stream = StreamObject()
        font_file_stream._data = compressed
        font_file_stream[NameObject('/Length')] = NumberObject(len(compressed))
        font_file_stream[NameObject('/Subtype')] = NameObject('/Type1C')
        font_file_stream[NameObject('/Filter')] = NameObject('/FlateDecode')
        font_file_ref = writer._add_object(font_file_stream)

        font_descriptor = DictionaryObject()
        font_descriptor[NameObject('/Type')] = NameObject('/FontDescriptor')
        font_descriptor[NameObject('/FontName')] = NameObject('/' + unique_font_name)
        font_descriptor[NameObject('/Flags')] = NumberObject(_get_font_flags(font_dict))
        font_descriptor[NameObject('/FontBBox')] = ArrayObject([
            NumberObject(int(font_bbox[0])), NumberObject(int(font_bbox[1])),
            NumberObject(int(font_bbox[2])), NumberObject(int(font_bbox[3])),
        ])
        font_descriptor[NameObject('/ItalicAngle')] = NumberObject(0)
        font_descriptor[NameObject('/Ascent')] = NumberObject(int(font_bbox[3]))
        font_descriptor[NameObject('/Descent')] = NumberObject(int(font_bbox[1]))
        font_descriptor[NameObject('/CapHeight')] = NumberObject(int(font_bbox[3] * 0.7))
        font_descriptor[NameObject('/StemV')] = NumberObject(80)
        font_descriptor[NameObject('/FontFile3')] = font_file_ref
        font_descriptor_ref = writer._add_object(font_descriptor)

        tounicode_map = _build_tounicode_map(font_dict, glyphs_used)
        tounicode_ref = None
        if tounicode_map:
            cmap_data = generate_tounicode_cmap(tounicode_map, font_name_str)
            cmap_stream = StreamObject()
            cmap_stream._data = cmap_data
            cmap_stream[NameObject('/Length')] = NumberObject(len(cmap_data))
            tounicode_ref = writer._add_object(cmap_stream)

        font_obj = DictionaryObject()
        font_obj[NameObject('/Type')] = NameObject('/Font')
        font_obj[NameObject('/Subtype')] = NameObject('/Type1')
        font_obj[NameObject('/BaseFont')] = NameObject('/' + unique_font_name)
        font_obj[NameObject('/FirstChar')] = NumberObject(first_char)
        font_obj[NameObject('/LastChar')] = NumberObject(last_char)
        font_obj[NameObject('/Widths')] = widths
        font_obj[NameObject('/FontDescriptor')] = font_descriptor_ref
        if tounicode_ref:
            font_obj[NameObject('/ToUnicode')] = tounicode_ref

        font_ref = writer._add_object(font_obj)

        base_name = font_name_str.replace('-', '').replace(' ', '')
        resource_name = f'/{base_name}_{instance_num}' if instance_num > 0 else f'/{base_name}'
        return (resource_name, font_ref)

    def _embed_type42_font(self, writer: PdfWriter, font_name: bytes,
                           usage: FontUsage,
                           instance_num: int) -> tuple[str, object] | None:
        """Embed a Type 42 (TrueType wrapper) font.

        Type 42 fonts are TrueType fonts wrapped in a PostScript dict.
        We extract the sfnts data and embed as TrueType (/FontFile2).
        """
        font_dict = usage.font_dict
        glyphs_used = usage.glyphs_used

        font_name_str = font_name.decode('latin-1') if isinstance(font_name, bytes) else str(font_name)
        unique_font_name = f"{font_name_str}_{instance_num}" if instance_num > 0 else font_name_str

        # Type 42 fonts have sfnts in their own dict (not in FDepVector)
        sfnts = font_dict.val.get(b'sfnts')
        if not sfnts:
            return None

        # Concatenate sfnts string segments into TrueType binary
        font_data = bytearray()
        for s in sfnts.val:
            if s.TYPE == ps.T_STRING:
                b = s.byte_string()
                if isinstance(b, str):
                    b = b.encode('latin-1')
                # Odd-length strings have a trailing padding byte to strip
                if len(b) & 1:
                    b = b[:-1]
                font_data.extend(b)
        if len(font_data) < 12:
            return None
        font_file_data = bytes(font_data)

        # Parse TrueType tables for metrics and cmap rewrite
        tables = self.cid_font_embedder._parse_table_directory(font_file_data)

        # Rewrite cmap to map char codes -> GIDs per PostScript encoding.
        # Symbolic TrueType fonts in PDF use cmap directly (ignoring /Encoding).
        font_file_data = _rewrite_type42_cmap(
            font_file_data, tables, font_dict, glyphs_used)
        # Re-parse tables after reassembly (offsets changed)
        tables = self.cid_font_embedder._parse_table_directory(font_file_data)

        units_per_em = self.cid_font_embedder._get_units_per_em(
            font_file_data, tables)
        scale = 1000.0 / units_per_em if units_per_em > 0 else 1.0

        font_bbox = _get_type42_bbox(font_file_data, tables, scale)
        first_char, last_char = _get_char_range(glyphs_used)

        # Get widths from CharStrings (maps char codes to glyph names)
        # and Metrics if available
        glyph_widths_map = self._get_type42_glyph_widths(
            font_dict, glyphs_used, font_file_data, tables, units_per_em)
        default_width = 600
        widths = ArrayObject([
            NumberObject(glyph_widths_map.get(cc, default_width))
            for cc in range(first_char, last_char + 1)
        ])

        compressed = zlib.compress(font_file_data)
        font_file_stream = StreamObject()
        font_file_stream._data = compressed
        font_file_stream[NameObject('/Length')] = NumberObject(len(compressed))
        font_file_stream[NameObject('/Length1')] = NumberObject(len(font_file_data))
        font_file_stream[NameObject('/Filter')] = NameObject('/FlateDecode')
        font_file_ref = writer._add_object(font_file_stream)

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

        font_descriptor = DictionaryObject()
        font_descriptor[NameObject('/Type')] = NameObject('/FontDescriptor')
        font_descriptor[NameObject('/FontName')] = NameObject('/' + unique_font_name)
        font_descriptor[NameObject('/Flags')] = NumberObject(_get_font_flags(font_dict))
        font_descriptor[NameObject('/FontBBox')] = ArrayObject([
            NumberObject(int(font_bbox[0])), NumberObject(int(font_bbox[1])),
            NumberObject(int(font_bbox[2])), NumberObject(int(font_bbox[3])),
        ])
        font_descriptor[NameObject('/ItalicAngle')] = NumberObject(0)
        font_descriptor[NameObject('/Ascent')] = NumberObject(ascent)
        font_descriptor[NameObject('/Descent')] = NumberObject(descent)
        font_descriptor[NameObject('/CapHeight')] = NumberObject(cap_height)
        font_descriptor[NameObject('/StemV')] = NumberObject(80)
        font_descriptor[NameObject('/FontFile2')] = font_file_ref
        font_descriptor_ref = writer._add_object(font_descriptor)

        tounicode_map = _build_tounicode_map(font_dict, glyphs_used)
        tounicode_ref = None
        if tounicode_map:
            cmap_data = generate_tounicode_cmap(tounicode_map, font_name_str)
            cmap_stream = StreamObject()
            cmap_stream._data = cmap_data
            cmap_stream[NameObject('/Length')] = NumberObject(len(cmap_data))
            tounicode_ref = writer._add_object(cmap_stream)

        font_obj = DictionaryObject()
        font_obj[NameObject('/Type')] = NameObject('/Font')
        font_obj[NameObject('/Subtype')] = NameObject('/TrueType')
        font_obj[NameObject('/BaseFont')] = NameObject('/' + unique_font_name)
        font_obj[NameObject('/FirstChar')] = NumberObject(first_char)
        font_obj[NameObject('/LastChar')] = NumberObject(last_char)
        font_obj[NameObject('/Widths')] = widths
        font_obj[NameObject('/FontDescriptor')] = font_descriptor_ref

        encoding_obj = _build_pdf_encoding(font_dict, first_char, last_char)
        if encoding_obj:
            font_obj[NameObject('/Encoding')] = encoding_obj
        if tounicode_ref:
            font_obj[NameObject('/ToUnicode')] = tounicode_ref

        font_ref = writer._add_object(font_obj)

        base_name = font_name_str.replace('-', '').replace(' ', '')
        resource_name = f'/{base_name}_{instance_num}' if instance_num > 0 else f'/{base_name}'
        return (resource_name, font_ref)

    def _get_icc_profile_ref(self, writer: PdfWriter,
                             profile_hash: bytes,
                             n: int) -> object | None:
        """Get or create an ICC profile stream as an indirect reference.

        Deduplicates across pages — same profile hash reuses the same ref.

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

    def _build_image_color_space(self, writer: PdfWriter,
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

    def _build_image_xobject(self, writer: PdfWriter,
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

            # Type 4 color key mask → PDF /Mask array
            if color_key_mask is not None:
                img_stream[NameObject('/Mask')] = ArrayObject(
                    [NumberObject(v) for v in color_key_mask])

            # Type 3 stencil mask → PDF /Mask with Image XObject ref
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
    def _build_stencil_mask_xobject(writer: PdfWriter,
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

        # polarity=True (PS Decode [0 1]): bit=0 → paint base image
        #   Matches PDF default (value 0 = paint) — no Decode needed
        # polarity=False (PS Decode [1 0]): bit=1 → paint base image
        #   Need Decode [1 0] to invert
        if not polarity:
            mask_stream[NameObject('/Decode')] = ArrayObject([
                NumberObject(1), NumberObject(0)])

        return writer._add_object(mask_stream)

    @staticmethod
    def _is_cff_font(font_dict: ps.Dict) -> bool:
        """Check if a font dictionary is a CFF (Type 2) font."""
        font_type = font_dict.val.get(b'FontType')
        return font_type is not None and font_type.val == 2

    @staticmethod
    def _is_type42_font(font_dict: ps.Dict) -> bool:
        """Check if a font dictionary is a Type 42 (TrueType) font."""
        font_type = font_dict.val.get(b'FontType')
        return font_type is not None and font_type.val == 42

    def _get_type42_glyph_widths(self, font_dict: ps.Dict,
                                 glyphs_used: set[int],
                                 font_data: bytes | None = None,
                                 tables: dict | None = None,
                                 units_per_em: int | None = None) -> dict[int, int]:
        """Get glyph widths for Type 42 font.

        Uses Encoding -> CharStrings -> hmtx mapping when font data is
        available, falls back to Metrics dict otherwise.
        """
        # Try hmtx-based widths if font data is available
        if font_data is not None and tables is not None:
            if units_per_em is None:
                units_per_em = self.cid_font_embedder._get_units_per_em(
                    font_data, tables)
            hmtx_widths = _get_type42_hmtx_widths(
                font_dict, font_data, tables, units_per_em, glyphs_used)
            if hmtx_widths:
                return hmtx_widths

        # Fallback: Metrics dict
        widths: dict[int, int] = {}
        metrics = font_dict.val.get(b'Metrics')
        if metrics and metrics.TYPE == ps.T_DICT:
            encoding = font_dict.val.get(b'Encoding')
            if encoding and encoding.TYPE in ps.ARRAY_TYPES:
                for cc in glyphs_used:
                    if cc < encoding.length:
                        glyph_name = encoding.val[encoding.start + cc]
                        if hasattr(glyph_name, 'val') and glyph_name.val in metrics.val:
                            w_obj = metrics.val[glyph_name.val]
                            if hasattr(w_obj, 'val'):
                                widths[cc] = int(w_obj.val)
        return widths


# --- Helper functions (shared with pdf_injector patterns) ---

def _get_font_bbox(font_dict: ps.Dict) -> tuple[float, float, float, float]:
    """Extract FontBBox from a PostScript font dictionary."""
    bbox = font_dict.val.get(b'FontBBox')
    if bbox and bbox.TYPE in ps.ARRAY_TYPES and bbox.length >= 4:
        vals = [bbox.val[bbox.start + i].val for i in range(4)]
        # Apply FontMatrix scaling (typically [0.001 0 0 0.001 0 0])
        fm = font_dict.val.get(b'FontMatrix')
        if fm and fm.TYPE in ps.ARRAY_TYPES:
            sx = fm.val[fm.start].val
            sy = fm.val[fm.start + 3].val
            # Scale to 1000-unit space (standard for PDF)
            if abs(sx) > 0 and abs(sx) < 0.01:
                scale = 1.0 / sx
                return (vals[0] * scale, vals[1] * scale,
                        vals[2] * scale, vals[3] * scale)
        return tuple(vals)
    return (0, -200, 1000, 800)


def _get_char_range(glyphs_used: set[int]) -> tuple[int, int]:
    """Get first and last character codes from glyph usage set."""
    if not glyphs_used:
        return (0, 0)
    return (min(glyphs_used), max(glyphs_used))


def _get_widths(font_dict: ps.Dict, glyphs_used: set[int],
                first_char: int, last_char: int,
                font_embedder: FontEmbedder) -> ArrayObject:
    """Get widths array for Type 1 font."""
    width_map = font_embedder.get_glyph_widths(font_dict, glyphs_used)
    default_width = 600
    return ArrayObject([
        NumberObject(width_map.get(cc, default_width))
        for cc in range(first_char, last_char + 1)
    ])


_STANDARD_ENCODINGS = {
    b'StandardEncoding', b'ISOLatin1Encoding',
    b'WinAnsiEncoding', b'MacRomanEncoding', b'MacExpertEncoding',
}


def _get_font_flags(font_dict: ps.Dict) -> int:
    """Compute PDF font descriptor Flags from PostScript font dictionary.

    Fonts with standard named encodings get NonSymbolic (32), which tells
    PDF viewers the encoding can be mapped through standard encoding tables.

    Fonts with custom array encodings (e.g., music fonts, TeX CM fonts) get
    Symbolic (4), which tells PDF viewers to use the encoding built into
    the font program without attempting standard encoding remapping.
    """
    encoding = font_dict.val.get(b'Encoding')
    if encoding is not None and encoding.TYPE == ps.T_NAME:
        if encoding.val in _STANDARD_ENCODINGS:
            return 32  # NonSymbolic — standard encoding
    # Custom array encoding, unknown named encoding, or no encoding → Symbolic
    return 4  # Symbolic — encoding built into font program


def _to_pfb(font_data: bytes, length1: int, length2: int,
            length3: int) -> bytes:
    """Convert PFA-style font data to PFB format with segment markers."""
    import struct
    parts = []

    # ASCII header segment
    header = font_data[:length1]
    parts.append(b'\x80\x01')
    parts.append(struct.pack('<I', len(header)))
    parts.append(header)

    # Binary eexec segment
    binary = font_data[length1:length1 + length2]
    parts.append(b'\x80\x02')
    parts.append(struct.pack('<I', len(binary)))
    parts.append(binary)

    # ASCII trailer segment
    trailer = font_data[length1 + length2:]
    if trailer:
        parts.append(b'\x80\x01')
        parts.append(struct.pack('<I', len(trailer)))
        parts.append(trailer)

    # EOF marker
    parts.append(b'\x80\x03')

    return b''.join(parts)


def _build_tounicode_map(font_dict: ps.Dict,
                         glyphs_used: set[int]) -> dict[int, str] | None:
    """Build ToUnicode mapping from font encoding."""
    encoding = font_dict.val.get(b'Encoding')
    if not encoding or encoding.TYPE not in ps.ARRAY_TYPES:
        return None

    tounicode: dict[int, str] = {}
    for cc in glyphs_used:
        if cc >= encoding.length:
            continue
        glyph_name_obj = encoding.val[encoding.start + cc]
        if not hasattr(glyph_name_obj, 'val'):
            continue
        glyph_name = glyph_name_obj.val
        if isinstance(glyph_name, bytes):
            glyph_name = glyph_name.decode('latin-1')
        if glyph_name == '.notdef':
            continue
        unicode_val = glyph_name_to_unicode(glyph_name)
        if unicode_val:
            tounicode[cc] = unicode_val

    return tounicode if tounicode else None


def _build_pdf_encoding(font_dict: ps.Dict, first_char: int,
                        last_char: int) -> DictionaryObject | None:
    """Build PDF Encoding dictionary from PostScript encoding."""
    encoding = font_dict.val.get(b'Encoding')
    if not encoding or encoding.TYPE not in ps.ARRAY_TYPES:
        return None

    differences = []
    last_code = -2  # Force first entry to emit code

    for cc in range(first_char, last_char + 1):
        if cc >= encoding.length:
            break
        glyph_name_obj = encoding.val[encoding.start + cc]
        if not hasattr(glyph_name_obj, 'val'):
            continue
        glyph_name = glyph_name_obj.val
        if isinstance(glyph_name, bytes):
            glyph_name = glyph_name.decode('latin-1')
        if glyph_name == '.notdef':
            continue

        if cc != last_code + 1:
            differences.append(NumberObject(cc))
        differences.append(NameObject('/' + glyph_name))
        last_code = cc

    if not differences:
        return None

    enc_dict = DictionaryObject()
    enc_dict[NameObject('/Type')] = NameObject('/Encoding')
    enc_dict[NameObject('/Differences')] = ArrayObject(differences)
    return enc_dict


def _get_glyph_name_for_code(encoding: object, char_code: int) -> bytes | None:
    """Get glyph name for a character code from an encoding."""
    if encoding is None:
        return None
    if encoding.TYPE == ps.T_NAME:
        if 32 <= char_code <= 126:
            return chr(char_code).encode('latin-1')
        return None
    if encoding.TYPE in ps.ARRAY_TYPES:
        if 0 <= char_code < encoding.length:
            elem = encoding.val[encoding.start + char_code]
            if elem.TYPE == ps.T_NAME:
                return elem.val
    return None


def _rewrite_type42_cmap(font_data: bytes, tables: dict,
                          font_dict: ps.Dict,
                          glyphs_used: set[int]) -> bytes:
    """Rewrite cmap table in TrueType font to match PostScript encoding.

    Symbolic TrueType fonts in PDF use cmap directly (ignoring /Encoding).
    We must rewrite the cmap so char codes map to the correct GIDs as
    specified by the PostScript Encoding + CharStrings.

    Builds a cmap with:
    - Platform (1,0) format 0: direct char_code -> GID (Mac/generic)
    - Platform (3,0) format 4: 0xF000+char_code -> GID (Windows Symbol)
    """
    encoding = font_dict.val.get(b'Encoding')
    char_strings = font_dict.val.get(b'CharStrings')
    if not char_strings or char_strings.TYPE != ps.T_DICT:
        return font_data

    # Build char_code -> GID mapping from Encoding + CharStrings
    code_to_gid: dict[int, int] = {}
    for char_code in range(256):
        glyph_name = _get_glyph_name_for_code(encoding, char_code)
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
    fmt0_glyph_array = bytearray(256)
    for cc, gid in code_to_gid.items():
        if 0 <= cc < 256 and gid <= 255:
            fmt0_glyph_array[cc] = gid

    fmt0_data = struct.pack('>HHH', 0, 262, 0) + bytes(fmt0_glyph_array)

    # Build format 4 subtable for platform (3, 0) - Windows Symbol
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

    for _, end, _, _ in segments:
        fmt4.extend(struct.pack('>H', end))
    fmt4.extend(struct.pack('>H', 0))  # reservedPad
    for start, _, _, _ in segments:
        fmt4.extend(struct.pack('>H', start))
    for _, _, delta, _ in segments:
        fmt4.extend(struct.pack('>H', delta & 0xFFFF))
    for _, _, _, ro in segments:
        fmt4.extend(struct.pack('>H', ro))

    fmt4_len = len(fmt4)
    struct.pack_into('>H', fmt4, length_pos, fmt4_len)
    fmt4_data = bytes(fmt4)

    # Assemble cmap table
    num_subtables = 2
    cmap_header = struct.pack('>HH', 0, num_subtables)

    records_size = 4 + num_subtables * 8
    fmt0_offset = records_size
    fmt4_offset = fmt0_offset + len(fmt0_data)

    cmap_header += struct.pack('>HHI', 1, 0, fmt0_offset)
    cmap_header += struct.pack('>HHI', 3, 0, fmt4_offset)

    new_cmap = cmap_header + fmt0_data + fmt4_data

    # Replace cmap table in font data and reassemble
    keep_tables: dict[bytes, bytes] = {}
    for tag, (tbl_offset, tbl_length) in tables.items():
        keep_tables[tag] = font_data[tbl_offset:tbl_offset + tbl_length]
    keep_tables[b'cmap'] = new_cmap

    assembler = CIDFontEmbedder()
    return assembler._assemble_truetype(keep_tables)


def _get_type42_bbox(font_data: bytes, tables: dict,
                      scale: float) -> list[int]:
    """Get font bounding box from TrueType head table."""
    head_info = tables.get(b'head')
    if head_info:
        ho = head_info[0]
        if ho + 54 <= len(font_data):
            x_min = struct.unpack('>h', font_data[ho + 36:ho + 38])[0]
            y_min = struct.unpack('>h', font_data[ho + 38:ho + 40])[0]
            x_max = struct.unpack('>h', font_data[ho + 40:ho + 42])[0]
            y_max = struct.unpack('>h', font_data[ho + 42:ho + 44])[0]
            return [
                int(round(x_min * scale)), int(round(y_min * scale)),
                int(round(x_max * scale)), int(round(y_max * scale)),
            ]
    return [0, -200, 1000, 800]


def _get_type42_hmtx_widths(font_dict: ps.Dict, font_data: bytes,
                             tables: dict, units_per_em: int,
                             glyphs_used: set[int]) -> dict[int, int]:
    """Get glyph widths for Type 42 font via Encoding -> CharStrings -> hmtx.

    Maps char_code -> glyph_name (Encoding) -> GID (CharStrings) -> width (hmtx).
    """
    hmtx_info = tables.get(b'hmtx')
    hhea_info = tables.get(b'hhea')
    if not hmtx_info or not hhea_info:
        return {}

    hmtx_offset = hmtx_info[0]
    hhea_offset = hhea_info[0]
    scale = 1000.0 / units_per_em if units_per_em > 0 else 1.0

    if hhea_offset + 36 > len(font_data):
        return {}
    num_hmetrics = int.from_bytes(
        font_data[hhea_offset + 34:hhea_offset + 36], 'big')

    encoding = font_dict.val.get(b'Encoding')
    char_strings = font_dict.val.get(b'CharStrings')
    if not char_strings or char_strings.TYPE != ps.T_DICT:
        return {}

    widths: dict[int, int] = {}
    for char_code in glyphs_used:
        glyph_name = _get_glyph_name_for_code(encoding, char_code)
        if not glyph_name:
            continue
        cs_entry = char_strings.val.get(glyph_name)
        if cs_entry is None:
            continue
        gid = cs_entry.val if hasattr(cs_entry, 'val') else int(cs_entry)

        if gid < num_hmetrics:
            offset = hmtx_offset + gid * 4
            if offset + 2 <= len(font_data):
                w = int.from_bytes(font_data[offset:offset + 2], 'big')
                widths[char_code] = int(round(w * scale))
        elif num_hmetrics > 0:
            offset = hmtx_offset + (num_hmetrics - 1) * 4
            if offset + 2 <= len(font_data):
                w = int.from_bytes(font_data[offset:offset + 2], 'big')
                widths[char_code] = int(round(w * scale))

    return widths


def _build_pdf_w_array(w_array_data: list) -> ArrayObject | None:
    """Convert width data list to PDF /W ArrayObject."""
    if not w_array_data:
        return None
    result = ArrayObject()
    for entry in w_array_data:
        if isinstance(entry, int):
            result.append(NumberObject(entry))
        elif isinstance(entry, list):
            inner = ArrayObject([NumberObject(w) for w in entry])
            result.append(inner)
    return result


def _make_pdf_string(value: str) -> StreamObject:
    """Create a PDF string object from a Python string."""
    # Use a simple byte string wrapped in StreamObject
    from pypdf.generic import create_string_object
    return create_string_object(value)


def _build_pdf_shading(writer: PdfWriter,
                        desc: dict) -> object | None:
    """Convert a shading description dict to a pypdf indirect object.

    Handles Type 2/3 (gradient with function) and Type 4/6/7 (stream-based).
    """
    shading_type = desc.get('type')
    if shading_type is None:
        return None

    if shading_type in (2, 3):
        # Gradient shading — build function first
        func_desc = desc.get('function')
        if func_desc is None:
            return None
        func_ref = _build_pdf_function(writer, func_desc)
        if func_ref is None:
            return None

        shading_obj = DictionaryObject()
        shading_obj[NameObject('/ShadingType')] = NumberObject(shading_type)
        shading_obj[NameObject('/ColorSpace')] = NameObject(
            '/' + desc['color_space'])

        coords = desc['coords']
        shading_obj[NameObject('/Coords')] = ArrayObject(
            [_pdf_number(v) for v in coords])

        extend = desc.get('extend', [False, False])
        shading_obj[NameObject('/Extend')] = ArrayObject([
            BooleanObject(extend[0]), BooleanObject(extend[1])])

        shading_obj[NameObject('/Function')] = func_ref

        if 'bbox' in desc:
            shading_obj[NameObject('/BBox')] = ArrayObject(
                [_pdf_number(v) for v in desc['bbox']])

        return writer._add_object(shading_obj)

    elif shading_type in (4, 6, 7):
        # Stream-based shading (mesh, Coons, tensor)
        stream_data = desc.get('stream_data')
        if not stream_data:
            return None

        compressed = zlib.compress(stream_data)
        sh_stream = StreamObject()
        sh_stream._data = compressed
        sh_stream[NameObject('/Length')] = NumberObject(len(compressed))
        sh_stream[NameObject('/Filter')] = NameObject('/FlateDecode')
        sh_stream[NameObject('/ShadingType')] = NumberObject(shading_type)
        sh_stream[NameObject('/ColorSpace')] = NameObject(
            '/' + desc['color_space'])
        sh_stream[NameObject('/BitsPerCoordinate')] = NumberObject(
            desc['bits_per_coordinate'])
        sh_stream[NameObject('/BitsPerComponent')] = NumberObject(
            desc['bits_per_component'])
        sh_stream[NameObject('/BitsPerFlag')] = NumberObject(
            desc['bits_per_flag'])

        decode = desc['decode']
        sh_stream[NameObject('/Decode')] = ArrayObject(
            [_pdf_number(v) for v in decode])

        if 'bbox' in desc:
            sh_stream[NameObject('/BBox')] = ArrayObject(
                [_pdf_number(v) for v in desc['bbox']])

        return writer._add_object(sh_stream)

    return None


def _build_pdf_function(writer: PdfWriter,
                         func_desc: dict) -> object | None:
    """Convert a function description dict to a pypdf indirect object.

    Supports Type 0 (sampled) functions.
    """
    func_type = func_desc.get('type')
    if func_type != 0:
        return None

    stream_data = func_desc.get('stream_data')
    if not stream_data:
        return None

    compressed = zlib.compress(stream_data)
    func_stream = StreamObject()
    func_stream._data = compressed
    func_stream[NameObject('/Length')] = NumberObject(len(compressed))
    func_stream[NameObject('/Filter')] = NameObject('/FlateDecode')
    func_stream[NameObject('/FunctionType')] = NumberObject(0)
    func_stream[NameObject('/Domain')] = ArrayObject(
        [_pdf_number(v) for v in func_desc['domain']])
    func_stream[NameObject('/Range')] = ArrayObject(
        [_pdf_number(v) for v in func_desc['range']])
    func_stream[NameObject('/Size')] = ArrayObject(
        [NumberObject(s) for s in func_desc['size']])
    func_stream[NameObject('/BitsPerSample')] = NumberObject(func_desc['bps'])
    func_stream[NameObject('/Order')] = NumberObject(func_desc.get('order', 1))

    return writer._add_object(func_stream)


def _pdf_number(v: float) -> NumberObject:
    """Create a NumberObject, using int when the value is whole."""
    if isinstance(v, int) or (isinstance(v, float) and v == int(v)):
        return NumberObject(int(v))
    return FloatObject(round(v, 6))
