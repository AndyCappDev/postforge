# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
PostForge Types Utility Classes Module

This module contains utility PostScript types that support the PostScript interpreter
including operator wrappers, VM save objects, and font objects. These types provide
essential infrastructure for PostScript execution but don't fit into other specific
categories.
"""

import time
from typing import Callable

# Import base classes and constants
from .base import PSObject
from .constants import (
    T_OPERATOR, T_SAVE, T_FONT,
    ATTRIB_EXEC
)


# Utility classes extracted from legacy file

class Operator(PSObject):
    TYPE = T_OPERATOR
    
    def __init__(self, op: Callable, attrib: int = ATTRIB_EXEC) -> None:
        super().__init__(op, attrib=attrib)

    def __copy__(self) -> Operator:
        """Optimized copy for Operator - function wrapper."""
        return Operator(self.val, self.attrib)
    
    def __str__(self) -> str:
        return (
            f"--{self.val.__name__[3:]}--"
            if self.val.__name__.startswith("ps_")
            else f"--{self.val.__name__}--"
        )

    def __repr__(self) -> str:
        return self.__str__()


class Save(PSObject):
    TYPE = T_SAVE
    
    def __init__(self, id: int) -> None:
        super().__init__(id)

        self.id = id
        self.created = time.monotonic_ns()  # creation time for this composite object
        self.valid = True  # save objects become invalid after restore

    def __copy__(self) -> Save:
        """Optimized copy for Save - VM save level object."""
        new_obj = Save.__new__(Save)
        new_obj.val = self.val
        new_obj.access = self.access
        new_obj.attrib = self.attrib
        new_obj.is_composite = self.is_composite
        new_obj.is_global = self.is_global
        new_obj.id = self.id
        new_obj.created = self.created
        new_obj.valid = self.valid
        return new_obj
    
    def __str__(self) -> str:
        return f"save: level {self.id}"

    def __repr__(self) -> str:
        return self.__str__()


class Font(PSObject):
    TYPE = T_FONT
    
    def __init__(self) -> None:
        super().__init__(None)

        self.created = time.monotonic_ns()  # creation time for this composite object

    def __copy__(self) -> Font:
        """Optimized copy for Font - font object with timestamp."""
        new_obj = Font.__new__(Font)
        new_obj.val = self.val
        new_obj.access = self.access
        new_obj.attrib = self.attrib
        new_obj.is_composite = self.is_composite
        new_obj.is_global = self.is_global
        new_obj.created = self.created
        return new_obj
