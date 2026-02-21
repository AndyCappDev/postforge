# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

# PostScript Filter System Implementation
#
# Implements PostScript Language Reference Manual Section 3.8.4 filter functionality
# Based on Filter_Architecture_Analysis.md delegation pattern

import copy
from ..core import types as ps
from ..core import error as ps_error
from . import control as ps_control


# Filter Implementation Base Classes

class FilterBase:
    """Abstract base class for all PostScript filters"""

    def __init__(self, data_source: DataSource, params: dict | ps.Dict | None = None) -> None:
        self.data_source = data_source
        self.params = params or {}
        self.closed = False
        self.buffer = bytearray()
        self.eof_reached = False
        self.eod_reached = False  # End-of-data marker seen (for filters with EOD markers)

    def read_data(self, ctxt: ps.Context, max_bytes: int | None = None) -> bytes:
        """Read and decode data from underlying source"""
        raise NotImplementedError("Subclasses must implement read_data")

    def write_data(self, ctxt: ps.Context, data: bytes) -> None:
        """Encode and write data to underlying target"""
        raise NotImplementedError("Subclasses must implement write_data")

    def close(self, ctxt: ps.Context) -> None:
        """Close **filter** and underlying source/target"""
        if not self.closed:
            self.closed = True
            # DataSource wraps the actual source, so close the wrapped source
            if hasattr(self.data_source, 'source') and hasattr(self.data_source.source, 'close'):
                self.data_source.source.close()  # Regular File.close() takes no params


class DataSource:
    """Abstraction for file/string/procedure data sources"""

    def __init__(self, source_obj: ps.File | ps.String | ps.Array, ctxt: ps.Context) -> None:
        self.source = source_obj
        self.exhausted = False
        self.string_data = None  # Cache for string data
        self.string_position = 0  # Current position in string
        self.putback_buffer = bytearray()  # Buffer for pushed-back bytes

    def putback(self, data: bytes | bytearray) -> None:
        """Push bytes back to be read again on next read_data call.

        This is essential for filters like ASCII85Decode that may read past
        their end-of-data marker. The leftover bytes need to be available
        for subsequent reads from the underlying source.

        When the underlying source is a FilterFile, we propagate the **putback**
        directly to the FilterFile's buffer so the bytes survive even if this
        DataSource is discarded.
        """
        if data:
            # Propagate putback to underlying FilterFile if possible
            # This ensures bytes survive when filter chains are discarded
            if hasattr(self.source, 'putback'):
                self.source.putback(data)
            else:
                # Fall back to local buffer for regular Files/Strings
                self.putback_buffer = bytearray(data) + self.putback_buffer
            # If we had marked exhausted but now have data, clear that flag
            self.exhausted = False

    def read_data(self, ctxt: ps.Context, max_bytes: int | None = None) -> bytes:
        """Read data based on source type"""
        # First return any data from putback buffer
        if self.putback_buffer:
            if max_bytes is None:
                result = bytes(self.putback_buffer)
                self.putback_buffer = bytearray()
                return result
            else:
                result = bytes(self.putback_buffer[:max_bytes])
                self.putback_buffer = self.putback_buffer[max_bytes:]
                return result

        if self.exhausted:
            return b''

        if isinstance(self.source, ps.File):
            # FilterFile sources have their own buffering and EOD handling,
            # so bulk read is safe and avoids per-byte overhead.
            # Regular File sources (e.g. currentfile) must NOT be bulk-read
            # because filters rely on reading small amounts to stop precisely
            # at EOD markers — excess bytes would be consumed from the stream
            # and lost.
            if hasattr(self.source, 'filter'):
                bytes_to_read = max_bytes if max_bytes is not None else 1024
                data = self.source.read_bulk(ctxt, bytes_to_read)
                if not data:
                    self.exhausted = True
                    return b''
                return data
            else:
                # Regular File — return 1 byte to let filters control consumption
                data = self.source.read(ctxt)
                if data is None:
                    self.exhausted = True
                    return b''
                return bytes([data]) if isinstance(data, int) else data
            
        elif isinstance(self.source, ps.String):
            # String source - provide streaming interface
            if self.string_data is None:
                self.string_data = self.source.byte_string()
            
            if self.string_position >= len(self.string_data):
                self.exhausted = True
                return b''
            
            # Return requested amount of data
            end_pos = len(self.string_data)
            if max_bytes is not None:
                end_pos = min(self.string_position + max_bytes, len(self.string_data))
            
            result = self.string_data[self.string_position:end_pos]
            self.string_position = end_pos
            
            if self.string_position >= len(self.string_data):
                self.exhausted = True
                
            return result
            
        elif isinstance(self.source, ps.Array) and self.source.attrib == ps.ATTRIB_EXEC:
            # Procedure source - call procedure to get string data (PLRM Section 3.13.1)
            if self.exhausted:
                return b''

            # Push hard return and procedure onto execution stack
            ctxt.e_stack.append(ps.HardReturn())
            ctxt.e_stack.append(copy.copy(self.source))

            # Execute the procedure
            result = ps_control.exec_exec(ctxt, ctxt.o_stack, ctxt.e_stack)
            # Note: exec_exec may return None on success, so we check for actual error conditions
            if result is not None and result != (True, None, None, None):
                # Procedure execution failed
                self.exhausted = True
                return b''

            # The procedure should have left exactly one string on the operand stack (PLRM Section 3.13.1)
            if len(ctxt.o_stack) == 0:
                raise IOError("Procedure data source did not return a value")

            string_result = ctxt.o_stack.pop()

            # PLRM: Procedure data sources must return a string
            if string_result.TYPE != ps.T_STRING:
                raise TypeError("Procedure data source must return a string")

            # Get the byte data from the string (returns Python bytes object)
            string_data = string_result.byte_string()

            # If string is empty, mark as exhausted (PLRM: zero-length string indicates no more data)
            if len(string_data) == 0:  # This calls Python's len() on bytes object
                self.exhausted = True
                return b''

            return string_data
            
        else:
            raise TypeError(f"Unsupported data source type: {type(self.source)}")
    
    def at_eof(self) -> bool:
        return self.exhausted


