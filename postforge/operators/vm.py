# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import copy
import gc
import io
import pickle
import sys

from ..core import error as ps_error
from ..core import types as ps
from .graphics_state import grestoreall
from . import dict as ps_dict

# In-memory storage for VM snapshots (pickle to BytesIO for speed)
# Key: (context_id, save_id), Value: dict of pickled bytes
_vm_snapshots = {}


def _handle_fontdirectory_rebinding(ctxt: ps.Context, global_vm_mode: bool) -> None:
    """
    Handle **FontDirectory** rebinding per PLRM specification.
    
    PLRM: "**FontDirectory** is temporarily rebound to the value of **GlobalFontDirectory**
    when global VM allocation mode is in effect"
    
    Args:
        ctxt: PostScript execution context
        global_vm_mode: True if entering global VM mode, False for local VM mode
    """
    try:
        # Get systemdict from global VM
        gvm = ps.global_resources.get_gvm()
        if not gvm or b"systemdict" not in gvm.val:
            return  # System not fully initialized yet
        
        systemdict = gvm.val[b"systemdict"]
        
        # Get both FontDirectory and GlobalFontDirectory references
        fontdir_name = ps.Name(b"FontDirectory")
        globalfontdir_name = ps.Name(b"GlobalFontDirectory")
        
        local_fontdir = ps_dict.lookup(ctxt, fontdir_name, systemdict)
        global_fontdir = ps_dict.lookup(ctxt, globalfontdir_name, systemdict)
        
        if not local_fontdir or not global_fontdir:
            return  # Font directories not initialized yet
        
        if global_vm_mode:
            # Entering global VM mode: rebind FontDirectory to GlobalFontDirectory
            # Store original FontDirectory reference if not already stored
            if not hasattr(ctxt, '_original_fontdirectory'):
                ctxt._original_fontdirectory = local_fontdir
            systemdict.val[b"FontDirectory"] = global_fontdir
        else:
            # Entering local VM mode: restore original FontDirectory
            if hasattr(ctxt, '_original_fontdirectory'):
                systemdict.val[b"FontDirectory"] = ctxt._original_fontdirectory
            else:
                systemdict.val[b"FontDirectory"] = local_fontdir
                
    except Exception:
        # Silently ignore errors during FontDirectory rebinding to avoid
        # disrupting normal VM allocation mode changes
        pass


def currentglobal(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    - **currentglobal** bool


    returns the VM allocation mode currently in effect.

    **Errors**:     **stackoverflow**
    **See Also**:   **setglobal**
    """

    if ctxt.MaxOpStack and len(ostack) + 1 > ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentglobal.__name__)

    ostack.append(ps.Bool(ctxt.vm_alloc_mode))


def gcheck(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    any **gcheck** bool

    Returns true if any is a composite object whose value is in global VM,
    or if any is a simple object. Returns false only for composite objects
    whose value is in local VM.

    Note: PLRM states simple objects should return false since they're "not
    allocated in either VM." However, practical PostScript code (like
    Ghostscript's opdfread.ps cp2g) interprets **gcheck** as "does this need
    copying to global VM?" For simple objects, the answer is no - they're
    values not references and don't need VM copying. Returning true for
    simple objects prevents cp2g from attempting to copy them, which avoids
    stack corruption bugs in common PostScript code patterns.

    Stack: any **gcheck** bool
    **Errors**: **stackunderflow**
    """
    if not len(ostack):
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, gcheck.__name__)

    obj = ostack.pop()  # Pop the object to test

    # Composite objects: return their actual is_global status
    # Simple objects: return true (they don't need VM copying)
    if hasattr(obj, 'is_composite') and obj.is_composite:
        ostack.append(ps.Bool(obj.is_global))
    else:
        # Simple objects are effectively "global" - they're values that
        # exist independently of VM allocation and don't need copying
        ostack.append(ps.Bool(True))
        

