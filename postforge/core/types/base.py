# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostForge Types Base Classes Module

This module contains the core base classes that form the foundation of the
PostScript type system. These classes define the fundamental patterns and
behaviors shared across all PostScript objects.
"""

from typing import Any
from math import isclose

# Import constants needed by base classes
from .constants import (
    ACCESS_UNLIMITED, ACCESS_READ_ONLY, ATTRIB_LIT, 
    T_STRING, T_BOOL, T_INT, T_REAL, T_NULL
)


class PSObject(object):
    """
    Base class for all PostScript objects.
    
    Defines the fundamental structure and behavior patterns shared by all
    PostScript types including access control, attributes, and basic operations.
    """
    TYPE = None  # Base class - no specific type
    
    def __init__(
        self,
        val: Any,
        access: int = ACCESS_UNLIMITED,
        attrib: int = ATTRIB_LIT,
        is_composite: bool = False,
        is_global: bool = False,
    ) -> None:
        self.val = val

        self._access = access
        self.attrib = attrib
        self.is_composite = is_composite
        self.is_global = is_global
    
    def __copy__(self):
        """Optimized copy method for base PSObject.
        Creates new instance with same data, avoiding expensive pickle protocol."""
        new_obj = self.__class__.__new__(self.__class__)
        new_obj.val = self.val
        new_obj._access = self._access
        new_obj.attrib = self.attrib
        new_obj.is_composite = self.is_composite
        new_obj.is_global = self.is_global
        return new_obj

    def access(self):
        return self._access

    # Comparison operators will be implemented by concrete subclasses
    # that have access to the Bool class to return proper PostScript objects


class Stream(PSObject):
    """
    Base class for all stream-type PostScript objects.
    
    Provides common functionality for objects that support stream operations
    like reading, writing, and positioning. Subclasses must implement specific
    stream operations: read, unread, close, fileposition.
    """
    TYPE = T_STRING  # Default for stream types, override in subclasses
    
    def __init__(
        self,
        val: Any,
        access: int = ACCESS_UNLIMITED,
        attrib: int = ATTRIB_LIT,
        is_composite=True,
        is_global=False,
    ) -> None:
        super().__init__(val, access, attrib, is_composite, is_global)

        self.line_num = 1
    
    def __copy__(self):
        """Optimized copy for Stream base class."""
        new_obj = self.__class__.__new__(self.__class__)
        new_obj.val = self.val
        new_obj._access = self._access
        new_obj.attrib = self.attrib
        new_obj.is_composite = self.is_composite
        new_obj.is_global = self.is_global
        new_obj.line_num = self.line_num
        return new_obj