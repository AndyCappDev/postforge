# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

import copy
from typing import Any

from . import array as ps_array
from . import control_flow as ps_compound
from . import control as ps_control
from . import device_output as ps_device_ouput
from ..core import error as ps_error
from . import file as ps_file
from . import filter as ps_filter
from . import font_ops as ps_font_ops
from . import text_show as ps_text_show
from . import image as ps_image
from . import graphics_state as ps_gstate
from . import color_ops as ps_color_ops
from . import halftone_transfer as ps_halftone
from . import pattern_form as ps_pattern_form
from . import interpreter_params as ps_interpreter_params
from . import job_control as ps_job_control
from . import math as ps_math
from . import matrix as ps_matrix
from . import misc as ps_misc
from . import operand_stack as ps_operand_stack
from . import packed_array as ps_packedarray
from . import painting as ps_painting
from . import clipping as ps_clipping
from . import path as ps_path
from . import path_query as ps_path_query
from . import relational as ps_rel_bool_bitwise
from . import resource as ps_resource
from . import string as ps_string
from . import device_color_state as ps_device_color_state
from . import insideness as ps_insideness
from . import show_variants as ps_show_variants
from . import strokepath as ps_strokepath
from . import userpath as ps_userpath
from . import type_convert as ps_type_attrib_conv
from . import cff_ops as ps_cff_ops
from ..core import types as ps
from . import vm as ps_vm


def init_dictionaries(ctxt, name: str):
    # set the vm allocation mode to global
    ctxt.vm_alloc_mode = True

    d = create_system_dict(ctxt, b"systemdict")

    # define systemdict in global vm
    gvm = ps.global_resources.get_gvm()
    if gvm is None:
        raise RuntimeError("Global VM not initialized before init_dictionaries called")
    gvm.val[b"systemdict"] = d

    # push the system dict onto the dictionary stack
    ctxt.d_stack.append(d)

    # Build fast-path operator dispatch table from systemdict
    ctxt._operator_table = {
        key: value for key, value in d.val.items()
        if value.TYPE == ps.T_OPERATOR
    }

    # set the vm allocation mode back to local
    ctxt.vm_alloc_mode = False


def add_to_dict(ctxt, d, name: str, the_type: Any, val) -> None:
    d.val[bytes(name, "ascii")] = the_type(val)


