# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostForge Types Package - Public API

This package provides the unified PostScript types interface for the PostForge interpreter.
All PostScript types, constants, and classes are available through this single namespace
to support the standard import pattern: `from core import types as ps`

**Public API Design:**
The package re-exports all PostScript types from 9 focused internal modules to provide
a clean, stable public interface. This design allows internal reorganization without
breaking the 33+ files that depend on the `ps.ClassName` access pattern.

**Internal Module Organization:**
- constants.py: PostScript constants and type definitions (25 constants)
- base.py: Base PSObject and Stream classes (2 classes)  
- primitive.py: Primitive PostScript types (5 classes)
- context.py: Execution context infrastructure (3 classes)
- file_types.py: File I/O abstractions (6 classes)
- composite.py: Composite PostScript types (5 classes)
- graphics.py: Graphics and display list types (17 classes)
- control.py: Control flow and execution types (3 classes)
- utility.py: Utility types and wrappers (3 classes)

**Usage:**
```python
from .. import types as ps

# Access any PostScript type or constant
context = ps.Context(params)
string = ps.String(ctxt_id, offset, length)
array = ps.Array(ctxt_id)
bool_val = ps.Bool(True)
```
"""

# =============================================================================
# PUBLIC API EXPORTS
# Re-export all PostScript types and constants for the public interface
# =============================================================================

from .constants import *
from .base import *
from .primitive import *
from .context import *
from .file_types import *
from .composite import *
from .graphics import *
from .control import *
from .utility import *

# =============================================================================
# INTERNAL SETUP - Cross-module dependencies and dynamic method assignment
# =============================================================================

# Resolve forward references between modules after all imports are complete
from . import file_types
file_types.String = String  # String class available from composite imports
# Note: Composite sub-package handles its own forward references internally

# Set up PSObject comparison operators (requires Bool class to be available)
def _ps_object_eq(self, other):
    return self.val == other.val

def _ps_object_ne(self, other):
    return Bool(self.val != other.val)

def _ps_object_lt(self, other):
    return Bool(self.val < other.val)

def _ps_object_gt(self, other):
    return Bool(self.val > other.val)

def _ps_object_le(self, other):
    return Bool(self.val <= other.val)

def _ps_object_ge(self, other):
    return Bool(self.val >= other.val)

# Dynamically attach comparison operators to PSObject base class
PSObject.__eq__ = _ps_object_eq
PSObject.__ne__ = _ps_object_ne
PSObject.__lt__ = _ps_object_lt
PSObject.__gt__ = _ps_object_gt
PSObject.__le__ = _ps_object_le
PSObject.__ge__ = _ps_object_ge