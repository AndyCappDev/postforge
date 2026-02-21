# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
PostForge Types Control Flow Classes Module

This module contains control flow and execution control PostScript types that manage
PostScript's execution model including loops, stopped execution states, and hard returns.
These types handle PostScript's control flow constructs like for/forall/repeat loops
and execution flow control mechanisms.
"""

# Import base classes and constants
from .base import PSObject
from .constants import (
    T_STOPPED, T_LOOP, T_HARD_RETURN, 
    ATTRIB_EXEC,
    LT_FOR, LT_FORALL, LT_REPEAT, LT_CSHOW, 
    LT_FILENAMEFORALL, LT_KSHOW, LT_PATHFORALL, LT_RESOURSEFORALL
)


# Control Flow classes extracted from legacy file

class Stopped(PSObject):
    TYPE = T_STOPPED
    
    def __init__(self, attrib=ATTRIB_EXEC) -> None:
        super().__init__(None, attrib=attrib)

    def __copy__(self) -> Stopped:
        """Optimized copy for Stopped - simple stopped object."""
        return Stopped(self.attrib)
    
    def __str__(self) -> str:
        return "--stopped--"

    def __repr__(self) -> str:
        return self.__str__()


class Loop(PSObject):
    TYPE = T_LOOP
    
    def __init__(self, loop_type: int) -> None:
        super().__init__(loop_type, attrib=ATTRIB_EXEC)

        # the for operand variables
        self.control = 0
        self.increment = 1
        self.limit = 1

        # the forall obj
        self.obj = None

        # for iterating dictionaries
        self.generator = None

        # for pathforall
        self.path_index = 0
        self.sub_path_index = 0
        self.path = None
        self.moveto_proc = None
        self.lineto_proc = None
        self.curveto_proc = None
        self.closepath_proc = None

    def __str__(self) -> str:
        if self.val == LT_FOR:
            return "for loop"
        elif self.val == LT_FORALL:
            return "forall loop"
        elif self.val == LT_REPEAT:
            return "repeat loop"
        elif self.val == LT_CSHOW:
            return "cshow loop"
        elif self.val == LT_FILENAMEFORALL:
            return "filenameforall loop"
        elif self.val == LT_KSHOW:
            return "kshow loop"
        elif self.val == LT_PATHFORALL:
            return "pathforall loop"
        elif self.val == LT_RESOURSEFORALL:
            return "resourceforall loop"
        else:
            return "loop"


class HardReturn(PSObject):
    TYPE = T_HARD_RETURN

    def __init__(self) -> None:
        super().__init__(None, attrib=ATTRIB_EXEC)

    def __copy__(self) -> HardReturn:
        """Optimized copy for HardReturn - simple control flow object."""
        return HardReturn()