class _FilterReadState:
    """Shared mutable read state for FilterFile.

    PostScript files are composite (reference) objects — multiple copies of
    a FilterFile created by the execution engine's name-lookup copy (see
    exec_exec line 692) must all share the same read position, buffer, and
    unread state.  Wrapping this state in a single object that is shared
    across copies preserves PostScript reference semantics.
    """
    __slots__ = ('buffer', 'buf_pos', 'last_read_byte', 'has_unread_byte')

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.buf_pos = 0
        self.last_read_byte = None
        self.has_unread_byte = False


class FilterFile(ps.File):
    """File object representing a filtered stream - implements delegation pattern"""

    def __init__(self, underlying_source: ps.File | ps.String | ps.Array, filter_impl: FilterBase, ctxt: ps.Context, is_input: bool = True) -> None:
        # Initialize as File with appropriate attributes
        mode = "r" if is_input else "w"

        super().__init__(
            ctxt.id,
            b"(FilterFile)",
            mode,
            access=ps.ACCESS_READ_ONLY if is_input else ps.ACCESS_WRITE_ONLY,
            is_global=ctxt.vm_alloc_mode
        )

        self.underlying = underlying_source  # File/String/Procedure or another FilterFile
        self.filter = filter_impl           # ASCIIHexDecode, ASCII85Decode, etc.
        self.is_input = is_input
        self._state = _FilterReadState()    # Shared mutable read state
        self.is_real_file = False          # FilterFile is not a real disk file
        self.ctxt = ctxt                   # Store context for later use

    def __copy__(self) -> FilterFile:
        """Optimized copy for FilterFile - includes **filter**-specific attributes.

        The _state object is shared (not copied) so that all copies of the same
        FilterFile see identical buffer/unread state — matching PostScript's
        reference semantics for composite objects.
        """
        new_obj = FilterFile.__new__(FilterFile)
        # File attrs
        new_obj.val = self.val
        new_obj.access = self.access
        new_obj.attrib = self.attrib
        new_obj.is_composite = self.is_composite
        new_obj.is_global = self.is_global
        new_obj.line_num = self.line_num
        new_obj.name = self.name
        new_obj.mode = self.mode
        new_obj.created = self.created
        new_obj.ctxt_id = self.ctxt_id
        new_obj.is_real_file = self.is_real_file
        # FilterFile attrs — shared references for reference semantics
        new_obj.underlying = self.underlying
        new_obj.filter = self.filter
        new_obj.is_input = self.is_input
        new_obj._state = self._state        # SHARED — critical for reference semantics
        new_obj.ctxt = self.ctxt
        return new_obj

    def filename(self) -> str:
        """Return a descriptive name for this filtered file."""
        # Try to get the underlying file's name if it has one
        if hasattr(self.underlying, 'filename') and callable(self.underlying.filename):
            underlying_name = self.underlying.filename()
        elif hasattr(self.underlying, 'name'):
            underlying_name = self.underlying.name
        else:
            underlying_name = "<unknown>"
        
        # Return a descriptive name showing it's filtered
        filter_name = getattr(self.filter, '__class__', type(self.filter)).__name__
        return f"<{filter_name} filter of {underlying_name}>"
        
    def _compact_buffer(self) -> None:
        """Discard consumed bytes from the front of the buffer."""
        s = self._state
        if s.buf_pos > 0:
            del s.buffer[:s.buf_pos]
            s.buf_pos = 0

    def reset(self) -> None:
        """Discard all buffered data for this **filter** file.

        Used by the **resetfile** operator to **clear** internal buffers without
        consuming additional data from the underlying source.
        """
        s = self._state
        s.buffer = bytearray()
        s.buf_pos = 0
        s.last_read_byte = None
        s.has_unread_byte = False

    def putback(self, data: bytes | bytearray) -> None:
        """Push bytes back to be read again on next read call.

        For pass-through filters (SubFileDecode), always propagate the putback
        through to the underlying source.  This is critical for inline images
        in PDF content streams: the filter chain may use a different FilterFile
        than the one on the exec stack, but both share the same underlying
        File/Run.  Propagating ensures the bytes reach that shared source and
        are available when the interpreter continues reading tokens.

        For transforming filters (Flate, LZW, etc.) that have finished
        (EOF/EOD reached), also propagate — the bytes are raw stream data
        that belongs to the underlying source.

        Otherwise, store in the local buffer for re-reading.
        """
        if data:
            has_ds = (hasattr(self.filter, 'data_source') and
                      hasattr(self.filter.data_source, 'putback'))
            # SubFileDecode is a pass-through (no data transformation),
            # so putback bytes can always safely propagate to the source
            is_passthrough = isinstance(self.filter, SubFileDecodeFilter)
            filter_done = self.filter.eof_reached or self.filter.eod_reached
            if has_ds and (is_passthrough or filter_done):
                self.filter.data_source.putback(data)
                # For SubFileDecode in byte-count mode, adjust the counter
                # to avoid double-counting when these bytes are re-read.
                # The bytes were already counted on first read; pushing them
                # back and re-reading would increment byte_count again.
                if (is_passthrough and hasattr(self.filter, 'byte_count')
                        and len(self.filter.eod_string) == 0):
                    self.filter.byte_count = max(
                        0, self.filter.byte_count - len(data))
                    if (self.filter.eof_reached
                            and self.filter.byte_count < self.filter.eod_count):
                        self.filter.eof_reached = False
            else:
                # Filter still active and transforms data — store locally
                self._compact_buffer()
                self._state.buffer = bytearray(data) + self._state.buffer

    def unread(self) -> None:
        """Push the last read byte back to be read again.

        This is essential for the tokenizer which may need to look ahead
        and then put back characters it doesn't consume.
        """
        s = self._state
        if s.last_read_byte is not None and not s.has_unread_byte:
            s.has_unread_byte = True

    def read(self, ctxt: ps.Context) -> int | None:
        """Read single byte through **filter** delegation chain"""
        s = self._state
        # Return unread byte if available
        if s.has_unread_byte:
            s.has_unread_byte = False
            return s.last_read_byte

        # If we have buffered data, return it first
        if s.buf_pos < len(s.buffer):
            b = s.buffer[s.buf_pos]
            s.buf_pos += 1
            # Compact when the consumed prefix gets large
            if s.buf_pos > 4096:
                self._compact_buffer()
            s.last_read_byte = b  # Store for potential unread
            return b

        # Buffer exhausted — reset
        s.buffer = bytearray()
        s.buf_pos = 0

        # Read through the filter (request 1 byte; filters may return more)
        try:
            data = self.filter.read_data(ctxt, 1)
            if data:
                if len(data) > 1:
                    s.buffer.extend(data[1:])
                s.last_read_byte = data[0]  # Store for potential unread
                return data[0]
            else:
                return None  # EOF
        except (TypeError, IOError):
            raise
        except Exception:
            raise

    def read_bulk(self, ctxt: ps.Context, count: int) -> bytes:
        """Read up to *count* bytes efficiently.  Returns bytes (may be shorter at EOF)."""
        s = self._state
        result = bytearray()

        # 1. Drain any buffered bytes first
        buffered = len(s.buffer) - s.buf_pos
        if buffered > 0:
            take = min(buffered, count)
            result.extend(s.buffer[s.buf_pos:s.buf_pos + take])
            s.buf_pos += take
            if s.buf_pos >= len(s.buffer):
                s.buffer = bytearray()
                s.buf_pos = 0
            if len(result) >= count:
                return bytes(result)

        # 2. Read remaining directly from the filter in large chunks
        remaining = count - len(result)
        while remaining > 0:
            try:
                data = self.filter.read_data(ctxt, remaining)
            except (TypeError, IOError):
                raise
            except Exception:
                raise
            if not data:
                break  # EOF
            result.extend(data)
            remaining = count - len(result)

        return bytes(result)
    
    def write(self, ctxt: ps.Context, byte_val: int) -> None:
        """Write single byte through **filter** delegation chain"""
        if self.is_input:
            return ps_error.e(ctxt, ps_error.IOERROR, "write")
            
        try:
            self.filter.write_data(ctxt, bytes([byte_val]))
        except Exception as e:
            return ps_error.e(ctxt, ps_error.IOERROR, "write")
    
    def close(self) -> None:
        """Close **filter** and underlying source"""
        if not hasattr(self, 'closed'):
            self.closed = False
        if not self.closed:
            self.filter.close(self.ctxt)  # Pass stored context to filter
            # Don't call super().close() because FilterFile has no real file handle
            self.closed = True