def create_system_dict(ctxt, name: bytes) -> dict:
    obj = ps.Dict(ctxt.id, None, name, is_global=True)

    ops = [
        # boolean operators
        ("true", ps.Bool, True),
        ("false", ps.Bool, False),
        ("null", ps.Null, None),
        # array operators
        ("aload", ps.Operator, ps_array.aload),
        ("array", ps.Operator, ps_array.array),
        ("astore", ps.Operator, ps_array.astore),
        ("]", ps.Operator, ps_array.array_from_mark),
        ("}", ps.Operator, ps_array.procedure_from_mark),
        # packedarray operators
        ("currentpacking", ps.Operator, ps_packedarray.currentpacking),
        ("packedarray", ps.Operator, ps_packedarray.packedarray),
        ("setpacking", ps.Operator, ps_packedarray.setpacking),
        # compound operators
        ("copy", ps.Operator, ps_compound.ps_copy),
        ("length", ps.Operator, ps_compound.length),
        ("get", ps.Operator, ps_compound.get),
        ("getinterval", ps.Operator, ps_compound.getinterval),
        ("put", ps.Operator, ps_compound.put),
        ("putinterval", ps.Operator, ps_compound.putinterval),
        ("reverse", ps.Operator, ps_compound.reverse),
        # control operators
        ("break", ps.Operator, ps_control.ps_break),
        ("breaki", ps.Operator, ps_control.ps_breaki),
        ("countexecstack", ps.Operator, ps_control.countexecstack),
        ("exec", ps.Operator, ps_control.ps_exec),
        ("execstack", ps.Operator, ps_control.execstack),
        ("for", ps.Operator, ps_control.ps_for),
        ("forall", ps.Operator, ps_control.forall),
        ("if", ps.Operator, ps_control.ps_if),
        ("ifelse", ps.Operator, ps_control.ifelse),
        ("loop", ps.Operator, ps_control.loop),
        ("repeat", ps.Operator, ps_control.repeat),
        ("stop", ps.Operator, ps_control.stop),
        ("stopped", ps.Operator, ps_control.stopped),
        ("exit", ps.Operator, ps_control.ps_exit),
        # device setup and output operators
        ("copypage", ps.Operator, ps_device_ouput.copypage),
        ("currentpagedevice", ps.Operator, ps_device_ouput.currentpagedevice),
        ("setpagedevice", ps.Operator, ps_device_ouput.setpagedevice),
        ("showpage", ps.Operator, ps_device_ouput.showpage),
        ("flushpage", ps.Operator, ps_device_ouput.flushpage),
        ("nulldevice", ps.Operator, ps_device_ouput.nulldevice),
        # dictionary operators
        (">>", ps.Operator, dict_from_mark),
        ("begin", ps.Operator, begin),
        ("begin", ps.Operator, begin),
        ("countdictstack", ps.Operator, countdictstack),
        ("currentdict", ps.Operator, currentdict),
        ("def", ps.Operator, ps_def),
        ("dict", ps.Operator, ps_dict),
        ("dictname", ps.Operator, dictname),
        ("dictstack", ps.Operator, dictstack),
        ("end", ps.Operator, end),
        ("known", ps.Operator, known),
        ("load", ps.Operator, load),
        ("maxlength", ps.Operator, maxlength),
        ("store", ps.Operator, store),
        ("where", ps.Operator, where),
        ("undef", ps.Operator, undef),
        (".systemundef", ps.Operator, systemundef),
        # file operators
        # ('==', ps.Operator, ps_equal_equal_print),
        ("bytesavailable", ps.Operator, ps_file.bytesavailable),
        ("closefile", ps.Operator, ps_file.closefile),
        ("currentfile", ps.Operator, ps_file.currentfile),
        ("currentobjectformat", ps.Operator, ps_file.currentobjectformat),
        ("deletefile", ps.Operator, ps_file.deletefile),
        ("file", ps.Operator, ps_file.ps_file),
        ("filename", ps.Operator, ps_file.filename),
        ("filenameforall", ps.Operator, ps_file.filenameforall),
        ("fileposition", ps.Operator, ps_file.fileposition),
        ("flush", ps.Operator, ps_file.flush),
        ("flushfile", ps.Operator, ps_file.flushfile),
        ("line", ps.Operator, ps_file.line),
        ("print", ps.Operator, ps_file.ps_print),
        ("printarray", ps.Operator, ps_file.printarray),
        ("printobject", ps.Operator, ps_file.printobject),
        ("read", ps.Operator, ps_file.read),
        ("readhexstring", ps.Operator, ps_file.readhexstring),
        ("readline", ps.Operator, ps_file.readline),
        ("readstring", ps.Operator, ps_file.readstring),
        ("renamefile", ps.Operator, ps_file.renamefile),
        ("resetfile", ps.Operator, ps_file.resetfile),
        ("run", ps.Operator, ps_file.run),
        ("runlibfile", ps.Operator, ps_file.runlibfile),
        ("setfileposition", ps.Operator, ps_file.setfileposition),
        ("setobjectformat", ps.Operator, ps_file.setobjectformat),
        ("status", ps.Operator, ps_file.status),
        ("write", ps.Operator, ps_file.write),
        ("writehexstring", ps.Operator, ps_file.writehexstring),
        ("writeobject", ps.Operator, ps_file.writeobject),
        ("writestring", ps.Operator, ps_file.writestring),
        # filter operators
        ("filter", ps.Operator, ps_filter.ps_filter),
        # glyph and font operators
        ("ashow", ps.Operator, ps_text_show.ashow),
        ("awidthshow", ps.Operator, ps_text_show.awidthshow),
        ("charpath", ps.Operator, ps_text_show.charpath),
        ("composefont", ps.Operator, ps_font_ops.composefont),
        ("cshow", ps.Operator, ps_text_show.cshow),
        ("currentfont", ps.Operator, ps_font_ops.currentfont),
        ("rootfont", ps.Operator, ps_font_ops.rootfont),
        ("makefont", ps.Operator, ps_font_ops.makefont),
        ("scalefont", ps.Operator, ps_font_ops.scalefont),
        ("setfont", ps.Operator, ps_font_ops.setfont),
        ("show", ps.Operator, ps_text_show.show),
        ("stringwidth", ps.Operator, ps_text_show.stringwidth),
        ("widthshow", ps.Operator, ps_text_show.widthshow),
        ("kshow", ps.Operator, ps_show_variants.kshow),
        ("glyphshow", ps.Operator, ps_show_variants.glyphshow),
        ("xshow", ps.Operator, ps_text_show.xshow),
        ("xyshow", ps.Operator, ps_text_show.xyshow),
        ("yshow", ps.Operator, ps_text_show.yshow),

        # Type 3 font operators
        ("setcachedevice", ps.Operator, ps_text_show.setcachedevice),
        ("setcachedevice2", ps.Operator, ps_text_show.setcachedevice2),
        ("setcharwidth", ps.Operator, ps_text_show.setcharwidth),
        # graphics state operators
        ("currentflat", ps.Operator, ps_gstate.currentflat),
        ("currentlinecap", ps.Operator, ps_gstate.currentlinecap),
        ("currentlinejoin", ps.Operator, ps_gstate.currentlinejoin),
        ("currentlinewidth", ps.Operator, ps_gstate.currentlinewidth),
        ("currentdash", ps.Operator, ps_gstate.currentdash),
        ("currentmiterlimit", ps.Operator, ps_gstate.currentmiterlimit),
        ("currenthalftone", ps.Operator, ps_halftone.currenthalftone),
        ("currentscreen", ps.Operator, ps_halftone.currentscreen),
        ("grestore", ps.Operator, ps_gstate.grestore),
        ("grestoreall", ps.Operator, ps_gstate.grestoreall),
        ("gsave", ps.Operator, ps_gstate.gsave),
        ("gstate", ps.Operator, ps_gstate.gstate),
        ("currentgstate", ps.Operator, ps_gstate.currentgstate),
        ("setgstate", ps.Operator, ps_gstate.setgstate),
        ("initgraphics", ps.Operator, ps_gstate.initgraphics),
        ("currentcolor", ps.Operator, ps_color_ops.currentcolor),
        ("currentcolorspace", ps.Operator, ps_color_ops.currentcolorspace),
        ("currentcmykcolor", ps.Operator, ps_color_ops.currentcmykcolor),
        ("currentgray", ps.Operator, ps_color_ops.currentgray),
        ("currenthsbcolor", ps.Operator, ps_color_ops.currenthsbcolor),
        ("currentrgbcolor", ps.Operator, ps_color_ops.currentrgbcolor),
        ("currentoverprint", ps.Operator, ps_gstate.currentoverprint),
        ("currentstrokeadjust", ps.Operator, ps_gstate.currentstrokeadjust),
        ("setcolor", ps.Operator, ps_color_ops.setcolor),
        ("setcolorspace", ps.Operator, ps_color_ops.setcolorspace),
        ("setdash", ps.Operator, ps_gstate.setdash),
        ("setflat", ps.Operator, ps_gstate.setflat),
        ("setgray", ps.Operator, ps_color_ops.setgray),
        ("sethsbcolor", ps.Operator, ps_color_ops.sethsbcolor),
        ("setlinecap", ps.Operator, ps_gstate.setlinecap),
        ("setlinejoin", ps.Operator, ps_gstate.setlinejoin),
        ("setlinewidth", ps.Operator, ps_gstate.setlinewidth),
        ("setmiterlimit", ps.Operator, ps_gstate.setmiterlimit),
        ("setrgbcolor", ps.Operator, ps_color_ops.setrgbcolor),
        ("setcmykcolor", ps.Operator, ps_color_ops.setcmykcolor),
        ("sethalftone", ps.Operator, ps_halftone.sethalftone),
        ("setscreen", ps.Operator, ps_halftone.setscreen),
        ("setoverprint", ps.Operator, ps_gstate.setoverprint),
        ("setstrokeadjust", ps.Operator, ps_gstate.setstrokeadjust),
        ("settransfer", ps.Operator, ps_halftone.settransfer),
        ("setblackgeneration", ps.Operator, ps_halftone.setblackgeneration),
        ("currentblackgeneration", ps.Operator, ps_halftone.currentblackgeneration),
        ("setundercolorremoval", ps.Operator, ps_halftone.setundercolorremoval),
        ("currentundercolorremoval", ps.Operator, ps_halftone.currentundercolorremoval),
        # device-dependent color state operators
        ("currenttransfer", ps.Operator, ps_device_color_state.currenttransfer),
        ("setcolorscreen", ps.Operator, ps_device_color_state.setcolorscreen),
        ("currentcolorscreen", ps.Operator, ps_device_color_state.currentcolorscreen),
        ("setcolortransfer", ps.Operator, ps_device_color_state.setcolortransfer),
        ("currentcolortransfer", ps.Operator, ps_device_color_state.currentcolortransfer),
        ("setcolorrendering", ps.Operator, ps_device_color_state.setcolorrendering),
        ("currentcolorrendering", ps.Operator, ps_device_color_state.currentcolorrendering),
        # pattern and form operators
        ("makepattern", ps.Operator, ps_pattern_form.makepattern),
        ("setpattern", ps.Operator, ps_pattern_form.setpattern),
        ("execform", ps.Operator, ps_pattern_form.execform),
        # interpreter parameter operators
        ("setsystemparams", ps.Operator, ps_interpreter_params.setsystemparams),
        ("currentsystemparams", ps.Operator, ps_interpreter_params.currentsystemparams),
        ("setuserparams", ps.Operator, ps_interpreter_params.setuserparams),
        ("currentuserparams", ps.Operator, ps_interpreter_params.currentuserparams),
        ("vmreclaim", ps.Operator, ps_interpreter_params.vmreclaim),
        ("setvmthreshold", ps.Operator, ps_interpreter_params.setvmthreshold),
        ("cachestatus", ps.Operator, ps_interpreter_params.cachestatus),
        ("setcacheparams", ps.Operator, ps_interpreter_params.setcacheparams),
        ("currentcacheparams", ps.Operator, ps_interpreter_params.currentcacheparams),
        ("setucacheparams", ps.Operator, ps_interpreter_params.setucacheparams),
        ("ucachestatus", ps.Operator, ps_interpreter_params.ucachestatus),
        ("setdevparams", ps.Operator, ps_interpreter_params.setdevparams),
        ("currentdevparams", ps.Operator, ps_interpreter_params.currentdevparams),
        ("setcachelimit", ps.Operator, ps_interpreter_params.setcachelimit),
        # math operators
        ("abs", ps.Operator, ps_math.ps_abs),
        ("add", ps.Operator, ps_math.add),
        ("atan", ps.Operator, ps_math.atan),
        ("ceiling", ps.Operator, ps_math.ceiling),
        ("cos", ps.Operator, ps_math.cos),
        ("div", ps.Operator, ps_math.div),
        ("exp", ps.Operator, ps_math.exp),
        ("floor", ps.Operator, ps_math.floor),
        ("idiv", ps.Operator, ps_math.idiv),
        ("ln", ps.Operator, ps_math.ln),
        ("log", ps.Operator, ps_math.log),
        ("max", ps.Operator, ps_math.ps_max),
        ("min", ps.Operator, ps_math.ps_min),
        ("mod", ps.Operator, ps_math.mod),
        ("mul", ps.Operator, ps_math.mul),
        ("neg", ps.Operator, ps_math.neg),
        ("round", ps.Operator, ps_math.ps_round),
        ("rand", ps.Operator, ps_math.rand),
        ("rrand", ps.Operator, ps_math.rrand),
        ("sin", ps.Operator, ps_math.sin),
        ("srand", ps.Operator, ps_math.srand),
        ("sub", ps.Operator, ps_math.sub),
        ("sqrt", ps.Operator, ps_math.sqrt),
        ("truncate", ps.Operator, ps_math.truncate),
        # matrix operators
        ("concat", ps.Operator, ps_matrix.concat),
        ("concatmatrix", ps.Operator, ps_matrix.concatmatrix),
        ("currentmatrix", ps.Operator, ps_matrix.currentmatrix),
        ("defaultmatrix", ps.Operator, ps_matrix.defaultmatrix),
        ("dtransform", ps.Operator, ps_matrix.dtransform),
        ("identmatrix", ps.Operator, ps_matrix.identmatrix),
        ("idtransform", ps.Operator, ps_matrix.idtransform),
        ("initmatrix", ps.Operator, ps_matrix.initmatrix),
        ("invertmatrix", ps.Operator, ps_matrix.invertmatrix),
        ("itransform", ps.Operator, ps_matrix.itransform),
        ("matrix", ps.Operator, ps_matrix.matrix),
        ("rotate", ps.Operator, ps_matrix.rotate),
        ("scale", ps.Operator, ps_matrix.scale),
        ("setmatrix", ps.Operator, ps_matrix.setmatrix),
        ("transform", ps.Operator, ps_matrix.transform),
        ("translate", ps.Operator, ps_matrix.translate),
        # misc operators
        ("bind", ps.Operator, ps_misc.bind),
        ("echo", ps.Operator, ps_misc.echo),
        ("eexec", ps.Operator, ps_misc.eexec),
        ("exechistorystack", ps.Operator, ps_misc.exechistorystack),
        ("help", ps.Operator, ps_misc.help),
        ("internaldict", ps.Operator, ps_misc.internaldict),
        ("loopname", ps.Operator, ps_misc.loopname),
        ("pauseexechistory", ps.Operator, ps_misc.pauseexechistory),
        ("resumeexechistory", ps.Operator, ps_misc.resumeexechistory),
        ("usertime", ps.Operator, ps_misc.usertime),
        ("realtime", ps.Operator, ps_misc.realtime),
        # operand stack operators
        ("clear", ps.Operator, ps_operand_stack.clear),
        ("cleartomark", ps.Operator, ps_operand_stack.cleartomark),
        ("counttomark", ps.Operator, ps_operand_stack.counttomark),
        ("count", ps.Operator, ps_operand_stack.count),
        ("dup", ps.Operator, ps_operand_stack.dup),
        ("exch", ps.Operator, ps_operand_stack.exch),
        ("index", ps.Operator, ps_operand_stack.index),
        ("mark", ps.Operator, ps_operand_stack.ps_mark),
        ("pop", ps.Operator, ps_operand_stack.pop),
        ("printostack", ps.Operator, ps_operand_stack.printostck),
        ("roll", ps.Operator, ps_operand_stack.roll),
        # painting operators
        ("erasepage", ps.Operator, ps_painting.erasepage),
        ("eofill", ps.Operator, ps_painting.eofill),
        ("fill", ps.Operator, ps_painting.fill),
        ("rectfill", ps.Operator, ps_painting.rectfill),
        ("rectstroke", ps.Operator, ps_painting.rectstroke),
        ("stroke", ps.Operator, ps_painting.stroke),
        ("strokepath", ps.Operator, ps_strokepath.strokepath),
        ("shfill", ps.Operator, ps_painting.shfill),
        # insideness testing operators
        ("infill", ps.Operator, ps_insideness.infill),
        ("ineofill", ps.Operator, ps_insideness.ineofill),
        ("instroke", ps.Operator, ps_insideness.instroke),
        # user path operators
        ("inueofill", ps.Operator, ps_userpath.inueofill),
        ("inufill", ps.Operator, ps_userpath.inufill),
        ("inustroke", ps.Operator, ps_userpath.inustroke),
        ("uappend", ps.Operator, ps_userpath.uappend),
        ("ucache", ps.Operator, ps_userpath.ucache),
        ("ueofill", ps.Operator, ps_userpath.ueofill),
        ("ufill", ps.Operator, ps_userpath.ufill),
        ("upath", ps.Operator, ps_userpath.upath),
        ("ustroke", ps.Operator, ps_userpath.ustroke),
        ("ustrokepath", ps.Operator, ps_userpath.ustrokepath),
        # image operators
        ("image", ps.Operator, ps_image.ps_image),
        ("imagemask", ps.Operator, ps_image.ps_imagemask),
        ("colorimage", ps.Operator, ps_image.ps_colorimage),
        # path construction operators
        ("arc", ps.Operator, ps_path.arc),
        ("arcn", ps.Operator, ps_path.arcn),
        ("arct", ps.Operator, ps_path.arct),
        ("arcto", ps.Operator, ps_path.arcto),
        ("clip", ps.Operator, ps_clipping.clip),
        ("clippath", ps.Operator, ps_clipping.clippath),
        ("cliprestore", ps.Operator, ps_clipping.cliprestore),
        ("clipsave", ps.Operator, ps_clipping.clipsave),
        ("eoclip", ps.Operator, ps_clipping.eoclip),
        ("closepath", ps.Operator, ps_path.closepath),
        ("currentpoint", ps.Operator, ps_path_query.currentpoint),
        ("curveto", ps.Operator, ps_path.curveto),
        ("flattenpath", ps.Operator, ps_path_query.flattenpath),
        ("initclip", ps.Operator, ps_clipping.initclip),
        ("lineto", ps.Operator, ps_path.lineto),
        ("moveto", ps.Operator, ps_path.moveto),
        ("pathbbox", ps.Operator, ps_path_query.pathbbox),
        ("pathforall", ps.Operator, ps_path_query.pathforall),
        ("rcurveto", ps.Operator, ps_path.rcurveto),
        ("rectclip", ps.Operator, ps_clipping.rectclip),
        ("reversepath", ps.Operator, ps_path_query.reversepath),
        ("setbbox", ps.Operator, ps_path_query.setbbox),
        ("rlineto", ps.Operator, ps_path.rlineto),
        ("rmoveto", ps.Operator, ps_path.rmoveto),
        ("newpath", ps.Operator, ps_path.newpath),
        # relational, boolean, and bitwise operators
        ("and", ps.Operator, ps_rel_bool_bitwise.ps_and),
        ("bitshift", ps.Operator, ps_rel_bool_bitwise.bitshift),
        ("eq", ps.Operator, ps_rel_bool_bitwise.eq),
        ("ge", ps.Operator, ps_rel_bool_bitwise.ge),
        ("gt", ps.Operator, ps_rel_bool_bitwise.gt),
        ("le", ps.Operator, ps_rel_bool_bitwise.le),
        ("lt", ps.Operator, ps_rel_bool_bitwise.lt),
        ("ne", ps.Operator, ps_rel_bool_bitwise.ne),
        ("not", ps.Operator, ps_rel_bool_bitwise.ps_not),
        ("or", ps.Operator, ps_rel_bool_bitwise.ps_or),
        ("xor", ps.Operator, ps_rel_bool_bitwise.xor),
        # resource operators
        ("categoryimpdict", ps.Operator, ps_resource.categoryimpdict),
        ("createresourcecategory", ps.Operator, ps_resource.createresourcecategory),
        ("defineresource", ps.Operator, ps_resource.defineresource),
        ("findresource", ps.Operator, ps_resource.findresource),
        ("globalresourcedict", ps.Operator, ps_resource.globalresourcedict),
        ("localresourcedict", ps.Operator, ps_resource.localresourcedict),
        ("resourceforall", ps.Operator, ps_resource.resourceforall),
        ("resourcestatus", ps.Operator, ps_resource.resourcestatus),
        ("undefineresource", ps.Operator, ps_resource.undefineresource),
        # string operators
        ("anchorsearch", ps.Operator, ps_string.anchorsearch),
        ("join", ps.Operator, ps_string.join),
        ("search", ps.Operator, ps_string.search),
        ("string", ps.Operator, ps_string.ps_string),
        ("token", ps.Operator, ps_string.token),
        # type, attribute, and convertsion operators
        ("cvi", ps.Operator, ps_type_attrib_conv.cvi),
        ("cvlit", ps.Operator, ps_type_attrib_conv.cvlit),
        ("cvn", ps.Operator, ps_type_attrib_conv.cvn),
        ("cvr", ps.Operator, ps_type_attrib_conv.cvr),
        ("cvrs", ps.Operator, ps_type_attrib_conv.cvrs),
        ("cvs", ps.Operator, ps_type_attrib_conv.cvs),
        ("cvx", ps.Operator, ps_type_attrib_conv.cvx),
        ("executeonly", ps.Operator, ps_type_attrib_conv.executeonly),
        ("noaccess", ps.Operator, ps_type_attrib_conv.noaccess),
        ("readonly", ps.Operator, ps_type_attrib_conv.readonly),
        ("rcheck", ps.Operator, ps_type_attrib_conv.rcheck),
        ("type", ps.Operator, ps_type_attrib_conv.ps_type),
        ("wcheck", ps.Operator, ps_type_attrib_conv.wcheck),
        ("xcheck", ps.Operator, ps_type_attrib_conv.xcheck),
        # vm operators
        ("currentglobal", ps.Operator, ps_vm.currentglobal),
        ("defineuserobject", ps.Operator, ps_vm.defineuserobject),
        ("execuserobject", ps.Operator, ps_vm.execuserobject),
        ("gcheck", ps.Operator, ps_vm.gcheck),
        ("restore", ps.Operator, ps_vm.restore),
        ("save", ps.Operator, ps_vm.save),
        ("setglobal", ps.Operator, ps_vm.setglobal),
        ("undefineuserobject", ps.Operator, ps_vm.undefineuserobject),
        ("vmstatus", ps.Operator, ps_vm.vmstatus),
        # job control operators
        ("exitserver", ps.Operator, ps_job_control.exitserver),
        (".quitwithcode", ps.Operator, ps_job_control.ps_quitwithcode),
        ("startjob", ps.Operator, ps_job_control.startjob),
        # Font internal operators
        (".nextfid", ps.Operator, ps_font_ops.nextfid),
        # CFF font operators
        (".cff_startdata", ps.Operator, ps_cff_ops.cff_startdata),
        # System font cache operators
        (".loadsystemfont", ps.Operator, ps_resource.loadsystemfont),
        (".loadbinarysystemfont", ps.Operator, ps_resource.loadbinarysystemfont),
        (".loadbinaryfontfile", ps.Operator, ps_resource.loadbinaryfontfile),
    ]

    for op in ops:
        add_to_dict(ctxt, obj, *op)

    # add a reference to the systemdict itself
    obj.val[bytes("systemdict", "ascii")] = obj

    # set the systemdict to readonly
    obj._access = ps.ACCESS_READ_ONLY
    return obj


