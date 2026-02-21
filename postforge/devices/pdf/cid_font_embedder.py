# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
CID Font Embedder Module

Extracts TrueType font data from PostScript CID font dictionaries (Type 0
composite fonts with CIDFontType 2 / TrueType descendants) and generates
PDF embedding structures.

The TrueType font binary exists in the sfnts array of the CIDFont descendant.
We extract it directly (no reconstruction needed, unlike Type 1 fonts) and
generate the PDF structures needed for embedding: glyph widths (/W array),
ToUnicode CMap for searchability, and font metrics for the FontDescriptor.
"""

import struct

from ...core import types as ps


class CIDFontEmbedder:
    """
    Extract TrueType data from CID fonts and generate PDF embedding structures.
    """

    def __init__(self) -> None:
        """Initialize CID font embedder."""
        # Cache sfnts data by dict identity to avoid re-concatenation
        self._sfnts_cache = {}
        # Track which fonts were reconstructed (CID=GID identity built-in)
        self._reconstructed = set()
        # Cache parsed table directories
        self._table_cache = {}

    def get_sfnts_data(self, font_dict: ps.Dict) -> bytes | None:
        """
        Navigate Type 0 -> FDepVector[0] -> sfnts and concatenate string
        segments into a single TrueType binary.

        If the sfnts lacks glyf/loca tables (CUPS-style fonts store glyph
        outlines in PostScript GlyphDirectory instead), reconstructs a valid
        TrueType font by building glyf/loca from GlyphDirectory entries.

        Args:
            font_dict: PostScript Type 0 font dictionary

        Returns:
            bytes: Complete TrueType font binary, or None if unavailable
        """
        # Navigate to the CIDFont descendant
        cidfont_dict = self._get_cidfont(font_dict)
        if cidfont_dict is None:
            return None

        cache_key = id(cidfont_dict.val)
        if cache_key in self._sfnts_cache:
            return self._sfnts_cache[cache_key]

        sfnts = cidfont_dict.val.get(b'sfnts')
        if not sfnts or sfnts.TYPE not in ps.ARRAY_TYPES:
            self._sfnts_cache[cache_key] = None
            return None

        font_data = bytearray()
        for s in sfnts.val:
            if s.TYPE == ps.T_STRING:
                b = s.byte_string()
                if isinstance(b, str):
                    b = b.encode('latin-1')
                # Type 42 sfnts strings have a trailing padding byte (null
                # terminator) that is not part of the TrueType data. Per the
                # TrueType spec, actual data chunks have even length, so an
                # odd-length string indicates a padding byte that must be
                # stripped for table directory offsets to be correct.
                if len(b) & 1:
                    b = b[:-1]
                font_data.extend(b)

        if len(font_data) < 12:
            self._sfnts_cache[cache_key] = None
            return None

        # Check if glyf/loca tables exist
        tables = self._parse_table_directory(bytes(font_data))
        if b'glyf' not in tables or b'loca' not in tables:
            # CUPS-style font: glyph data in PostScript GlyphDirectory, not sfnts.
            # Reconstruct a valid TrueType by building glyf/loca tables.
            # CID = GID (identity CIDMap) so we place GlyphDirectory[CID] at GID=CID.
            font_data = self._reconstruct_truetype(
                font_data, cidfont_dict, tables)
            if font_data is None:
                self._sfnts_cache[cache_key] = None
                return None
            self._reconstructed.add(cache_key)

        result = bytes(font_data) if isinstance(font_data, bytearray) else font_data
        self._sfnts_cache[cache_key] = result
        return result

    def _reconstruct_truetype(self, raw_sfnts: bytearray,
                                cidfont_dict: ps.Dict,
                                old_tables: dict) -> bytes | None:
        """
        Reconstruct a valid TrueType font by adding glyf/loca tables
        built from the PostScript GlyphDirectory.

        CUPS-style CIDFont Type 2 fonts store glyph outlines in the
        PostScript GlyphDirectory dict (keyed by CID, which equals GID
        via identity CIDMap). The sfnts only contains header/metric tables
        (head, hhea, hmtx, maxp, etc.) but no glyf or loca.

        This method builds proper glyf and loca tables from GlyphDirectory
        entries and assembles a complete TrueType font binary.

        Args:
            raw_sfnts: Raw sfnts data (bytearray) without glyf/loca
            cidfont_dict: CIDFont dictionary containing GlyphDirectory
            old_tables: Parsed table directory from raw sfnts

        Returns:
            bytes: Reconstructed TrueType font binary, or None on failure
        """
        glyph_dir = cidfont_dict.val.get(b'GlyphDirectory')
        if not glyph_dir or glyph_dir.TYPE != ps.T_DICT:
            return None

        # Get numGlyphs from maxp table
        maxp_info = old_tables.get(b'maxp')
        if not maxp_info:
            return None
        mo = maxp_info[0]
        if mo + 6 > len(raw_sfnts):
            return None
        num_glyphs = int.from_bytes(raw_sfnts[mo + 4:mo + 6], 'big')
        if num_glyphs == 0:
            return None

        # Build glyf table from GlyphDirectory entries.
        # GlyphDirectory keys are CIDs (Python ints); CIDMap is identity so CID = GID.
        glyf_data = bytearray()
        loca_offsets = []  # uint32 offsets for long-format loca

        for gid in range(num_glyphs):
            loca_offsets.append(len(glyf_data))
            entry = glyph_dir.val.get(gid)
            if entry is not None and entry.TYPE == ps.T_STRING:
                glyph_bytes = entry.byte_string()
                if isinstance(glyph_bytes, str):
                    glyph_bytes = glyph_bytes.encode('latin-1')
                if glyph_bytes:
                    glyf_data.extend(glyph_bytes)
                    # Pad to 2-byte alignment
                    if len(glyf_data) % 2:
                        glyf_data.append(0)
            # Empty glyphs (no entry) get same offset as next — zero-length

        # Final loca entry = total glyf size
        loca_offsets.append(len(glyf_data))

        # Build loca table (long format = uint32 entries)
        loca_data = bytearray()
        for offset in loca_offsets:
            loca_data.extend(struct.pack('>I', offset))

        # Collect existing table data (excluding gdir which is non-standard)
        keep_tables = {}
        for tag, (tbl_offset, tbl_length) in old_tables.items():
            if tag == b'gdir':
                continue  # Remove non-standard gdir table
            keep_tables[tag] = bytes(raw_sfnts[tbl_offset:tbl_offset + tbl_length])

        # Add new tables
        keep_tables[b'glyf'] = bytes(glyf_data)
        keep_tables[b'loca'] = bytes(loca_data)

        # Update head table: set indexToLocFormat = 1 (long format)
        if b'head' in keep_tables:
            head = bytearray(keep_tables[b'head'])
            if len(head) >= 52:
                head[50:52] = struct.pack('>H', 1)  # long loca format
            keep_tables[b'head'] = bytes(head)

        # Reassemble the TrueType binary
        return self._assemble_truetype(keep_tables)

    def _assemble_truetype(self, table_dict: dict[bytes, bytes]) -> bytes:
        """
        Assemble a TrueType font binary from a dictionary of table data.

        Builds the offset table, table directory, and concatenated table data
        with proper alignment and TrueType checksums.

        Args:
            table_dict: {tag_bytes: table_data_bytes} for each table

        Returns:
            bytes: Complete TrueType font binary
        """
        num_tables = len(table_dict)
        # TrueType offset table values
        import math as _math
        entry_selector = int(_math.log2(num_tables)) if num_tables > 0 else 0
        search_range = (1 << entry_selector) * 16
        range_shift = num_tables * 16 - search_range

        # Sort tables by tag (TrueType convention)
        sorted_tags = sorted(table_dict.keys())

        # Calculate offsets: header(12) + directory(numTables * 16) + table data
        header_size = 12 + num_tables * 16
        current_offset = header_size

        # Plan table layout
        table_entries = []
        for tag in sorted_tags:
            data = table_dict[tag]
            # Pad table data to 4-byte alignment
            padded_len = (len(data) + 3) & ~3
            table_entries.append((tag, data, current_offset, padded_len))
            current_offset += padded_len

        # Build offset table
        result = bytearray()
        result.extend(struct.pack('>HH', 0x0001, 0x0000))  # sfVersion 1.0
        result.extend(struct.pack('>HHH', num_tables, search_range,
                                  entry_selector))
        result.extend(struct.pack('>H', range_shift))

        # Build table directory
        for tag, data, offset, padded_len in table_entries:
            checksum = self._calc_table_checksum(data)
            result.extend(tag)
            result.extend(struct.pack('>III', checksum, offset, len(data)))

        # Append table data
        for tag, data, offset, padded_len in table_entries:
            result.extend(data)
            # Pad to 4-byte alignment
            padding = padded_len - len(data)
            if padding > 0:
                result.extend(b'\x00' * padding)

        return bytes(result)

    @staticmethod
    def _calc_table_checksum(data: bytes) -> int:
        """Calculate TrueType table checksum (sum of uint32 values)."""
        # Pad to 4 bytes
        padded = data + b'\x00' * ((4 - len(data) % 4) % 4)
        total = 0
        for i in range(0, len(padded), 4):
            total += int.from_bytes(padded[i:i + 4], 'big')
        return total & 0xFFFFFFFF

    def _get_cidfont(self, font_dict: ps.Dict) -> ps.Dict | None:
        """
        Get the CIDFont descendant from a Type 0 font dictionary.

        Args:
            font_dict: PostScript Type 0 font dictionary

        Returns:
            CIDFont dictionary, or None
        """
        fdep_vector = font_dict.val.get(b'FDepVector')
        if not fdep_vector or fdep_vector.TYPE not in ps.ARRAY_TYPES:
            return None
        if not fdep_vector.val:
            return None
        return fdep_vector.val[0]

    def _parse_table_directory(self, font_data: bytes) -> dict[bytes, tuple[int, int]]:
        """
        Parse TrueType offset table and return table directory.

        Args:
            font_data: Complete TrueType font binary

        Returns:
            dict: {tag_bytes: (offset, length)} for each table
        """
        cache_key = id(font_data)
        if cache_key in self._table_cache:
            return self._table_cache[cache_key]

        if len(font_data) < 12:
            return {}

        num_tables = int.from_bytes(font_data[4:6], 'big')
        tables = {}
        for i in range(num_tables):
            entry_offset = 12 + i * 16
            if entry_offset + 16 > len(font_data):
                break
            tag = font_data[entry_offset:entry_offset + 4]
            tbl_offset = int.from_bytes(font_data[entry_offset + 8:entry_offset + 12], 'big')
            tbl_length = int.from_bytes(font_data[entry_offset + 12:entry_offset + 16], 'big')
            tables[tag] = (tbl_offset, tbl_length)

        self._table_cache[cache_key] = tables
        return tables

    def _get_unicode_to_gid(self, font_data: bytes,
                              tables: dict) -> dict[int, int]:
        """
        Get Unicode -> GID mapping from TrueType cmap table.

        Inverts the gid_to_unicode dict from _parse_cmap_table.

        Args:
            font_data: Complete TrueType font binary
            tables: Parsed table directory

        Returns:
            dict: {unicode_codepoint: gid}
        """
        cmap_info = tables.get(b'cmap')
        if not cmap_info:
            return {}

        cmap_offset = cmap_info[0]
        gid_to_unicode = self._parse_cmap_table(font_data, cmap_offset)

        # Invert: unicode -> gid (first GID wins for duplicate unicodes)
        unicode_to_gid = {}
        for gid, unicode_cp in gid_to_unicode.items():
            if unicode_cp not in unicode_to_gid:
                unicode_to_gid[unicode_cp] = gid

        return unicode_to_gid

    def get_cid_to_gid_dict(self, font_dict: ps.Dict,
                              glyphs_used: set[int]) -> dict[int, int]:
        """
        Compute CID -> GID mapping dict for used glyphs.

        For reconstructed fonts (where glyf/loca were built from GlyphDirectory
        with CID=GID ordering), returns empty dict to signal identity mapping.

        For fonts with existing glyf/loca, tries:
        1. TrueType cmap table: Unicode -> GID mapping (when CIDs = Unicode)
        2. GlyphDirectory matching: match glyf data against sfnts

        Args:
            font_dict: PostScript Type 0 font dictionary
            glyphs_used: Set of CID values used

        Returns:
            dict: {cid: gid} for matched entries, empty dict for identity
        """
        font_data = self.get_sfnts_data(font_dict)
        if font_data is None:
            return {}

        # If we reconstructed this font, CID=GID is baked in — use identity
        cidfont = self._get_cidfont(font_dict)
        if cidfont and id(cidfont.val) in self._reconstructed:
            return {}

        tables = self._parse_table_directory(font_data)

        # Try cmap-based mapping first
        cid_to_gid = {}
        unicode_to_gid = self._get_unicode_to_gid(font_data, tables)
        if unicode_to_gid:
            for cid in glyphs_used:
                gid = unicode_to_gid.get(cid)
                if gid is not None:
                    cid_to_gid[cid] = gid

        # If cmap didn't cover the used glyphs, try GlyphDirectory matching
        unmapped = glyphs_used - set(cid_to_gid.keys())
        if unmapped:
            gd_mapping = self._build_cid_to_gid_from_glyph_directory(
                font_dict, font_data, tables, unmapped)
            cid_to_gid.update(gd_mapping)

        return cid_to_gid

    def get_glyph_widths(self, font_dict: ps.Dict, glyphs_used: set[int],
                          cid_to_gid: dict[int, int] | None = None) -> dict[int, int]:
        """
        Get glyph widths for used CIDs, scaled to PDF 1000-unit space.

        For each used CID: maps CID -> GID via cid_to_gid mapping, looks up
        width in hmtx table, scales to 1000 units using unitsPerEm from head.

        Args:
            font_dict: PostScript Type 0 font dictionary
            glyphs_used: Set of CID values used
            cid_to_gid: Optional pre-computed {cid: gid} mapping dict.
                         If None, identity mapping is used as fallback.

        Returns:
            dict: {cid: width_in_1000_units} for each used CID
        """
        font_data = self.get_sfnts_data(font_dict)
        if font_data is None:
            return {}

        tables = self._parse_table_directory(font_data)
        units_per_em = self._get_units_per_em(font_data, tables)
        scale = 1000.0 / units_per_em if units_per_em > 0 else 1.0

        # Get hmtx and hhea table info
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

        widths = {}
        for cid in glyphs_used:
            gid = cid_to_gid.get(cid, cid) if cid_to_gid else cid
            if gid < num_hmetrics:
                offset = hmtx_offset + gid * 4
                if offset + 2 <= len(font_data):
                    w = int.from_bytes(font_data[offset:offset + 2], 'big')
                    widths[cid] = int(round(w * scale))
            else:
                # Use last full hmetric width
                if num_hmetrics > 0:
                    offset = hmtx_offset + (num_hmetrics - 1) * 4
                    if offset + 2 <= len(font_data):
                        w = int.from_bytes(font_data[offset:offset + 2], 'big')
                        widths[cid] = int(round(w * scale))

        return widths

    def build_w_array(self, glyph_widths: dict[int, int]) -> list:
        """
        Build compact PDF /W array for CID font widths.

        Format: [start_cid [w1 w2 ...] start_cid2 [w3 w4 ...] ...]
        Groups consecutive CIDs into runs.

        Args:
            glyph_widths: dict {cid: width} from get_glyph_widths()

        Returns:
            list: Alternating start_cid and width lists for consecutive runs
        """
        if not glyph_widths:
            return []

        sorted_cids = sorted(glyph_widths.keys())
        result = []
        run_start = sorted_cids[0]
        run_widths = [glyph_widths[sorted_cids[0]]]

        for i in range(1, len(sorted_cids)):
            cid = sorted_cids[i]
            if cid == sorted_cids[i - 1] + 1:
                # Consecutive - extend current run
                run_widths.append(glyph_widths[cid])
            else:
                # Gap - emit current run and start new one
                result.append(run_start)
                result.append(run_widths)
                run_start = cid
                run_widths = [glyph_widths[cid]]

        # Emit final run
        result.append(run_start)
        result.append(run_widths)

        return result

    def build_tounicode_map(self, font_dict: ps.Dict, glyphs_used: set[int],
                              cid_to_gid: dict[int, int] | None = None) -> dict[int, str]:
        """
        Build CID -> Unicode mapping for ToUnicode CMap.

        Uses the TrueType cmap table (GID -> Unicode) combined with
        CID -> GID mapping to produce CID -> Unicode for searchable text.

        If the cmap table is unavailable (stripped by CUPS), ToUnicode
        cannot be built and an empty dict is returned.

        Args:
            font_dict: PostScript Type 0 font dictionary
            glyphs_used: Set of CID values used
            cid_to_gid: Optional pre-computed {cid: gid} mapping dict

        Returns:
            dict: {cid: unicode_string} for each mappable CID
        """
        font_data = self.get_sfnts_data(font_dict)
        if font_data is None:
            return {}

        tables = self._parse_table_directory(font_data)

        # Get GID -> Unicode from cmap table
        cmap_info = tables.get(b'cmap')
        if cmap_info:
            gid_to_unicode = self._parse_cmap_table(font_data, cmap_info[0])
        else:
            gid_to_unicode = {}

        tounicode = {}
        if gid_to_unicode and cid_to_gid:
            # Map CID -> GID -> Unicode
            for cid in glyphs_used:
                gid = cid_to_gid.get(cid)
                if gid is not None:
                    unicode_cp = gid_to_unicode.get(gid)
                    if unicode_cp is not None and 0 < unicode_cp < 0x10000:
                        tounicode[cid] = chr(unicode_cp)
        elif not gid_to_unicode:
            # No cmap table — try heuristic for CUPS-style fonts.
            # CUPS font converter strips the cmap table but preserves the
            # original TrueType GID ordering.  Many TrueType fonts use a
            # standard layout: GID 0 = .notdef, GID 1 = .null, GID 2 = CR,
            # GID 3 = space (Unicode 32), then glyphs in Unicode order.
            # For such fonts, Unicode = GID + 29.
            cidfont = self._get_cidfont(font_dict)
            if cidfont and id(cidfont.val) in self._reconstructed:
                tounicode = self._infer_tounicode_from_gid_layout(
                    font_data, tables, glyphs_used)

        return tounicode

    def _infer_tounicode_from_gid_layout(self, font_data: bytes, tables: dict,
                                            glyphs_used: set[int]) -> dict[int, str]:
        """
        Infer CID -> Unicode mapping for CUPS-style fonts without cmap.

        Many TrueType fonts use a standard GID layout:
          GID 0 = .notdef, GID 1 = .null, GID 2 = CR, GID 3 = space
        followed by glyphs in Unicode code point order.  This gives a
        constant offset of 29 between GID and Unicode (32 - 3 = 29).

        This method validates the layout by checking hmtx advance widths:
        GID 1 (.null) should have zero advance width, and GID 3 should
        have a moderate advance width consistent with a space glyph.

        Args:
            font_data: Complete TrueType font binary
            tables: Parsed table directory
            glyphs_used: Set of CID values used

        Returns:
            dict: {cid: unicode_char} for each mappable CID, or {}
        """
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
        if num_hmetrics < 4:
            return {}

        units_per_em = self._get_units_per_em(font_data, tables)

        # Read advance widths for GIDs 0-3
        def get_advance(gid):
            if gid < num_hmetrics:
                off = hmtx_offset + gid * 4
                if off + 2 <= len(font_data):
                    return int.from_bytes(font_data[off:off + 2], 'big')
            return None

        w0 = get_advance(0)  # .notdef
        w1 = get_advance(1)  # .null
        w2 = get_advance(2)  # CR / nonmarkingreturn
        w3 = get_advance(3)  # space (expected)

        if w1 is None or w2 is None or w3 is None:
            return {}

        # Validate standard layout:
        # - GID 1 (.null) should be zero width
        # - GID 3 should have positive width (space), but ≤ 50% of em
        # - GID 2 (CR/nonmarkingreturn) may have zero or space-like width
        if w1 != 0:
            return {}
        if w3 <= 0 or w3 > units_per_em * 0.5:
            return {}

        # Standard layout confirmed: Unicode = GID + 29
        offset = 29
        tounicode = {}
        for cid in glyphs_used:
            unicode_cp = cid + offset
            if 0 < unicode_cp < 0x10000:
                tounicode[cid] = chr(unicode_cp)

        return tounicode

    def build_cid_to_gid_map(self, font_dict: ps.Dict, glyphs_used: set[int],
                               cid_to_gid: dict[int, int] | None = None) -> bytes | None:
        """
        Build CIDToGIDMap stream bytes for PDF embedding.

        Format: array of 2-byte big-endian GID values, indexed by CID.
        bytes[2*CID : 2*CID+2] = GID for that CID.

        Args:
            font_dict: PostScript Type 0 font dictionary
            glyphs_used: Set of CID values used
            cid_to_gid: Optional pre-computed {cid: gid} mapping dict.
                         If None, calls get_cid_to_gid_dict() to compute it.

        Returns:
            bytes: CIDToGIDMap stream data, or None if unavailable
        """
        if cid_to_gid is None:
            cid_to_gid = self.get_cid_to_gid_dict(font_dict, glyphs_used)

        if not cid_to_gid:
            return None

        # Build binary CIDToGIDMap stream
        max_cid = max(cid_to_gid.keys())
        map_size = max_cid + 1

        result = bytearray(map_size * 2)  # Zeros = GID 0 (.notdef)
        for cid, gid in cid_to_gid.items():
            if cid < map_size:
                result[cid * 2] = (gid >> 8) & 0xFF
                result[cid * 2 + 1] = gid & 0xFF

        return bytes(result)

    def _build_cid_to_gid_from_glyph_directory(self, font_dict: ps.Dict,
                                                 font_data: bytes,
                                                 tables: dict,
                                                 glyphs_used: set[int]) -> dict[int, int]:
        """
        Build CID→GID mapping by matching GlyphDirectory entries against
        the loca/glyf tables in the sfnts.

        The GlyphDirectory was populated during PostScript font construction
        by extracting glyf data at specific GIDs. By matching the data back
        to the sfnts, we can recover the CID→GID relationship.

        Args:
            font_dict: PostScript Type 0 font dictionary
            font_data: Complete TrueType font binary
            tables: Parsed table directory
            glyphs_used: Set of CID values to map

        Returns:
            dict: {cid: gid} for matched entries
        """
        cidfont = self._get_cidfont(font_dict)
        if cidfont is None:
            return {}

        glyph_dir = cidfont.val.get(b'GlyphDirectory')
        if not glyph_dir or glyph_dir.TYPE != ps.T_DICT:
            return {}

        # Parse loca and glyf table locations
        loca_info = tables.get(b'loca')
        glyf_info = tables.get(b'glyf')
        head_info = tables.get(b'head')
        maxp_info = tables.get(b'maxp')

        if not loca_info or not glyf_info or not head_info:
            return {}

        glyf_offset = glyf_info[0]

        # Get indexToLocFormat from head table (offset 50)
        head_offset = head_info[0]
        if head_offset + 52 > len(font_data):
            return {}
        loc_format = int.from_bytes(
            font_data[head_offset + 50:head_offset + 52], 'big')

        # Get numGlyphs from maxp table
        num_glyphs = 0
        if maxp_info:
            mo = maxp_info[0]
            if mo + 6 <= len(font_data):
                num_glyphs = int.from_bytes(font_data[mo + 4:mo + 6], 'big')
        if num_glyphs == 0:
            return {}

        # Parse loca table to get GID → (offset, length) in glyf
        loca_offset = loca_info[0]
        gid_offsets = []

        if loc_format == 0:
            # Short format: uint16, multiply by 2
            for i in range(num_glyphs + 1):
                pos = loca_offset + i * 2
                if pos + 2 > len(font_data):
                    break
                val = int.from_bytes(font_data[pos:pos + 2], 'big')
                gid_offsets.append(val * 2)
        else:
            # Long format: uint32
            for i in range(num_glyphs + 1):
                pos = loca_offset + i * 4
                if pos + 4 > len(font_data):
                    break
                val = int.from_bytes(font_data[pos:pos + 4], 'big')
                gid_offsets.append(val)

        if len(gid_offsets) < 2:
            return {}

        # Build hash of glyf data → GID for quick lookup
        glyf_hash_to_gid = {}
        for gid in range(min(num_glyphs, len(gid_offsets) - 1)):
            start = glyf_offset + gid_offsets[gid]
            end = glyf_offset + gid_offsets[gid + 1]
            if start >= end or end > len(font_data):
                continue  # Empty glyph (space, .notdef, etc.)
            glyf_data = font_data[start:end]
            glyf_hash_to_gid[glyf_data] = gid

        # Match GlyphDirectory entries against glyf data
        cid_to_gid = {}
        for cid in glyphs_used:
            glyph_entry = glyph_dir.val.get(cid)
            if glyph_entry is None:
                continue
            if glyph_entry.TYPE != ps.T_STRING:
                continue

            entry_bytes = glyph_entry.byte_string()
            if isinstance(entry_bytes, str):
                entry_bytes = entry_bytes.encode('latin-1')

            if not entry_bytes:
                continue

            gid = glyf_hash_to_gid.get(entry_bytes)
            if gid is not None:
                cid_to_gid[cid] = gid

        return cid_to_gid

    def _parse_cmap_table(self, font_data: bytes,
                            cmap_offset: int) -> dict[int, int]:
        """
        Parse TrueType cmap table and return GID -> unicode codepoint mapping.

        Prefers platform 3 (Windows) / encoding 1 (Unicode BMP) subtable,
        falls back to platform 0 (Unicode) subtables.

        Args:
            font_data: Complete TrueType font binary
            cmap_offset: Offset of cmap table in font_data

        Returns:
            dict: {gid: unicode_codepoint}
        """
        if cmap_offset + 4 > len(font_data):
            return {}

        num_subtables = int.from_bytes(
            font_data[cmap_offset + 2:cmap_offset + 4], 'big')

        # Find best subtable: prefer (3,1) Windows Unicode BMP, then (0,*)
        best_offset = None
        best_priority = -1

        for i in range(num_subtables):
            rec_offset = cmap_offset + 4 + i * 8
            if rec_offset + 8 > len(font_data):
                break
            platform = int.from_bytes(font_data[rec_offset:rec_offset + 2], 'big')
            encoding = int.from_bytes(font_data[rec_offset + 2:rec_offset + 4], 'big')
            subtable_offset = int.from_bytes(font_data[rec_offset + 4:rec_offset + 8], 'big')

            priority = -1
            if platform == 3 and encoding == 1:
                priority = 10  # Windows Unicode BMP - best
            elif platform == 0:
                priority = 5  # Unicode platform - good fallback

            if priority > best_priority:
                best_priority = priority
                best_offset = cmap_offset + subtable_offset

        if best_offset is None:
            return {}

        # Parse the subtable
        if best_offset + 2 > len(font_data):
            return {}
        fmt = int.from_bytes(font_data[best_offset:best_offset + 2], 'big')

        if fmt == 4:
            return self._parse_cmap_format4(font_data, best_offset)
        elif fmt == 12:
            return self._parse_cmap_format12(font_data, best_offset)

        return {}

    def _parse_cmap_format4(self, font_data: bytes,
                              offset: int) -> dict[int, int]:
        """
        Parse cmap format 4 (segment-to-delta mapping) for BMP characters.

        Returns inverted mapping: {gid: unicode_codepoint}.

        Args:
            font_data: Complete TrueType font binary
            offset: Offset of format 4 subtable

        Returns:
            dict: {gid: unicode_codepoint}
        """
        if offset + 14 > len(font_data):
            return {}

        seg_count_x2 = int.from_bytes(font_data[offset + 6:offset + 8], 'big')
        seg_count = seg_count_x2 // 2

        # Arrays start after the header (14 bytes)
        end_code_start = offset + 14
        # +2 for reservedPad
        start_code_start = end_code_start + seg_count_x2 + 2
        id_delta_start = start_code_start + seg_count_x2
        id_range_offset_start = id_delta_start + seg_count_x2

        gid_to_unicode = {}

        for seg in range(seg_count):
            seg_off = seg * 2
            if (end_code_start + seg_off + 2 > len(font_data) or
                    start_code_start + seg_off + 2 > len(font_data) or
                    id_delta_start + seg_off + 2 > len(font_data) or
                    id_range_offset_start + seg_off + 2 > len(font_data)):
                break

            end_code = int.from_bytes(
                font_data[end_code_start + seg_off:end_code_start + seg_off + 2], 'big')
            start_code = int.from_bytes(
                font_data[start_code_start + seg_off:start_code_start + seg_off + 2], 'big')
            id_delta = struct.unpack('>h', font_data[
                id_delta_start + seg_off:id_delta_start + seg_off + 2])[0]
            id_range_offset = int.from_bytes(
                font_data[id_range_offset_start + seg_off:id_range_offset_start + seg_off + 2], 'big')

            if start_code == 0xFFFF:
                break

            for char_code in range(start_code, end_code + 1):
                if id_range_offset == 0:
                    gid = (char_code + id_delta) & 0xFFFF
                else:
                    # glyphIdArray index
                    glyph_offset = (id_range_offset_start + seg_off +
                                    id_range_offset +
                                    (char_code - start_code) * 2)
                    if glyph_offset + 2 > len(font_data):
                        continue
                    gid = int.from_bytes(
                        font_data[glyph_offset:glyph_offset + 2], 'big')
                    if gid != 0:
                        gid = (gid + id_delta) & 0xFFFF

                if gid != 0 and gid not in gid_to_unicode:
                    gid_to_unicode[gid] = char_code

        return gid_to_unicode

    def _parse_cmap_format12(self, font_data: bytes,
                               offset: int) -> dict[int, int]:
        """
        Parse cmap format 12 (segmented coverage) for full Unicode range.

        Args:
            font_data: Complete TrueType font binary
            offset: Offset of format 12 subtable

        Returns:
            dict: {gid: unicode_codepoint}
        """
        if offset + 16 > len(font_data):
            return {}

        num_groups = int.from_bytes(font_data[offset + 12:offset + 16], 'big')
        gid_to_unicode = {}

        for i in range(num_groups):
            grp_offset = offset + 16 + i * 12
            if grp_offset + 12 > len(font_data):
                break
            start_char = int.from_bytes(font_data[grp_offset:grp_offset + 4], 'big')
            end_char = int.from_bytes(font_data[grp_offset + 4:grp_offset + 8], 'big')
            start_gid = int.from_bytes(font_data[grp_offset + 8:grp_offset + 12], 'big')

            for j in range(end_char - start_char + 1):
                gid = start_gid + j
                char_code = start_char + j
                if gid != 0 and gid not in gid_to_unicode:
                    gid_to_unicode[gid] = char_code

        return gid_to_unicode

    def get_font_metrics(self, font_dict: ps.Dict) -> dict[str, object]:
        """
        Parse TrueType tables for font metrics needed by PDF FontDescriptor.

        Reads head, hhea, and OS/2 tables for ascent, descent, capHeight,
        stemV, and bbox. All values scaled to 1000-unit space.

        Args:
            font_dict: PostScript Type 0 font dictionary

        Returns:
            dict with keys: ascent, descent, cap_height, stem_v, bbox
        """
        font_data = self.get_sfnts_data(font_dict)
        if font_data is None:
            return self._default_metrics()

        tables = self._parse_table_directory(font_data)
        units_per_em = self._get_units_per_em(font_data, tables)
        scale = 1000.0 / units_per_em if units_per_em > 0 else 1.0

        metrics = self._default_metrics()

        # Parse head table for bbox
        head_info = tables.get(b'head')
        if head_info:
            ho = head_info[0]
            if ho + 54 <= len(font_data):
                x_min = struct.unpack('>h', font_data[ho + 36:ho + 38])[0]
                y_min = struct.unpack('>h', font_data[ho + 38:ho + 40])[0]
                x_max = struct.unpack('>h', font_data[ho + 40:ho + 42])[0]
                y_max = struct.unpack('>h', font_data[ho + 42:ho + 44])[0]
                metrics['bbox'] = [
                    int(round(x_min * scale)),
                    int(round(y_min * scale)),
                    int(round(x_max * scale)),
                    int(round(y_max * scale)),
                ]

        # Parse hhea table for ascent/descent
        hhea_info = tables.get(b'hhea')
        if hhea_info:
            ho = hhea_info[0]
            if ho + 8 <= len(font_data):
                ascent = struct.unpack('>h', font_data[ho + 4:ho + 6])[0]
                descent = struct.unpack('>h', font_data[ho + 6:ho + 8])[0]
                metrics['ascent'] = int(round(ascent * scale))
                metrics['descent'] = int(round(descent * scale))

        # Parse OS/2 table for capHeight and stemV
        os2_info = tables.get(b'OS/2')
        if os2_info:
            ho = os2_info[0]
            tbl_len = os2_info[1]
            # sCapHeight is at offset 88 (version >= 2)
            if tbl_len >= 90 and ho + 90 <= len(font_data):
                cap_height = struct.unpack('>h', font_data[ho + 88:ho + 90])[0]
                if cap_height > 0:
                    metrics['cap_height'] = int(round(cap_height * scale))

        return metrics

    def _default_metrics(self) -> dict[str, object]:
        """Return default font metrics."""
        return {
            'ascent': 800,
            'descent': -200,
            'cap_height': 700,
            'stem_v': 80,
            'bbox': [0, -200, 1000, 800],
        }

    def _get_units_per_em(self, font_data: bytes, tables: dict) -> int:
        """Get unitsPerEm from head table."""
        head_info = tables.get(b'head')
        if not head_info:
            return 1000
        ho = head_info[0]
        if ho + 20 <= len(font_data):
            return int.from_bytes(font_data[ho + 18:ho + 20], 'big')
        return 1000

    def get_default_width(self, font_dict: ps.Dict) -> int:
        """
        Get default glyph width for the /DW entry.

        Uses GID 0 (.notdef) width or falls back to 1000.

        Args:
            font_dict: PostScript Type 0 font dictionary

        Returns:
            int: Default width in 1000-unit space
        """
        font_data = self.get_sfnts_data(font_dict)
        if font_data is None:
            return 1000

        tables = self._parse_table_directory(font_data)
        units_per_em = self._get_units_per_em(font_data, tables)
        scale = 1000.0 / units_per_em if units_per_em > 0 else 1.0

        hhea_info = tables.get(b'hhea')
        hmtx_info = tables.get(b'hmtx')
        if not hhea_info or not hmtx_info:
            return 1000

        hmtx_offset = hmtx_info[0]
        # GID 0 width
        if hmtx_offset + 2 <= len(font_data):
            w = int.from_bytes(font_data[hmtx_offset:hmtx_offset + 2], 'big')
            return int(round(w * scale))

        return 1000

    def get_cid_system_info(self, font_dict: ps.Dict) -> tuple[str, str, int]:
        """
        Extract CIDSystemInfo (Registry/Ordering/Supplement) from the CIDFont.

        Args:
            font_dict: PostScript Type 0 font dictionary

        Returns:
            tuple: (registry, ordering, supplement) as (str, str, int)
        """
        cidfont = self._get_cidfont(font_dict)
        if cidfont is None:
            return ('Adobe', 'Identity', 0)

        cid_system_info = cidfont.val.get(b'CIDSystemInfo')
        if not cid_system_info or cid_system_info.TYPE != ps.T_DICT:
            return ('Adobe', 'Identity', 0)

        registry = cid_system_info.val.get(b'Registry')
        ordering = cid_system_info.val.get(b'Ordering')
        supplement = cid_system_info.val.get(b'Supplement')

        reg_str = 'Adobe'
        if registry and registry.TYPE == ps.T_STRING:
            b = registry.byte_string()
            if isinstance(b, str):
                reg_str = b
            else:
                reg_str = b.decode('latin-1', errors='replace')

        ord_str = 'Identity'
        if ordering and ordering.TYPE == ps.T_STRING:
            b = ordering.byte_string()
            if isinstance(b, str):
                ord_str = b
            else:
                ord_str = b.decode('latin-1', errors='replace')

        sup_val = 0
        if supplement and supplement.TYPE in ps.NUMERIC_TYPES:
            sup_val = int(supplement.val)

        return (reg_str, ord_str, sup_val)


def generate_cid_tounicode_cmap(tounicode_map: dict[int, str],
                                font_name: str = 'Unknown') -> bytes:
    """
    Generate a ToUnicode CMap stream for CID font PDF embedding.

    Uses 2-byte codespace <0000>-<FFFF> for CID values.

    Args:
        tounicode_map: dict mapping cid (int) -> unicode_string (str)
        font_name: Font name for the CMap

    Returns:
        bytes: ToUnicode CMap stream content
    """
    lines = [
        b'/CIDInit /ProcSet findresource begin',
        b'12 dict begin',
        b'begincmap',
        b'/CIDSystemInfo <<',
        b'  /Registry (Adobe)',
        b'  /Ordering (UCS)',
        b'  /Supplement 0',
        b'>> def',
        f'/CMapName /{font_name}-UCS def'.encode('latin-1'),
        b'/CMapType 2 def',
        b'1 begincodespacerange',
        b'<0000> <FFFF>',
        b'endcodespacerange',
    ]

    # Build character mappings
    mappings = []
    for cid, unicode_str in sorted(tounicode_map.items()):
        hex_code = f'{cid:04X}'
        unicode_hex = ''.join(f'{ord(c):04X}' for c in unicode_str)
        mappings.append((hex_code, unicode_hex))

    # Emit in batches of 100 (PDF limit per block)
    for i in range(0, len(mappings), 100):
        batch = mappings[i:i + 100]
        lines.append(f'{len(batch)} beginbfchar'.encode())
        for hex_code, unicode_hex in batch:
            lines.append(f'<{hex_code}> <{unicode_hex}>'.encode())
        lines.append(b'endbfchar')

    lines.extend([
        b'endcmap',
        b'CMapName currentdict /CMap defineresource pop',
        b'end',
        b'end',
    ])

    return b'\n'.join(lines)
