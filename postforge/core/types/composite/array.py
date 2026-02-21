# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostForge Types Composite Array Module

This module contains the PostScript Array and PackedArray type implementations,
which provide dynamic and immutable array operations respectively with complex
VM (Virtual Memory) management, reference tracking, and serialization support.

Array objects support mutable operations and complex reference semantics, while
PackedArray objects are immutable arrays with read-only access restrictions.

Extracted from composite.py during composite sub-package refactoring.
"""

import time
import copy
from typing import Union, Tuple

# Import error handling
from ... import error as ps_error

# Import base classes and constants
from ..base import PSObject
from ..constants import (
    ACCESS_UNLIMITED, ACCESS_READ_ONLY, ATTRIB_LIT, ATTRIB_EXEC,
    T_ARRAY, T_PACKED_ARRAY
)

# Import primitive types and context infrastructure
from ..primitive import Bool, Int
from ..context import contexts


class Array(PSObject):
    TYPE = T_ARRAY
    
    def __init__(
        self,
        ctxt_id: int,
        access: int = ACCESS_UNLIMITED,
        attrib: int = ATTRIB_LIT,
        is_composite: bool = True,
        is_global:bool = False
    ) -> None:
        super().__init__(None, access, attrib, is_composite, is_global)

        self.ctxt_id = ctxt_id
        self.val = []

        self.start = 0
        self.length = 0
        self.bound = False
        self.created = time.monotonic_ns()  # creation time for this composite object
        
        # Track all composite objects in appropriate refs immediately upon creation
        if ctxt_id is not None and contexts[ctxt_id] is not None:
            if self.is_global:
                contexts[ctxt_id].global_refs[self.created] = self.val
            else:
                contexts[ctxt_id].local_refs[self.created] = self.val

    _ALL_ATTRS = ('val', 'access', 'attrib', 'is_composite', 'is_global',
                  'ctxt_id', 'start', 'length', 'bound', 'created')

    def __copy__(self):
        """Optimized copy for Array - shallow copy of array contents."""
        new_array = Array.__new__(Array)
        new_array.val = self.val
        new_array.access = self.access
        new_array.attrib = self.attrib
        new_array.is_composite = self.is_composite
        new_array.is_global = self.is_global
        new_array.ctxt_id = self.ctxt_id
        new_array.start = self.start
        new_array.length = self.length
        new_array.bound = self.bound
        new_array.created = self.created
        return new_array

    def __deepcopy__(self, memo):
        """Deep copy for Array - creates new timestamp for copy."""
        import copy
        import time
        # Create new Array without calling __init__
        new_array = Array.__new__(Array)
        memo[id(self)] = new_array  # Register before recursing to handle cycles

        # Copy all attributes explicitly
        new_array.created = time.monotonic_ns()
        new_array.val = [copy.deepcopy(item, memo) for item in self.val]
        new_array.access = self.access
        new_array.attrib = self.attrib
        new_array.is_composite = self.is_composite
        new_array.is_global = copy.deepcopy(self.is_global, memo)
        new_array.ctxt_id = copy.deepcopy(self.ctxt_id, memo)
        new_array.start = self.start
        new_array.length = self.length
        new_array.bound = self.bound

        # Register in refs if composite
        if new_array.ctxt_id is not None and contexts[new_array.ctxt_id] is not None:
            if new_array.is_global:
                contexts[new_array.ctxt_id].global_refs[new_array.created] = new_array.val
            else:
                contexts[new_array.ctxt_id].local_refs[new_array.created] = new_array.val

        return new_array

    def __getstate__(self) -> dict:
        state = {attr: getattr(self, attr) for attr in Array._ALL_ATTRS}

        # dont save any global arrays we are referencing from local vm
        if self.is_global and contexts[self.ctxt_id].saving:
            contexts[self.ctxt_id].global_refs[self.created] = self.val
            # state['val'] = None

        return state

    def __setstate__(self, state: dict) -> None:
        for key, value in state.items():
            setattr(self, key, value)

        if self.is_global and contexts[self.ctxt_id].restoring:
            # restore the global val if it is in global_refs
            try:
                self.val = contexts[self.ctxt_id].global_refs[self.created]
            except KeyError:
                # This should not happen with proper reference tracking
                raise RuntimeError(f"SETSTATE: Missing global_refs timestamp {self.created} - reference tracking failed")
        elif not self.is_global:
            # Always save local composite objects in local_refs for reference tracking
            # This ensures all local objects can be found during restore operations
            contexts[self.ctxt_id].local_refs[self.created] = self.val

    def len(self) -> Int:
        return Int(self.length)

    def setval(self, value: list) -> None:
        # No _cow_check needed: setval replaces the entire backing store,
        # which naturally preserves the snapshot's reference to the old list.
        self.val = value
        self.length = len(value)

        # Update refs to track the correct val after reassignment
        if self.ctxt_id is not None:
            if self.is_global:
                contexts[self.ctxt_id].global_refs[self.created] = self.val
            else:
                contexts[self.ctxt_id].local_refs[self.created] = self.val

    def get(self, index: Int) -> Tuple[bool, Union[int, 'PSObject']]:
        if index.val < 0 or index.val > self.length - 1:
            return (False, ps_error.RANGECHECK)
        if self.access < ACCESS_READ_ONLY:
            return (False, ps_error.INVALIDACCESS)
        return (True, copy.copy(self.val[self.start + index.val]))

    def getinterval(self, index: Int, count: Int) -> Tuple[bool, Union[int, 'PSObject']]:
        if index.val < 0 or count.val < 0:
            return (False, ps_error.RANGECHECK)
        if index.val + count.val > self.length:
            return (False, ps_error.RANGECHECK)
        if self.access < ACCESS_READ_ONLY:
            return (False, ps_error.INVALIDACCESS)
        sub_array = copy.copy(self)
        sub_array.start = self.start + index.val
        sub_array.length = count.val
        return (True, sub_array)

    def _cow_check(self):
        """Copy-on-write barrier: save current state into snapshots, keep live ref intact."""
        if self.ctxt_id is not None:
            ctxt = contexts[self.ctxt_id]
            if ctxt and ctxt.cow_active and self.created in ctxt.cow_protected:
                # Save a frozen copy of current state into snapshots that still
                # reference the live backing store (not already frozen)
                old_copy = list(self.val)
                for snap_refs in ctxt.cow_snapshots.values():
                    if self.created in snap_refs and snap_refs[self.created] is self.val:
                        snap_refs[self.created] = old_copy
                ctxt.cow_protected.discard(self.created)
                # self.val stays the same — all live references continue working

    def put(self, index: Int, obj: 'PSObject') -> Tuple[bool, Union[int, None]]:
        # Check bounds against underlying storage, not just subarray length.
        # This matches GhostScript behavior where subarrays from getinterval
        # can write past their nominal length if within the original array's bounds.
        # Common in encoding vector manipulation (e.g., StandardEncoding subarrays).
        actual_index = self.start + index.val
        if index.val < 0 or actual_index >= len(self.val):
            return (False, ps_error.RANGECHECK)
        if self.access < ACCESS_UNLIMITED:
            return (False, ps_error.INVALIDACCESS)
        if obj.is_composite and self.is_global and not obj.is_global:
            return (False, ps_error.INVALIDACCESS)
        self._cow_check()
        self.val[actual_index] = copy.copy(obj)
        return (True, None)

    def putinterval(self, other: 'PSObject', index: Int) -> Tuple[bool, Union[int, None]]:
        if index.val < 0 or other.length + index.val > self.length:
            return (False, ps_error.RANGECHECK)
        if self.access < ACCESS_UNLIMITED or other.access < ACCESS_READ_ONLY:
            return (False, ps_error.INVALIDACCESS)
        if self.is_global:
            for i in range(other.length):
                if (
                    other.val[other.start + i].is_composite
                    and not other.val[other.start + i].is_global
                ):
                    return (False, ps_error.INVALIDACCESS)
        self._cow_check()
        for i in range(other.length):
            self.val[self.start + index.val + i] = copy.copy(other.val[other.start + i])

            # if this is a global composite object referenced from a local array
            # then add it to the global_refs dict
            if (
                not self.is_global
                and other.val[other.start + i].is_composite
                and other.val[other.start + i].is_global
            ):
                contexts[self.ctxt_id].global_refs[
                    other.val[other.start + i].created
                ] = other.val[other.start + i].val
        return (True, None)

    def reverse(self) -> None:
        self._cow_check()
        self.val.reverse()

    def __hash__(self):
        return self.created

    def __eq__(self, other) -> bool:
        if isinstance(other, Array):
            return id(self.val) == id(other.val)
        return False

    def __str__(self) -> str:
        if self.attrib == ATTRIB_EXEC:
            return (
                "{"
                + " ".join(
                    self.val[i].__str__()
                    for i in range(self.start, self.start + self.length, 1)
                )
                + "}"
            )
        else:
            return (
                "["
                + " ".join(
                    self.val[i].__str__()
                    for i in range(self.start, self.start + self.length, 1)
                )
                + "]"
            )

    def __repr__(self) -> str:
        return self.__str__()


class PackedArray(Array):
    TYPE = T_PACKED_ARRAY
    
    def __init__(
        self,
        ctxt_id: int,
        access: int = ACCESS_UNLIMITED,
        attrib: int = ATTRIB_LIT,
        is_composite: bool = True,
        is_global: bool = False,
    ) -> None:
        super().__init__(
            ctxt_id,
            access=ACCESS_READ_ONLY,
            attrib=attrib,
            is_composite=is_composite,
            is_global=is_global,
        )

    def __copy__(self):
        """Optimized copy for PackedArray - inherits from Array but read-only."""
        new_obj = PackedArray.__new__(PackedArray)
        new_obj.val = self.val
        new_obj.access = self.access
        new_obj.attrib = self.attrib
        new_obj.is_composite = self.is_composite
        new_obj.is_global = self.is_global
        new_obj.ctxt_id = self.ctxt_id
        new_obj.start = self.start
        new_obj.length = self.length
        new_obj.bound = self.bound
        new_obj.created = self.created
        return new_obj

    def __eq__(self, other) -> bool:
        if isinstance(other, PackedArray):
            return self.val == other.val
        return False