def dict_from_mark(ctxt, ostack):
    """
    mark key1 value1 ... keyn valuen **>>** dict

    Creates and returns a dictionary containing the specified key-value
    pairs. The operands are a mark followed by an even number of objects,
    which the operator uses alternately as keys and values to be inserted
    into the dictionary. The dictionary is allocated space for precisely
    the number of key-value pairs supplied.

    The dictionary is allocated in local or global VM according to the
    current VM allocation mode. An **invalidaccess** error occurs if the
    dictionary is in global VM and any keys or values are in local VM.
    A **rangecheck** error occurs if there is an odd number of objects
    above the topmost mark on the stack.

    **Errors**: **invalidaccess**, **rangecheck**, **typecheck**, **unmatchedmark**
    **See Also**: **<<**, **mark**, **dict**
    """
    ret = ps_operand_stack.counttomark(ctxt, ostack, internal=True)
    if ret is not None:
        return ps_error.e(ctxt, ret, ">>")
    pairs = ostack[-1].val
    ostack.pop()

    if pairs % 2:
        return ps_error.e(ctxt, ps_error.RANGECHECK, ">>")

    d = ps.Dict(ctxt.id, is_global=ctxt.vm_alloc_mode)
    while ostack[-1].TYPE != ps.T_MARK:
        val = ostack.pop()
        key = ostack.pop()

        if key.TYPE in ps.LITERAL_TYPES:
            if ( # literal keys
                d.is_global
                and val.is_composite
                and not val.is_global
            ):
                return ps_error.e(ctxt, ps_error.INVALIDACCESS, ">>")
        elif (
            # composite keys
            d.is_global
            and not key.is_global
            and val.is_composite
            and not val.is_global
        ):
            return ps_error.e(ctxt, ps_error.INVALIDACCESS, ">>")

        # if this is a string being def'd, set it's is_defined flag to True
        if val.TYPE == ps.T_STRING:
            val.is_defined = True

        d.put(key, val)

    d.max_length = len(d.val) + 10
    d._access = ps.ACCESS_UNLIMITED
    ostack[-1] = d