def setglobal(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    bool **setglobal** -


    sets the VM allocation mode: true denotes global, false denotes local. This controls
    the VM region in which the values of new composite objects are to be allocated
    (see Section 3.7, "Memory Management"). It applies to objects created implicitly
    by the scanner and to those created explicitly by PostScript operators.

    Modifications to the VM allocation mode are subject to **save** and **restore**. In an
    interpreter that supports multiple execution contexts, the VM allocation mode is
    maintained separately for each context.

    The standard error handlers in **errordict** execute false **setglobal**, reverting to local
    VM allocation mode if an error occurs.

    **Errors**:     **stackunderflow**, **typecheck**
    **See Also**:   **currentglobal**
    """

    if not len(ostack):
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, setglobal.__name__)

    if ostack[-1].TYPE != ps.T_BOOL:
        return ps_error.e(ctxt, ps_error.TYPECHECK, setglobal.__name__)

    old_vm_mode = ctxt.vm_alloc_mode
    new_vm_mode = ostack[-1].val
    ctxt.vm_alloc_mode = new_vm_mode

    # Handle FontDirectory rebinding per PLRM specification
    # "FontDirectory is temporarily rebound to the value of GlobalFontDirectory" 
    # when global VM allocation mode is in effect
    if old_vm_mode != new_vm_mode:
        _handle_fontdirectory_rebinding(ctxt, new_vm_mode)

    ostack.pop()


def save(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    - **save** **save**


    creates a snapshot of the current state of virtual memory (VM) and returns a **save**
    object representing that snapshot. The **save** object is composite and logically
    belongs to the local VM, regardless of the current VM allocation mode.

    Subsequently, the returned **save** object may be presented to **restore** to reset VM to
    this snapshot. See Section 3.7, "Memory Management," for a description of VM
    and of the effects of **save** and **restore**. See the **restore** operator for a detailed
    description of what is saved in the snapshot.

    **save** also saves the current graphics state by pushing a copy of it on the graphics
    state stack in a manner similar to **gsave**. This saved graphics state is restored by
    **restore** and **grestoreall**.

    **Example**
        /saveobj **save** def
            ... Arbitrary computation ...
        saveobj **restore**                         % Restore saved VM state

    **Errors**:     **limitcheck**, **stackoverflow**
    **See Also**:   **restore**, **gsave**, **grestoreall**, **vmstatus**
    """

    if ctxt.MaxOpStack and len(ostack) + 1 > ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, save.__name__)

    ctxt.save_id += 1
    sv = ps.Save(ctxt.save_id)
    ctxt.active_saves.add(ctxt.save_id)  # Track this save as valid

    # Create in-memory snapshot storage key
    snapshot_key = (ctxt.id, ctxt.save_id)
    snapshot = {}

    # Check if this is a job-level save (outermost save for current context)
    is_job_level_save = len(ctxt.active_saves) == 1  # This is the only active save

    if ctxt.save_id == 0 or is_job_level_save:
        # Job-level save: use pickle (rare, involves global VM + strings)
        snapshot['cow'] = False

        ctxt.saving = True
        lvm_buffer = io.BytesIO()
        pickle.dump(ctxt.lvm, lvm_buffer)
        snapshot['lvm'] = lvm_buffer.getvalue()
        ctxt.saving = False

        dstack_vals = {}
        for d in ctxt.d_stack:
            if not d.is_global:
                dstack_vals[d.created] = pickle.dumps(d.val)
        snapshot['dstack_vals'] = dstack_vals

        # Save the contents of global VM (for initial save or job-level saves)
        gvm_buffer = io.BytesIO()
        pickle.dump(ps.global_resources.get_gvm(), gvm_buffer)
        snapshot['gvm'] = gvm_buffer.getvalue()

        gstrings_buffer = io.BytesIO()
        pickle.dump(ps.global_resources.global_strings, gstrings_buffer)
        snapshot['gstrings'] = gstrings_buffer.getvalue()

        lstrings_buffer = io.BytesIO()
        pickle.dump(ctxt.local_strings, lstrings_buffer)
        snapshot['lstrings'] = lstrings_buffer.getvalue()

        global_refs_buffer = io.BytesIO()
        pickle.dump(ctxt.global_refs, global_refs_buffer)
        snapshot['global_refs'] = global_refs_buffer.getvalue()
    else:
        # Non-job-level save: use COW (fast path for gradient strips etc.)
        snapshot['cow'] = True

        # Snapshot current local_refs: maps created_timestamp -> backing_store
        # This is a shallow copy of the dict mapping, NOT a copy of the backing stores
        local_refs_snapshot = dict(ctxt.local_refs)
        snapshot['local_refs'] = local_refs_snapshot

        # Record d_stack local dict created timestamps for restore
        snapshot['dstack_created'] = [d.created for d in ctxt.d_stack if not d.is_global]

        # Record lvm created timestamp
        snapshot['lvm_created'] = ctxt.lvm.created

        # Protect all current local backing stores from mutation
        ctxt.cow_protected.update(local_refs_snapshot.keys())
        ctxt.cow_active = True

        # Store in cow_snapshots for rebuild of protected set on restore
        ctxt.cow_snapshots[ctxt.save_id] = local_refs_snapshot

    # Save the graphics state (implicit gsave) - check for limitcheck first
    if len(ctxt.gstate_stack) >= ps.G_STACK_MAX:
        return ps_error.e(ctxt, ps_error.LIMITCHECK, save.__name__)

    ctxt.gstate_stack.append(ctxt.gstate.copy())  # This uses deepcopy() internally
    ctxt.gstate_stack[-1].saved = True  # Mark as saved by 'save' (not 'gsave')

    # Save per-context parameters
    snapshot['context_params'] = {
        'packing': ctxt.packing,
        'vm_alloc_mode': ctxt.vm_alloc_mode,
        'object_format': ctxt.object_format,
    }

    # Store snapshot in memory
    _vm_snapshots[snapshot_key] = snapshot

    ostack.append(sv)


