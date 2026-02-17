# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostForge Types Constants Module

This module contains all constants, enums, and type definitions used throughout
the PostForge PostScript interpreter. These constants define the core numerical
values and identifiers that control PostScript execution behavior.
"""

# PostScript execution stack limits
# These values are based on PostScript Level 2 specification requirements
O_STACK_MAX = 500                           # Operand stack maximum depth
E_STACK_MAX = 250                           # Execution stack maximum depth
D_STACK_MAX = 250                           # Dictionary stack maximum depth (used in context creation)
G_STACK_MAX = 20                            # Graphics state stack maximum depth
CP_STACK_MAX = 20                           # Clipping path stack maximum depth

# PostScript system constants
MAX_POSTSCRIPT_INTEGER = 2147483647         # Maximum 32-bit signed integer (2^31 - 1)
# Used for random number generation per PostScript spec

# access types
ACCESS_UNLIMITED = 4                        # Can read, write, and execute
ACCESS_WRITE_ONLY = 3                       # Can write but not read
ACCESS_READ_ONLY = 2                        # Can read but not write  
ACCESS_EXECUTE_ONLY = 1                     # Can execute but not read/write
ACCESS_NONE = 0                             # No access allowed

# attribute types
ATTRIB_LIT = 0
ATTRIB_EXEC = 1

# PSObject types
T_ARRAY = 0
T_BOOL = 1
T_DICT = 2
T_FILE = 3
T_FONT = 4
T_GSTATE = 5
T_INT = 6
T_MARK = 7
T_NAME = 8
T_NULL = 9
T_OPERATOR = 10
T_PACKED_ARRAY = 11
T_REAL = 12
T_SAVE = 13
T_STRING = 14
T_STOPPED = 15
T_LOOP = 16
T_HARD_RETURN = 17

# Type grouping constants for fast type checking
# These frozensets provide O(1) membership testing for common type groups
# For single types, use direct comparison: obj.TYPE == T_XXX (faster than frozensets)
NUMERIC_TYPES = frozenset({T_INT, T_REAL})
COMPOSITE_TYPES = frozenset({T_ARRAY, T_DICT, T_STRING, T_PACKED_ARRAY})
STREAM_TYPES = frozenset({T_FILE, T_STRING})
CONTAINER_TYPES = frozenset({T_ARRAY, T_DICT, T_PACKED_ARRAY})
IMMUTABLE_TYPES = frozenset({T_INT, T_REAL, T_BOOL, T_NULL, T_NAME})
MARK_TYPES = frozenset({T_MARK, T_STOPPED})  # Stack marker types

# exec_exec specific type groups for performance optimization
LITERAL_TYPES = frozenset({T_INT, T_REAL, T_BOOL, T_NULL, T_MARK})  # Always literal objects
TOKENIZABLE_TYPES = frozenset({T_FILE, T_STRING})  # Objects that can be tokenized (Run inherits from File)
ARRAY_TYPES = frozenset({T_ARRAY, T_PACKED_ARRAY})  # Array and PackedArray objects (forall compatibility)
VM_COMPOSITE_TYPES = frozenset({T_ARRAY, T_PACKED_ARRAY, T_DICT})  # VM composite objects (save/restore compatibility)

# Loop types
LT_CSHOW = 1
LT_FILENAMEFORALL = 2
LT_FOR = 3
LT_FORALL = 4
LT_KSHOW = 5
LT_LOOP = 6
LT_PATHFORALL = 7
LT_REPEAT = 8
LT_RESOURSEFORALL = 9

# Winding Rule types
WINDING_NON_ZERO = 0
WINDING_EVEN_ODD = 1

# line cap types
LINE_CAP_BUTT = 0
LINE_CAP_ROUND = 1
LINE_CAP_SQUARE = 2

# line join types
LINE_JOIN_MITER = 0
LINE_JOIN_ROUND = 1
LINE_JOIN_BEVEL = 2

# points per inch
PPI = 72.0

ENDPAGE_SHOWPAGE = 0
ENDPAGE_COPYPAGE = 1
ENDPAGE_DEVICE = 2

# the output directory
OUTPUT_DIRECTORY = "pf_output"