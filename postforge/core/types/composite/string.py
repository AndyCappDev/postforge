# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostForge Types Composite String Module

This module contains the PostScript String type implementation, which provides
stream-based string operations with VM (Virtual Memory) management, encoding
handling, and PostScript-compliant string semantics.

String objects act as both data containers and streams, supporting read/unread
operations and complex memory allocation between local and global VM.

Extracted from composite.py during composite sub-package refactoring.
"""

import time
import copy
from typing import Union, Tuple

# Import error handling
from ... import error as ps_error

# Import base classes and constants
from ..base import Stream
from ..constants import (
    ACCESS_UNLIMITED, ACCESS_READ_ONLY, ATTRIB_LIT,
    T_STRING
)

# Import primitive types and context infrastructure
from ..primitive import Bool, Int
from ..context import contexts, global_resources

# Forward reference - will be set by composite package __init__.py
Name = None  # For String ↔ Name comparison operations


class String(Stream):
    TYPE = T_STRING
    
    def __init__(
        self,
        ctxt_id: int,
        offset: int,
        length: int,
        start: int = 0,
        access: int = ACCESS_UNLIMITED,
        attrib: int = ATTRIB_LIT,
        is_composite: bool = True,
        is_global=False,
        val = None
    ) -> None:
        # for strings, the val is actualy the Context the string belongs to
        super().__init__(None, access, attrib, is_composite, is_global)

        self.ctxt_id = ctxt_id
        self.offset = offset
        self.length = length
        self.start = start
        self.access = access
        if val:
            self.val = val
        # self.is_global = is_global
        self.created = time.monotonic_ns()  # creation time for this composite object
        
        # Track all local composite objects in local_refs immediately upon creation
        if not self.is_global and ctxt_id is not None and contexts[ctxt_id] is not None:
            # For strings, the "val" is the string content in the context's string storage
            # Try to decode as UTF-8, but fall back to binary representation for hex strings
            try:
                contexts[ctxt_id].local_refs[self.created] = self.python_string()
            except UnicodeDecodeError:
                # This is likely a hex string with binary data - store as bytes representation
                contexts[ctxt_id].local_refs[self.created] = f"<binary data: {len(self.byte_string())} bytes>"

        # need to compute this on the fly
        # self.strings = ctxt.global_strings if ctxt.vm_alloc_mode else ctxt.local_strings

        # is_defined is set to true if this string has been defined in any dictionary
        # this is used to save string vm by backing out the last string that was
        # allocated if it was just a temporary string not def'd
        self.is_defined = False

        # is_substring is set to True if this string is the substring of another string.
        # we dont want to reclaim the vm used by this string if it is a substring
        # even if self.is_defined is False
        self.is_substring = False

    def byte_string(self) -> bytes:
        global contexts
        strings = (
            global_resources.global_strings
            if self.is_global
            else contexts[self.ctxt_id].local_strings
        )
        return bytes(
            strings[self.offset + self.start : self.offset + self.start + self.length]
        )

    def close(self) -> None:
        pass  # ???

    def len(self) -> Int:
        return Int(self.length)

    def get(self, index: Int) -> Tuple[bool, Union[int, 'PSObject']]:
        if index.val < 0 or index.val > self.length - 1:
            return (False, ps_error.RANGECHECK)
        if self.access < ACCESS_READ_ONLY:
            return (False, ps_error.INVALIDACCESS)
        strings = (
            global_resources.global_strings
            if self.is_global
            else contexts[self.ctxt_id].local_strings
        )
        return (True, Int(strings[self.offset + self.start + index.val]))

    def getinterval(self, index: Int, count: Int) -> Union[None, 'PSObject']:
        # TODO -- This must account for self and other NOT being in the same vm_alloc_mode
        if self.access < ACCESS_READ_ONLY:
            return (False, ps_error.INVALIDACCESS)
        if index.val < 0 or count.val < 0:
            return (False, ps_error.RANGECHECK)
        if index.val + count.val > self.length:
            return (False, ps_error.RANGECHECK)
        sub_str = copy.copy(self)
        sub_str.start = self.start + index.val
        sub_str.length = count.val
        return (True, sub_str)

    def put(self, index: Int, integer: Int) -> Tuple[bool, None]:
        if (
            index.val < 0
            or index.val > self.length - 1
            or integer.val < 0
            or integer.val > 255
        ):
            return (False, ps_error.RANGECHECK)
        strings = (
            global_resources.global_strings
            if self.is_global
            else contexts[self.ctxt_id].local_strings
        )
        strings[self.offset + self.start + index.val] = integer.val
        return (True, None)

    def putinterval(self, other: 'PSObject', index: Int) -> Union[None, 'PSObject']:
        if index.val < 0 or other.length + index.val > self.length:
            return (False, ps_error.RANGECHECK)
        # Destination buffer (self)
        dst_strings = (
            global_resources.global_strings
            if self.is_global
            else contexts[self.ctxt_id].local_strings
        )
        # Source buffer (other) - may be in different VM allocation
        src_strings = (
            global_resources.global_strings
            if other.is_global
            else contexts[other.ctxt_id].local_strings
        )
        for i in range(other.length):
            dst_strings[self.offset + self.start + index.val + i] = src_strings[
                other.offset + other.start + i
            ]
        return (True, None)

    def __hash__(self):
        return hash(self.byte_string())

    def __eq__(self, other) -> bool:
        if isinstance(other, String):
            return self.byte_string() == other.byte_string()
        if Name is not None and isinstance(other, Name):
            return self.byte_string() == other.val
        if isinstance(other, bytes):
            return self.byte_string() == other
        return False

    def __ge__(self, other) -> Bool:
        if isinstance(other, String):
            return Bool(self.byte_string() >= other.byte_string())
        return Bool(False)

    def __gt__(self, other) -> Bool:
        if isinstance(other, String):
            return Bool(self.byte_string() > other.byte_string())
        return Bool(False)

    def __le__(self, other) -> Bool:
        if isinstance(other, String):
            return Bool(self.byte_string() <= other.byte_string())
        return Bool(False)

    def __lt__(self, other) -> Bool:
        if isinstance(other, String):
            return Bool(self.byte_string() < other.byte_string())
        return Bool(False)

    def python_string(self) -> str:
        global contexts

        strings = (
            global_resources.global_strings
            if self.is_global
            else contexts[self.ctxt_id].local_strings
        )
        try:
            return strings[
                self.offset + self.start : self.offset + self.start + self.length
            ].decode()
        except UnicodeDecodeError:
            return strings[
                self.offset + self.start : self.offset + self.start + self.length
            ].decode(errors='ignore')

    def read(self, ctxt: "Context") -> Union[int, None]:
        """
        reads 1 byte from the string.
        this actually "consumes" one byte from the string
        """

        if not self.length:
            # string is completely consumed
            return None

        self.start += 1
        self.length -= 1
        strings = global_resources.global_strings if self.is_global else ctxt.local_strings
        return strings[self.offset + self.start - 1]

    def unread(self) -> None:
        # puts a byte back into the string
        # need this because we need to read ahead 1 byte a lot

        if not self.start:
            return

        self.start -= 1
        self.length += 1

    _ALL_ATTRS = ('val', 'access', 'attrib', 'is_composite', 'is_global',
                  'line_num', 'ctxt_id', 'offset', 'length', 'start',
                  'created', 'is_defined', 'is_substring')

    def __copy__(self):
        """Optimized copy for String - preserves string data and metadata."""
        new_obj = String.__new__(String)
        new_obj.val = self.val
        new_obj.access = self.access
        new_obj.attrib = self.attrib
        new_obj.is_composite = self.is_composite
        new_obj.is_global = self.is_global
        new_obj.line_num = self.line_num
        new_obj.ctxt_id = self.ctxt_id
        new_obj.offset = self.offset
        new_obj.length = self.length
        new_obj.start = self.start
        new_obj.created = self.created
        new_obj.is_defined = self.is_defined
        new_obj.is_substring = self.is_substring
        return new_obj

    def __deepcopy__(self, memo):
        """Deep copy for String - creates new timestamp for copy."""
        import copy
        import time
        # Create new String without calling __init__
        new_str = String.__new__(String)
        memo[id(self)] = new_str  # Register before recursing to handle cycles

        # Copy all attributes explicitly
        new_str.created = time.monotonic_ns()
        new_str.val = copy.deepcopy(self.val, memo)
        new_str.access = self.access
        new_str.attrib = self.attrib
        new_str.is_composite = self.is_composite
        new_str.is_global = copy.deepcopy(self.is_global, memo)
        new_str.line_num = self.line_num
        new_str.ctxt_id = copy.deepcopy(self.ctxt_id, memo)
        new_str.offset = self.offset
        new_str.length = self.length
        new_str.start = self.start
        new_str.is_defined = self.is_defined
        new_str.is_substring = self.is_substring

        # Register in local_refs if local
        if not new_str.is_global and new_str.ctxt_id is not None and contexts[new_str.ctxt_id] is not None:
            try:
                contexts[new_str.ctxt_id].local_refs[new_str.created] = new_str.python_string()
            except UnicodeDecodeError:
                contexts[new_str.ctxt_id].local_refs[new_str.created] = f"<binary data: {len(new_str.byte_string())} bytes>"

        return new_str

    def __str__(self) -> str:
        return f"({self.python_string()})"

    def __repr__(self) -> str:
        return self.__str__()