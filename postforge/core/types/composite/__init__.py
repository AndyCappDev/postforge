# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
PostForge Types Composite Sub-Package

This sub-package contains the PostScript composite data types organized into
focused modules for improved maintainability. Each composite type is implemented
in its own module while maintaining a unified public interface.

**Module Organization:**
- string.py: String class - Stream-based string operations with VM management
- name.py: Name class - Symbol operations with hash-based lookups  
- array.py: Array + PackedArray classes - Dynamic and immutable array operations
- dict.py: Dict class - Key-value store operations with VM management

**Forward Reference Resolution:**
This package handles cross-references between String ↔ Name comparison operations
and other inter-module dependencies to ensure proper PostScript type semantics.

**Public Interface:**
All classes are re-exported to maintain compatibility with the main types package.
The standard import pattern `from core import types as ps` continues to work
through the main package's re-export of this sub-package.
"""

# =============================================================================
# COMPOSITE TYPE IMPORTS
# Re-export all composite classes for the public interface
# =============================================================================

from .string import String
from .name import Name
from .array import Array, PackedArray
from .dict import Dict
from .gstate import GState

# =============================================================================
# FORWARD REFERENCE RESOLUTION
# Handle cross-module dependencies after all classes are imported
# =============================================================================

# Resolve String ↔ Name cross-comparison operations
from . import string
from . import name
from . import dict as dict_module

# Set up forward references for cross-type comparisons
string.Name = Name        # String.__eq__ method needs Name class for isinstance check
name.String = String      # Name.__eq__ method needs String class for isinstance check
dict_module.Name = Name   # Dict.__str__ method needs Name class for key representation
dict_module.String = String  # Dict.put() needs String class for PLRM string key copy

# =============================================================================
# PUBLIC API - All composite types available at package level
# =============================================================================

__all__ = [
    'String',
    'Name',
    'Array',
    'PackedArray',
    'Dict',
    'GState'
]