def restore(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    **save** **restore** -


    resets virtual memory (VM) to the state represented by the supplied **save** object—
    in other words, the state at the time the corresponding **save** operator was executed.
    See Section 3.7, "Memory Management," for a description of VM and the
    effects of **save** and **restore**.

    If the current execution context supports job encapsulation and if **save** represents
    the outermost saved VM state for this context, then objects in both local and
    global VM revert to their saved state. If the current context does not support job
    encapsulation or if **save** is not the outermost saved VM state for this context, then
    only objects in local VM revert to their saved state; objects in global VM are undisturbed.
    Job encapsulation is described in Section 3.7.7, "Job Execution Environment."

    **restore** can reset VM to the state represented by any **save** object that is still valid,
    not necessarily the one produced by the most recent **save**. After restoring VM,
    **restore** invalidates its **save** operand along with any other **save** objects created more
    recently than that one. That is, a VM snapshot can be used only once; to **restore**
    the same environment repeatedly, it is necessary to do a new **save** each time.

    **restore** does not alter the contents of the operand, dictionary, or execution stack,
    except to pop its **save** operand. If any of these stacks contains composite objects
    whose values reside in local VM and are newer than the snapshot being restored,
    an **invalidrestore** error occurs. This restriction applies to **save** objects and, in
    LanguageLevel 1, to name objects.

    **restore** does alter the graphics state stack: it performs the equivalent of a
    **grestoreall** and then removes the graphics state created by **save** from the graphics
    state stack. **restore** also resets several per-context parameters to their state at the
    time of **save**. These include:

        • Array packing mode (see **setpacking**)

        • VM allocation mode (see **setglobal**)

        • Object output format (see **setobjectformat**)

        • All user interpreter parameters (see **setuserparams**)

    **Errors**:     **invalidrestore**, **stackunderflow**, **typecheck**
    **See Also**:   **save**, **grestoreall**, **vmstatus**, **startjob**
    """

    if not len(ostack):
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, restore.__name__)

    if ostack[-1].TYPE != ps.T_SAVE:
        return ps_error.e(ctxt, ps_error.TYPECHECK, restore.__name__)
    
    # Check if save object is still valid
    if not ostack[-1].valid or ostack[-1].id not in ctxt.active_saves:
        return ps_error.e(ctxt, ps_error.INVALIDRESTORE, restore.__name__)


    # INVALIDRESTORE CHECK: Per PostScript manual Section 3.7.3
    # "If any of these stacks contains composite objects whose values reside in local VM 
    # and are newer than the snapshot being restored, an invalidrestore error occurs."
    
    save_timestamp = ostack[-1].created

    # Check dictionary stack for local objects newer than the save
    for i in range(-1, -len(ctxt.d_stack) + 2, -1):
        obj = ctxt.d_stack[i]
        if not obj.is_global and obj.created > save_timestamp:
            return ps_error.e(ctxt, ps_error.INVALIDRESTORE, restore.__name__)

    # Check operand stack for local composite objects newer than the save (excluding the save object itself)
    for i in range(-2, -(len(ostack) + 1), -1):  # Skip the save object at -1
        obj = ostack[i]
        if obj.is_composite and not obj.is_global and obj.created > save_timestamp:
            return ps_error.e(ctxt, ps_error.INVALIDRESTORE, restore.__name__)

    # Check execution stack for local composite objects newer than the save
    for i in range(-1, -(len(ctxt.e_stack) + 1), -1):
        obj = ctxt.e_stack[i]
        if obj.is_composite and not obj.is_global and obj.created > save_timestamp:
            return ps_error.e(ctxt, ps_error.INVALIDRESTORE, restore.__name__)

    # Store reference to save object and prepare invalidation list
    save_obj_to_restore = ostack[-1]
    save_id_to_restore = save_obj_to_restore.id
    newer_saves_to_invalidate = [save_id for save_id in ctxt.active_saves if save_id > save_id_to_restore]

    # Get snapshot from in-memory storage
    snapshot_key = (ctxt.id, save_id_to_restore)
    snapshot = _vm_snapshots.get(snapshot_key)
    if snapshot is None:
        raise RuntimeError(f"RESTORE: Snapshot not found for save_id={save_id_to_restore}")

    if snapshot.get('cow'):
        # ---- COW restore: revert local backing stores in-place ----
        saved_local_refs = snapshot['local_refs']
        saved_lvm_created = snapshot['lvm_created']

        # Revert modified backing stores in-place so ALL references
        # (including //-captured Dicts in bound procedures) see the change.
        for created, saved_backing in saved_local_refs.items():
            live_backing = ctxt.local_refs.get(created)
            if live_backing is None or live_backing is saved_backing:
                # Not modified or same object — no revert needed
                ctxt.local_refs[created] = saved_backing
                continue
            if isinstance(live_backing, dict):
                live_backing.clear()
                live_backing.update(saved_backing)
                # local_refs keeps pointing to live_backing (same object)
            elif isinstance(live_backing, list):
                live_backing.clear()
                live_backing.extend(saved_backing)
            else:
                ctxt.local_refs[created] = saved_backing

        # Remove objects created after save from local_refs
        save_timestamp = save_obj_to_restore.created
        keys_to_remove = [k for k in ctxt.local_refs if k > save_timestamp and k not in saved_local_refs]
        for k in keys_to_remove:
            del ctxt.local_refs[k]

        # Clean up COW state for this save
        ctxt.cow_snapshots.pop(save_id_to_restore, None)

    else:
        snapshot_cow_saved = False
        # ---- Pickle-based restore (job-level) ----
        ctxt.restoring = True

        old_lvm = ctxt.lvm
        old_gvm_ref = ps.global_resources.get_gvm()
        old_global_refs = ctxt.global_refs
        ctxt.lvm = None
        ps.global_resources.set_gvm(None)
        del old_lvm

        lvm_buffer = io.BytesIO(snapshot['lvm'])
        ctxt.lvm = pickle.load(lvm_buffer)
        ctxt.restoring = False

        if 'dstack_vals' in snapshot:
            saved_dstack_vals = snapshot['dstack_vals']
            for d in ctxt.d_stack:
                if not d.is_global and d.created in saved_dstack_vals:
                    restored_val = pickle.loads(saved_dstack_vals[d.created])
                    d.val.clear()
                    d.val.update(restored_val)
                    ctxt.local_refs[d.created] = d.val

        is_job_level_restore = save_id_to_restore in ctxt.active_saves and len(ctxt.active_saves) == 1

        if save_id_to_restore == 0 or is_job_level_restore:
            del old_gvm_ref

            global_refs_buffer = io.BytesIO(snapshot['global_refs'])
            ctxt.global_refs = pickle.load(global_refs_buffer)

            gvm_buffer = io.BytesIO(snapshot['gvm'])
            ps.global_resources.set_gvm(pickle.load(gvm_buffer))

            lstrings_buffer = io.BytesIO(snapshot['lstrings'])
            ctxt.local_strings = pickle.load(lstrings_buffer)

            gstrings_buffer = io.BytesIO(snapshot['gstrings'])
            ps.global_resources.global_strings = pickle.load(gstrings_buffer)

            old_global_refs.clear()
            del old_global_refs
        else:
            del old_global_refs
            ps.global_resources.set_gvm(old_gvm_ref)

        fix_vm_global_composites(ctxt, ps.global_resources.get_gvm())
        fix_stack_references(ctxt, ctxt.d_stack)

        if save_id_to_restore == 0 or is_job_level_restore:
            old_local_refs = ctxt.local_refs
            ctxt.local_refs = {}
            old_local_refs.clear()
            del old_local_refs

    # Ensure local dicts on d_stack are in local_refs for future restores
    for d in ctxt.d_stack:
        if not d.is_global:
            ctxt.local_refs[d.created] = d.val

    ostack.pop()

    # Reset userparams
    if ctxt.lvm is not None and b"UserParams" in ctxt.lvm.val:
        user_params = ctxt.lvm[b"UserParams"].val
        for key, val in user_params.items():
            if val.TYPE == ps.T_STRING:
                ctxt.__setattr__(key.val.decode(), val.python_string())
            else:
                ctxt.__setattr__(key.val.decode(), val.val)

    # Restore per-context parameters from snapshot
    context_params = snapshot['context_params']
    ctxt.packing = context_params['packing']
    ctxt.vm_alloc_mode = context_params['vm_alloc_mode']
    ctxt.object_format = context_params.get('object_format', 0)

    # Invalidate newer saves and clean up their snapshots
    for save_id in newer_saves_to_invalidate:
        newer_snapshot_key = (ctxt.id, save_id)
        _vm_snapshots.pop(newer_snapshot_key, None)
        ctxt.cow_snapshots.pop(save_id, None)
        ctxt.active_saves.discard(save_id)

    # Invalidate and remove the current save object we just restored
    save_obj_to_restore.valid = False
    ctxt.active_saves.discard(save_id_to_restore)

    # Update save_id to reflect current save level after restore
    if len(ctxt.active_saves) == 0:
        ctxt.save_id = -1
    else:
        ctxt.save_id = max(ctxt.active_saves)

    # Clean up the current save's snapshot
    _vm_snapshots.pop(snapshot_key, None)

    # Rebuild COW protected set from remaining active cow snapshots
    if ctxt.cow_snapshots:
        ctxt.cow_protected = set()
        for snap_refs in ctxt.cow_snapshots.values():
            ctxt.cow_protected.update(snap_refs.keys())
        ctxt.cow_active = True
    else:
        ctxt.cow_protected = set()
        ctxt.cow_active = False

    # Perform implicit grestoreall
    grestoreall(ctxt, ostack)

    # After grestoreall, remove the graphics state created by save from the stack
    if ctxt.gstate_stack and ctxt.gstate_stack[-1].saved:
        ctxt.gstate_stack.pop()


def fix_vm_global_composites(ctxt: ps.Context, obj: ps.PSObject, visited: set | None = None) -> None:
    # go through ALL local composite objects in global vm and
    # replace the references to the restored values
    if visited is None:
        visited = set()

    if obj.TYPE == ps.T_DICT:
        # for each item in the dictionary
        for value in obj.val.values():
            # if this is a local dict or array...
            if (
                value.is_composite
                and not value.is_global
                and value.TYPE in ps.VM_COMPOSITE_TYPES
            ):
                # replace the reference to the actual composite value
                try:
                    value.val = ctxt.local_refs[value.created]
                except KeyError:
                    # This should not happen with proper reference tracking
                    raise RuntimeError(f"RESTORE: Missing local_refs timestamp {value.created} during VM fix - reference tracking failed")
            elif (
                value.is_composite
                and value.is_global
                and value.TYPE in ps.VM_COMPOSITE_TYPES
                and value.created not in visited
            ):
                # add this global composite object to the visited list - so we dont interate though it again
                visited.add(value.created)
                # iterate though this composite object
                fix_vm_global_composites(ctxt, value, visited)

    elif obj.TYPE == ps.T_ARRAY:
        # for each item in the dictionary
        for value in obj.val:
            # if this is a local dict or array...
            if (
                value.is_composite
                and not value.is_global
                and value.TYPE in ps.VM_COMPOSITE_TYPES
            ):
                # replace the reference to the actual composite value
                try:
                    value.val = ctxt.local_refs[value.created]
                except KeyError:
                    # This should not happen with proper reference tracking
                    raise RuntimeError(f"RESTORE: Missing local_refs timestamp {value.created} during VM fix - reference tracking failed")
            elif (
                value.is_composite
                and value.is_global
                and value.TYPE in ps.VM_COMPOSITE_TYPES
                and value.created not in visited
            ):
                # add this global composite object to the visited list - so we dont interate though it again
                visited.add(value.created)
                # iterate though this composite object
                fix_vm_global_composites(ctxt, value, visited)


def fix_stack_references(ctxt: ps.Context, stack: list, visited: set | None = None) -> None:
    # re-establish references on the dictionary stack
    if visited is None:
        visited = set()

    for obj in stack:
        if obj.TYPE == ps.T_DICT and not obj.is_global:
            # this object is a local dictionary
            try:
                obj.val = ctxt.local_refs[obj.created]
            except KeyError:
                # This should not happen with proper reference tracking
                raise RuntimeError(f"RESTORE: Missing local_refs timestamp {obj.created} during stack fix - reference tracking failed")
        # iterate though this composite object
        fix_vm_global_composites(ctxt, obj)


def vmstatus(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    - **vmstatus** level used maximum

    returns three integers describing the state of the PostScript interpreter's virtual
    memory (VM). level is the current depth of **save** nesting—in other words, the
    number of **save** operations that have not been matched by a **restore** operation.
    used and maximum measure VM resources in units of 8-bit bytes; used is the number
    of bytes currently in use and maximum is the maximum available capacity.

    VM consumption is monitored separately for local and global VM. The used and
    maximum values apply to either local or global VM according to the current VM
    allocation mode (see **setglobal**).

    **Errors**:     **stackoverflow**
    **See Also**:   **save**, **restore**, **vmreclaim**
    """
    if ctxt.MaxOpStack and len(ostack) + 3 > ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, vmstatus.__name__)

    # level: number of active saves (save nesting depth)
    level = len(ctxt.active_saves)

    # used: approximate VM usage based on object sizes
    # This is an approximation since Python manages memory differently
    if ctxt.vm_alloc_mode:
        # Global VM mode - estimate from global VM
        gvm = ps.global_resources.get_gvm()
        used = sys.getsizeof(gvm.val) if gvm else 0
    else:
        # Local VM mode - estimate from local VM
        used = sys.getsizeof(ctxt.lvm.val) if ctxt.lvm else 0

    # maximum: available memory (use a large default value)
    # PostScript programs typically just want to know there's enough memory
    maximum = 2**24  # 16 MB - reasonable for most PostScript programs

    ostack.append(ps.Int(level))
    ostack.append(ps.Int(used))
    ostack.append(ps.Int(maximum))


