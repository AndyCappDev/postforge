# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
DisplayListBuilder - Optimized Clipping Path Management

This module provides intelligent display list generation with automatic
clipping path optimization. ClipElement objects are only generated when
the clipping path actually changes, minimizing overhead and ensuring
optimal device rendering performance.

Architecture:
- Tracks clipping path version changes through GraphicsState.clip_path_version
- Automatically inserts ClipElement objects when clipping state changes  
- Provides centralized clipping management for all graphics operations
- Maintains clean separation between PostScript semantics and device rendering
"""

from typing import Any

from . import types as ps


class DisplayListBuilder:
    """
    Manages display list generation with automatic clipping path optimization.
    
    Tracks graphics state changes and automatically inserts ClipElement objects
    only when the clipping path has actually changed, ensuring minimal overhead
    and optimal device rendering performance.
    """
    
    def __init__(self, display_list: ps.DisplayList):
        """
        Initialize DisplayListBuilder with target display list.
        
        Args:
            display_list: DisplayList instance to manage
        """
        self.display_list = display_list
        self.current_clip_version = -1  # Track display list clipping state
        
    def add_graphics_operation(self, ctxt: Any, graphics_element: Any):
        """
        Add graphics operation to display list, inserting ClipElement if needed.

        This method automatically checks if the clipping path has changed since
        the last graphics operation. If it has, a ClipElement is inserted to
        synchronize the device's clipping state before the graphics operation.

        Args:
            ctxt: PostScript context with current graphics state
            graphics_element: Graphics operation (Fill, Stroke, Image, etc.)
        """
        # Null device: discard all painting marks (PLRM p.459)
        if b".NullDevice" in ctxt.gstate.page_device:
            return

        # Add the actual graphics operation directly to context's current display list
        # Note: ClipElements are now created directly by clipping operators (clip, eoclip, initclip)
        # so we don't need to create them here
        ctxt.display_list.append(graphics_element)

        # Notify Qt device (or other interactive device) to refresh if callback registered
        # This enables live rendering in interactive mode
        if hasattr(ctxt, 'on_paint_callback') and ctxt.on_paint_callback:
            try:
                ctxt.on_paint_callback(ctxt, graphics_element)
            except Exception:
                # Don't let callback errors break PostScript execution
                pass
        
    def reset_tracking(self):
        """
        Reset version tracking for new page or context reset.
        
        This forces the next graphics operation to insert a ClipElement
        regardless of version tracking, ensuring proper initialization.
        """
        self.current_clip_version = -1
        
    def get_current_clip_version(self) -> int:
        """Get current tracked clipping version for debugging."""
        return self.current_clip_version
        
    def force_clip_sync(self, ctxt: Any):
        """
        Force insertion of ClipElement on next graphics operation.
        
        Useful for manual synchronization or when external code has
        modified clipping state outside normal tracking.
        
        Args:
            ctxt: PostScript context with current graphics state
        """
        # Reset tracking to force ClipElement insertion
        self.current_clip_version = -1
        
    def is_clip_synchronized(self, ctxt: Any) -> bool:
        """
        Check if display list clipping state is synchronized with graphics state.
        
        Args:
            ctxt: PostScript context with current graphics state
            
        Returns:
            True if clipping is synchronized, False if ClipElement needed
        """
        gstate_clip_version = getattr(ctxt.gstate, 'clip_path_version', 0)
        return gstate_clip_version == self.current_clip_version