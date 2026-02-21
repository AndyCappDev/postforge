# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostForge Types Primitive Classes Module

This module contains simple atomic PostScript types that represent fundamental
data values. These types are typically immutable and have straightforward
value semantics.
"""

from typing import Union

# Import base classes and constants
from .base import PSObject
from .constants import (
    ACCESS_READ_ONLY, ATTRIB_LIT,
    T_BOOL, T_NULL, T_INT, T_REAL, T_MARK
)


class Bool(PSObject):
    """PostScript boolean type - represents true/false values."""
    TYPE = T_BOOL

    def __init__(self, val: bool, access: int = ACCESS_READ_ONLY, attrib: int = ATTRIB_LIT) -> None:
        self.val = val
        self.access = access
        self.attrib = attrib
        self.is_composite = False
        self.is_global = False

    def __copy__(self):
        """Optimized copy for Bool - immutable-like type."""
        new_obj = object.__new__(Bool)
        new_obj.val = self.val
        new_obj.access = self.access
        new_obj.attrib = self.attrib
        new_obj.is_composite = False
        new_obj.is_global = False
        return new_obj

    def __str__(self) -> str:
        return str(self.val).lower()

    def __repr__(self) -> str:
        return self.__str__()


class Null(PSObject):
    """PostScript null type - represents null/empty values."""
    TYPE = T_NULL

    def __init__(self, val: None = None, access: int = ACCESS_READ_ONLY, attrib: int = ATTRIB_LIT) -> None:
        self.val = val
        self.access = access
        self.attrib = attrib
        self.is_composite = False
        self.is_global = False

    def __copy__(self) -> 'Null':
        new_obj = object.__new__(Null)
        new_obj.val = self.val
        new_obj.access = self.access
        new_obj.attrib = self.attrib
        new_obj.is_composite = False
        new_obj.is_global = False
        return new_obj

    def __eq__(self, other: PSObject) -> bool:
        if not isinstance(other, Null):
            return False
        return self.val == other.val

    def __str__(self) -> str:
        return "null"

    def __repr__(self) -> str:
        return self.__str__()


class Int(PSObject):
    """PostScript integer type - represents whole number values."""
    TYPE = T_INT

    def __init__(self, val: int, access: int = ACCESS_READ_ONLY, attrib: int = ATTRIB_LIT) -> None:
        self.val = val
        self.access = access
        self.attrib = attrib
        self.is_composite = False
        self.is_global = False

    def __copy__(self):
        """Optimized copy for Int - direct attribute assignment."""
        new_obj = object.__new__(Int)
        new_obj.val = self.val
        new_obj.access = self.access
        new_obj.attrib = self.attrib
        new_obj.is_composite = False
        new_obj.is_global = False
        return new_obj

    def __eq__(self, other: PSObject) -> bool:
        if not isinstance(other, (Int, Real)):
            return False
        # Use exact comparison - PostScript eq requires same mathematical value
        return self.val == other.val

    def __str__(self) -> str:
        return str(self.val)

    def __repr__(self) -> str:
        return self.__str__()


class Real(PSObject):
    """PostScript real (floating-point) type - represents decimal number values."""
    TYPE = T_REAL

    def __init__(self, val: float, access: int = ACCESS_READ_ONLY, attrib: int = ATTRIB_LIT):
        self.val = val
        self.access = access
        self.attrib = attrib
        self.is_composite = False
        self.is_global = False

    def __copy__(self):
        """Optimized copy for Real - direct attribute assignment."""
        new_obj = object.__new__(Real)
        new_obj.val = self.val
        new_obj.access = self.access
        new_obj.attrib = self.attrib
        new_obj.is_composite = False
        new_obj.is_global = False
        return new_obj

    def __eq__(self, other: PSObject) -> bool:
        if not isinstance(other, (Int, Real)):
            return False
        # Use exact comparison - PostScript eq requires same mathematical value
        return self.val == other.val

    def __str__(self) -> str:
        # Format real numbers compactly per PLRM cvs requirements
        # Round to 6 decimal places to avoid floating-point noise
        rounded = round(self.val, 6)

        # Check if it's effectively an integer
        if rounded == int(rounded) and abs(rounded) < 1e10:
            # Format as integer-like but keep decimal point for PostScript
            return f"{int(rounded)}.0"

        # Format with up to 6 decimal places, stripping trailing zeros
        formatted = f"{rounded:.6f}".rstrip('0')

        # Ensure at least one digit after decimal point
        if formatted.endswith('.'):
            formatted += '0'

        return formatted

    def __repr__(self) -> str:
        return self.__str__()


class Mark(PSObject):
    """PostScript mark type - represents stack markers for array/procedure construction."""
    TYPE = T_MARK
    
    def __init__(self, code: bytearray, attrib=ATTRIB_LIT) -> None:
        super().__init__(bytes(code), attrib=attrib)

    def python_string(self) -> str:
        return "".join(chr(c) for c in self.code)

    def __eq__(self, other) -> bool:
        if isinstance(other, Mark):
            return True
        return False

    def __copy__(self):
        """Optimized copy for Mark - simple mark object."""
        return Mark(bytearray(self.val), self.attrib)
    
    def __str__(self) -> str:
        return f"mark-{self.val.decode()}"

    def __repr__(self) -> str:
        return self.__str__()
