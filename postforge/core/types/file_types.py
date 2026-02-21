# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostForge Types File Classes Module

This module contains all file-related PostScript types including file streams,
file management, and input/output abstractions. These classes handle both
standard file operations and specialized PostScript file types.
"""

import sys
import io
import errno
import threading
import time
import copy
from typing import Any, Union, Tuple

# Import error handling
from .. import error as ps_error

# Import base classes and constants
from .base import PSObject, Stream
from .constants import (
    ACCESS_UNLIMITED, ACCESS_READ_ONLY, ATTRIB_LIT, ATTRIB_EXEC,
    T_FILE, T_STRING, T_INT
)

# Import primitive types and context infrastructure (for file operations)
from .primitive import Bool, Int
from .context import contexts, global_resources

# Forward reference for type annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .context import Context


# Forward reference to String class (will be in composite.py)
# This is needed because file classes reference String
String = None  # Will be set by package __init__.py after all modules are loaded


class File(Stream):
    TYPE = T_FILE
    
    def __init__(
        self,
        ctxt_id: int,
        name: str,
        mode: str,
        access: int = ACCESS_UNLIMITED,
        attrib: int = ATTRIB_LIT,
        is_composite: bool = True,
        is_global=False,
    ) -> None:
        super().__init__(None, access, attrib, is_composite, is_global)

        self.name = name
        self.mode = mode if mode.endswith("b") else mode + "b"
        self.created = time.monotonic_ns()  # creation time for this composite object
        self.ctxt_id = ctxt_id
        self.is_real_file = False  # Default to False, set to True in init_file() for actual files
        self.ps_section_end = None  # Set for DOS EPS files to limit reads to PS section
        self._putback_buf = bytearray()  # Buffer for bytes pushed back by filter chains
        self._putback_pos = 0
        self._last_read_from_putback = None  # Track last byte read from putback for unread()

        if self.name == "%statementedit":
            self.is_global = True
        
        # Track all composite objects in appropriate refs immediately upon creation
        if ctxt_id is not None and contexts[ctxt_id] is not None:
            # For files, store the file name as the reference value
            if self.is_global:
                contexts[ctxt_id].global_refs[self.created] = self.name
            else:
                contexts[ctxt_id].local_refs[self.created] = self.name

    def open(self) -> Union[None, int]:
        if self.name == "%statementedit":
            # Flush Qt events before blocking on terminal input so the
            # display window actually paints (e.g. PSChess board display).
            # Multiple rounds needed: show → resize → paint.
            ctxt = contexts[self.ctxt_id] if self.ctxt_id is not None else None
            if ctxt and hasattr(ctxt, 'event_loop_callback') and ctxt.event_loop_callback:
                ctxt.event_loop_callback()
                ctxt.event_loop_callback()
            try:
                st = input()
            except (EOFError, KeyboardInterrupt):
                return ps_error.UNDEFINEDFILENAME

            strings = (
                global_resources.global_strings
                if self.is_global
                else contexts[self.ctxt_id].local_strings
            )

            offset = len(strings)
            strings += bytearray(st, "ascii")

            self.val = String(self.ctxt_id, offset, len(st), is_global=self.is_global)
            self.is_global = True
            
        elif self.name == "%lineedit":
            # Similar to %statementedit but for single line input
            # Read a single line instead of a complete statement
            # Flush Qt events before blocking on terminal input so the
            # display window actually paints (e.g. PSChess board display).
            # Multiple rounds needed: show → resize → paint.
            ctxt = contexts[self.ctxt_id] if self.ctxt_id is not None else None
            if ctxt and hasattr(ctxt, 'event_loop_callback') and ctxt.event_loop_callback:
                ctxt.event_loop_callback()
                ctxt.event_loop_callback()
            try:
                line = sys.stdin.readline()
                if not line:  # EOF
                    return ps_error.UNDEFINEDFILENAME
                
                # Remove trailing newline if present
                if line.endswith('\n'):
                    line = line[:-1]

                strings = (
                    global_resources.global_strings
                    if self.is_global
                    else contexts[self.ctxt_id].local_strings
                )

                offset = len(strings)
                strings += bytearray(line, "ascii")

                self.val = String(self.ctxt_id, offset, len(line), is_global=self.is_global)
                self.is_global = True
                
            except (EOFError, KeyboardInterrupt):
                return ps_error.UNDEFINEDFILENAME
            
            # Update refs to track the correct val after reassignment
            if self.ctxt_id is not None:
                if self.is_global:
                    contexts[self.ctxt_id].global_refs[self.created] = self.name
                else:
                    contexts[self.ctxt_id].local_refs[self.created] = self.name

            self.is_real_file = False
            return None
        else:
            self.is_real_file = True
            try:
                self.val = open(self.name, self.mode)

                # Check for DOS EPS Binary File Header (TIFF preview format)
                # Magic number: C5 D0 D3 C6 (little-endian 0xC6D3D0C5)
                # This header wraps PostScript content with optional TIFF/WMF previews
                if 'r' in self.mode:
                    header = self.val.read(4)
                    if header == b'\xc5\xd0\xd3\xc6':
                        # DOS EPS Binary header detected
                        # Bytes 4-7: offset to PostScript data (little-endian uint32)
                        # Bytes 8-11: length of PostScript data (little-endian uint32)
                        ps_offset_bytes = self.val.read(4)
                        ps_offset = int.from_bytes(ps_offset_bytes, byteorder='little')
                        ps_length_bytes = self.val.read(4)
                        ps_length = int.from_bytes(ps_length_bytes, byteorder='little')
                        # Seek to the start of actual PostScript content
                        self.val.seek(ps_offset)
                        # Store PS section boundary so reads stop at the right place
                        self.ps_section_end = ps_offset + ps_length
                    else:
                        # Not a DOS EPS Binary file, seek back to beginning
                        self.val.seek(0)

                # Update refs to track the correct val after reassignment
                if self.ctxt_id is not None:
                    if self.is_global:
                        contexts[self.ctxt_id].global_refs[self.created] = self.name
                    else:
                        contexts[self.ctxt_id].local_refs[self.created] = self.name

                return None
            except OSError as error:
                if error.errno == errno.ENOENT:
                    # no such file or directory
                    return ps_error.UNDEFINEDFILENAME
                elif error.errno == errno.EACCES:
                    # permission denied
                    return ps_error.INVALIDFILEACCESS
                elif error.errno == errno.EIO:
                    # io error
                    return ps_error.IOERROR
                elif error.errno == errno.EMFILE:
                    # too many open files
                    return ps_error.LIMITCHECK
                else:
                    # make anything else an invalid file access error
                    return ps_error.INVALIDFILEACCESS

    def close(self) -> None:
        if self.name not in [
            "%stdin",
            "%stdout",
            "%stderr",
            "%statementedit",
            "%lineedit",
        ]:
            if self.val:
                self.val.close()
                self.val = None  # Clear the file handle reference after closing

    def filename(self):
        """Return the filename for this file object."""
        return self.name

    def __eq__(self, other) -> bool:
        if isinstance(other, File):
            return self.val == other.val
        return False

    def read(self, ctxt: "Context") -> int:
        # Check putback buffer first
        if self._putback_pos < len(self._putback_buf):
            b = self._putback_buf[self._putback_pos]
            self._putback_pos += 1
            if self._putback_pos >= len(self._putback_buf):
                self._putback_buf = bytearray()
                self._putback_pos = 0
            self._last_read_from_putback = b
            return b
        self._last_read_from_putback = None
        # reads one byte from the file
        if self.is_real_file:
            try:
                if self.val is None:
                    return None
                if self.ps_section_end is not None and self.val.tell() >= self.ps_section_end:
                    return None  # EOF for PS section in DOS EPS
                byte = self.val.read(1)
                if not len(byte):
                    return None
                return byte[0]
            except (IndexError, OSError):
                return None
        else:
            return self.val.read(contexts[self.ctxt_id])

    def read_bulk(self, ctxt: "Context", count: int) -> bytes:
        """Read up to *count* bytes in one call.  Returns bytes (may be shorter at EOF)."""
        result = bytearray()
        # Drain putback buffer first
        if self._putback_pos < len(self._putback_buf):
            avail = len(self._putback_buf) - self._putback_pos
            take = min(avail, count)
            result.extend(self._putback_buf[self._putback_pos:self._putback_pos + take])
            self._putback_pos += take
            if self._putback_pos >= len(self._putback_buf):
                self._putback_buf = bytearray()
                self._putback_pos = 0
            if len(result) >= count:
                return bytes(result)
        remaining = count - len(result)
        if self.is_real_file:
            try:
                if self.val is None:
                    return bytes(result)
                if self.ps_section_end is not None:
                    ps_remaining = self.ps_section_end - self.val.tell()
                    if ps_remaining <= 0:
                        return bytes(result)
                    remaining = min(remaining, ps_remaining)
                data = self.val.read(remaining)
                if data:
                    result.extend(data)
                return bytes(result)
            except OSError:
                return bytes(result)
        else:
            # Non-real files (stdin proxy etc.) — fall back to byte-at-a-time
            for _ in range(remaining):
                b = self.read(ctxt)
                if b is None:
                    break
                result.append(b)
            return bytes(result)

    def write(self, ctxt: "Context", data) -> None:
        # writes data to the file
        if self.is_real_file:
            try:
                if isinstance(data, int):
                    # Single byte write
                    self.val.write(bytes([data]))
                elif isinstance(data, (bytes, bytearray)):
                    # Multiple bytes write
                    self.val.write(data)
                else:
                    # String write - convert to bytes
                    self.val.write(str(data).encode('latin-1'))
            except OSError:
                raise ps_error.IOERROR
        else:
            # For non-real files, delegate to val's write method if it exists
            if hasattr(self.val, 'write'):
                self.val.write(ctxt, data)
            else:
                raise ps_error.IOERROR

    def flush(self) -> None:
        """Flush any buffered characters to the file."""
        if self.is_real_file and hasattr(self.val, 'flush'):
            self.val.flush()
        elif hasattr(self.val, 'flush'):
            self.val.flush()
            
    def putback(self, data):
        """Push bytes back to be read again on next read call.

        Used by filter chains (e.g. ASCII85Decode) that read past their
        end-of-data marker and need to return unconsumed bytes to the
        underlying file so subsequent reads (by the tokenizer) find them.
        """
        if data:
            # Compact existing buffer
            if self._putback_pos > 0:
                self._putback_buf = self._putback_buf[self._putback_pos:]
                self._putback_pos = 0
            self._putback_buf = bytearray(data) + self._putback_buf

    def unread(self) -> None:
        if self._last_read_from_putback is not None:
            # Last read came from putback buffer — push byte back there
            if self._putback_pos > 0:
                self._putback_pos -= 1
            else:
                self._putback_buf = bytearray([self._last_read_from_putback]) + self._putback_buf
            self._last_read_from_putback = None
            return
        if self.val is None:
            # Stream was closed or never initialized - nothing to unread
            return
        if self.is_real_file:
            self.val.seek(-1, 1)
        else:
            self.val.unread()

    _ALL_ATTRS = ('val', 'access', 'attrib', 'is_composite', 'is_global',
                  'line_num', 'name', 'mode', 'created', 'ctxt_id', 'is_real_file',
                  'ps_section_end', '_putback_buf', '_putback_pos',
                  '_last_read_from_putback')

    def __copy__(self):
        # Custom copy method for regular copying (dup, etc.)
        # This preserves the shared file handle without triggering pickle logic
        new_file = self.__class__.__new__(self.__class__)
        new_file.val = self.val
        new_file.access = self.access
        new_file.attrib = self.attrib
        new_file.is_composite = self.is_composite
        new_file.is_global = self.is_global
        new_file.line_num = self.line_num
        new_file.name = self.name
        new_file.mode = self.mode
        new_file.created = self.created
        new_file.ctxt_id = self.ctxt_id
        new_file.is_real_file = self.is_real_file
        new_file.ps_section_end = self.ps_section_end
        new_file._putback_buf = bytearray()
        new_file._putback_pos = 0
        new_file._last_read_from_putback = None
        return new_file

    def __getstate__(self) -> dict:
        state = {attr: getattr(self, attr, None) for attr in File._ALL_ATTRS}
        # Don't pickle the actual file handle - check for any file handle in val
        if hasattr(self, 'val') and self.val is not None:
            # Check if val contains a file handle (TextIOWrapper or similar)
            if isinstance(self.val, type(sys.stdin)) or hasattr(self.val, 'read') or hasattr(self.val, 'write'):
                # Store file position if possible, then nullify val
                if self.is_real_file:
                    try:
                        state['file_position'] = self.val.tell()
                    except (OSError, AttributeError, ValueError):
                        state['file_position'] = 0
                state['val'] = None
        # StandardFile objects should override this method entirely
        # Do not attempt to handle them in the base class
        return state

    def __setstate__(self, state: dict) -> None:
        for key, value in state.items():
            setattr(self, key, value)
        # Restore file handle after unpickling
        if self.is_real_file and state.get('val') is None:
            try:
                self.val = open(self.name, self.mode)
                # Restore file position
                if 'file_position' in state:
                    self.val.seek(state['file_position'])
            except (OSError, IOError):
                # If we can't reopen the file, mark as invalid
                self.val = None
        # For StandardFile objects, restore the stream
        elif hasattr(self, 'stream') or 'stream' in state:
            if self.name == "%stdin":
                self.stream = sys.stdin
            elif self.name == "%stdout":
                self.stream = sys.stdout  
            elif self.name == "%stderr":
                self.stream = sys.stderr
            else:
                self.stream = None
            self.val = self.stream

    def __str__(self) -> str:
        return f"ps.File({self.name})"

    def __repr__(self) -> str:
        return f"ps.File({self.name})"


class StandardFileManager:
    """
    Global registry for StandardFile objects to enable indirection.
    
    This prevents direct references to StandardFile objects containing TextIOWrapper
    from being serialized during VM save operations. Instead, we use proxy objects
    with IDs that reference entries in this global registry.
    """
    _instance = None
    _registry = {}
    _next_id = 1
    
    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def register(self, standard_file) -> int:
        """Register a StandardFile object and return its ID."""
        file_id = self._next_id
        self._next_id += 1
        self._registry[file_id] = standard_file
        return file_id
    
    def get(self, file_id: int):
        """Retrieve a StandardFile object by ID."""
        return self._registry.get(file_id)
    
    def remove(self, file_id: int):
        """Remove a StandardFile object from registry."""
        self._registry.pop(file_id, None)
    
    def clear(self):
        """Clear all registered files (for testing/cleanup)."""
        self._registry.clear()
        self._next_id = 1


class StandardFileProxy(PSObject):
    """
    Proxy object that references a StandardFile via ID instead of direct reference.
    
    This prevents StandardFile objects containing TextIOWrapper from being
    serialized during VM save operations. The proxy only contains an ID that
    can be safely pickled and restored.
    """
    TYPE = T_FILE  # Acts as a file proxy
    
    def __init__(self, file_id: int, name: str, attrib=ATTRIB_LIT, is_global=True):
        # Store minimal state - just the ID and name for identification
        super().__init__(None, attrib=attrib, is_global=is_global)
        self.file_id = file_id
        self.name = name  # For debugging and identification
        
    def get_standard_file(self):
        """Get the actual StandardFile object from the registry."""
        return StandardFileManager.get_instance().get(self.file_id)
    
    def __copy__(self):
        """Optimized copy for StandardFileProxy - lightweight proxy object."""
        return StandardFileProxy(self.file_id, self.name, self.attrib, self.is_global) # type: ignore
    
    def __str__(self) -> str:
        return f"ps.StandardFileProxy({self.name}, id={self.file_id})"
    
    def __repr__(self) -> str:
        return self.__str__()
    
    # Delegate all file operations to the actual StandardFile
    def read(self, ctxt=None):
        actual_file = self.get_standard_file()
        return actual_file.read(ctxt) if actual_file else None
    
    def write(self, data, ctxt=None):
        actual_file = self.get_standard_file()
        return actual_file.write(data) if actual_file else None
    
    def close(self):
        actual_file = self.get_standard_file()
        if actual_file:
            actual_file.close()
    
    def flush(self):
        actual_file = self.get_standard_file()
        if actual_file:
            actual_file.flush()
    
    def filename(self):
        """Return the filename for this file object."""
        # For standard files, just return the name directly
        return self.name
    
    def status(self):
        """Return the status of this file object."""
        actual_file = self.get_standard_file()
        return actual_file.status() if actual_file else False
    
    def readline(self, ctxt=None):
        actual_file = self.get_standard_file()
        return actual_file.readline(ctxt) if actual_file else None
    
    def readstring(self, string_obj, ctxt=None):
        actual_file = self.get_standard_file()
        return actual_file.readstring(string_obj, ctxt) if actual_file else None
    
    def writestring(self, string_obj, ctxt=None):
        actual_file = self.get_standard_file()
        return actual_file.writestring(string_obj, ctxt) if actual_file else None
    
    def open(self):
        actual_file = self.get_standard_file()
        return actual_file.open() if actual_file else None
    
    # Properties that should delegate to actual file
    @property
    def mode(self):
        actual_file = self.get_standard_file()
        return actual_file.mode if actual_file else "r"
    
    @property
    def closable(self):
        actual_file = self.get_standard_file()
        return actual_file.closable if actual_file else False
    
    @property
    def is_real_file(self):
        actual_file = self.get_standard_file()
        return actual_file.is_real_file if actual_file else False


class StandardFile(File):
    TYPE = T_FILE
    
    """
    Special file objects for stdin/stdout/stderr.
    
    These files wrap Python's sys.stdin/stdout/stderr streams and provide
    PostScript-compliant behavior including:
    - Cannot be closed by user programs
    - Persistent objects (same object returned by multiple file operations)
    - Proper flush() implementation
    """
    
    def __init__(
        self,
        ctxt_id: int,
        name: str,
        stream,
        mode: str,
        access: int = ACCESS_UNLIMITED,
        attrib: int = ATTRIB_LIT,
        is_global: bool = True,  # Standard files are always global
    ) -> None:
        # Initialize with global allocation by default
        super().__init__(ctxt_id, name, mode, access, attrib, True, is_global)
        self.stream = stream  # sys.stdin, sys.stdout, or sys.stderr
        self.closable = False  # Standard files cannot be closed
        self.is_real_file = False  # Standard files are not real disk files
        self.val = stream  # Set val to the stream for compatibility with File interface
        
    def open(self) -> Union[None, int]:
        # Standard files are always "open" - no actual opening needed
        return None
        
    def close(self) -> None:
        # Standard files cannot be closed by user programs
        # This is a no-op to comply with PLRM requirements
        pass
        
    def read(self, ctxt=None) -> Union[int, None]:
        """Read a single byte from the standard input stream."""
        if self.mode.startswith('w'):
            return ps_error.INVALIDACCESS
            
        try:
            # For stdin, read one character at a time
            if self.stream == sys.stdin:
                char = self.stream.read(1)
                if char:
                    return ord(char)
                else:
                    return None  # EOF
            else:
                # For other standard streams, this shouldn't happen
                return None
        except (EOFError, KeyboardInterrupt):
            return None
            
    def write(self, ctxt_or_data, data=None) -> None:
        """Write data to the standard output/error stream."""
        # Support both File.write(ctxt, data) and direct write(data) signatures
        if data is None:
            data = ctxt_or_data
        if self.mode.startswith('r'):
            raise ps_error.INVALIDACCESS

        if isinstance(data, int):
            # Single byte write
            self.stream.write(chr(data))
        elif isinstance(data, (bytes, bytearray)):
            # Multiple bytes write
            self.stream.write(data.decode('ascii', errors='replace'))
        else:
            # String write
            self.stream.write(str(data))
            
    def flush(self) -> None:
        """Flush the standard output/error stream."""
        if hasattr(self.stream, 'flush'):
            self.stream.flush()

    _ALL_ATTRS = ('val', 'access', 'attrib', 'is_composite', 'is_global',
                  'line_num', 'name', 'mode', 'created', 'ctxt_id',
                  'is_real_file', 'stream', 'closable')

    def __copy__(self):
        # Custom copy method for regular copying (dup, etc.)
        # This preserves the shared stream reference without triggering pickle logic
        new_file = self.__class__.__new__(self.__class__)
        new_file.val = self.val
        new_file.access = self.access
        new_file.attrib = self.attrib
        new_file.is_composite = self.is_composite
        new_file.is_global = self.is_global
        new_file.line_num = self.line_num
        new_file.name = self.name
        new_file.mode = self.mode
        new_file.created = self.created
        new_file.ctxt_id = self.ctxt_id
        new_file.is_real_file = self.is_real_file
        new_file.stream = self.stream
        new_file.closable = self.closable
        new_file.ps_section_end = getattr(self, 'ps_section_end', None)
        new_file._putback_buf = bytearray()
        new_file._putback_pos = 0
        new_file._last_read_from_putback = None
        return new_file

    def __getstate__(self) -> dict:
        """Handle pickle serialization - don't serialize the stream object."""
        state = {attr: getattr(self, attr) for attr in StandardFile._ALL_ATTRS}
        # Don't pickle the stream object (sys.stdin/stdout/stderr)
        
        # CRITICAL: Also nullify the actual object attributes, not just the state dict
        # This ensures no references remain in the live object
        self.stream = None
        self.val = None
        
        state['stream'] = None
        state['val'] = None
        
        # Double-check: ensure no TextIOWrapper remains anywhere in state
        for key, value in state.items():
            if isinstance(value, io.TextIOWrapper):
                state[key] = None

        return state
        
    def __setstate__(self, state: dict) -> None:
        """Handle pickle deserialization - restore the stream object."""
        for key, value in state.items():
            setattr(self, key, value)
        # Restore the appropriate stream based on the file name
        if self.name == "%stdin":
            self.stream = sys.stdin
        elif self.name == "%stdout":
            self.stream = sys.stdout  
        elif self.name == "%stderr":
            self.stream = sys.stderr
        else:
            # Fallback - shouldn't happen for standard files
            self.stream = None
        # Also restore val for compatibility
        self.val = self.stream


