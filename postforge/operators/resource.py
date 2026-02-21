# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import copy
import fnmatch
import sys

from . import control as ps_control
from . import dict as ps_dict
from ..core import error as ps_error
from ..core import system_font_cache as sfc
from ..core import system_font_loader as sfl
from ..core import types as ps


def categoryimpdict(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    - <**categoryimpdict**> dict
    """

    if ctxt.MaxOpStack and len(ostack) == ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, categoryimpdict.__name__)

    ostack.append(copy.copy(ps.global_resources.get_gvm().val[b"resource"].val[b"Category"]))


def createresourcecategory(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    category instancetype resourcedir resourceextension <**createresourcecategory**> -

    """

    proc = ps_dict.lookup(
        ctxt, ps.Name(b".createresourcecategory"), ps.global_resources.get_gvm()[b"systemdict"]
    )
    # override ACCESS_NONE
    proc.access = ps.ACCESS_READ_ONLY
    ctxt.e_stack.append(proc)


def globalresourcedict(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    category <**globalresourcedict**> dict true
    category <**globalresourcedict**> false
    """

    if not ostack:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, globalresourcedict.__name__)

    # make sure the category is a name or string
    if ostack[-1].TYPE not in {ps.T_NAME, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, globalresourcedict.__name__)

    resource_dict = ps_dict.lookup(ctxt, ostack[-1], ps.global_resources.get_gvm().val[b"resource"])
    if not resource_dict:
        ostack[-1] = ps.Bool(False)
    else:
        ostack[-1] = resource_dict
        ostack.append(ps.Bool(True))


def localresourcedict(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    category <**localresourcedict**> dict true
    category <**localresourcedict**> false
    """

    if not ostack:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, localresourcedict.__name__)

    # make sure the category is a name or string
    if ostack[-1].TYPE not in {ps.T_NAME, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, localresourcedict.__name__)

    resource_dict = ps_dict.lookup(ctxt, ostack[-1], ctxt.lvm.val[b"resource"])
    if not resource_dict:
        ostack[-1] = ps.Bool(False)
    else:
        ostack[-1] = resource_dict
        ostack.append(ps.Bool(True))


def findresource(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    key category **findresource** instance


    attempts to obtain a named resource instance in a specified category. category is a
    name object that identifies a resource category, such as **Font** (see Section 3.9.2,
    "Resource Categories"). key is a name or string object that identifies the resource
    instance. (Names and strings are interchangeable; other types of keys are permitted
    but are not recommended.) If it succeeds, **findresource** pushes the resource
    instance on the operand stack; this is an object whose type depends on the resource
    category.

    **findresource** first attempts to obtain a resource instance that has previously been
    defined in virtual memory by **defineresource**. If the current VM allocation mode
    is local, **findresource** considers local resource definitions first, then global definitions
    (see **defineresource**). However, if the current VM allocation mode is global,
    **findresource** considers only global resource definitions.

    If the requested resource instance is not currently defined in VM, **findresource** attempts
    to obtain it from an external source. The way this is done is not specified
    by the PostScript language; it varies among different implementations and different
    resource categories. The effect of this action is to create an object in VM and
    execute **defineresource**. **findresource** then returns the newly created object. If key
    is not a name or a string, **findresource** will not attempt to obtain an external resource.

    When **findresource** loads an object into VM, it may use global VM even if the current
    VM allocation mode is local. In other words, it may set the VM allocation
    mode to global (true **setglobal**) while loading the resource instance and executing
    **defineresource**. The policy for whether to use global or local VM resides in the
    **Findresource** procedure for the specific resource category; see Section 3.9.2, "Resource
    Categories."

    During its execution, **findresource** may remove the definitions of resource instances
    that were previously loaded into VM by **findresource**. The mechanisms
    and policies for this depend on the category and the implementation; reclamation
    of resources may occur at times other than during execution of **findresource**.
    However, resource definitions that were made by explicit execution of **defineresource**
    are never disturbed by automatic reclamation.

    If the specified resource category does not exist, an undefined error occurs. If the
    category exists but there is no instance whose name is key, an undefinedresource
    error occurs.

    **Errors**:     **stackunderflow**, **typecheck**, **undefined**, **undefinedresource**
    **See Also**:   **defineresource**, **resourcestatus**, **resourceforall**, **undefineresource**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, findresource.__name__)
    # 2. TYPECHECK - Check operand types
    # Category must be a name or string; key can be any type per PLRM
    # ("other types of keys are permitted but are not recommended")
    if ostack[-1].TYPE not in {ps.T_NAME, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, findresource.__name__)

    category_name = ostack[-1]
    key = ostack[-2]

    # convert any strings to names
    if category_name.TYPE == ps.T_STRING:
        category_name = ps.Name(category_name.byte_string())
    if key.TYPE == ps.T_STRING:
        key = ps.Name(key.byte_string())

    if category_name.val == b"Category":
        resource_dict = ps_dict.lookup(
            ctxt, key, ps.global_resources.get_gvm().val[b"resource"].val[b"Category"]
        )
        if not resource_dict:
            return ps_error.e(ctxt, ps_error.UNDEFINED, findresource.__name__)
        ostack.pop()
        ostack[-1] = resource_dict
        return

    # get the implementation dict
    imp_dict = ps_dict.lookup(
        ctxt, category_name, ps.global_resources.get_gvm().val[b"resource"].val[b"Category"]
    )
    if not imp_dict:
        # the Category is undefined
        return ps_error.e(ctxt, ps_error.UNDEFINED, findresource.__name__)

    # get the DefineResource proc
    proc = imp_dict.val[b"FindResource"]

    if proc.TYPE in ps.ARRAY_TYPES:
        # DefineResourse is a procedure - execute it
        ctxt.e_stack.append(ps.HardReturn())
        ctxt.e_stack.append(copy.copy(proc))
        # begin the implementation dict
        ctxt.d_stack.append(imp_dict)
        # pop the category name
        ostack.pop()
        # execute the proc
        ps_control.exec_exec(ctxt, ostack, ctxt.e_stack)
        # end the implementation dict
        # Check if the top of the dict stack is still the imp_dict before popping.
        # If an error occurred during execution (caught by 'stopped'), the imp_dict
        # may have already been cleaned up or the d_stack state may have changed.
        if ctxt.d_stack and ctxt.d_stack[-1].val == imp_dict.val:
            ctxt.d_stack.pop()

    elif proc.TYPE == ps.T_OPERATOR and proc.val == findresource:
        # FindResource is this function

        if ctxt.vm_alloc_mode:
            # only search global resources
            if category_name.val not in ps.global_resources.get_gvm().val[b"resource"].val:
                return ps_error.e(ctxt, ps_error.UNDEFINED, findresource.__name__)
            resource_dict = ps_dict.lookup(
                ctxt, key, ps.global_resources.get_gvm().val[b"resource"].val[category_name.val]
            )
            if not resource_dict:
                return ps_error.e(
                    ctxt, ps_error.UNDEFINEDRESOURCE, findresource.__name__
                )
        else:
            # search local resources
            if category_name.val not in ctxt.lvm.val[b"resource"].val:
                return ps_error.e(ctxt, ps_error.UNDEFINED, findresource.__name__)
            resource_dict = ps_dict.lookup(
                ctxt, key, ctxt.lvm.val[b"resource"].val[category_name.val]
            )
            if not resource_dict:
                # now try global resources
                if category_name.val not in ps.global_resources.get_gvm().val[b"resource"].val:
                    return ps_error.e(ctxt, ps_error.UNDEFINED, findresource.__name__)
                resource_dict = ps_dict.lookup(
                    ctxt, key, ps.global_resources.get_gvm().val[b"resource"].val[category_name.val]
                )
                if not resource_dict:
                    return ps_error.e(
                        ctxt, ps_error.UNDEFINEDRESOURCE, findresource.__name__
                    )

        ostack.pop()
        ostack[-1] = resource_dict


def defineresource(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    key instance category **defineresource** instance


    associates a resource instance with a resource name in a specified category.
    category is a name object that identifies a resource category, such as Font (see
    Section 3.9.2, "Resource Categories"). key is a name or string object that will be
    used to identify the resource instance. (Names and strings are interchangeable;
    other types of keys are permitted but are not recommended.) instance is the resource
    instance itself; its type must be appropriate to the resource category.

    Before defining the resource instance, **defineresource** verifies that the instance object
    is the correct type. Depending on the resource category, it may also perform
    additional validation of the object and may have other side effects (see
    Section 3.9.2); these side effects are determined by the **DefineResource** procedure
    in the category implementation dictionary. Finally, **defineresource** makes the object
    read-only if its access is not already restricted.

    The lifetime of the definition depends on the VM allocation mode at the time
    **defineresource** is executed. If the current VM allocation mode is local
    (**currentglobal** returns false), the effect of **defineresource** is undone by the next
    nonnested **restore** operation. If the current VM allocation mode is global
    (**currentglobal** returns true), the effect of **defineresource** persists until global VM
    is restored at the end of the job. If the current job is not encapsulated, the effect of
    a global **defineresource** operation persists indefinitely, and may be visible to other
    execution contexts.

    Local and global definitions are maintained separately. If a new resource instance
    is defined with the same category and key as an existing one, the new definition
    overrides the old one. The precise effect depends on whether the old definition is
    local or global and whether the new definition (current VM allocation mode) is
    local or global. There are two main cases:

    •  The new definition is local. **defineresource** installs the new local definition,
       replacing an existing local definition if there is one. If there is an existing global
       definition, **defineresource** does not disturb it. However, the global definition is
       obscured by the local one. If the local definition is later removed, the global
       definition reappears.

    •  The new definition is global. **defineresource** first removes an existing local definition
       if there is one. It then installs the new global definition, replacing an
       existing global definition if there is one.

    **defineresource** can be used multiple times to associate a given resource instance
    with more than one key.

    If the category name is unknown, an **undefined** error occurs. If the instance is of
    the wrong type for the specified category, a **typecheck** error occurs. If the instance
    is in local VM but the current VM allocation mode is global, an **invalidaccess**
    error occurs; this is analogous to storing a local object into a global dictionary.
    Other errors can occur for specific categories; for example, when dealing with the
    **Font** or **CIDFont** category, **defineresource** may execute an **invalidfont** error.

    **Errors**:     **invalidaccess**, **stackunderflow**, **typecheck**, **undefined**
    **See Also**:   **undefineresource**, **findresource**, **resourcestatus**, **resourceforall**
    """

    # Stack validation: key instance category
    if len(ostack) < 3:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, defineresource.__name__)
    
    # Category must be Name or String
    if ostack[-1].TYPE not in {ps.T_NAME, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, defineresource.__name__)

    category_name = ostack[-1]
    instance = ostack[-2]
    key = ostack[-3]

    # Handle the case where key is a dictionary (common in Level 1 font definitions)
    # This pattern occurs with "fontdict dup definefont" where both key and instance
    # are the same font dictionary. Extract /FontName or /FID, or generate a unique name.
    key_was_dict = key.TYPE == ps.T_DICT
    if key_was_dict:
        # Try /FontName first
        if b"FontName" in key.val:
            key = key.val[b"FontName"]
        # Try /FID next
        elif b"FID" in key.val:
            key = key.val[b"FID"]
        else:
            # Generate a unique name based on the dictionary's identity
            # This handles anonymous fonts from "dup definefont" pattern
            unique_name = f"Font_{id(key)}".encode()
            key = ps.Name(unique_name)
            # Also store this name in the font dict for future reference
            if instance.TYPE == ps.T_DICT:
                instance.val[b"FontName"] = ps.Name(unique_name)
        # Update the operand stack so category-specific DefineResource procedures
        # receive the correct key (a Name) instead of the original dictionary
        ostack[-3] = key

    # the key must be name, string, or int
    if key.TYPE not in {ps.T_NAME, ps.T_STRING, ps.T_INT}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, defineresource.__name__)
    if key.TYPE == ps.T_INT:
        key = ps.Name(str(key.val))
        ostack[-3] = key

    required = [
        ps.Name(b"DefineResource"),
        ps.Name(b"UndefineResource"),
        ps.Name(b"FindResource"),
        ps.Name(b"ResourceStatus"),
        ps.Name(b"ResourceForAll"),
    ]

    # convert any strings to names
    if category_name.TYPE == ps.T_STRING:
        category_name = ps.Name(category_name.byte_string())
    if key.TYPE == ps.T_STRING:
        key = ps.Name(key.byte_string())

    if category_name.val == b"Category":
        # defining a new Category
        # instance must be a dictionary
        if instance.TYPE != ps.T_DICT:
            return ps_error.e(ctxt, ps_error.TYPECHECK, defineresource.__name__)

        # make sure we are not trying to set a local instance to global vm
        # and that the current vm mode is global
        if not instance.is_global or not ctxt.vm_alloc_mode:
            return ps_error.e(ctxt, ps_error.INVALIDACCESS, defineresource.__name__)

        # check for required entries and types
        for r in required:
            if not ps_dict.lookup(ctxt, r, instance):
                return ps_error.e(
                    ctxt, ps_error.UNDEFINEDRESULT, defineresource.__name__
                )
            if (
                instance.val[r.val].TYPE not in {ps.T_ARRAY, ps.T_PACKED_ARRAY, ps.T_OPERATOR}
                or instance.val[r.val].attrib != ps.ATTRIB_EXEC
            ):
                return ps_error.e(ctxt, ps_error.TYPECHECK, defineresource.__name__)

        # make the instance readonly
        # we have to disable setting access to readonly or we cannot define any resources!
        # instance.attrib = ps.ACCESS_READ_ONLY

        # set the instance dictionary's name
        instance.name = key.val

        # insert the Category name
        instance.val[b"Category"] = key

        # insert the new Category into the Category dictionary
        ps.global_resources.get_gvm().val[b"resource"].val[b"Category"].val[key.val] = instance

        # create the new Category in the global resource dictionary (with an initialy empty dictionary)
        d = ps.Dict(ctxt.id, name=key.val, access=ps.ACCESS_READ_ONLY, is_global=True)
        if b"__status__" not in d.val:
            d.val[b"__status__"] = ps.Int(0)
        ps.global_resources.get_gvm().val[b"resource"].val[key.val] = d

        # add it to the global_ref dictionary
        ctxt.global_refs[d.created] = d

        # create the new Category in the local resource dictionary (with an initialy empty dictionary)
        d = ps.Dict(ctxt.id, name=key.val, access=ps.ACCESS_READ_ONLY, is_global=False)
        if b"__status__" not in d.val:
            d.val[b"__status__"] = ps.Int(0)
        ctxt.lvm.val[b"resource"].val[key.val] = d

        ostack.pop()
        ostack[-1], ostack[-2] = ostack[-2], ostack[-1]
        ostack.pop()
    else:
        # make sure we are not trying to set a local instance to global vm
        if instance.is_composite and not instance.is_global and ctxt.vm_alloc_mode:
            return ps_error.e(ctxt, ps_error.INVALIDACCESS, defineresource.__name__)

        # get the implementation dict
        imp_dict = ps_dict.lookup(
            ctxt, category_name, ps.global_resources.get_gvm().val[b"resource"].val[b"Category"]
        )
        if not imp_dict:
            # the Category is undefined
            return ps_error.e(ctxt, ps_error.UNDEFINED, defineresource.__name__)

        # get the DefineResource proc
        proc = imp_dict.val[b"DefineResource"]

        if proc.TYPE in ps.ARRAY_TYPES:
            # DefineResourse is a procedure - execute it
            ctxt.e_stack.append(ps.HardReturn())
            ctxt.e_stack.append(copy.copy(proc))
            # begin the implementation dict
            ctxt.d_stack.append(imp_dict)
            # pop the category name
            ostack.pop()
            # execute the proc
            ps_control.exec_exec(ctxt, ostack, ctxt.e_stack)
            # end the implementation dict
            # Check if the top of the dict stack is still the imp_dict before popping.
            # If an error occurred during execution (caught by 'stopped'), the imp_dict
            # may have already been cleaned up or the d_stack state may have changed.
            if ctxt.d_stack and ctxt.d_stack[-1].val == imp_dict.val:
                ctxt.d_stack.pop()

        elif proc.TYPE == ps.T_OPERATOR and proc.val == defineresource:
            # DefineResourse is this function
            # check the instance type if necessary
            instance_type = ps_dict.lookup(ctxt, b"InstanceType", imp_dict)
            if instance_type:
                if (
                    ctxt.type_names[instance.TYPE] != instance_type.val
                    and instance.TYPE != ps.T_NULL
                ):
                    return ps_error.e(ctxt, ps_error.TYPECHECK, defineresource.__name__)

            if ctxt.vm_alloc_mode:
                # first remove the existing local definition, if there is one
                old_category_dict = ps_dict.lookup(
                    ctxt, category_name, ctxt.lvm.val[b"resource"]
                )
                if old_category_dict:
                    old_resource_dict = ps_dict.lookup(ctxt, key, old_category_dict)
                    if old_resource_dict:
                        del old_resource_dict.val[key.val]

            # make the instance readonly
            instance.access = ps.ACCESS_READ_ONLY

            if instance.TYPE == ps.T_DICT:
                # set the instance dictionary's name
                instance.name = key.val
                # set the dictionary's access to read only
                instance.access = ps.ACCESS_READ_ONLY
                if b"__status__" not in instance.val:
                    instance.val[b"__status__"] = ps.Int(0)

            # add the resource to the specified category
            resource_dict = (
                ps.global_resources.get_gvm().val[b"resource"]
                if ctxt.vm_alloc_mode
                else ctxt.lvm.val[b"resource"]
            )
            category_dict = resource_dict.val[category_name.val]
            category_dict.val[key.val] = instance

            ostack.pop()
            ostack[-1], ostack[-2] = ostack[-2], ostack[-1]
            ostack.pop()


def resourcestatus(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    key category **resourcestatus** status size true    (if resource exists)
                                false               (if not)


    returns status information about a named resource instance. category is a name
    object that identifies a resource category, such as **Font** (see Section 3.9.2, "Resource
    Categories"). key is a name or string object that identifies the resource instance.
    (Names and strings are interchangeable; keys of other types are permitted
    but are not recommended.)

    If the named resource instance exists, either defined in virtual memory or available
    from some external source, **resourcestatus** returns status, size, and the value
    true; otherwise, it returns false. Unlike **findresource**, **resourcestatus** never loads a
    resource instance into virtual memory.

    status is an integer with the following meanings:

        0   Defined in VM by an explicit **defineresource**; not subject to
            automatic removal

        1   Defined in VM by a previous execution of **findresource**; subject to
            automatic removal

        2   Not currently defined in VM, but available from external storage

    size is an integer giving the estimated VM consumption of the resource instance in
    bytes. This information may not be available for certain resources; if the size is
    unknown, -1 is returned. Usually, **resourcestatus** can obtain the size of a status 1
    or 2 resource (derived from the %%VMusage: comment in the resource file), but it
    has no general way to determine the size of a status 0 resource. See Section 3.9.4,
    "Resources as Files," for an explanation of how the size is determined. A size value
    of 0 is returned for implicit resources, whose instances do not occupy VM.

    If the current VM allocation mode is local, **resourcestatus** considers both local
    and global resource definitions, in that order (see **defineresource**). However, if
    the current VM allocation mode is global, only global resource definitions are visible
    to **resourcestatus**. Resource instances in external storage are visible without
    regard to the current VM allocation mode.

    If the specified resource category does not exist, an **undefined** error occurs.

    **Errors**:     **stackoverflow**, **stackunderflow**, **typecheck**, **undefined**
    **See Also**:   **defineresource**, **undefineresource**, **findresource**, **resourceforall**
    """

    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, resourcestatus.__name__)

    category_name = ostack[-1]
    key = ostack[-2]

    # the category name
    if category_name.TYPE not in {ps.T_NAME, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, resourcestatus.__name__)

    # Key can be any type per PLRM: "Names and strings are interchangeable;
    # keys of other types are permitted but are not recommended."
    # e.g., FontType category uses integer keys

    # convert any strings to names
    if category_name.TYPE == ps.T_STRING:
        category_name = ps.Name(category_name.byte_string())
    if key.TYPE == ps.T_STRING:
        key = ps.Name(key.byte_string())

    if category_name.val == b"Category":
        resource_dict = ps_dict.lookup(
            ctxt, key, ps.global_resources.get_gvm().val[b"resource"].val[b"Category"]
        )
        if not resource_dict:
            ostack.pop()
            ostack[-1] = ps.Bool(False)
        else:
            # Get status, defaulting to 0 if not present
            status_value = 0
            if b"__status__" in resource_dict.val:
                status_value = resource_dict.val[b"__status__"].val
            
            ostack[-2] = ps.Int(status_value)
            ostack[-1] = ps.Int(sys.getsizeof(resource_dict))
            ostack.append(ps.Bool(True))
        return

    # get the implementation dict
    imp_dict = ps_dict.lookup(
        ctxt, category_name, ps.global_resources.get_gvm().val[b"resource"].val[b"Category"]
    )
    if not imp_dict:
        # the Category is undefined
        return ps_error.e(ctxt, ps_error.UNDEFINED, resourcestatus.__name__)

    # get the ResourceStatus proc
    proc = imp_dict.val[b"ResourceStatus"]

    if proc.TYPE in ps.ARRAY_TYPES:
        # ResourceStatus is a procedure - execute it
        ctxt.e_stack.append(ps.HardReturn())
        ctxt.e_stack.append(copy.copy(proc))
        # begin the implementation dict
        ctxt.d_stack.append(imp_dict)
        # pop the category name
        ostack.pop()
        # execute the proc
        ps_control.exec_exec(ctxt, ostack, ctxt.e_stack)
        # end the implementation dict
        # Check if the top of the dict stack is still the imp_dict before popping.
        if ctxt.d_stack and ctxt.d_stack[-1].val == imp_dict.val:
            ctxt.d_stack.pop()

    else:
        # ResourceStatus procedure should handle the logic
        # This shouldn't normally be reached since categories should define ResourceStatus procedures
        ostack.pop()  # pop category
        ostack[-1] = ps.Bool(False)  # replace key with false


def undefineresource(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    key category **undefineresource** —

    removes the named resource instance identified by key from the specified category.
    This undoes the effect of a previous **defineresource**. If no such resource instance
    exists in VM, **undefineresource** does nothing; no error occurs. However, the
    resource category must exist, or else an undefined error occurs.

    Local and global resource definitions are maintained separately; the precise effect
    of **undefineresource** depends on the current VM allocation mode:

    1. Local—**undefineresource** removes a local definition if there is one. If there
       is a global definition with the same key, **undefineresource** does not disturb it;
       the global definition, formerly obscured by the local one, now reappears.

    2. Global—**undefineresource** removes a local definition, a global definition, or both.

    Depending on the resource category, **undefineresource** may have other side effects
    (see section 3.9.2, "Resource Categories"). However, it does not alter the resource
    instance in any way. If the instance is still accessible (say, stored directly in some
    dictionary or defined as a resource under another name), it can still be used in
    whatever ways are appropriate. The object becomes a candidate for garbage collection
    only if it is no longer accessible.

    The effect of **undefineresource** is subject to normal VM semantics. In particular,
    removal of a local resource instance can be undone by a subsequent non-nested **restore**.
    In this case, the resource instance is not a candidate for garbage collection.

    **undefineresource** removes the resource instance definition from VM only. If the
    resource instance also exists in external storage, it can still be found by **findresource**,
    **resourcestatus**, and **resourceforall**.

    PLRM Section 8.2, Page 656
    Stack: key category **undefineresource** —
    **Errors**: **stackunderflow**, **typecheck**, **undefined**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, undefineresource.__name__)
    # 2. TYPECHECK - Check operand types
    # Category must be name or string; key can be any type per PLRM
    if ostack[-1].TYPE not in {ps.T_NAME, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, undefineresource.__name__)

    category_name = ostack[-1]
    key = ostack[-2]

    # convert any strings to names
    if category_name.TYPE == ps.T_STRING:
        category_name = ps.Name(category_name.byte_string())
    if key.TYPE == ps.T_STRING:
        key = ps.Name(key.byte_string())

    if category_name.val == b"Category":
        # Cannot undefine Category entries - they are system-defined
        ostack.pop()
        ostack.pop()
        return

    # get the implementation dict
    imp_dict = ps_dict.lookup(
        ctxt, category_name, ps.global_resources.get_gvm().val[b"resource"].val[b"Category"]
    )
    if not imp_dict:
        # the Category is undefined
        return ps_error.e(ctxt, ps_error.UNDEFINED, undefineresource.__name__)

    # get the UndefineResource proc
    proc = imp_dict.val[b"UndefineResource"]

    if proc.TYPE in ps.ARRAY_TYPES:
        # UndefineResource is a procedure - execute it
        ctxt.e_stack.append(ps.HardReturn())
        ctxt.e_stack.append(copy.copy(proc))
        # begin the implementation dict
        ctxt.d_stack.append(imp_dict)
        # pop the category name
        ostack.pop()
        # execute the proc
        ps_control.exec_exec(ctxt, ostack, ctxt.e_stack)
        # end the implementation dict
        # Check if the top of the dict stack is still the imp_dict before popping.
        if ctxt.d_stack and ctxt.d_stack[-1].val == imp_dict.val:
            ctxt.d_stack.pop()

    elif proc.TYPE == ps.T_OPERATOR and proc.val == undefineresource:
        # UndefineResource is this function - implement generic behavior
        
        if ctxt.vm_alloc_mode:
            # Global mode - remove both local and global definitions
            
            # Remove local definition if it exists
            local_resource_dict = ps_dict.lookup(ctxt, category_name, ctxt.lvm.val[b"resource"])
            if local_resource_dict and key.val in local_resource_dict.val:
                del local_resource_dict.val[key.val]
            
            # Remove global definition if it exists
            global_resource_dict = ps_dict.lookup(
                ctxt, category_name, ps.global_resources.get_gvm().val[b"resource"]
            )
            if global_resource_dict and key.val in global_resource_dict.val:
                del global_resource_dict.val[key.val]
                
        else:
            # Local mode - remove only local definition
            local_resource_dict = ps_dict.lookup(ctxt, category_name, ctxt.lvm.val[b"resource"])
            if local_resource_dict and key.val in local_resource_dict.val:
                del local_resource_dict.val[key.val]

        # Pop operands
        ostack.pop()  # category
        ostack.pop()  # key


def resourceforall(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """
    template proc scratch category **resourceforall** —

    enumerates the resource instances that are available in a specified category. For each
    available resource instance whose name matches template (using the same matching
    rules as the string **forall** operator), **resourceforall** pushes the resource name on the
    operand stack, then executes proc. The template is a string that may contain the
    wildcard characters * and ?; see the string operator. The scratch operand should be a
    string at least as long as any resource name; it is used as temporary storage and may
    be overwritten with arbitrary values.

    category is a name object that identifies a resource category, such as Font (see
    Section 3.9.2, "Resource Categories"). **resourceforall** enumerates all available
    resource instances in the specified category. This includes instances that are defined
    in VM and instances that exist in external storage but are not currently loaded into VM.

    If the current VM allocation mode is local, **resourceforall** enumerates both local and
    global resource instances. However, if the current VM allocation mode is global,
    **resourceforall** enumerates only global resource instances (and external instances
    not currently loaded into VM).

    For categories with very large sets of available resource instances, **resourceforall**
    may consume considerable time and may strain the interpreter's memory capacity.
    The string **forall** operator should be used with care.

    If the specified resource category does not exist, an undefined error occurs.

    PLRM Section 8.2, Page 624
    Stack: template proc scratch category **resourceforall** —
    **Errors**: **stackunderflow**, **typecheck**, **undefined**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 4:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, resourceforall.__name__)

    # Validate operand types using list indexing (don't pop yet)
    category = ostack[-1]
    scratch = ostack[-2]
    proc = ostack[-3]
    template = ostack[-4]

    # Validate types
    if category.TYPE not in {ps.T_NAME, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, resourceforall.__name__)
    if template.TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, resourceforall.__name__)
    if proc.TYPE not in ps.ARRAY_TYPES or proc.attrib != ps.ATTRIB_EXEC:
        return ps_error.e(ctxt, ps_error.TYPECHECK, resourceforall.__name__)
    if scratch.TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, resourceforall.__name__)

    # Convert category to name if needed
    if category.TYPE == ps.T_STRING:
        category = ps.Name(category.byte_string())

    # Special case: Category category enumerates all defined categories
    if category.val == b"Category":
        ostack.pop()  # category
        scratch = ostack.pop()
        proc = ostack.pop()
        template = ostack.pop()

        cat_dict = ps.global_resources.get_gvm().val[b"resource"].val[b"Category"]
        template_str = template.python_string()

        e_stack_base = len(ctxt.e_stack)
        for key in sorted(cat_dict.val.keys()):
            key_str = key.decode('latin1') if isinstance(key, bytes) else str(key)
            if fnmatch.fnmatch(key_str, template_str):
                name_bytes = key if isinstance(key, bytes) else key.encode('latin1')
                strings = ctxt.local_strings
                offset = len(strings)
                length = len(name_bytes)
                strings.extend(name_bytes)
                name_obj = ps.String(ctxt.id, offset, length,
                                   access=ps.ACCESS_READ_ONLY, is_global=False)
                ostack.append(name_obj)
                ctxt.e_stack.append(copy.copy(proc))
                ps_control.exec_exec(ctxt, ostack, ctxt.e_stack)
                # If e_stack was unwound past our base (by stop), break
                if len(ctxt.e_stack) < e_stack_base:
                    break
        return

    # Check if category exists
    imp_dict = ps_dict.lookup(
        ctxt, category, ps.global_resources.get_gvm().val[b"resource"].val[b"Category"]
    )
    if not imp_dict:
        return ps_error.e(ctxt, ps_error.UNDEFINED, resourceforall.__name__)

    # Pop operands now that validation is complete
    ostack.pop()  # category
    scratch = ostack.pop()
    proc = ostack.pop()
    template = ostack.pop()

    # Always use Python-native implementation for resourceforall.
    # The PS-defined ResourceForAll (Generic category) pushes extra dicts onto
    # d_stack which corrupts user callback 'def' operations — the callback's
    # 'def' writes to the implementation dict instead of userdict.
    # The Python-native path enumerates the same resource dicts without
    # interfering with the dict stack during callback execution.

    # Collect all matching resource names
    resource_names = set()
    template_str = template.python_string()

    # Collect from local VM resources (if in local mode)
    if not ctxt.vm_alloc_mode:
        local_resource_dict = ps_dict.lookup(ctxt, category, ctxt.lvm.val[b"resource"])
        if local_resource_dict:
            for key in local_resource_dict.val:
                key_bytes = key if isinstance(key, bytes) else (key.val if hasattr(key, 'val') else str(key).encode('latin1'))
                if key_bytes not in (b"__status__", b"__access__"):
                    key_str = key_bytes.decode('latin1', errors='replace')
                    if fnmatch.fnmatch(key_str, template_str):
                        resource_names.add(key_str)

    # Collect from global VM resources
    global_resource_dict = ps_dict.lookup(
        ctxt, category, ps.global_resources.get_gvm().val[b"resource"]
    )
    if global_resource_dict:
        for key in global_resource_dict.val:
            key_bytes = key if isinstance(key, bytes) else (key.val if hasattr(key, 'val') else str(key).encode('latin1'))
            if key_bytes not in (b"__status__", b"__access__"):
                key_str = key_bytes.decode('latin1', errors='replace')
                if fnmatch.fnmatch(key_str, template_str):
                    resource_names.add(key_str)

    # Execute procedure for each matching resource
    e_stack_base = len(ctxt.e_stack)
    for resource_name in sorted(resource_names):
        name_bytes = resource_name.encode('latin1')
        strings = ctxt.local_strings
        offset = len(strings)
        length = len(name_bytes)
        strings.extend(name_bytes)
        name_obj = ps.String(ctxt.id, offset, length,
                           access=ps.ACCESS_READ_ONLY, is_global=False)
        ostack.append(name_obj)

        # Execute the procedure — HardReturn acts as a boundary so
        # exec_exec doesn't consume the outer execution context
        ctxt.e_stack.append(ps.HardReturn())
        ctxt.e_stack.append(copy.copy(proc))
        ps_control.exec_exec(ctxt, ostack, ctxt.e_stack)
        # If e_stack was unwound past our base (by stop), break
        if len(ctxt.e_stack) < e_stack_base:
            break


def loadsystemfont(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """Look up a system font path from the system font cache.

    Stack: font_name_key  .loadsystemfont  path_string true  |  false

    If the font is found in the system font cache, pushes the file path
    as a String and true.  Otherwise pushes false.
    """
    if not ostack:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, ".loadsystemfont")

    if ostack[-1].TYPE not in {ps.T_NAME, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, ".loadsystemfont")

    font_name_obj = ostack.pop()
    if font_name_obj.TYPE == ps.T_NAME:
        name_str = font_name_obj.val.decode("latin-1")
    else:
        name_str = font_name_obj.python_string()

    cache = sfc.SystemFontCache.get_instance()
    path = cache.get_font_path(name_str)

    # Only return paths for PostScript-runnable font files.
    # TTF/OTF binary fonts cannot be executed with `run`.
    if path is not None:
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext not in ("pfa", "t1"):
            path = None

    if path is None:
        ostack.append(ps.Bool(False))
    else:
        path_bytes = path.encode("latin-1", errors="replace")
        offset = len(ps.global_resources.global_strings)
        ps.global_resources.global_strings += path_bytes
        ostack.append(
            ps.String(
                ctxt.id,
                offset=offset,
                length=len(path_bytes),
                is_global=True,
            )
        )
        ostack.append(ps.Bool(True))


def loadbinarysystemfont(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """Load a binary system font (OTF/TTF) directly.

    Stack: font_name_key  .loadbinarysystemfont  true  |  false

    Looks up the font in the system font cache.  If found and the file
    is a binary font (.otf/.ttf/.pfb), loads it directly via Python
    and pushes true.  Otherwise pushes false.
    """
    if not ostack:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, ".loadbinarysystemfont")

    if ostack[-1].TYPE not in {ps.T_NAME, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, ".loadbinarysystemfont")

    font_name_obj = ostack.pop()
    if font_name_obj.TYPE == ps.T_NAME:
        name_str = font_name_obj.val.decode("latin-1")
    else:
        name_str = font_name_obj.python_string()

    cache = sfc.SystemFontCache.get_instance()
    path = cache.get_font_path(name_str)

    if path is None:
        ostack.append(ps.Bool(False))
        return

    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext not in ("otf", "ttf", "pfb"):
        ostack.append(ps.Bool(False))
        return

    # Dispatch by format
    success = False
    try:
        if ext == "otf":
            # Check if CFF-based (OTTO magic) or TrueType-based OTF
            with open(path, 'rb') as f:
                magic = f.read(4)
            if magic == b'OTTO':
                success = sfl.load_otf_cff(ctxt, path)
            else:
                success = sfl.load_ttf(ctxt, path)
        elif ext == "ttf":
            success = sfl.load_ttf(ctxt, path)
    except Exception:
        success = False

    ostack.append(ps.Bool(success))


def loadbinaryfontfile(ctxt: ps.Context, ostack: ps.Stack) -> None:
    """Load a binary font (OTF/TTF) from an explicit file path.

    Stack: font_name_key  file_path_string  .loadbinaryfontfile  true | false

    Used by fontcategory.ps when a .ttf/.otf file is found in resources/Font/.
    Unlike .loadbinarysystemfont (which looks up the system font cache),
    this operator takes the file path directly.
    """
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, ".loadbinaryfontfile")

    if ostack[-1].TYPE != ps.T_STRING:
        return ps_error.e(ctxt, ps_error.TYPECHECK, ".loadbinaryfontfile")
    if ostack[-2].TYPE not in {ps.T_NAME, ps.T_STRING}:
        return ps_error.e(ctxt, ps_error.TYPECHECK, ".loadbinaryfontfile")

    file_path_obj = ostack.pop()
    _font_name_obj = ostack.pop()

    path = file_path_obj.python_string()

    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""

    success = False
    try:
        if ext == "otf":
            with open(path, 'rb') as f:
                magic = f.read(4)
            if magic == b'OTTO':
                success = sfl.load_otf_cff(ctxt, path)
            else:
                success = sfl.load_ttf(ctxt, path)
        elif ext == "ttf":
            success = sfl.load_ttf(ctxt, path)
    except Exception:
        success = False

    ostack.append(ps.Bool(success))
