# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostForge Types Composite Dict Module

This module contains the PostScript Dict (Dictionary) type implementation,
which provides key-value store operations with VM (Virtual Memory) management,
reference tracking, and complex serialization support for PostScript dictionaries.

Dict objects support mutable operations, custom key creation, and complex
reference semantics essential for PostScript's dictionary-based execution model.

Extracted from composite.py during composite sub-package refactoring.
"""

import time
from typing import Union, Tuple

# Import base classes and constants
from ..base import PSObject
from ..constants import (
    ACCESS_UNLIMITED, ATTRIB_LIT,
    T_DICT
)

# Import primitive types and context infrastructure
from ..primitive import Bool, Int, Real, Null
from ..context import contexts, global_resources

# Forward references - resolved by composite package __init__.py
Name = None
String = None


def _copy_string_key(key):
    """Create an independent copy of a String for use as a dict key.

    Per PLRM: 'If key is a string, put first makes a copy of key
    and uses the copy as the key in dict.'

    This ensures the stored key has stable byte_string() and __hash__(),
    even if the original string's underlying storage is later modified
    (e.g., string literals in procedure bodies reused across loop iterations).
    """
    src_bytes = key.byte_string()
    if key.is_global:
        storage = global_resources.global_strings
    else:
        storage = contexts[key.ctxt_id].local_strings
    offset = len(storage)
    storage += src_bytes
    return String(
        key.ctxt_id,
        offset,
        len(src_bytes),
        start=0,
        access=key.access,
        attrib=key.attrib,
        is_global=key.is_global,
    )


class Dict(PSObject):
    TYPE = T_DICT
    
    def __init__(
        self,
        ctxt_id: int,
        d: dict = None,
        name: bytes = b"dictionary",
        max_length: int = 0,
        access: int = ACCESS_UNLIMITED,
        attrib: int = ATTRIB_LIT,
        is_composite: bool = True,
        is_global=False,
    ) -> None:
        super().__init__(None, access, attrib, is_composite, is_global)

        self.ctxt_id = ctxt_id
        self.val = d
        if not self.val:
            self.val = {}
        self.name = name
        self.max_length = max_length
        self.created = time.monotonic_ns()  # creation time for this composite object

        self.access = access
        
        # Track all composite objects in appropriate refs immediately upon creation
        if ctxt_id is not None and contexts[ctxt_id] is not None:
            if self.is_global:
                contexts[ctxt_id].global_refs[self.created] = self.val
            else:
                contexts[ctxt_id].local_refs[self.created] = self.val

    def __getitem__(self, item):
        return self.val[item]

    _ALL_ATTRS = ('val', 'access', 'attrib', 'is_composite', 'is_global',
                  'ctxt_id', 'name', 'max_length', 'created')

    def __copy__(self):
        """Optimized copy for Dict - preserves shared dictionary reference."""
        new_dict = Dict.__new__(Dict)
        new_dict.val = self.val
        new_dict.access = self.access
        new_dict.attrib = self.attrib
        new_dict.is_composite = self.is_composite
        new_dict.is_global = self.is_global
        new_dict.ctxt_id = self.ctxt_id
        new_dict.name = self.name
        new_dict.max_length = self.max_length
        new_dict.created = self.created
        return new_dict

    def __deepcopy__(self, memo):
        """Deep copy for Dict - creates new timestamp for copy."""
        import copy
        import time
        # Create new Dict without calling __init__
        new_dict = Dict.__new__(Dict)
        memo[id(self)] = new_dict  # Register before recursing to handle cycles

        # Copy all attributes explicitly
        new_dict.created = time.monotonic_ns()
        new_dict.val = {}
        for k, v in self.val.items():
            new_dict.val[k] = copy.deepcopy(v, memo)
        new_dict.access = self.access
        new_dict.attrib = self.attrib
        new_dict.is_composite = self.is_composite
        new_dict.is_global = copy.deepcopy(self.is_global, memo)
        new_dict.ctxt_id = copy.deepcopy(self.ctxt_id, memo)
        new_dict.name = copy.deepcopy(self.name, memo)
        new_dict.max_length = self.max_length

        # Register in local_refs if local
        if new_dict.ctxt_id is not None and contexts[new_dict.ctxt_id] is not None:
            if new_dict.is_global:
                contexts[new_dict.ctxt_id].global_refs[new_dict.created] = new_dict.val
            else:
                contexts[new_dict.ctxt_id].local_refs[new_dict.created] = new_dict.val

        return new_dict

    def __getstate__(self) -> dict:
        state = {attr: getattr(self, attr) for attr in Dict._ALL_ATTRS}

        # dont save any global dictionaries we are referencing from local vm
        if self.is_global and contexts[self.ctxt_id].saving:
            contexts[self.ctxt_id].global_refs[self.created] = self.val

        return state

    def __setstate__(self, state: dict) -> None:
        for key, value in state.items():
            setattr(self, key, value)

        if self.is_global and contexts[self.ctxt_id].restoring:
            # restore the val if it is in global_refs
            try:
                self.val = contexts[self.ctxt_id].global_refs[self.created]
            except KeyError:
                # This should not happen with proper reference tracking
                raise RuntimeError(f"SETSTATE: Missing global_refs timestamp {self.created} - reference tracking failed")
        elif not self.is_global:
            # Always save local composite objects in local_refs for reference tracking
            # This ensures all local objects can be found during restore operations
            contexts[self.ctxt_id].local_refs[self.created] = self.val

    def maxlength(self) -> Int:
        return Int(len(self.val))

    def create_key(self, obj: 'PSObject') -> Union[bytes, int, float, bool]:
        # get the actual key to use based on the type of object
        # assumes obj is ONLY one of the following hashable types:
        #   ps.String, ps.Name, ps.Int, ps.Real, ps.Bool

        if isinstance(obj, (Int, Real, Null, Bool)):
            return obj.val
        return obj

    def _cow_check(self):
        """Copy-on-write barrier: save current state into snapshots, keep live ref intact."""
        if self.ctxt_id is not None:
            ctxt = contexts[self.ctxt_id]
            if ctxt and ctxt.cow_active and self.created in ctxt.cow_protected:
                # Save a frozen copy of current state into snapshots that still
                # reference the live backing store (not already frozen)
                old_copy = dict(self.val)
                for snap_refs in ctxt.cow_snapshots.values():
                    if self.created in snap_refs and snap_refs[self.created] is self.val:
                        snap_refs[self.created] = old_copy
                ctxt.cow_protected.discard(self.created)
                # self.val stays the same — all live references continue working

    def put(self, key: 'PSObject', value: 'PSObject') -> Tuple[bool, None]:
        self._cow_check()
        # PLRM: "If key is a string, put first makes a copy of key
        # and uses the copy as the key in dict."
        if String is not None and isinstance(key, String):
            key = _copy_string_key(key)
        self.val[self.create_key(key)] = value
        if len(self.val) == self.max_length:
            # increase the max_length by 10 if we have reached the limit
            self.max_length += 10
        return (True, None)

    def len(self) -> Int:
        return Int(len(self.val))

    def __hash__(self):
        return self.created

    def __eq__(self, other) -> bool:
        if isinstance(other, Dict):
            return self.created == other.created
        return False

    def __str__(self) -> str:
        if self.name is not None:
            return f"<<{self.name}>>"
        else:
            return "<<dictionary>>"
        """
        Return a string reprsentation of all the keys and values in the dictionary.
        Except for the self referenced systemdict if this is the systemdict.
        """
        return (
            "<< "
            + ", ".join(
                [
                    f"{Name(key) if isinstance(key, bytes) else key}: {val}"
                    for key, val in self.val.items()
                    if val.val != self.val
                ]
            )
            + " >>"
        )

    def __repr__(self) -> str:
        return self.__str__()