class EexecDecryptionFilter(File):
    TYPE = T_FILE
    
    """
    Decryption filter for eexec operator.
    
    Implements Adobe Type 1 Font Format eexec decryption algorithm.
    Acts as a file that decrypts data on read from an underlying source.
    
    Adobe Algorithm Specification:
    - Initial key R = 55665
    - Random bytes to skip n = 4
    - Constants: c1 = 52845, c2 = 22719
    - Decryption: plain = cipher ^ (R >> 8); R = (cipher + R) * c1 + c2
    """
    
    def __init__(
        self,
        ctxt_id: int,
        source_file: "File",
        ctxt: "Context",
        access: int = ACCESS_UNLIMITED,
        attrib: int = ATTRIB_LIT,
        is_global: bool = False,
    ) -> None:
        # Initialize with a unique name for the filter
        super().__init__(ctxt_id, f"eexec-filter-{source_file.name}", "r", access, attrib, True, is_global)
        
        # Set val to the source file for compatibility with File interface
        self.val = source_file
        self.source_file = source_file
        self.ctxt = ctxt
        
        # Adobe eexec encryption constants
        self.R = 55665  # Initial encryption key
        self.c1 = 52845  # Constant 1
        self.c2 = 22719  # Constant 2
        self.random_bytes_to_skip = 4  # Number of random bytes at start
        
        # State tracking
        self.bytes_read = 0
        self.is_ascii_hex = None  # Will be determined on first read
        self.hex_buffer = ""  # For ASCII hex parsing
        self.is_closed = False
        self.systemdict_pushed = False
        self.format_determined = False
        
        # Unread support - buffer the last decrypted byte
        self.last_decrypted_byte = None
        self.has_unread_byte = False
        
    
    def _determine_format_from_first_bytes(self):
        """
        Determine format by reading first few bytes and buffering them.
        
        For string sources: Always binary (strings contain the encrypted data directly)
        For file sources: Detect ASCII hex vs binary based on content
        """
        self.first_bytes_buffer = []
        
        # String sources are always binary encrypted data
        if hasattr(self.source_file, '_is_string_source') and self.source_file._is_string_source:
            self.is_ascii_hex = False  # String content is binary encrypted data
            self.format_determined = True
            self.buffer_index = 0
            return
        
        # For files, read first 8 bytes to determine format
        for i in range(8):
            byte = self.source_file.read(self.ctxt)
            if byte is None:
                break
            self.first_bytes_buffer.append(byte)
        
        if not self.first_bytes_buffer:
            self.is_ascii_hex = True  # Default to hex for empty files
            self.format_determined = True
            self.buffer_index = 0
            return
        
        # Check if all bytes are valid hex characters or whitespace
        all_hex = True
        for byte in self.first_bytes_buffer:
            char = chr(byte) if isinstance(byte, int) else byte
            if not (char.isdigit() or char.upper() in 'ABCDEF' or char in ' \t\r\n'):
                all_hex = False
                break
        
        self.is_ascii_hex = all_hex
        self.format_determined = True
        self.buffer_index = 0  # Track position in buffered bytes
    
    def _read_raw_byte(self):
        """Read one raw byte, using buffer first if available."""
        # Use buffered bytes first
        if hasattr(self, 'first_bytes_buffer') and self.buffer_index < len(self.first_bytes_buffer):
            byte = self.first_bytes_buffer[self.buffer_index]
            self.buffer_index += 1
            return byte
        
        # Read from source file
        return self.source_file.read(self.ctxt)
    
    def _read_hex_byte(self):
        """Read a single byte from ASCII hexadecimal format."""
        while len(self.hex_buffer) < 2:
            char_byte = self._read_raw_byte()
            if char_byte is None:
                return None
            
            char = chr(char_byte) if isinstance(char_byte, int) else char_byte
            
            # Skip whitespace in hex format
            if char in ' \t\r\n':
                continue
            
            # Validate hex character
            if char.upper() not in '0123456789ABCDEF':
                # Invalid hex character indicates end of encrypted section
                return None
            
            self.hex_buffer += char
        
        # Convert two hex characters to byte
        hex_pair = self.hex_buffer[:2]
        self.hex_buffer = self.hex_buffer[2:]
        
        try:
            return int(hex_pair, 16)
        except ValueError:
            return None
    
    def _decrypt_byte(self, cipher_byte):
        """Decrypt a single byte using Adobe algorithm."""
        plain_byte = cipher_byte ^ (self.R >> 8)
        self.R = ((cipher_byte + self.R) * self.c1 + self.c2) & 0xFFFF
        return plain_byte
    
    def read(self, ctxt: "Context") -> Union[int, None]:
        """Read and decrypt one byte from the source."""
        if self.is_closed:
            return None
        
        # Return unread byte if available
        if self.has_unread_byte:
            self.has_unread_byte = False
            return self.last_decrypted_byte
        
        # Determine format on first read to avoid file positioning issues
        if not self.format_determined:
            self._determine_format_from_first_bytes()
        
        # Read cipher byte based on format
        if self.is_ascii_hex:
            cipher_byte = self._read_hex_byte()
        else:
            cipher_byte = self._read_raw_byte()
        
        if cipher_byte is None:
            # End of input - close the filter
            self.close()
            return None
        
        # Decrypt the byte
        plain_byte = self._decrypt_byte(cipher_byte)
        self.bytes_read += 1
        
        # Skip the first n random bytes
        if self.bytes_read <= self.random_bytes_to_skip:
            return self.read(ctxt)  # Recursively read next byte
        
        # Store this byte for potential unread
        self.last_decrypted_byte = plain_byte
        
        return plain_byte
    
    def unread(self) -> None:
        """Put a byte back - required by tokenizer."""
        if self.last_decrypted_byte is not None and not self.has_unread_byte:
            self.has_unread_byte = True
    
    def close(self) -> None:
        """Close the decryption filter."""
        if not self.is_closed:
            self.is_closed = True
            
            # Pop systemdict if we pushed it
            if self.systemdict_pushed and len(self.ctxt.d_stack) > 3:
                if hasattr(self.ctxt.d_stack[-1], 'name') and self.ctxt.d_stack[-1].name == b"systemdict":
                    self.ctxt.d_stack.pop()
                    self.systemdict_pushed = False
    
    _ALL_ATTRS = ('val', 'access', 'attrib', 'is_composite', 'is_global',
                  'line_num', 'name', 'mode', 'created', 'ctxt_id', 'is_real_file',
                  'source_file', 'ctxt', 'R', 'c1', 'c2', 'random_bytes_to_skip',
                  'bytes_read', 'is_ascii_hex', 'hex_buffer', 'is_closed',
                  'systemdict_pushed', 'format_determined',
                  'last_decrypted_byte', 'has_unread_byte')

    def __copy__(self):
        """Optimized copy for EexecDecryptionFilter - complex file filter."""
        new_obj = EexecDecryptionFilter.__new__(EexecDecryptionFilter)
        for attr in EexecDecryptionFilter._ALL_ATTRS:
            setattr(new_obj, attr, getattr(self, attr))
        # Copy optional attrs that may exist after _determine_format_from_first_bytes
        if hasattr(self, 'first_bytes_buffer'):
            new_obj.first_bytes_buffer = self.first_bytes_buffer
            new_obj.buffer_index = self.buffer_index
        # Fresh putback state (inherited from File)
        new_obj._putback_buf = bytearray()
        new_obj._putback_pos = 0
        new_obj._last_read_from_putback = None
        return new_obj
    
    def __str__(self) -> str:
        return f"ps.EexecDecryptionFilter({self.source_file.name})"
    
    def __repr__(self) -> str:
        return self.__str__()


class Run(File):
    TYPE = T_FILE
    
    def __init__(
        self,
        ctxt_id: int,
        name: str,
        mode: str,
        access: int = ACCESS_UNLIMITED,
        attrib: int = ATTRIB_LIT,
        is_composite: bool = False,
        is_global: bool = False,
    ) -> None:

        super().__init__(ctxt_id, name, mode, access, attrib, is_composite, is_global)

    def __copy__(self):
        """Optimized copy for Run - file-based run object."""
        new_obj = Run.__new__(Run)
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
        new_obj.ps_section_end = self.ps_section_end
        new_obj._putback_buf = bytearray()
        new_obj._putback_pos = 0
        new_obj._last_read_from_putback = None
        return new_obj

    def __str__(self) -> str:
        return f"ps.Run({self.val.name})"

    def __repr__(self) -> str:
        return f"ps.Run({self.val.name})"
