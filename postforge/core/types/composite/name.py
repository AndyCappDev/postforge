# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostForge Types Composite Name Module

This module contains the PostScript Name type implementation, which provides
symbol and identifier operations with hash-based lookups and comparison
semantics compatible with PostScript language requirements.

Name objects represent PostScript names (symbols) and support efficient
hash-based dictionary operations and cross-type comparisons with String objects.

Extracted from composite.py during composite sub-package refactoring.
"""

import time
from typing import Union

# Import base classes and constants
from ..base import PSObject
from ..constants import (
    ACCESS_UNLIMITED, ATTRIB_LIT, ATTRIB_EXEC,
    T_NAME, T_STRING
)

# Import primitive types
from ..primitive import Bool, Int

# Forward reference - will be set by composite package __init__.py
String = None  # For Name ↔ String comparison operations


class Name(PSObject):
    TYPE = T_NAME

    def __init__(
        self,
        name: Union[bytes, bytearray],
        access: int = ACCESS_UNLIMITED,
        attrib: int = ATTRIB_LIT,
        is_composite=False,
        is_global=False,
    ) -> None:
        val = bytes(name) if isinstance(name, bytearray) else name
        super().__init__(
            val,
            access,
            attrib,
            is_composite,
            is_global,
        )
        # Cache hash since Name.val is immutable - avoids 76M hash() calls
        self._hash = hash(val)
        self.created = time.monotonic_ns()  # creation time for this composite object

    def __copy__(self):
        """Optimized copy for Name - immutable-like type."""
        new = Name.__new__(Name)
        new.val = self.val
        new.access = self.access
        new.attrib = self.attrib
        new.is_composite = self.is_composite
        new.is_global = self.is_global
        new._hash = self._hash  # Copy cached hash
        new.created = self.created
        return new

    def __hash__(self):
        return self._hash

    def __eq__(self, other) -> bool:
        # Fast path: check TYPE attribute first (avoids isinstance for PS objects)
        other_type = getattr(other, 'TYPE', None)
        if other_type == T_NAME:
            return self.val == other.val
        if other_type == T_STRING and String is not None:
            return self.val == other.byte_string()
        if isinstance(other, bytes):
            return self.val == other
        return False

    def len(self) -> Int:
        return Int(len(self.val))

    def __str__(self) -> str:
        return (
            f"{self.val.decode()}"
            if self.attrib == ATTRIB_EXEC
            else f"/{self.val.decode()}"
        )

    def __repr__(self) -> str:
        return self.__str__()