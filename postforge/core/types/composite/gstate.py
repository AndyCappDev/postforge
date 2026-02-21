# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostForge Types Composite GState Module

This module contains the PostScript GState composite type implementation, which
provides PostScript Level 2 gstate object functionality for saving and restoring
complete graphics state contexts independently of the graphics state stack.

The GState composite object contains a GraphicsState instance and provides
proper VM (Virtual Memory) allocation tracking and access control.
"""

import time
import copy

# Import error handling
from ... import error as ps_error

# Import base classes and constants
from ..base import PSObject
from ..primitive import Bool
from ..constants import (
    ACCESS_UNLIMITED, ATTRIB_LIT, T_GSTATE
)

# Import context infrastructure for VM management
from ..context import contexts


class GState(PSObject):
    """
    PostScript Level 2 gstate composite object.

    Contains a complete graphics state (GraphicsState instance) and provides
    VM allocation tracking for save/restore operations. Used by the gstate,
    currentgstate, and setgstate operators.
    """
    TYPE = T_GSTATE

    def __init__(
        self,
        ctxt_id: int,
        graphics_state,  # GraphicsState instance
        access: int = ACCESS_UNLIMITED,
        attrib: int = ATTRIB_LIT,
        is_composite: bool = True,
        is_global: bool = False
    ) -> None:
        super().__init__(graphics_state, access, attrib, is_composite, is_global)

        self.ctxt_id = ctxt_id
        self.created = time.monotonic_ns()  # creation time for this composite object

        # Track all composite objects in appropriate refs immediately upon creation
        if ctxt_id is not None and contexts[ctxt_id] is not None:
            if self.is_global:
                contexts[ctxt_id].global_refs[self.created] = self.val
            else:
                contexts[ctxt_id].local_refs[self.created] = self.val

    _ALL_ATTRS = ('val', 'access', 'attrib', 'is_composite', 'is_global',
                  'ctxt_id', 'created')

    def __copy__(self):
        """Optimized copy for GState - shallow copy of gstate contents."""
        new_gstate = GState.__new__(GState)
        new_gstate.val = self.val
        new_gstate.access = self.access
        new_gstate.attrib = self.attrib
        new_gstate.is_composite = self.is_composite
        new_gstate.is_global = self.is_global
        new_gstate.ctxt_id = self.ctxt_id
        new_gstate.created = self.created
        return new_gstate

    def __getstate__(self) -> dict:
        state = {attr: getattr(self, attr) for attr in GState._ALL_ATTRS}

        # dont save any global gstates we are referencing from local vm
        if self.is_global and contexts[self.ctxt_id].saving:
            contexts[self.ctxt_id].global_refs[self.created] = self.val
            # state['val'] = None

        return state

    def __setstate__(self, state: dict) -> None:
        for key, value in state.items():
            setattr(self, key, value)

        # Restore references if needed during VM restore
        if self.is_global and contexts[self.ctxt_id] is not None:
            if self.created in contexts[self.ctxt_id].global_refs:
                self.val = contexts[self.ctxt_id].global_refs[self.created]

    def validate_global_vm_constraints(self, ctxt):
        """
        Validate that this GState can be stored in global VM.

        Checks all composite objects in the contained GraphicsState to ensure
        they are compatible with global VM storage per PLRM requirements.

        Args:
            ctxt: PostScript execution context

        Returns:
            True if valid for global VM, False if contains local objects
        """
        if not self.is_global:
            return True  # Local GState objects have no restrictions

        # Check GraphicsState composite objects for local VM violations
        graphics_state = self.val

        # Check CTM and iCTM Arrays (most common cause of invalidaccess)
        if hasattr(graphics_state, 'CTM') and graphics_state.CTM and not graphics_state.CTM.is_global:
            return False
        if hasattr(graphics_state, 'iCTM') and graphics_state.iCTM and not graphics_state.iCTM.is_global:
            return False

        # Check font dictionary if present
        if hasattr(graphics_state, 'font') and graphics_state.font:
            if hasattr(graphics_state.font, 'is_global') and not graphics_state.font.is_global:
                return False

        # Check transfer function if present
        if hasattr(graphics_state, 'transfer_function') and graphics_state.transfer_function:
            if hasattr(graphics_state.transfer_function, 'is_global') and not graphics_state.transfer_function.is_global:
                return False

        # Check halftone dictionary if present (would be in page_device or separate)
        # Note: page_device is a plain Python dict in current implementation

        # All checks passed
        return True

    def copy_graphics_state(self):
        """
        Create a deep copy of the contained GraphicsState.

        Returns:
            Deep copy of the GraphicsState instance
        """
        return self.val.copy() if self.val else None

    def __eq__(self, other) -> bool:
        """
        GState object equality comparison.

        Following the same pattern as Array objects, compare the identity
        of the underlying val (GraphicsState) objects.
        """

        if isinstance(other, GState):
            return id(self.val) == id(other.val)
        return False