def lookup(ctxt, obj, dictionary=None):
    # lookup a value in the dictionary stack
    # returns the object found or None if not found

    key = ctxt.d_stack[-1].create_key(obj)

    if dictionary is not None:
        # look it up in the specified dictionary
        value = dictionary.val.get(key, None)
        if value is None or value.TYPE == ps.T_OPERATOR:
            return value
        return value.__copy__()  # Direct call avoids copy module overhead
    else:
        # Fast-path: check operator table for unshadowed systemdict operators
        op_table = ctxt._operator_table
        if op_table is not None and key in op_table:
            # Check if any user dict shadows this name (skip systemdict at index 0)
            d_stack = ctxt.d_stack
            for i in range(len(d_stack) - 1, 0, -1):
                if key in d_stack[i].val:
                    break  # Shadowed, fall through to normal path
            else:
                return op_table[key]  # Unshadowed operator, return directly

        d_stack = ctxt.d_stack
        for i in range(len(d_stack) - 1, -1, -1):
            if d_stack[i].access() < ps.ACCESS_READ_ONLY:
                continue

            value = d_stack[i].val.get(key, None)

            if value is not None:
                # found it - operators are immutable, skip copy
                if value.TYPE == ps.T_OPERATOR:
                    return value
                return value.__copy__()  # Direct call avoids copy module overhead

        # not found in any of the dictionaries
        return None