def _get_userdict(ctxt: ps.Context) -> ps.Dict:
    """Get **userdict** from the dictionary stack (bottom 3: **systemdict**, **globaldict**, **userdict**)."""
    return ctxt.d_stack[2]


def _get_or_create_userobjects(ctxt: ps.Context) -> ps.Array | None:
    """Get or create the UserObjects array in **userdict**.

    Returns the existing UserObjects array, or None if it doesn't exist
    (caller must create it if needed).
    """
    userdict = _get_userdict(ctxt)
    key = b"UserObjects"
    if key in userdict.val:
        return userdict.val[key]
    return None


def defineuserobject(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    index any **defineuserobject** –

    Establishes an association between the nonnegative integer index and the
    object any in the UserObjects array. Creates or extends the UserObjects
    array in **userdict** as needed. The array is always allocated in local VM.

    Stack: index any **defineuserobject** –
    **Errors**: **limitcheck**, **rangecheck**, **stackunderflow**, **typecheck**, **VMerror**
    """
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, defineuserobject.__name__)

    if ostack[-2].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, defineuserobject.__name__)

    index = ostack[-2].val
    if index < 0:
        return ps_error.e(ctxt, ps_error.RANGECHECK, defineuserobject.__name__)

    any_obj = ostack[-1]

    # Get or create UserObjects array
    user_objects = _get_or_create_userobjects(ctxt)
    userdict = _get_userdict(ctxt)

    if user_objects is None:
        # Create new array in local VM with enough room
        size = max(index + 1, 4)
        user_objects = ps.Array(ctxt.id, is_global=False)
        user_objects.val = [ps.Null() for _ in range(size)]
        user_objects.length = size
        userdict.val[b"UserObjects"] = user_objects
    elif index >= user_objects.length:
        # Extend the array
        new_size = max(index + 1, user_objects.length * 2)
        old_val = user_objects.val
        user_objects.val = old_val + [ps.Null() for _ in range(new_size - len(old_val))]
        user_objects.length = new_size

    # Pop operands after all validation
    ostack.pop()
    ostack.pop()

    # Store the object
    user_objects.val[index] = any_obj


def execuserobject(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    index **execuserobject** –

    Executes the object associated with the nonnegative integer index in the
    UserObjects array. Equivalent to:
        **userdict** /UserObjects get exch get exec

    Stack: index **execuserobject** –
    **Errors**: **invalidaccess**, **rangecheck**, **stackunderflow**, **typecheck**, **undefined**
    """
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, execuserobject.__name__)

    if ostack[-1].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, execuserobject.__name__)

    index = ostack[-1].val
    if index < 0:
        return ps_error.e(ctxt, ps_error.RANGECHECK, execuserobject.__name__)

    user_objects = _get_or_create_userobjects(ctxt)
    if user_objects is None:
        return ps_error.e(ctxt, ps_error.UNDEFINED, execuserobject.__name__)

    if index >= user_objects.length:
        return ps_error.e(ctxt, ps_error.RANGECHECK, execuserobject.__name__)

    obj = user_objects.val[index]

    # Pop index after validation
    ostack.pop()

    # Execute the object (push on e_stack if executable, else push on o_stack)
    ctxt.e_stack.append(obj)


def undefineuserobject(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    index **undefineuserobject** –

    Breaks the association between the nonnegative integer index and an object
    by replacing the UserObjects array element with null. Equivalent to:
        **userdict** /UserObjects get exch null put

    Stack: index **undefineuserobject** –
    **Errors**: **rangecheck**, **stackunderflow**, **typecheck**, **undefined**
    """
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, undefineuserobject.__name__)

    if ostack[-1].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, undefineuserobject.__name__)

    index = ostack[-1].val
    if index < 0:
        return ps_error.e(ctxt, ps_error.RANGECHECK, undefineuserobject.__name__)

    user_objects = _get_or_create_userobjects(ctxt)
    if user_objects is None:
        return ps_error.e(ctxt, ps_error.UNDEFINED, undefineuserobject.__name__)

    if index >= user_objects.length:
        return ps_error.e(ctxt, ps_error.RANGECHECK, undefineuserobject.__name__)

    # Pop index after validation
    ostack.pop()

    # Replace with null
    user_objects.val[index] = ps.Null()