# Core Filter Operator Implementation

def ps_filter(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    source/target param1 … paramn filtername **filter** file

    creates and returns a filtered file (see Sections 3.8.4, "Filters," and 3.13, "Filtered
    Files Details").
    The first operand specifies the underlying data source or data target that the **filter**
    is to read or write. It can be a file, a procedure, or a string. 
    The dict operand contains additional parameters that control how the **filter** is to
    operate. It can be omitted whenever all dictionary-supplied parameters have their
    default values for the given **filter**. The operands param1 through paramn are additional
    parameters that some filters require as operands rather than in dict; most
    filters do not require these operands. The number and types of parameters specified
    in dict or as operands depends on the **filter** name.
    
    PLRM Section 8.2, Page 590
           datatgt dict param1 … paramn filtername **filter** → file
    **Errors**: **invalidaccess**, **ioerror**, **limitcheck**, **rangecheck**, 
                          stackunderflow, typecheck, undefined
    """
    # STEP 1: COMPLETE VALIDATION WITHOUT POPPING

    # 1. STACKUNDERFLOW - Check minimum stack depth (datasrc/datatgt + filtername)
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, "filter")

    # 2. TYPECHECK - Validate filter name (top of stack) is Name object
    if ostack[-1].TYPE != ps.T_NAME:
        return ps_error.e(ctxt, ps_error.TYPECHECK, "filter")

    filter_name_obj = ostack[-1]  # Don't pop yet
    filter_name = filter_name_obj.val

    # 3. UNDEFINED - Validate filter name is recognized (PLRM Section 3.8.4)
    valid_filters = {
        b'ASCIIHexEncode', b'ASCIIHexDecode',
        b'ASCII85Encode', b'ASCII85Decode',
        b'LZWEncode', b'LZWDecode',
        b'FlateEncode', b'FlateDecode',
        b'RunLengthEncode', b'RunLengthDecode',
        b'CCITTFaxEncode', b'CCITTFaxDecode',
        b'DCTEncode', b'DCTDecode',
        b'NullEncode', b'SubFileDecode',
        b'ReusableStreamDecode'
    }
    if filter_name not in valid_filters:
        return ps_error.e(ctxt, ps_error.UNDEFINED, "filter")

    # 4. Validate special filter parameter requirements
    special_params = {}
    expected_stack_consumption = 1  # filter name

    # SubFileDecode: datasrc EODCount EODString /SubFileDecode filter
    if filter_name == b'SubFileDecode' and not (len(ostack) >= 2 and ostack[-2].TYPE == ps.T_DICT):
        # STACKUNDERFLOW - Check stack depth for SubFileDecode parameters
        if len(ostack) < 4:  # datasrc + EODCount + EODString + filtername
            return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, "filter")

        # TYPECHECK - Validate EODString (string) at position -2
        if ostack[-2].TYPE != ps.T_STRING:
            return ps_error.e(ctxt, ps_error.TYPECHECK, "filter")

        # TYPECHECK - Validate EODCount (integer) at position -3
        if ostack[-3].TYPE != ps.T_INT:
            return ps_error.e(ctxt, ps_error.TYPECHECK, "filter")

        expected_stack_consumption = 3  # EODCount + EODString + filtername

    # 5. Validate data source/target at correct position
    data_source_pos = -(expected_stack_consumption + 1)
    if len(ostack) < expected_stack_consumption + 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, "filter")

    data_source_obj = ostack[data_source_pos]

    # 6. Validate optional dictionary parameter BEFORE popping anything
    dict_param_pos = -(expected_stack_consumption + 1)

    has_dict_param = (len(ostack) >= expected_stack_consumption + 2 and
                      dict_param_pos < -1 and
                      ostack[dict_param_pos].TYPE == ps.T_DICT)

    if has_dict_param:
        # INVALIDACCESS - Check dictionary access permission
        if ostack[dict_param_pos].access < ps.ACCESS_READ_ONLY:
            return ps_error.e(ctxt, ps_error.INVALIDACCESS, "filter")

    # 7. Validate filter-specific requirements BEFORE popping operands
    if filter_name == b'DCTEncode':
        # DCTEncode requires parameters at filter creation time
        if not has_dict_param:
            return ps_error.e(ctxt, ps_error.RANGECHECK, "filter")

        # Check dictionary content for required parameters and validate values
        dict_obj = ostack[dict_param_pos]
        param_dict = dict_obj.val
        if (b'Columns' not in param_dict or b'Rows' not in param_dict or
            b'Colors' not in param_dict):
            return ps_error.e(ctxt, ps_error.RANGECHECK, "filter")

        # Validate parameter values
        try:
            columns_obj = param_dict[b'Columns']
            rows_obj = param_dict[b'Rows']
            colors_obj = param_dict[b'Colors']

            # Extract values and validate types
            columns_val = columns_obj.val if hasattr(columns_obj, 'val') else columns_obj
            rows_val = rows_obj.val if hasattr(rows_obj, 'val') else rows_obj
            colors_val = colors_obj.val if hasattr(colors_obj, 'val') else colors_obj

            # Validate ranges
            if columns_val <= 0 or rows_val <= 0 or colors_val not in (1, 2, 3, 4):
                return ps_error.e(ctxt, ps_error.RANGECHECK, "filter")

            # Validate optional parameters if present

            # Validate HSamples array
            if b'HSamples' in param_dict:
                hsample_obj = param_dict[b'HSamples']
                if not hasattr(hsample_obj, 'val') or not hasattr(hsample_obj.val, '__iter__'):
                    return ps_error.e(ctxt, ps_error.TYPECHECK, "filter")

                hsample_array = hsample_obj.val
                if len(hsample_array) != colors_val:
                    return ps_error.e(ctxt, ps_error.RANGECHECK, "filter")

                for element in hsample_array:
                    element_val = element.val if hasattr(element, 'val') else element
                    if element_val not in (1, 2, 3, 4):
                        return ps_error.e(ctxt, ps_error.RANGECHECK, "filter")

            # Validate VSamples array
            if b'VSamples' in param_dict:
                vsample_obj = param_dict[b'VSamples']
                if not hasattr(vsample_obj, 'val') or not hasattr(vsample_obj.val, '__iter__'):
                    return ps_error.e(ctxt, ps_error.TYPECHECK, "filter")

                vsample_array = vsample_obj.val
                if len(vsample_array) != colors_val:
                    return ps_error.e(ctxt, ps_error.RANGECHECK, "filter")

                for element in vsample_array:
                    element_val = element.val if hasattr(element, 'val') else element
                    if element_val not in (1, 2, 3, 4):
                        return ps_error.e(ctxt, ps_error.RANGECHECK, "filter")

            # Validate QFactor
            if b'QFactor' in param_dict:
                qfactor_obj = param_dict[b'QFactor']
                qfactor_val = qfactor_obj.val if hasattr(qfactor_obj, 'val') else qfactor_obj
                if qfactor_val <= 0:
                    return ps_error.e(ctxt, ps_error.RANGECHECK, "filter")

            # Validate ColorTransform for DCTEncode
            if b'ColorTransform' in param_dict:
                color_transform_obj = param_dict[b'ColorTransform']
                color_transform_val = color_transform_obj.val if hasattr(color_transform_obj, 'val') else color_transform_obj
                if color_transform_val not in (0, 1):
                    return ps_error.e(ctxt, ps_error.RANGECHECK, "filter")

        except (AttributeError, TypeError, KeyError):
            return ps_error.e(ctxt, ps_error.TYPECHECK, "filter")

    elif filter_name == b'DCTDecode' and has_dict_param:
        # DCTDecode optional parameter validation
        dict_obj = ostack[dict_param_pos]
        param_dict = dict_obj.val

        # Validate ColorTransform parameter if present
        if b'ColorTransform' in param_dict:
            try:
                color_transform_obj = param_dict[b'ColorTransform']
                color_transform_val = color_transform_obj.val if hasattr(color_transform_obj, 'val') else color_transform_obj

                # ColorTransform must be 0 or 1
                if color_transform_val not in (0, 1):
                    return ps_error.e(ctxt, ps_error.RANGECHECK, "filter")

            except (AttributeError, TypeError):
                return ps_error.e(ctxt, ps_error.TYPECHECK, "filter")

    # 8. Validate data source/target type and access BEFORE popping
    # Compute actual data source position accounting for optional dict
    if has_dict_param:
        actual_ds_pos = -(expected_stack_consumption + 2)
    else:
        actual_ds_pos = -(expected_stack_consumption + 1)

    if len(ostack) < abs(actual_ds_pos):
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, "filter")

    ds_obj = ostack[actual_ds_pos]

    # Check type: file, string, or executable array (procedure)
    valid_source_types = (ps.File, ps.String)
    is_procedure = (isinstance(ds_obj, ps.Array) and
                   ds_obj.attrib == ps.ATTRIB_EXEC)

    if not (isinstance(ds_obj, valid_source_types) or is_procedure):
        return ps_error.e(ctxt, ps_error.TYPECHECK, "filter")

    # Validate access permissions based on filter direction
    is_decoding_filter = (filter_name.endswith(b'Decode') or
                         filter_name in {b'SubFileDecode', b'ReusableStreamDecode'})

    if isinstance(ds_obj, ps.File):
        required_access = (ps.ACCESS_READ_ONLY if is_decoding_filter
                          else ps.ACCESS_WRITE_ONLY)
        if ds_obj.access not in [required_access, ps.ACCESS_UNLIMITED]:
            return ps_error.e(ctxt, ps_error.INVALIDACCESS, "filter")

    # STEP 2: ALL VALIDATION PASSED - NOW POP OPERANDS

    # Pop filter name first
    ostack.pop()

    # Handle special parameter popping for SubFileDecode
    if filter_name == b'SubFileDecode' and expected_stack_consumption == 3:
        # Pop in reverse order: EODString, then EODCount
        eod_string_obj = ostack.pop()
        eod_count_obj = ostack.pop()

        # Store parameters
        special_params = {
            b'EODCount': eod_count_obj,
            b'EODString': eod_string_obj,
            b'CloseSource': ps.Bool(True)  # Default value
        }

    # Handle special parameter popping for RunLengthEncode
    elif filter_name == b'RunLengthEncode' and expected_stack_consumption == 2:
        # Pop recordsize parameter
        recordsize_obj = ostack.pop()

        # Store parameters
        special_params = {
            b'RecordSize': recordsize_obj,
            b'CloseTarget': ps.Bool(True)  # Default value
        }

    # Pop optional dictionary parameter
    params_dict = None
    if has_dict_param:
        params_dict = ostack.pop()

    # Merge special filter parameters if applicable
    if special_params:
        if params_dict:
            # LanguageLevel 3: parameters already in dictionary
            pass
        else:
            params_dict = special_params

    # Check stack overflow for result (will push 1 item)
    if ctxt.MaxOpStack and len(ostack) >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, "filter")

    # Pop data source and create filter
    data_source = ostack.pop()

    try:
        # Create filter implementation
        filter_impl = create_filter(filter_name, data_source, params_dict, ctxt)
        if filter_impl is None:
            return ps_error.e(ctxt, ps_error.UNDEFINED, "filter")

        # Create FilterFile with proper VM allocation (PLRM requirement)
        filter_file = FilterFile(data_source, filter_impl, ctxt, is_decoding_filter)

    except Exception:
        # Filter creation failed - ioerror covers I/O problems and resource issues
        return ps_error.e(ctxt, ps_error.IOERROR, "filter")

    # STEP 11: Push result onto operand stack
    ostack.append(filter_file)


def create_filter(filter_name: bytes, data_source: ps.File | ps.String | ps.Array, params: dict | ps.Dict | None, ctxt: ps.Context) -> FilterBase | None:
    """Factory function to create specific **filter** implementations"""
    # Deferred imports to avoid circular dependency (leaf modules import FilterBase
    # from this module)
    from .filter_ascii import (
        ASCIIHexDecodeFilter, ASCIIHexEncodeFilter,
        NullEncodeFilter,
        ASCII85DecodeFilter, ASCII85EncodeFilter,
    )
    from .filter_compression import (
        RunLengthDecodeFilter, RunLengthEncodeFilter,
        LZWDecodeFilter, LZWEncodeFilter,
        FlateDecodeFilter, FlateEncodeFilter,
    )
    from .filter_dct import DCTDecodeFilter, DCTEncodeFilter
    from .filter_ccitt import CCITTFaxDecodeFilter

    # Wrap data source in DataSource abstraction
    source = DataSource(data_source, ctxt)

    if filter_name == b'ASCIIHexDecode':
        return ASCIIHexDecodeFilter(source, params)
    elif filter_name == b'ASCIIHexEncode':
        return ASCIIHexEncodeFilter(source, params)
    elif filter_name == b'ASCII85Decode':
        return ASCII85DecodeFilter(source, params)
    elif filter_name == b'ASCII85Encode':
        return ASCII85EncodeFilter(source, params)
    elif filter_name == b'SubFileDecode':
        return SubFileDecodeFilter(source, params)
    elif filter_name == b'NullEncode':
        return NullEncodeFilter(source, params)
    elif filter_name == b'RunLengthDecode':
        return RunLengthDecodeFilter(source, params)
    elif filter_name == b'RunLengthEncode':
        return RunLengthEncodeFilter(source, params)
    elif filter_name == b'LZWDecode':
        return LZWDecodeFilter(source, params)
    elif filter_name == b'LZWEncode':
        return LZWEncodeFilter(source, params)
    elif filter_name == b'FlateDecode':
        return FlateDecodeFilter(source, params)
    elif filter_name == b'FlateEncode':
        return FlateEncodeFilter(source, params)
    elif filter_name == b'DCTDecode':
        return DCTDecodeFilter(source, params)
    elif filter_name == b'DCTEncode':
        return DCTEncodeFilter(source, params)
    elif filter_name == b'CCITTFaxDecode':
        return CCITTFaxDecodeFilter(source, params)
    else:
        return None  # Return None for unknown filters


class SubFileDecodeFilter(FilterBase):
    """SubFileDecode filter - PLRM compliant subfile detection with EOD handling"""
    
    def __init__(self, data_source: DataSource, params: dict | ps.Dict | None = None) -> None:
        super().__init__(data_source, params)

        # Extract required parameters from dictionary or defaults
        self.eod_count = 0
        self.eod_string = b''
        self.close_source = True  # Default value
        
        if params and isinstance(params, dict):
            # LanguageLevel 3 parameter dictionary format
            if b'EODCount' in params:
                eod_count_obj = params[b'EODCount']
                if hasattr(eod_count_obj, 'val'):
                    self.eod_count = eod_count_obj.val
                else:
                    self.eod_count = int(eod_count_obj)
                    
            if b'EODString' in params:
                eod_string_obj = params[b'EODString']
                if hasattr(eod_string_obj, 'byte_string'):
                    self.eod_string = eod_string_obj.byte_string()
                else:
                    self.eod_string = bytes(eod_string_obj)
                    
            if b'CloseSource' in params:
                close_source_obj = params[b'CloseSource']
                if hasattr(close_source_obj, 'val'):
                    self.close_source = bool(close_source_obj.val)
                else:
                    self.close_source = bool(close_source_obj)
        
        # Tracking state
        self.eod_reached = False
        self.eod_instances_found = 0
        self.search_buffer = bytearray()
        self.byte_count = 0  # For zero-length EOD strings
        
    def read_data(self, ctxt: ps.Context, max_bytes: int | None = None) -> bytes:
        """Read data through **filter** until EOD condition is met - PLRM Section 3.13"""
        if self.eof_reached:
            return b''
            
        result = bytearray()
        target_bytes = max_bytes or 1024
        
        # Special case: zero-length EOD string means count bytes
        if len(self.eod_string) == 0:
            return self._read_byte_count_mode(ctxt, target_bytes)
        
        # Normal case: search for EOD string occurrences
        while len(result) < target_bytes and not self.eod_reached:
            # Read more data from source
            source_data = self.data_source.read_data(ctxt, 2048)
            if not source_data:
                self.eof_reached = True
                break
                
            self.search_buffer.extend(source_data)
            
            # Process data looking for EOD string
            while len(result) < target_bytes and not self.eod_reached:
                if not self.search_buffer:
                    break
                
                # Check if we have enough data to potentially match EOD string
                if len(self.search_buffer) < len(self.eod_string):
                    break
                
                # Look for EOD string at current position
                eod_match = self._check_eod_match()
                
                if eod_match:
                    # Found EOD string - increment counter
                    self.eod_instances_found += 1
                    
                    # Add the EOD string to output (PLRM: it's passed through)
                    for byte_val in self.eod_string:
                        if len(result) < target_bytes:
                            result.append(byte_val)
                    
                    # Remove EOD string from buffer  
                    self.search_buffer = self.search_buffer[len(self.eod_string):]
                    
                    # Check if we've reached the required count
                    if self.eod_instances_found >= self.eod_count:
                        self.eod_reached = True
                        # Push remaining bytes back to source so they're
                        # available for subsequent reads from the underlying
                        # file (e.g. tokenizer reading after inline image)
                        if self.search_buffer:
                            self.data_source.putback(bytes(self.search_buffer))
                            self.search_buffer = bytearray()
                        break
                else:
                    # No match - pass through one byte
                    result.append(self.search_buffer.pop(0))
        
        return bytes(result)
    
    def _read_byte_count_mode(self, ctxt: ps.Context, target_bytes: int) -> bytes:
        """Handle zero-length EOD string case - just pass through EODCount bytes"""
        if self.byte_count >= self.eod_count:
            self.eof_reached = True
            return b''
        
        # Calculate how many bytes we can read
        bytes_remaining = self.eod_count - self.byte_count
        bytes_to_read = min(target_bytes, bytes_remaining)
        
        # Read from source
        source_data = self.data_source.read_data(ctxt, bytes_to_read)
        if not source_data:
            self.eof_reached = True
            return b''
        
        # Update counter and check for completion
        self.byte_count += len(source_data)
        if self.byte_count >= self.eod_count:
            self.eof_reached = True
            
        return source_data
    
    def _check_eod_match(self) -> bool:
        """Check if EOD string matches at start of **search** buffer - PLRM case-sensitive"""
        if len(self.search_buffer) < len(self.eod_string):
            return False
        
        # PLRM: matching is case-sensitive based on 8-bit character codes
        for i in range(len(self.eod_string)):
            if self.search_buffer[i] != self.eod_string[i]:
                return False
                
        return True
    
    def close(self, ctxt: ps.Context) -> None:
        """Close **filter** and optionally **close** source based on CloseSource parameter"""
        if not self.closed:
            if self.close_source:
                super().close(ctxt)  # This will close the underlying source
            self.closed = True