def begin(ctxt, ostack):
    """
    dict **begin** –


    pushes dict on the dictionary stack, making it the current dictionary and installing
    it as the first of the dictionaries consulted during implicit name lookup and by
    **def**, load, store, and **where**.

    **Errors**:     **dictstackoverflow**, **invalidaccess**, **stackunderflow**, **typecheck**
    **See Also**:   **end**, **countdictstack**, **dictstack**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, begin.__name__)
    
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE != ps.T_DICT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, begin.__name__)
    
    # 3. INVALIDACCESS - Check access permission
    if ostack[-1].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, begin.__name__)

    ctxt.d_stack.append(ctxt.o_stack[-1])

    if not ctxt.d_stack[-1].is_global:
        # add this to the ctxt's local_refs
        # this takes care of any dictionaries that are not def'd
        ctxt.local_refs[ctxt.d_stack[-1].created] = ctxt.d_stack[-1].val
    ctxt.o_stack.pop()


def countdictstack(ctxt, ostack):
    """
    – **countdictstack** int


    counts the number of dictionaries currently on the dictionary stack and pushes
    this count on the operand stack.

    **Errors**:     **stackoverflow**
    **See Also**:   **dictstack**, **begin**, **end**
    """

    ostack.append(ps.Int(len(ctxt.d_stack)))


def currentdict(ctxt, ostack):
    """
    – **currentdict** dict


    pushes the current dictionary (the dictionary on the top of the dictionary stack)
    on the operand stack. **currentdict** does not pop the dictionary stack; it just pushes
    a duplicate of its top element on the operand stack.

    **Errors**:     **stackoverflow**
    **See Also**:   **begin**, **dictstack**
    """

    if ctxt.MaxOpStack and len(ostack) >= ctxt.MaxOpStack:
        return ps_error.e(ctxt, ps_error.STACKOVERFLOW, currentdict.__name__)

    ostack.append(copy.copy(ctxt.d_stack[-1]))


def ps_def(ctxt, ostack):
    """
    key value **def** –


    associates key with value in the current dictionary—the one on the top of the dictionary
    stack (see Section 3.4, "Stacks"). If key is already present in the current
    dictionary, **def** simply replaces its value; otherwise, **def** creates a new entry for key
    and stores value with it.

    If the current dictionary is in global VM and value is a composite object whose
    value is in local VM, an **invalidaccess** error occurs (see Section 3.7.2, "Local and
    Global VM").

    **Examples**
        /ncnt 1 **def**             % Define ncnt to be 1 in current dict
        /ncnt ncnt 1 add **def**    % ncnt now has value 2

    **Errors**:     **dictfull**, **invalidaccess**, **limitcheck**, **stackunderflow**, **typecheck**, **VMerror**
    **See Also**:   **store**, **put**
    """
    op = "def"

    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, op)

    d = ctxt.d_stack[-1]

    if not ctxt.initializing and d.access() < ps.ACCESS_UNLIMITED:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, op)

    if not ctxt.initializing and d.is_global:
        if ostack[-1].is_composite:
            if not ostack[-1].is_global:
                return ps_error.e(ctxt, ps_error.INVALIDACCESS, op)

    key = d.create_key(ostack[-2])

    # if this is a string being def'd, set it's is_defined flag to True
    if ostack[-1].TYPE == ps.T_STRING:
        ostack[-1].is_defined = True

    # set the name if this is a dict
    if ostack[-1].TYPE == ps.T_DICT:
        # key may be a PSObject (Name, String) or a raw Python value (int, float, bool)
        # from create_key() which extracts .val for numeric types
        if isinstance(key, ps.PSObject) and key.TYPE == ps.T_NAME:
            ostack[-1].name = key.val
        elif isinstance(key, bytes):
            ostack[-1].name = key
        else:
            ostack[-1].name = bytes(str(key.val if isinstance(key, ps.PSObject) else key), "ascii")

    d.put(ostack[-2], ostack[-1])

    if (
        ctxt.initializing
        and ostack[-1].TYPE == ps.T_DICT
        and not ostack[-1].is_global
    ):
        # this is a local dictionary created during the init phase
        # it gets def'd inside the systemdict
        # so add it to local vm
        ctxt.lvm.val[ostack[-2]] = ostack[-1]

    # if this is a global composite object referenced from a local dictionary
    # then add it to the global_refs dict
    if ostack[-1].is_global and ostack[-1].is_composite and not d.is_global:
        ctxt.global_refs[ostack[-1].created] = ostack[-1].val

    ostack.pop()
    ostack.pop()


def ps_dict(ctxt, ostack):
    """
    int **dict** **dict**


    creates an empty dictionary with an initial capacity of int elements and pushes the
    created dictionary object on the operand stack. int is expected to be a nonnegative
    integer. The dictionary is allocated in local or global VM according to the VM allocation
    mode (see Section 3.7.2, "Local and Global VM").

    In LanguageLevel 1, the resulting dictionary has a maximum capacity of int elements.
    Attempting to exceed that limit causes a **dictfull** error.

    In LanguageLevels 2 and 3, the int operand specifies only the initial capacity; the
    dictionary can grow beyond that capacity if necessary. The **dict** operator immediately
    consumes sufficient VM to hold int entries. If more than that number of entries
    are subsequently stored in the dictionary, additional VM is consumed at that
    time.

    There is a cost associated with expanding a dictionary beyond its initial allocation.
    For efficiency reasons, a dictionary is expanded in chunks rather than one element
    at a time, so it may contain a substantial amount of unused space. If a program
    knows how large a dictionary it needs, it should create one of that size
    initially. On the other hand, if a program cannot predict how large the dictionary
    will eventually grow, it should choose a small initial allocation sufficient for its
    immediate needs. The built-in writeable dictionaries (for example, **userdict**) follow
    the latter convention.

    **Errors**:     **limitcheck**, **stackunderflow**, **typecheck**, **VMerror**
    **See Also**:   **begin**, **end**, **length**, **maxlength**
    """
    op = "dict"

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, op)

    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE != ps.T_INT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, op)
    
    ostack[-1] = ps.Dict(
        ctxt.id, max_length=ostack[-1].val, is_global=ctxt.vm_alloc_mode
    )


def dictstack(ctxt, ostack):
    """
    array **dictstack** subarray


    stores all elements of the dictionary stack into array and returns an object describing
    the initial n-element subarray of array, where n is the current depth of the dictionary
    stack. **dictstack** copies the topmost dictionary into element n - 1 of array
    and the bottommost one into element 0. The dictionary stack itself is unchanged.
    If the length of array is less than the depth of the dictionary stack, a **rangecheck**
    error occurs.

    **Errors**:     **invalidaccess**, **rangecheck**, **stackunderflow**, **typecheck**
    **See Also**:   **countdictstack**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, dictstack.__name__)
    
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE != ps.T_ARRAY:
        return ps_error.e(ctxt, ps_error.TYPECHECK, dictstack.__name__)
    
    # 4. RANGECHECK - Check array length
    if ostack[-1].length < len(ctxt.d_stack):
        return ps_error.e(ctxt, ps_error.RANGECHECK, dictstack.__name__)

    sub_array = copy.copy(ostack[-1])
    sub_array.length = len(ctxt.d_stack)
    index = len(ctxt.d_stack) - 1
    for i in range(-1, -len(ctxt.d_stack) - 1, -1):
        success, e = sub_array.put(ps.Int(index), ctxt.d_stack[i])
        if not success:
            return ps_error.e(ctxt, e, dictstack.__name__)
        index -= 1

    ostack[-1] = sub_array


