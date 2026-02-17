# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostForge Types Context and Execution Infrastructure Module

This module contains the core execution context and infrastructure classes that
manage PostScript program execution state, including context management,
stack operations, and global resource coordination.
"""

import collections
import sys
import threading

# Import constants
from .constants import G_STACK_MAX


class GlobalResources:
    """Singleton for PostScript Global VM resources shared across all contexts.
    
    This class manages the global resources that must be shared between
    multiple PostScript execution contexts according to the PostScript Language
    Reference Manual Section 3.7.2 "Local and Global VM".
    """
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if getattr(self, '_initialized', False):
            return
        self.global_strings = bytearray()           # Shared global string storage
        self.gvm = None                             # Will be initialized with first context
        self.resource_lock = threading.Lock()       # For thread-safe access
        self.stderr_file = None                     # Shared stderr file across all contexts
        self._glyph_cache = None                    # Lazy-initialized glyph cache for Type 3 fonts
        self._glyph_bitmap_cache = None             # Lazy-initialized bitmap cache for glyph rendering
        self.glyph_cache_disabled = False           # Enabled by default
        self._system_params = None                  # Reference to system params dict (set by create_context)
        self._initialized = True
    
    def get_gvm(self):
        """Thread-safe access to global VM"""
        with self.resource_lock:
            return self.gvm
    
    def set_gvm(self, gvm_dict):
        """Thread-safe assignment of global VM"""
        with self.resource_lock:
            self.gvm = gvm_dict

    def set_system_params(self, system_params):
        """Store reference to the system params dict for cache limit lookups."""
        self._system_params = system_params
            
    def get_stderr_file(self):
        """Get the shared stderr file object, creating it if necessary."""
        with self.resource_lock:
            if self.stderr_file is None:
                # Import here to avoid circular imports - these classes are now in file_types
                from .file_types import StandardFile, StandardFileManager, StandardFileProxy

                # Create stderr file with indirection to avoid TextIOWrapper serialization
                stderr_std_file = StandardFile(
                    ctxt_id=-1,  # Global file
                    name="%stderr",
                    stream=sys.stderr,
                    mode="w",
                    is_global=True
                )
                # Register with manager and create proxy object
                file_manager = StandardFileManager.get_instance()
                stderr_id = file_manager.register(stderr_std_file)
                self.stderr_file = StandardFileProxy(stderr_id, "%stderr", is_global=True) # type: ignore
            return self.stderr_file

    def get_glyph_cache(self):
        """Get or create the global glyph cache for Type 3 font rendering.

        Lazy initialization avoids creating the cache until first Type 3 font
        is rendered. The cache is shared across all contexts because font
        definitions are typically global.

        Returns:
            GlyphCache instance for Type 3 glyph caching
        """
        if self._glyph_cache is None:
            from ..glyph_cache import GlyphCache
            self._glyph_cache = GlyphCache()
        return self._glyph_cache

    def get_glyph_bitmap_cache(self):
        """Get or create the global glyph bitmap cache for device-level rendering.

        Lazy initialization avoids creating the cache until first glyph rendering.
        Separate from the path-level cache; stores Cairo surfaces keyed by GlyphCacheKey.
        Uses MaxFontCache from system params as the memory limit.

        Returns:
            GlyphBitmapCache instance
        """
        if self._glyph_bitmap_cache is None:
            from ..glyph_cache import GlyphBitmapCache
            max_bytes = None
            if self._system_params is not None:
                max_bytes = self._system_params.get("MaxFontCache")
            self._glyph_bitmap_cache = GlyphBitmapCache(max_bytes=max_bytes)
        return self._glyph_bitmap_cache


class Context(object):
    """
    PostScript execution context containing all execution state.
    
    Manages stacks, virtual memory, graphics state, and execution control
    for a single PostScript execution environment.
    """
    def __init__(self, system_params: dict) -> None:
        self.system_params = (
            system_params                                   # system_params is a native python dictionary
        )
        self.id = None                                      # the unique id of this context
        self.save_id = -1                                   # the id of the latest save object
        self.active_saves = set()                           # track valid save object IDs

        self.initializing = False

        self.o_stack = None
        self.e_stack = None
        self.d_stack = None
        self.g_stack = None

        self.lvm = None
        # gvm moved to GlobalResources singleton

        self.gstate = None
        self.gstate_stack = Stack(G_STACK_MAX)              # the graphic state stack

        self.packing = False                                # subject to save/restore

        self.local_strings = bytearray()                    # Local VM string storage

        # Standard file objects (context-specific for stdin/stdout, shared for stderr)
        self.stdin_file = None                              # Initialized in create_context()
        self.stdout_file = None                             # Initialized in create_context()

        self.proc_count = 0

        self.vm_alloc_mode = False                          # the current vm allocation mode
                                                            # False = use local vm (default)
                                                            # True  = use global vm

        self.global_refs = {}
        self.local_refs = {}
        self.saving = False
        self.restoring = False

        # Copy-on-Write (COW) support for fast save/restore
        self.cow_active = False          # True when any COW snapshot is active
        self.cow_protected = set()       # Set of 'created' timestamps whose backing stores are protected
        self.cow_snapshots = {}          # save_id -> dict mapping created -> backing_store reference

        self.display_list = None

        # Execution history for debugging - zero overhead when disabled via function pointer pattern
        self.execution_history = collections.deque(maxlen=20)         # Circular buffer of (input_obj, resolved_obj) tuples
        self.execution_history_enabled = False                       # Feature disabled by default for performance
        self.execution_history_paused = False                        # Temporarily pause recording (e.g., during error handling)
        self.record_execution = self._record_execution_noop          # Function pointer - starts as no-op

        # Interactive executive state
        self.echo = True                                    # PLRM echo flag for %lineedit/%statementedit

        # Binary object format (PLRM setobjectformat/currentobjectformat)
        # 0=disable, 1=IEEE high, 2=IEEE low, 3=native high, 4=native low
        self.object_format = 0                              # Subject to save/restore

        # Job control state for startjob/exitserver operators (PLRM Section 3.7.7)
        self.supports_job_encapsulation = True              # PostForge supports job server functionality
        self.job_save_level_stack = []                      # Stack of save objects tracking nested job boundaries
                                                            # Note: save_id starts at -1, first save in execjob()
                                                            #       becomes save_id=0 (job level)

        # Job timing - track time spent waiting for user input (not actual execution)
        self.user_wait_time = 0.0                           # Accumulated time waiting for user (e.g., Qt keypress)

        # Event loop callback for GUI devices (e.g., Qt processEvents)
        # Called periodically during execution to keep GUI responsive
        self.event_loop_callback = None                     # Set by device to a callable
        self._event_loop_counter = 0                        # Counter for periodic callback

        # Exit code for shell propagation (set by .quitwithcode operator)
        self.exit_code = 0

        # CLI page range filter (set of ints, or None for all pages)
        self.page_filter = None

        # Fast-path operator dispatch table: maps key -> Operator for unshadowed systemdict operators
        # Built after systemdict is populated, checked before full dict stack traversal
        self._operator_table = None                         # Initialized after systemdict setup

        self.type_names = [
            b"arraytype",
            b"booleantype",
            b"dicttype",
            b"filetype",
            b"fonttype",
            b"gstatetype",
            b"integertype",
            b"marktype",
            b"nametype",
            b"nulltype",
            b"operatortype",
            b"packedarraytype",
            b"realtype",
            b"savetype",
            b"stringtype",
            b"stoppedtype",
            b"looptype",
            b"hardreturn",
        ]

        self.error_names = [
            b"VMError",
            b"dictfull",
            b"dictstackoverflow",
            b"dictstackunderflow",
            b"execstackoverflow",
            b"invalidaccess",
            b"invalidexit",
            b"invalidfileaccess",
            b"invalidfont",
            b"invalidrestore",
            b"ioerror",
            b"limitcheck",
            b"nocurrentpoint",
            b"rangecheck",
            b"stackoverflow",
            b"stackunderflow",
            b"syntaxerror",
            b"timeout",
            b"typecheck",
            b"undefined",
            b"undefinedfilename",
            b"undefinedresource",
            b"undefinedresult",
            b"unmatchedmark",
            b"unregistered",
            b"unsupported",
            b"configurationerror",
        ]

    def enable_execution_history(self):
        """Enable execution history tracking - switches to real recording function."""
        self.execution_history_enabled = True
        self.record_execution = self._record_execution_real
        self.execution_history.clear()  # Clear any stale data

    def disable_execution_history(self):
        """Disable execution history tracking - switches to no-op function for zero overhead."""
        self.execution_history_enabled = False
        self.record_execution = self._record_execution_noop
        self.execution_history.clear()  # Free memory

    def _record_execution_noop(self, input_obj, resolved_obj=None):
        """No-op function for when execution history is disabled - zero overhead."""
        pass

    def _record_execution_real(self, input_obj, resolved_obj=None):
        """Real execution history recording function."""
        # Skip recording if paused (e.g., during error handling)
        if self.execution_history_paused:
            return
        
        # Skip recording the pauseexechistory/resumeexechistory operators themselves
        # Import constants at the function level to avoid circular imports
        from .constants import T_OPERATOR
        if (input_obj.TYPE == T_OPERATOR and hasattr(input_obj.val, '__name__') and
            input_obj.val.__name__ in ('pauseexechistory', 'resumeexechistory')):
            return
            
        # Store tuple in circular buffer (automatically truncates to maxlen=10)
        # resolved_obj can be None for operations without transformation
        self.execution_history.append((input_obj, resolved_obj))

    @property
    def current_job_start_save_level(self):
        """Get the current job's start save level from the top of the job save level stack."""
        if self.job_save_level_stack:
            return self.job_save_level_stack[-1].id
        return -1                                           # Default when no jobs on stack


class Stack(list):
    """
    PostScript execution stack with size limits and string representation.
    
    Extends Python list with PostScript-specific functionality for operand,
    execution, dictionary, and graphics state stacks.
    """
    def __init__(self, max_length: int) -> None:
        super().__init__()

    def __str__(self) -> str:
        return "[" + ", ".join(item.__str__() for item in self) + "]"

    def __repr__(self) -> str:
        return self.__str__()


# The global list of contexts
contexts = [None] * 10

# Global singleton instance
global_resources = GlobalResources()