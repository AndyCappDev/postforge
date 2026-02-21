# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
DCT parameter parsing and validation for PostScript DCT filters.

This module handles parsing and validation of PostScript dictionary parameters
for DCTDecode and DCTEncode filters as specified in PLRM Section 3.17.
"""

from . import types as ps
from . import error as ps_error


class DCTParameters:
    """Container for validated DCT filter parameters"""

    def __init__(self) -> None:
        # Required parameters for DCTEncode
        self.columns: int | None = None
        self.rows: int | None = None
        self.colors: int | None = None

        # Optional parameters
        self.hsample: list[int] | None = None
        self.vsample: list[int] | None = None
        self.quant_tables: list[list[float]] | None = None
        self.qfactor: float = 1.0
        self.huff_tables: list[list[int]] | None = None
        self.color_transform: int | None = None


class DCTParameterParser:
    """PostScript DCT parameter parsing and validation"""

    @staticmethod
    def parse_encode_params(params: ps.Dict | None, ctxt: ps.Context) -> DCTParameters | None:
        """Parse and validate DCTEncode parameters per PLRM Table 3.17.

        Args:
            params: PostScript Dict object containing parameters
            ctxt: PostScript execution context

        Returns:
            DCTParameters object with validated parameters

        Raises:
            PostScript errors for invalid parameters
        """
        if not params:
            return ps_error.e(ctxt, ps_error.RANGECHECK, "DCTEncode")

        if not hasattr(params, 'val'):
            return ps_error.e(ctxt, ps_error.TYPECHECK, "DCTEncode")

        param_dict = params.val
        result = DCTParameters()

        # Parse REQUIRED parameters
        result.columns = DCTParameterParser._parse_required_int(
            param_dict, b'Columns', "DCTEncode", ctxt)
        if isinstance(result.columns, tuple):  # Error result
            return result.columns

        result.rows = DCTParameterParser._parse_required_int(
            param_dict, b'Rows', "DCTEncode", ctxt)
        if isinstance(result.rows, tuple):  # Error result
            return result.rows

        result.colors = DCTParameterParser._parse_required_int(
            param_dict, b'Colors', "DCTEncode", ctxt)
        if isinstance(result.colors, tuple):  # Error result
            return result.colors

        # Validate required parameter ranges
        if result.columns <= 0:
            return ps_error.e(ctxt, ps_error.RANGECHECK, "DCTEncode")
        if result.rows <= 0:
            return ps_error.e(ctxt, ps_error.RANGECHECK, "DCTEncode")
        if result.colors not in (1, 2, 3, 4):
            return ps_error.e(ctxt, ps_error.RANGECHECK, "DCTEncode")

        # Parse OPTIONAL parameters
        result.hsample = DCTParameterParser._parse_sampling_array(
            param_dict, b'HSamples', result.colors, ctxt)
        result.vsample = DCTParameterParser._parse_sampling_array(
            param_dict, b'VSamples', result.colors, ctxt)

        result.qfactor = DCTParameterParser._parse_optional_number(
            param_dict, b'QFactor', 1.0, ctxt)
        if result.qfactor <= 0:
            return ps_error.e(ctxt, ps_error.RANGECHECK, "DCTEncode")

        result.color_transform = DCTParameterParser._parse_optional_int(
            param_dict, b'ColorTransform', None, ctxt)
        if result.color_transform is not None and result.color_transform not in (0, 1):
            return ps_error.e(ctxt, ps_error.RANGECHECK, "DCTEncode")

        # Validate sampling factor constraint (PLRM: sum ≤ 10)
        if result.hsample and result.vsample:
            total = sum(h * v for h, v in zip(result.hsample, result.vsample))
            if total > 10:
                return ps_error.e(ctxt, ps_error.RANGECHECK, "DCTEncode")

        # Parse quantization and Huffman tables (complex validation)
        result.quant_tables = DCTParameterParser._parse_quant_tables(
            param_dict, result.colors, ctxt)
        result.huff_tables = DCTParameterParser._parse_huff_tables(
            param_dict, result.colors, ctxt)

        return result

    @staticmethod
    def parse_decode_params(params: ps.Dict | None, ctxt: ps.Context) -> DCTParameters | None:
        """Parse and validate DCTDecode parameters.

        Args:
            params: PostScript Dict object containing parameters (usually empty)
            ctxt: PostScript execution context

        Returns:
            DCTParameters object with validated parameters

        Note:
            DCTDecode usually requires no parameters as JPEG contains
            decoding information. Only ColorTransform may be needed.
        """
        result = DCTParameters()

        if params and hasattr(params, 'val'):
            param_dict = params.val

            result.color_transform = DCTParameterParser._parse_optional_int(
                param_dict, b'ColorTransform', None, ctxt)
            if isinstance(result.color_transform, tuple):  # Error result
                return result.color_transform

            if result.color_transform is not None and result.color_transform not in (0, 1):
                return ps_error.e(ctxt, ps_error.RANGECHECK, "DCTDecode")

        return result

    @staticmethod
    def _parse_required_int(param_dict: dict, key: bytes, operator: str, ctxt: ps.Context) -> int | None:
        """Parse required integer parameter"""
        if key not in param_dict:
            return ps_error.e(ctxt, ps_error.RANGECHECK, operator)

        param_obj = param_dict[key]
        if param_obj.TYPE not in (ps.T_INT, ps.T_REAL):
            return ps_error.e(ctxt, ps_error.TYPECHECK, operator)

        return int(param_obj.val)

    @staticmethod
    def _parse_optional_int(param_dict: dict, key: bytes, default: int | None, ctxt: ps.Context) -> int | None:
        """Parse optional integer parameter"""
        if key not in param_dict:
            return default

        param_obj = param_dict[key]
        if param_obj.TYPE not in (ps.T_INT, ps.T_REAL):
            return ps_error.e(ctxt, ps_error.TYPECHECK, key.decode())

        return int(param_obj.val)

    @staticmethod
    def _parse_optional_number(param_dict: dict, key: bytes, default: float, ctxt: ps.Context) -> float | None:
        """Parse optional number (int or real) parameter"""
        if key not in param_dict:
            return default

        param_obj = param_dict[key]
        if param_obj.TYPE not in (ps.T_INT, ps.T_REAL):
            return ps_error.e(ctxt, ps_error.TYPECHECK, key.decode())

        return float(param_obj.val)

    @staticmethod
    def _parse_sampling_array(param_dict: dict, key: bytes, colors: int, ctxt: ps.Context) -> list[int] | None:
        """Parse HSamples or VSamples array parameter

        Args:
            param_dict: Parameter dictionary
            key: Parameter key (b'HSamples' or b'VSamples')
            colors: Number of color components
            ctxt: Execution context

        Returns:
            List of sampling factors or None if not specified
        """
        if key not in param_dict:
            # Default: all components sampled at same rate
            return [1] * colors

        param_obj = param_dict[key]
        if param_obj.TYPE != ps.T_ARRAY:
            return ps_error.e(ctxt, ps_error.TYPECHECK, key.decode())

        array_val = param_obj.val
        if len(array_val) != colors:
            return ps_error.e(ctxt, ps_error.RANGECHECK, key.decode())

        sampling_factors = []
        for i, element in enumerate(array_val):
            if element.TYPE not in (ps.T_INT, ps.T_REAL):
                return ps_error.e(ctxt, ps_error.TYPECHECK, key.decode())

            factor = int(element.val)
            if factor not in (1, 2, 3, 4):
                return ps_error.e(ctxt, ps_error.RANGECHECK, key.decode())

            sampling_factors.append(factor)

        return sampling_factors

    @staticmethod
    def _parse_quant_tables(param_dict: dict, colors: int, ctxt: ps.Context) -> list[list[float]] | None:
        """Parse QuantTables parameter

        Args:
            param_dict: Parameter dictionary
            colors: Number of color components
            ctxt: Execution context

        Returns:
            List of quantization tables or None for default tables

        PLRM: Array of Colors quantization tables. Each table must contain
        64 numbers organized according to zigzag pattern.
        """
        if b'QuantTables' not in param_dict:
            return None  # Use default tables

        param_obj = param_dict[b'QuantTables']
        if param_obj.TYPE != ps.T_ARRAY:
            return ps_error.e(ctxt, ps_error.TYPECHECK, "QuantTables")

        tables_array = param_obj.val
        if len(tables_array) != colors:
            return ps_error.e(ctxt, ps_error.RANGECHECK, "QuantTables")

        quant_tables = []
        for i, table_obj in enumerate(tables_array):
            if table_obj.TYPE == ps.T_ARRAY:
                # Array of numbers
                table_elements = table_obj.val
                if len(table_elements) != 64:
                    return ps_error.e(ctxt, ps_error.RANGECHECK, "QuantTables")

                table_values = []
                for element in table_elements:
                    if element.TYPE not in (ps.T_INT, ps.T_REAL):
                        return ps_error.e(ctxt, ps_error.TYPECHECK, "QuantTables")
                    table_values.append(float(element.val))

                quant_tables.append(table_values)

            elif table_obj.TYPE == ps.T_STRING:
                # String with 64 bytes (0-255 values)
                table_string = table_obj.byte_string()
                if len(table_string) != 64:
                    return ps_error.e(ctxt, ps_error.RANGECHECK, "QuantTables")

                table_values = [float(b) for b in table_string]
                quant_tables.append(table_values)

            else:
                return ps_error.e(ctxt, ps_error.TYPECHECK, "QuantTables")

        return quant_tables

    @staticmethod
    def _parse_huff_tables(param_dict: dict, colors: int, ctxt: ps.Context) -> list[list[int]] | None:
        """Parse HuffTables parameter

        Args:
            param_dict: Parameter dictionary
            colors: Number of color components
            ctxt: Execution context

        Returns:
            List of Huffman tables or None for default tables

        PLRM: Array of 2xColors encoding tables. First 16 values specify
        number of codes of each length 1-16 bits. Remaining values are
        symbols corresponding to each code.
        """
        if b'HuffTables' not in param_dict:
            return None  # Use default tables

        param_obj = param_dict[b'HuffTables']
        if param_obj.TYPE != ps.T_ARRAY:
            return ps_error.e(ctxt, ps_error.TYPECHECK, "HuffTables")

        tables_array = param_obj.val
        expected_count = 2 * colors  # DC and AC table for each component
        if len(tables_array) != expected_count:
            return ps_error.e(ctxt, ps_error.RANGECHECK, "HuffTables")

        huff_tables = []
        for i, table_obj in enumerate(tables_array):
            if table_obj.TYPE == ps.T_ARRAY:
                # Array of numbers
                table_elements = table_obj.val
                table_values = []
                for element in table_elements:
                    if element.TYPE not in (ps.T_INT, ps.T_REAL):
                        return ps_error.e(ctxt, ps_error.TYPECHECK, "HuffTables")
                    value = int(element.val)
                    if not (0 <= value <= 255):
                        return ps_error.e(ctxt, ps_error.RANGECHECK, "HuffTables")
                    table_values.append(value)

                # Validate Huffman table structure (first 16 are bit counts)
                if len(table_values) < 16:
                    return ps_error.e(ctxt, ps_error.RANGECHECK, "HuffTables")

                huff_tables.append(table_values)

            elif table_obj.TYPE == ps.T_STRING:
                # String with integer values (0-255)
                table_string = table_obj.byte_string()
                if len(table_string) < 16:
                    return ps_error.e(ctxt, ps_error.RANGECHECK, "HuffTables")

                table_values = list(table_string)
                huff_tables.append(table_values)

            else:
                return ps_error.e(ctxt, ps_error.TYPECHECK, "HuffTables")

        return huff_tables