def end(ctxt, ostack):
    """
    – **end** –


    pops the current dictionary off the dictionary stack, making the dictionary below
    it the current dictionary. If **end** tries to pop the bottommost instance of **userdict**,
    a **dictstackunderflow** error occurs.

    **Errors**:     **dictstackunderflow**
    **See Also**:   **begin**, **dictstack**, **countdictstack**
    """

    if len(ctxt.d_stack) == 3:
        return ps_error.e(ctxt, ps_error.DICTSTACKUNDERFLOW, end.__name__)

    ctxt.d_stack.pop()


def dictname(ctxt, ostack):
    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, dictname.__name__)
    
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE != ps.T_DICT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, dictname.__name__)

    ostack[-1] = ps.Name(ostack[-1].name, is_global=ctxt.vm_alloc_mode)


def known(ctxt, ostack):
    """
    dict key **known** bool


    returns true if there is an entry in the dictionary dict whose key is key;
    otherwise, it returns false. dict does not have to be on the dictionary stack.

    **Examples**
        /mydict 5 dict def
        mydict /total 0 put
        mydict /total **known**     -> true
        mydict /badname **known**   -> false

    **Errors**:     **invalidaccess**, **stackunderflow**, **typecheck**
    **See Also**:   **where**, **load**, **get**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, known.__name__)
    
    # 2. TYPECHECK - Check dictionary type
    if ostack[-2].TYPE != ps.T_DICT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, known.__name__)
    
    # 3. INVALIDACCESS - Check dictionary access (default READ_ONLY)
    if ostack[-2].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, known.__name__)

    key = ostack[-2].create_key(ostack.pop())

    if key in ostack[-1].val:
        ostack[-1] = ps.Bool(True)
    else:
        ostack[-1] = ps.Bool(False)


def load(ctxt, ostack):
    """
    key **load** value


    searches for key in each dictionary on the dictionary stack, starting with the topmost
    (current) dictionary. If key is found in some dictionary, **load** pushes the associated
    value on the operand stack; otherwise, an **undefined** error occurs.

    **load** looks up key the same way the interpreter looks up executable names that it
    encounters during execution. However, **load** always pushes the associated value
    on the operand stack; it never executes the value.

    **Examples**
        /avg {add 2 div} def
        /avg **load**               -> {add 2 div}

    **Errors**:     **invalidaccess**, **stackunderflow**, **typecheck**, **undefined**
    **See Also**:   **where**, **get**, **store**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, load.__name__)

    val = lookup(ctxt, ctxt.o_stack[-1])
    if val is None:
        return ps_error.e(ctxt, ps_error.UNDEFINED, load.__name__)

    ctxt.o_stack[-1] = val


