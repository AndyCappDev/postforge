# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
PDF Document Builder

Assembles PDF files from content streams, fonts, and page metadata.
Uses pypdf for PDF object management and serialization.
"""

import io
import logging
import struct
import zlib

from ...core import types as ps
from ..pdf.font_tracker import FontTracker, FontUsage
from ..pdf.font_embedder import FontEmbedder, generate_tounicode_cmap
from ..pdf.cid_font_embedder import CIDFontEmbedder, generate_cid_tounicode_cmap
from ..pdf.cff_font_embedder import CFFEmbedder
from .font_helpers import (
    _charstrings_fingerprint, _get_font_bbox, _get_char_range, _get_widths,
    _get_font_flags, _to_pfb, _build_tounicode_map, _build_pdf_encoding,
    _rewrite_type42_cmap, _get_type42_bbox, _get_type42_hmtx_widths,
    _build_pdf_w_array, _make_pdf_string)
from .type3_ops import build_type3_font
from .image_ops import ImageXObjectBuilder
from .shading_ops import _build_pdf_shading

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
    logging.getLogger('pypdf').setLevel(logging.ERROR)
except ImportError:
    PYPDF_AVAILABLE = False


class PageData:
    """Stores data for a single PDF page."""

    __slots__ = ('content_stream', 'width_pts', 'height_pts',
                 'font_resources', 'standard14_fonts', 'font_keys_used',
                 'needs_invisible_font', 'rotate', 'shading_defs',
                 'image_defs', 'type3_page_keys')

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
        self.type3_page_keys: set[tuple] = set()  # Type 3 font keys used on this page


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
        self.image_builder = ImageXObjectBuilder(lossless_images)

    def build_pdf(self, pages: list[PageData], font_tracker: FontTracker,
                  output_path: str,
                  type3_fonts: dict | None = None) -> dict[tuple, tuple[str, object]]:
        """Build and write a PDF file.

        Args:
            pages: List of PageData objects (one per page).
            font_tracker: Font usage tracker with all fonts used.
            output_path: Path to write the PDF file.
            type3_fonts: Document-level Type 3 font definitions (shared across
                pages). If None, Type 3 fonts are built per-page from
                page_data.type3_page_keys (backward compat).

        Returns:
            Dict of embedded_fonts: font_key -> (resource_name, font_ref).
        """
        writer = PdfWriter()

        # Embed all required fonts
        embedded_fonts = self._embed_all_fonts(writer, font_tracker)

        # Build document-level Type 3 fonts once (shared across pages)
        type3_font_refs: dict[tuple, tuple[str, object]] = {}
        if type3_fonts:
            for font_key, t3_def in type3_fonts.items():
                t3_ref = build_type3_font(writer, t3_def)
                if t3_ref is not None:
                    type3_font_refs[font_key] = (t3_def.resource_name, t3_ref)

        # Create pages
        for page_data in pages:
            self._add_page(writer, page_data, embedded_fonts,
                           type3_font_refs)

        # Write to memory, then re-serialize through PdfReader/PdfWriter.
        # pypdf's initial object ordering places the last page dict as the
        # final object in the file, which triggers a text-selection bug in
        # poppler-based viewers (Evince).  A round-trip through PdfReader
        # reorders objects so page dicts precede their resources, fixing the
        # issue with negligible overhead (the PDF is already in memory).
        buf = io.BytesIO()
        writer.write(buf)
        buf.seek(0)
        reader = PdfReader(buf)
        final_writer = PdfWriter()
        final_writer.append_pages_from_reader(reader)
        with open(output_path, 'wb') as f:
            final_writer.write(f)

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

        # Build shared FontFile + FontDescriptor for re-encoded Type 1 fonts
        shared_descriptors = self._build_shared_type1_descriptors(
            writer, font_tracker, best_subrs)

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
                shared_desc_ref = shared_descriptors.get(font_key[0])
                result = self._embed_type1_font(writer, font_name, usage,
                                                instance_num, subrs_override,
                                                shared_desc_ref)

            if result:
                embedded_fonts[font_key] = result

        return embedded_fonts

    def _add_page(self, writer: PdfWriter, page_data: PageData,
                  embedded_fonts: dict[tuple, tuple[str, object]],
                  type3_font_refs: dict[tuple, tuple[str, object]] | None = None) -> None:
        """Add a page to the PDF writer.

        Args:
            writer: PdfWriter to add the page to.
            page_data: Page content and dimensions.
            embedded_fonts: Dict of embedded fonts.
            type3_font_refs: Pre-built Type 3 font refs (font_key -> (name, ref)).
        """
        page = writer.add_blank_page(
            width=page_data.width_pts, height=page_data.height_pts)

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
            img_ref = self.image_builder.build_image_xobject(writer, img_desc)
            if img_ref is not None:
                xobject_dict[NameObject(img_name)] = img_ref

        # Add pre-built Type 3 fonts used on this page
        if type3_font_refs:
            for font_key in page_data.type3_page_keys:
                if font_key in type3_font_refs:
                    res_name, t3_ref = type3_font_refs[font_key]
                    font_dict[NameObject(res_name)] = t3_ref

        # Build resources
        resources = DictionaryObject()
        if font_dict:
            resources[NameObject('/Font')] = font_dict
        if shading_dict:
            resources[NameObject('/Shading')] = shading_dict
        if xobject_dict:
            resources[NameObject('/XObject')] = xobject_dict

        # Configure the page
        page[NameObject('/Contents')] = content_ref
        page[NameObject('/Resources')] = resources
        if page_data.rotate:
            page[NameObject('/Rotate')] = NumberObject(page_data.rotate)

    def _build_shared_type1_descriptors(
            self, writer: PdfWriter, font_tracker: FontTracker,
            best_subrs: dict) -> dict[int, object]:
        """Build shared FontFile + FontDescriptor for re-encoded Type 1 fonts.

        Groups Type 1 fonts by CharStrings content fingerprint (sorted glyph
        names). DVIPS re-encodings create new CharStrings dict objects per
        instance, so Python id() differs even though the content is identical.
        Content-based grouping correctly identifies same-font instances.

        For groups with 2+ members, builds ONE FontFile stream and ONE
        FontDescriptor shared by all instances.

        Args:
            writer: PdfWriter to add objects to.
            font_tracker: Font usage tracker.
            best_subrs: Dict mapping cs_id -> best Subrs array.

        Returns:
            Dict mapping cs_id -> FontDescriptor indirect reference.
        """
        # Group Type 1 font keys by content fingerprint
        fp_groups: dict[tuple, list[tuple[tuple, FontUsage]]] = {}
        for font_key, usage in font_tracker.get_fonts_in_order():
            if FontTracker.is_cid_font(font_key):
                continue
            if self._is_cff_font(usage.font_dict):
                continue
            if self._is_type42_font(usage.font_dict):
                continue
            fp = _charstrings_fingerprint(usage.font_dict)
            if fp not in fp_groups:
                fp_groups[fp] = []
            fp_groups[fp].append((font_key, usage))

        shared_descriptors: dict[int, object] = {}

        for fp, members in fp_groups.items():
            if len(members) < 2:
                continue  # Single-instance font, no sharing needed

            # Pick representative font for FontFile + FontDescriptor
            representative_usage = members[0][1]
            font_dict = representative_usage.font_dict
            font_name = representative_usage.font_name
            font_name_str = (font_name.decode('latin-1')
                             if isinstance(font_name, bytes) else str(font_name))

            # Find best Subrs across all cs_ids in this content group
            subrs_override = None
            for font_key, _ in members:
                candidate = best_subrs.get(font_key[0])
                if candidate is not None:
                    if (subrs_override is None
                            or len(candidate.val) > len(subrs_override.val)):
                        subrs_override = candidate

            # Merge CharStrings and resolve glyph names from all instances.
            # DVIPS re-encodings create new CharStrings dict objects per
            # instance, so Python id() differs even though the content is identical.
            # Additionally, DVIPS subsets CharStrings per instance and each has
            # its own Encoding, so we must:
            #  1. Resolve each instance's char codes through its OWN Encoding
            #  2. Collect all needed glyph names across all instances
            #  3. Merge CharStrings entries from all instances so the
            #     representative has glyph programs for every needed name
            merged_names: set[bytes] = set()
            for _, member_usage in members:
                member_dict = member_usage.font_dict
                member_enc = member_dict.val.get(b'Encoding')
                for code in member_usage.glyphs_used:
                    name = self.font_embedder._get_glyph_name_for_code(
                        member_enc, code)
                    if name:
                        merged_names.add(name)

            # Add missing CharStrings entries from other instances to the
            # representative so the font program contains all needed glyphs
            rep_cs = font_dict.val.get(b'CharStrings')
            saved_cs = None
            if rep_cs and rep_cs.TYPE == ps.T_DICT:
                saved_cs = dict(rep_cs.val)  # snapshot for restore
                for _, member_usage in members:
                    member_cs = member_usage.font_dict.val.get(
                        b'CharStrings')
                    if member_cs and member_cs.TYPE == ps.T_DICT:
                        for name in merged_names:
                            if name not in rep_cs.val and name in member_cs.val:
                                rep_cs.val[name] = member_cs.val[name]

            result = self.font_embedder.get_font_file_data(
                font_dict, font_name_str, None, subrs_override,
                needed_glyph_names=merged_names)

            # Restore original CharStrings
            if saved_cs is not None:
                rep_cs.val = saved_cs
            if result is None:
                continue  # Fall through to per-instance embedding

            font_file_data, length1, length2, length3 = result
            pfb_data = _to_pfb(font_file_data, length1, length2, length3)

            # FontFile stream
            compressed = zlib.compress(pfb_data)
            font_file_stream = StreamObject()
            font_file_stream._data = compressed
            font_file_stream[NameObject('/Length')] = NumberObject(
                len(compressed))
            font_file_stream[NameObject('/Length1')] = NumberObject(length1)
            font_file_stream[NameObject('/Length2')] = NumberObject(length2)
            font_file_stream[NameObject('/Length3')] = NumberObject(length3)
            font_file_stream[NameObject('/Filter')] = NameObject(
                '/FlateDecode')
            font_file_ref = writer._add_object(font_file_stream)

            # FontDescriptor — uses base font name (all instances are the
            # same PostScript font, just re-encoded)
            font_bbox = _get_font_bbox(font_dict)
            font_descriptor = DictionaryObject()
            font_descriptor[NameObject('/Type')] = NameObject(
                '/FontDescriptor')
            font_descriptor[NameObject('/FontName')] = NameObject(
                '/' + font_name_str)
            font_descriptor[NameObject('/Flags')] = NumberObject(
                _get_font_flags(font_dict))
            font_descriptor[NameObject('/FontBBox')] = ArrayObject([
                NumberObject(int(font_bbox[0])),
                NumberObject(int(font_bbox[1])),
                NumberObject(int(font_bbox[2])),
                NumberObject(int(font_bbox[3])),
            ])
            font_descriptor[NameObject('/ItalicAngle')] = NumberObject(0)
            font_descriptor[NameObject('/Ascent')] = NumberObject(
                int(font_bbox[3]))
            font_descriptor[NameObject('/Descent')] = NumberObject(
                int(font_bbox[1]))
            font_descriptor[NameObject('/CapHeight')] = NumberObject(
                int(font_bbox[3] * 0.7))
            font_descriptor[NameObject('/StemV')] = NumberObject(80)
            font_descriptor[NameObject('/FontFile')] = font_file_ref
            font_descriptor_ref = writer._add_object(font_descriptor)

            # Map ALL member cs_ids to this shared descriptor so lookups
            # via font_key[0] find it regardless of which instance is queried
            for font_key, _ in members:
                shared_descriptors[font_key[0]] = font_descriptor_ref

        return shared_descriptors

    def _embed_type1_font(self, writer: PdfWriter, font_name: bytes,
                          usage: FontUsage, instance_num: int,
                          subrs_override: object = None,
                          shared_descriptor_ref: object = None) -> tuple[str, object] | None:
        """Embed a Type 1 font.

        When shared_descriptor_ref is provided, reuses that FontDescriptor
        (and its FontFile) instead of building per-instance copies.  This
        deduplicates the font program across re-encoded instances of the
        same base font.
        """
        font_dict = usage.font_dict
        glyphs_used = usage.glyphs_used

        font_name_str = font_name.decode('latin-1') if isinstance(font_name, bytes) else str(font_name)
        unique_font_name = f"{font_name_str}_{instance_num}" if instance_num > 0 else font_name_str

        first_char, last_char = _get_char_range(glyphs_used)
        widths = _get_widths(font_dict, glyphs_used, first_char, last_char,
                             self.font_embedder)

        if shared_descriptor_ref is not None:
            # Shared FontFile + FontDescriptor — skip per-instance build
            font_descriptor_ref = shared_descriptor_ref
        else:
            # Single-instance font — build its own FontFile + FontDescriptor
            result = self.font_embedder.get_font_file_data(
                font_dict, unique_font_name, glyphs_used, subrs_override)
            if result is None:
                return None

            font_file_data, length1, length2, length3 = result
            pfb_data = _to_pfb(font_file_data, length1, length2, length3)

            # FontFile stream
            compressed = zlib.compress(pfb_data)
            font_file_stream = StreamObject()
            font_file_stream._data = compressed
            font_file_stream[NameObject('/Length')] = NumberObject(
                len(compressed))
            font_file_stream[NameObject('/Length1')] = NumberObject(length1)
            font_file_stream[NameObject('/Length2')] = NumberObject(length2)
            font_file_stream[NameObject('/Length3')] = NumberObject(length3)
            font_file_stream[NameObject('/Filter')] = NameObject(
                '/FlateDecode')
            font_file_ref = writer._add_object(font_file_stream)

            # FontDescriptor
            font_bbox = _get_font_bbox(font_dict)
            font_descriptor = DictionaryObject()
            font_descriptor[NameObject('/Type')] = NameObject(
                '/FontDescriptor')
            font_descriptor[NameObject('/FontName')] = NameObject(
                '/' + unique_font_name)
            font_descriptor[NameObject('/Flags')] = NumberObject(
                _get_font_flags(font_dict))
            font_descriptor[NameObject('/FontBBox')] = ArrayObject([
                NumberObject(int(font_bbox[0])),
                NumberObject(int(font_bbox[1])),
                NumberObject(int(font_bbox[2])),
                NumberObject(int(font_bbox[3])),
            ])
            font_descriptor[NameObject('/ItalicAngle')] = NumberObject(0)
            font_descriptor[NameObject('/Ascent')] = NumberObject(
                int(font_bbox[3]))
            font_descriptor[NameObject('/Descent')] = NumberObject(
                int(font_bbox[1]))
            font_descriptor[NameObject('/CapHeight')] = NumberObject(
                int(font_bbox[3] * 0.7))
            font_descriptor[NameObject('/StemV')] = NumberObject(80)
            font_descriptor[NameObject('/FontFile')] = font_file_ref
            font_descriptor_ref = writer._add_object(font_descriptor)

        # ToUnicode CMap (always per-instance)
        tounicode_map = _build_tounicode_map(font_dict, glyphs_used)
        tounicode_ref = None
        if tounicode_map:
            cmap_data = generate_tounicode_cmap(tounicode_map, font_name_str)
            cmap_stream = StreamObject()
            cmap_compressed = zlib.compress(cmap_data)
            cmap_stream._data = cmap_compressed
            cmap_stream[NameObject('/Length')] = NumberObject(
                len(cmap_compressed))
            cmap_stream[NameObject('/Filter')] = NameObject('/FlateDecode')
            tounicode_ref = writer._add_object(cmap_stream)

        # Font dictionary (always per-instance)
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
            cmap_compressed = zlib.compress(cmap_data)
            cmap_stream._data = cmap_compressed
            cmap_stream[NameObject('/Length')] = NumberObject(
                len(cmap_compressed))
            cmap_stream[NameObject('/Filter')] = NameObject('/FlateDecode')
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
            cmap_compressed = zlib.compress(cmap_data)
            cmap_stream._data = cmap_compressed
            cmap_stream[NameObject('/Length')] = NumberObject(
                len(cmap_compressed))
            cmap_stream[NameObject('/Filter')] = NameObject('/FlateDecode')
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
            cmap_compressed = zlib.compress(cmap_data)
            cmap_stream._data = cmap_compressed
            cmap_stream[NameObject('/Length')] = NumberObject(
                len(cmap_compressed))
            cmap_stream[NameObject('/Filter')] = NameObject('/FlateDecode')
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
