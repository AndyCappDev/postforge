# StandardFile Indirection System

## Problem

StandardFile objects wrap Python's `sys.stdin`/`stdout`/`stderr`
TextIOWrapper references. These cannot be pickled, which breaks PostScript
VM save operations — the TextIOWrapper references get captured in
PostScript's error handling system (`/$error/ostackarray`,
`/$error/dstackarray`) and cause serialization failures.

## Solution

An indirection layer separates the pickleable proxy from the unpickleable
file object.

### Components

**StandardFileManager** (`postforge/core/types/file_types.py`) — Global
singleton registry that stores actual StandardFile objects by integer ID.
The registry lives outside the serialization scope, so its contents are
never pickled.

**StandardFileProxy** (`postforge/core/types/file_types.py`) — Lightweight
proxy containing only an integer ID. Safely pickleable. Delegates all file
operations (`read`, `write`, `filename`, `status`, etc.) to the actual
StandardFile via registry lookup.

**StandardFile** (`postforge/core/types/file_types.py`) — The actual file
object wrapping a Python stream. Never stored directly on the context or
any pickleable structure.

### How It Fits Together

During context creation (`postforge/cli.py`), actual StandardFile objects
are created and registered with the manager. The context stores only the
proxy:

```python
stdin_std_file = ps.StandardFile(...)
file_manager = ps.StandardFileManager.get_instance()
stdin_id = file_manager.register(stdin_std_file)
ctxt.stdin_file = ps.StandardFileProxy(stdin_id, "%stdin")
```

PostScript code works unchanged — proxies delegate all operations
transparently. File operators in `postforge/operators/file.py` accept
StandardFileProxy and resolve to the underlying StandardFile when needed.

### Design Principles

- Context never holds direct StandardFile references
- The global registry is outside pickle scope
- PostScript sees no difference — proxies implement the full File interface
- No external dependencies — uses Python's standard pickle

When adding new file-like objects that contain non-serializable references,
follow this same pattern: proxy with ID-based lookup through a global
registry.