def maxlength(ctxt, ostack):
    """
    dict **maxlength** int


    returns the capacity of the dictionary dict — in other words, the maximum number
    of entries that dict can hold using the virtual memory currently allocated to it. In
    LanguageLevel 1, **maxlength** returns the length operand of the dict operator that
    created the dictionary; this is the dictionary’s maximum capacity (exceeding it
    causes a **dictfull** error). In a LanguageLevels 2 and 3, which permit a dictionary to
    grow beyond its initial capacity, **maxlength** returns its current capacity, a number
    at least as large as that returned by the length operator.

    **Examples**
        /mydict 5 dict def
        mydict length       -> 0
        mydict **maxlength**    -> 0

    **Errors**:     **invalidaccess**, **stackunderflow**, **typecheck**
    **See Also**:   **length**, **dict**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, maxlength.__name__)
    
    # 2. TYPECHECK - Check operand type
    if ostack[-1].TYPE != ps.T_DICT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, maxlength.__name__)
    
    # 3. INVALIDACCESS - Check access permission
    if ostack[-1].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, maxlength.__name__)

    ostack[-1] = ostack[-1].maxlength()


def store(ctxt, ostack):
    """
    key value **store** –


    searches for key in each dictionary on the dictionary stack, starting with the topmost
    (current) dictionary. If key is found in some dictionary, **store** replaces its
    value by the value operand; otherwise, **store** creates a new entry with key and value
    in the current dictionary.

    If the chosen dictionary is in global VM and value is a composite object whose
    value is in local VM, an **invalidaccess** error occurs (see Section 3.7.2, "Local and
    Global VM").

    **Example**
        /abc 123 **store**

        /abc where
                { }
                {**currentdict**}
            **ifelse**
        /abc 123 put

    The two code fragments above have the same effect.

    **Errors**:     **dictfull**, **invalidaccess**, **limitcheck**, **stackunderflow**
    **See Also**:   **def**, **put**, **where**, **load**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, store.__name__)

    d_stack = ctxt.d_stack
    key = d_stack[-1].create_key(ostack[-2])

    def_to = d_stack[-1]
    for i in range(len(d_stack) - 1, -1, -1):
        if d_stack[i].access() < ps.ACCESS_READ_ONLY:
            continue

        if key in d_stack[i].val:
            def_to = d_stack[i]
            break

    if def_to.is_global and ostack[-1].is_composite and not ostack[-1].is_global:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, store.__name__)

    def_to.put(ostack[-2], ostack[-1])
    ostack.pop()
    ostack.pop()


def undef(ctxt, ostack):
    """
    dict key **undef** –


    removes key and its associated value from the dictionary dict. dict does not need to
    be on the dictionary stack. No error occurs if key is not present in dict.

    If the value of dict is in local VM, the effect of **undef** can be undone by a subsequent
    **restore** operation. That is, if key was present in dict at the time of the matching
    **save** operation, **restore** will reinstate key and its former value. But if dict is in
    global VM, the effect of **undef** is permanent.

    **Errors**:     **invalidaccess**, **stackunderflow**, **typecheck**
    **See Also**:   **def**, **put**, **undefinefont**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, undef.__name__)
    
    # 2. TYPECHECK - Check dictionary type
    if ostack[-2].TYPE != ps.T_DICT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, undef.__name__)
    
    # 3. INVALIDACCESS - Check dictionary access (WRITE_ONLY required)
    if ostack[-2].access() < ps.ACCESS_WRITE_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, undef.__name__)

    key = ostack[-2].create_key(ostack[-1])
    if key in ostack[-2].val:
        if not ostack[-2].is_global and hasattr(ostack[-2], '_cow_check'):
            ostack[-2]._cow_check()
        del ostack[-2].val[key]

    ostack.pop()
    ostack.pop()


def systemundef(ctxt, ostack):
    """
    dict key .**undef** –


    removes key and its associated value from the dictionary dict. dict does not need to
    be on the dictionary stack. No error occurs if key is not present in dict.

    If the value of dict is in local VM, the effect of **undef** can be undone by a subsequent
    **restore** operation. That is, if key was present in dict at the time of the matching
    **save** operation, **restore** will reinstate key and its former value. But if dict is in
    global VM, the effect of **undef** is permanent.

    Note: this is used in places for undefining fonts from **FontDirectory** or
          **GlobalFontDirectory** that normally to not have unlimited access levels

    **Errors**:     **invalidaccess**, **stackunderflow**, **typecheck**
    **See Also**:   **def**, **put**, **undefinefont**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 2:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, ".undef")
    
    # 2. TYPECHECK - Check dictionary type
    if ostack[-2].TYPE != ps.T_DICT:
        return ps_error.e(ctxt, ps_error.TYPECHECK, ".undef")
    
    # 3. INVALIDACCESS - Check dictionary access (READ_ONLY sufficient for systemundef)
    if ostack[-2].access() < ps.ACCESS_READ_ONLY:
        return ps_error.e(ctxt, ps_error.INVALIDACCESS, ".undef")

    key = ostack[-2].create_key(ostack[-1])
    if key in ostack[-2].val:
        del ostack[-2].val[key]

    ostack.pop()
    ostack.pop()


def where(ctxt, ostack):
    """
    key **where** dict true     (if found)
                   false    (if not found)


    determines which dictionary on the dictionary stack, if any, contains an entry
    whose key is key. **where** searches for key in each dictionary on the dictionary stack,
    starting with the topmost (current) dictionary. If key is found in some dictionary,
    **where** returns that dictionary object and the boolean value true; otherwise, **where**
    simply returns false.

    **Errors**:     **invalidaccess**, **stackoverflow**, **stackunderflow**, **typecheck**
    **See Also**:   **known**, **load**, **get**
    """

    # 1. STACKUNDERFLOW - Check stack depth
    if len(ostack) < 1:
        return ps_error.e(ctxt, ps_error.STACKUNDERFLOW, where.__name__)

    key = ctxt.d_stack[-1].create_key(ostack[-1])

    d_stack = ctxt.d_stack
    for i in range(len(d_stack) - 1, -1, -1):
        if d_stack[i].access() == ps.ACCESS_NONE:
            return ps_error.e(ctxt, ps_error.INVALIDACCESS, where.__name__)

        if d_stack[i].access() < ps.ACCESS_READ_ONLY:
            continue

        if key in d_stack[i].val:
            ostack[-1] = d_stack[i]
            ostack.append(ps.Bool(True))
            return

    # not found in any of the dictionaries
    ostack[-1] = ps.Bool(False)
