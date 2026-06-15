# RDC2UE RenderDoc Exporter
# Minimal export contract for UE4.27 reconstruction.

import os
import json
import copy
import hashlib
import importlib
import re
import shutil
import struct
import subprocess
import traceback
from collections import OrderedDict

import renderdoc as rd

# ============================================================
# 全局配置
# ============================================================

# 当前需要导出的 draw 范围
RANGE_START_EID = 10878
RANGE_END_EID = 10929

# 工程目录和输出目录都跟随当前扩展，不再写绝对路径。
EXTENSION_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUTPUT_DIR = os.path.join(EXTENSION_DIR, "ExportResults")

# converter 与数据纹理共同使用的固定宽度。
CONVERTER_DATA_WIDTH = 8192

# HLSLDecompiler 与扩展放在同一目录，不配置机器相关绝对路径。
HLSL_DECOMPILER_EXE = os.path.join(
    EXTENSION_DIR,
    "cmd_Decompiler.exe"
)

# 每次 range 导出开始时重新加载一次 converter。
# 这样修改 converter 文件后，不会继续使用 RenderDoc 进程中缓存的旧模块。
_HLSL_CONVERTER = None

FLIP_WINDING = False

# 当前游戏内稳定的 semantic 含义。
# PostVS 的真实 byteOffset 不再写死，而是由每个 draw 的 VS output signature 计算。
MESH_SEMANTICS = {
    "position": "SV_POSITION",
    "tangent": "TEXCOORD10",
    "normal": "TEXCOORD11",
    "uv0": "TEXCOORD0",
}

# 手动记录 ViewProjection 矩阵
# pc cb1[8-11]
VIEW_PROJ = [
    [0.98146,   0.00537,   0.0,        0.19162],
    [0.19164,  -0.02735,   0.0,       -0.98135],
    [-0.00001,  1.77765,   0.0,       -0.01555],
    [0.0,       0.0,       1.0,        0.0],
]

# mobile
#VIEW_PROJ = [
#    [0.01399,  -0.27182,   0.0,      -0.04767],
#    [0.08591,   0.04426,   0.0,      -0.29278],
#    [0.57622,   0.0,       0.0,       0.04481],
#    [0.0,       0.0,       1.0,       0.0],
#]

# ============================================================
# 日志
# ============================================================

def log(message):
    print("[RDC2UE] {}".format(message))

def warn(message):
    print("[RDC2UE][warn] {}".format(message))

def error(message):
    print("[RDC2UE][error] {}".format(message))

def print_exception(prefix="Exception"):
    error(prefix)
    print(traceback.format_exc())

# ============================================================
# 路径管理
# ============================================================

def create_export_paths(output_dir, start_eid, end_eid):
    root = os.path.join(
        os.path.abspath(os.path.normpath(output_dir)),
        "range_{}_{}".format(start_eid, end_eid)
    )
    return {
        "range": root,
        "scene": os.path.join(root, "scene.json"),
        "meshes": os.path.join(root, "meshes"),
        "materials": os.path.join(root, "materials"),
        "textures": os.path.join(root, "textures"),
        "shaders": os.path.join(root, "shaders"),
        "data": os.path.join(root, "data"),
    }

def ensure_export_dirs(paths):
    for name in ("range", "meshes", "materials", "textures", "shaders", "data"):
        os.makedirs(paths[name], exist_ok=True)

def make_rel_path(path, base_dir):
    """生成 JSON 使用的相对路径，并统一使用正斜杠。"""
    path = os.path.abspath(os.path.normpath(os.fspath(path)))
    base_dir = os.path.abspath(os.path.normpath(os.fspath(base_dir)))

    try:
        rel_path = os.path.relpath(path, base_dir)
    except ValueError as e:
        raise ValueError(
            "无法生成相对路径：path='{}', base='{}', 原因：{}".format(
                path,
                base_dir,
                e
            )
        )

    return rel_path.replace("\\", "/")

def make_mesh_json_path(paths, event_id):
    return os.path.join(
        paths["meshes"],
        "mesh_eid_{}.json".format(event_id)
    )

def make_mesh_bin_path(paths, event_id):
    return os.path.join(
        paths["meshes"],
        "mesh_eid_{}.bin".format(event_id)
    )

def make_material_json_path(paths, event_id):
    return os.path.join(
        paths["materials"],
        "mat_eid_{}.json".format(event_id)
    )

def make_texture_filename(texture_id, mip=0, slice_index=0):
    texture_id_text = str(texture_id)
    texture_id_text = texture_id_text.replace("ResourceId::", "")

    return "T_{}_m{}_s{}_rdc.png".format(
        texture_id_text,
        mip,
        slice_index
    )

def make_texture_path(paths, texture_id, mip=0, slice_index=0):
    return os.path.join(
        paths["textures"],
        make_texture_filename(texture_id, mip, slice_index)
    )

def make_shared_shader_dir(paths, shader_hash):
    """按 Pixel Shader 字节码 SHA-256 创建共享目录。"""
    return os.path.join(
        paths["shaders"],
        "ps_{}".format(shader_hash)
    )

def make_data_dir(paths, event_id):
    return os.path.join(
        paths["data"],
        "eid_{}".format(event_id)
    )

def make_cbdata_path(data_dir):
    return os.path.join(data_dir, "cbdata.bin")

def make_buffer_data_path(data_dir, slot):
    return os.path.join(
        data_dir,
        "t{}data.bin".format(slot)
    )

def ensure_path_inside(path, root_dir, label):
    """确保 converter 产物没有写到当前 Shader 目录之外。"""
    path = os.path.abspath(os.path.normpath(os.fspath(path)))
    root_dir = os.path.abspath(os.path.normpath(os.fspath(root_dir)))

    try:
        common = os.path.commonpath([path, root_dir])
    except ValueError as e:
        raise ValueError(
            "{} 不在当前 Shader 磁盘：path='{}', root='{}', 原因：{}".format(
                label,
                path,
                root_dir,
                e
            )
        )

    if os.path.normcase(common) != os.path.normcase(root_dir):
        raise ValueError(
            "{} 脱离当前 Shader 目录：path='{}', root='{}'".format(
                label,
                path,
                root_dir
            )
        )

    return path

def make_shader_bytecode_path(shader_dir):
    return os.path.join(shader_dir, "ps_bytecode.dxbc")

def make_decompiled_hlsl_path(shader_dir):
    return os.path.join(shader_dir, "ps_decompiled.hlsl")

# ============================================================
# Drawcall 查找
# ============================================================

def get_draw_actions(controller, start_eid, end_eid=None):
    """
    仅寻找有 index 和 instance 的 drawcall
    """
    if end_eid is None:
        end_eid = start_eid
    
    result = []

    def visit(actions):
        for action in actions:
            if start_eid <= action.eventId <= end_eid:
                if (
                    action.numIndices > 0 and
                    action.numInstances > 0
                ):
                    result.append(action)
                
            visit(action.children)
        
    visit(controller.GetRootActions())

    result.sort(key=lambda x: x.eventId)
    return result

# ============================================================
# 数学工具
# ============================================================

def inverse_mat4(m):
    a = [[float(m[r][c]) for c in range(4)] for r in range(4)]
    inv = [[1.0 if r == c else 0.0 for c in range(4)] for r in range(4)]

    for col in range(4):
        pivot = col

        for row in range(col + 1, 4):
            if abs(a[row][col]) > abs(a[pivot][col]):
                pivot = row

        if pivot != col:
            a[col], a[pivot] = a[pivot], a[col]
            inv[col], inv[pivot] = inv[pivot], inv[col]

        pivot_value = a[col][col]

        for j in range(4):
            a[col][j] /= pivot_value
            inv[col][j] /= pivot_value

        for row in range(4):
            if row == col:
                continue

            factor = a[row][col]

            for j in range(4):
                a[row][j] -= factor * a[col][j]
                inv[row][j] -= factor * inv[col][j]

    return inv

def transpose_mat4(m):
    return [
        [m[0][0], m[1][0], m[2][0], m[3][0]],
        [m[0][1], m[1][1], m[2][1], m[3][1]],
        [m[0][2], m[1][2], m[2][2], m[3][2]],
        [m[0][3], m[1][3], m[2][3], m[3][3]],
    ]

VIEW_PROJ_FOR_PYTHON = transpose_mat4(VIEW_PROJ)
INV_VIEW_PROJ = inverse_mat4(VIEW_PROJ_FOR_PYTHON)

def mul_mat4_vec4(m, v):
    x, y, z, w = v

    return (
        m[0][0] * x + m[0][1] * y + m[0][2] * z + m[0][3] * w,
        m[1][0] * x + m[1][1] * y + m[1][2] * z + m[1][3] * w,
        m[2][0] * x + m[2][1] * y + m[2][2] * z + m[2][3] * w,
        m[3][0] * x + m[3][1] * y + m[3][2] * z + m[3][3] * w,
    )

def clip_to_world(clip_pos):
    world_h = mul_mat4_vec4(INV_VIEW_PROJ, clip_pos)

    if world_h[3] == 0.0:
        return world_h[:3]

    inv_w = 1.0 / world_h[3]
    return (world_h[0] * inv_w, world_h[1] * inv_w, world_h[2] * inv_w)

# ============================================================
# 动态 VS Output 读取
# ============================================================

SCALAR_PACK_CODES = {
    "float": "f",
    "int": "i",
    "uint": "I",
}

def normalize_semantic(value):
    return str(value or "").strip().upper()

def get_semantic_name(parameter):
    semantic = normalize_semantic(
        getattr(parameter, "semanticIdxName", "")
    )

    if semantic:
        return semantic

    name = normalize_semantic(
        getattr(parameter, "semanticName", "")
    )
    index = int(getattr(parameter, "semanticIndex", 0))

    if not name:
        return ""

    if bool(getattr(parameter, "needSemanticIndex", False)) or index != 0:
        return "{}{}".format(name, index)

    return name

def get_scalar_type(var_type):
    text = str(var_type).lower()

    if "uint" in text or "bool" in text:
        return "uint"

    if "sint" in text or text.endswith(".int") or text == "int":
        return "int"

    return "float"

def normalize_storage_scalar_type(value):
    text = str(value or "float").lower()

    if "uint" in text or "bool" in text:
        return "uint"

    if text == "int" or "sint" in text:
        return "int"

    # half / double 在当前 PostVS 中仍按 32-bit 分量保存。
    return "float"

def get_component_channels(mask, component_count):
    channels = [
        index
        for index in range(4)
        if int(mask) & (1 << index)
    ]

    if not channels:
        channels = list(range(min(int(component_count), 4)))

    return channels[:int(component_count)]

def mask_to_text(mask):
    return "".join(
        "xyzw"[index]
        for index in range(4)
        if int(mask) & (1 << index)
    )

def signature_to_json(parameter):
    reg_index = int(getattr(parameter, "regIndex", 0xFFFFFFFF))
    if reg_index >= 0xFFFFFFFF:
        reg_index = None

    return {
        "registerIndex": reg_index,
        "semantic": get_semantic_name(parameter),
        "scalarType": get_scalar_type(getattr(parameter, "varType", "")),
        "components": int(getattr(parameter, "compCount", 0)),
        "registerMask": mask_to_text(getattr(parameter, "regChannelMask", 0)),
        "usedMask": mask_to_text(getattr(parameter, "channelUsedMask", 0)),
        "systemValue": str(getattr(parameter, "systemValue", "")),
    }

def collect_pixel_input_signature(controller):
    pipe = controller.GetPipelineState()
    reflection = pipe.GetShaderReflection(rd.ShaderStage.Pixel)

    if reflection is None:
        return []

    result = [
        signature_to_json(parameter)
        for parameter in reflection.inputSignature
    ]

    result.sort(key=lambda item: (
        item["registerIndex"]
        if item["registerIndex"] is not None
        else 0xFFFFFFFF,
        item["semantic"]
    ))

    return result

def collect_vertex_output_layout(controller):
    """
    按 RenderDoc D3D12 PostVS 的 stream-output 布局计算 byteOffset。

    RenderDoc 会：
      1. 按 VS output signature 顺序紧密写入各 semantic；
      2. 每个分量固定占 4 bytes；
      3. 将 SV_Position 移到第一个输出；
      4. SV_Position 固定写 4 个分量。
    """
    pipe = controller.GetPipelineState()
    reflection = pipe.GetShaderReflection(rd.ShaderStage.Vertex)

    if reflection is None:
        raise RuntimeError("当前 draw 没有 Vertex Shader reflection")

    outputs = []

    for parameter in reflection.outputSignature:
        # 当前 VSOut 只使用 rasterized stream 0。
        if int(getattr(parameter, "stream", 0)) != 0:
            continue

        entry = signature_to_json(parameter)
        entry["byteOffset"] = 0
        outputs.append(entry)

    position_index = None

    for index, entry in enumerate(outputs):
        if entry["semantic"] == MESH_SEMANTICS["position"]:
            position_index = index
            break

        if "POSITION" in entry["systemValue"].upper():
            position_index = index
            break

    if position_index is None:
        raise RuntimeError("VS output signature 中未找到 SV_Position")

    if position_index != 0:
        position = outputs.pop(position_index)
        outputs.insert(0, position)

    byte_offset = 0

    for entry in outputs:
        component_count = int(entry["components"])

        if entry["semantic"] == MESH_SEMANTICS["position"]:
            component_count = 4
            entry["components"] = 4

        entry["byteOffset"] = byte_offset
        byte_offset += component_count * 4

    return outputs, byte_offset

def find_output_semantic(vertex_outputs, semantic):
    semantic = normalize_semantic(semantic)

    for entry in vertex_outputs:
        if entry["semantic"] == semantic:
            return entry

    return None

def parse_register_index(register_name):
    """Parse 3DMigoto input aliases such as v5 and w5 as register 5."""
    match = re.fullmatch(r"[vVwW](\d+)", str(register_name or ""))
    return int(match.group(1)) if match else None

def build_varying_layout(varying_inputs, pixel_inputs, vertex_outputs):
    """Map converter inputs to PostVS semantics, including vN/wN aliases."""
    by_register = {}
    for parameter in pixel_inputs:
        index = parameter["registerIndex"]
        if index is not None:
            by_register.setdefault(index, []).append(parameter)

    resolved = []
    missing = []
    warnings = []

    for varying in varying_inputs:
        custom_input = varying.get("name") or str(varying.get("register", "")).upper()
        register_index = parse_register_index(varying.get("register"))
        semantic = normalize_semantic(varying.get("semantic"))

        candidates = by_register.get(register_index, []) if register_index is not None else []

        # A single PS register may contain several semantics. 3DMigoto then
        # names them v5, w5, etc. Prefer the exact semantic instead of merging
        # every semantic occupying the same register.
        parameters = [
            item for item in candidates
            if semantic and item["semantic"] == semantic
        ]

        if not parameters and semantic:
            parameters = [
                item for item in pixel_inputs
                if item["semantic"] == semantic
            ]

        if not parameters and candidates:
            parameters = candidates

        if register_index is None and len(parameters) == 1:
            register_index = parameters[0]["registerIndex"]

        if register_index is None or not parameters:
            missing.append(custom_input)
            warnings.append(
                "{}({}) 未找到 PS input signature".format(
                    custom_input,
                    semantic or "unknown"
                )
            )
            continue

        component_count = max(1, min(int(varying.get("components", 4)), 4))
        sources = []
        destination_offset = 0

        for parameter in parameters:
            source = find_output_semantic(vertex_outputs, parameter["semantic"])
            if source is None:
                continue

            source_count = min(
                int(source.get("components", 0)),
                component_count - destination_offset
            )
            if source_count <= 0:
                break

            sources.append({
                "semantic": source["semantic"],
                "byteOffset": source["byteOffset"],
                "scalarType": source["scalarType"],
                "components": source["components"],
                "copyCount": source_count,
                "destinationOffset": destination_offset,
            })
            destination_offset += source_count

        if destination_offset < component_count:
            missing.append(custom_input)
            warnings.append(
                "{}({}) 未完整映射到 PostVS: {}/{} components".format(
                    custom_input,
                    semantic or "unknown",
                    destination_offset,
                    component_count
                )
            )
            continue

        resolved.append({
            "customInput": custom_input,
            "meshAttribute": custom_input,
            "scalarType": normalize_storage_scalar_type(varying.get("type")),
            "components": component_count,
            "sources": sources,
        })

    return resolved, missing, warnings

def read_packed_output(raw_bytes, vertex_index, vertex_stride, output_entry):
    scalar_type = output_entry.get("scalarType", "float")
    component_count = int(output_entry.get("components", 0))
    pack_code = SCALAR_PACK_CODES.get(scalar_type)

    if pack_code is None:
        raise ValueError("不支持的 VS Output scalarType: {}".format(scalar_type))

    offset = (
        vertex_index * vertex_stride +
        int(output_entry["byteOffset"])
    )
    byte_count = component_count * 4

    if offset < 0 or offset + byte_count > len(raw_bytes):
        raise ValueError(
            "VS Output 越界：vertex={} offset={} size={} buffer={}".format(
                vertex_index,
                offset,
                byte_count,
                len(raw_bytes)
            )
        )

    return struct.unpack_from(
        "<{}{}".format(component_count, pack_code),
        raw_bytes,
        offset
    )

def pad_values(values, component_count, defaults):
    result = list(values[:component_count])

    while len(result) < component_count:
        result.append(defaults[len(result)])

    return tuple(result)

def read_standard_vertex(raw_bytes, vertex_index, vertex_stride, standard_layout):
    position_values = read_packed_output(
        raw_bytes,
        vertex_index,
        vertex_stride,
        standard_layout["position"]
    )
    clip_position = pad_values(
        position_values,
        4,
        (0.0, 0.0, 0.0, 1.0)
    )
    position = clip_to_world(clip_position)

    tangent_entry = standard_layout.get("tangent")
    normal_entry = standard_layout.get("normal")
    uv_entry = standard_layout.get("uv0")

    if tangent_entry:
        tangent_values = pad_values(
            read_packed_output(
                raw_bytes,
                vertex_index,
                vertex_stride,
                tangent_entry
            ),
            4,
            (1.0, 0.0, 0.0, 1.0)
        )
    else:
        tangent_values = (1.0, 0.0, 0.0, 1.0)

    if normal_entry:
        normal_values = pad_values(
            read_packed_output(
                raw_bytes,
                vertex_index,
                vertex_stride,
                normal_entry
            ),
            3,
            (0.0, 0.0, 1.0)
        )
    else:
        normal_values = (0.0, 0.0, 1.0)

    if uv_entry:
        uv_values = pad_values(
            read_packed_output(
                raw_bytes,
                vertex_index,
                vertex_stride,
                uv_entry
            ),
            2,
            (0.0, 0.0)
        )
    else:
        uv_values = (0.0, 0.0)

    return (
        position,
        tangent_values[:3],
        tangent_values[3],
        normal_values[:3],
        uv_values[:2],
    )

def read_varying_register(raw_bytes, vertex_index, vertex_stride, varying):
    """Pack one or more PostVS semantics into the Custom input value."""
    values = [0, 0, 0, 0]

    for source in varying["sources"]:
        source_values = read_packed_output(
            raw_bytes,
            vertex_index,
            vertex_stride,
            source
        )
        destination_offset = int(source.get("destinationOffset", 0))
        copy_count = int(source.get("copyCount", len(source_values)))

        for source_index in range(min(copy_count, len(source_values))):
            destination_index = destination_offset + source_index
            if destination_index < len(values):
                values[destination_index] = source_values[source_index]

    return tuple(values[:varying["components"]])

def read_postvs_indices(controller, postvs, index_count):
    index_stride = postvs.indexByteStride

    if index_stride not in (2, 4):
        raise ValueError(
            "不支持的 PostVS index stride: {}".format(index_stride)
        )

    index_bytes = controller.GetBufferData(
        postvs.indexResourceId,
        postvs.indexByteOffset,
        index_count * index_stride
    )

    pack_code = "H" if index_stride == 2 else "I"
    indices = []

    for index in range(index_count):
        indices.append(struct.unpack_from(
            "<{}".format(pack_code),
            index_bytes,
            index * index_stride
        )[0])

    return indices

# ============================================================
# Mesh 数据组装
# ============================================================

def build_standard_layout(vertex_outputs):
    layout = {
        name: find_output_semantic(vertex_outputs, semantic)
        for name, semantic in MESH_SEMANTICS.items()
    }

    if layout["position"] is None:
        raise RuntimeError("动态 VS Output layout 中缺少 SV_Position")

    return layout

def create_empty_mesh_data(varying_layout):
    return {
        "positions": [],
        "tangents": [],
        "binormalSigns": [],
        "normals": [],
        "uvs": [],
        "varyings": OrderedDict(
            (varying["meshAttribute"], [])
            for varying in varying_layout
        ),
        "instances": [],
    }

def append_vertex_from_vsout(
    raw_bytes,
    vertex_stride,
    vertex_index,
    standard_layout,
    varying_layout,
    mesh_data
):
    position, tangent, binormal_sign, normal, uv = read_standard_vertex(
        raw_bytes,
        vertex_index,
        vertex_stride,
        standard_layout
    )

    mesh_data["positions"].append(position)
    mesh_data["tangents"].append(tangent)
    mesh_data["binormalSigns"].append((binormal_sign,))
    mesh_data["normals"].append(normal)
    mesh_data["uvs"].append(uv)

    for varying in varying_layout:
        mesh_data["varyings"][varying["meshAttribute"]].append(
            read_varying_register(
                raw_bytes,
                vertex_index,
                vertex_stride,
                varying
            )
        )

def append_triangle(
    raw_bytes,
    vertex_stride,
    indices,
    tri_start,
    order,
    standard_layout,
    varying_layout,
    mesh_data
):
    for local_index in order:
        vertex_index = indices[tri_start + local_index]

        append_vertex_from_vsout(
            raw_bytes,
            vertex_stride,
            vertex_index,
            standard_layout,
            varying_layout,
            mesh_data
        )

def append_instance(
    controller,
    instance_id,
    index_count,
    expected_vertex_stride,
    standard_layout,
    varying_layout,
    mesh_data
):
    vertex_offset = len(mesh_data["positions"])

    postvs = controller.GetPostVSData(
        instance_id,
        0,
        rd.MeshDataStage.VSOut
    )

    vertex_stride = int(postvs.vertexByteStride)

    if vertex_stride != expected_vertex_stride:
        raise ValueError(
            "PostVS stride 不匹配：RenderDoc={}，signature计算={}".format(
                vertex_stride,
                expected_vertex_stride
            )
        )

    raw_bytes = controller.GetBufferData(
        postvs.vertexResourceId,
        postvs.vertexByteOffset,
        0
    )
    indices = read_postvs_indices(controller, postvs, index_count)

    order = (0, 2, 1) if FLIP_WINDING else (0, 1, 2)
    triangle_index_count = (index_count // 3) * 3

    for tri_start in range(0, triangle_index_count, 3):
        append_triangle(
            raw_bytes,
            vertex_stride,
            indices,
            tri_start,
            order,
            standard_layout,
            varying_layout,
            mesh_data
        )

    vertex_count = len(mesh_data["positions"]) - vertex_offset

    mesh_data["instances"].append(OrderedDict([
        ("vertexOffset", vertex_offset),
        ("vertexCount", vertex_count),
    ]))

def build_mesh_attributes(mesh_data, varying_layout):
    attributes = OrderedDict([
        ("POSITION", {
            "data": mesh_data["positions"],
            "componentCount": 3,
            "scalarType": "float",
        }),
        ("TANGENT", {
            "data": mesh_data["tangents"],
            "componentCount": 3,
            "scalarType": "float",
        }),
        ("BINORMAL_SIGN", {
            "data": mesh_data["binormalSigns"],
            "componentCount": 1,
            "scalarType": "float",
        }),
        ("NORMAL", {
            "data": mesh_data["normals"],
            "componentCount": 3,
            "scalarType": "float",
        }),
        ("TEXCOORD_0", {
            "data": mesh_data["uvs"],
            "componentCount": 2,
            "scalarType": "float",
        }),
    ])

    for varying in varying_layout:
        attribute_name = varying["meshAttribute"]

        attributes[attribute_name] = {
            "data": mesh_data["varyings"][attribute_name],
            "componentCount": varying["components"],
            "scalarType": varying["scalarType"],
        }

    return attributes

# ============================================================
# Mesh 文件写入
# ============================================================

def write_mesh_bin(bin_path, attributes):
    vertex_count = len(next(iter(attributes.values()))["data"])
    json_attributes = {}
    byte_offset = 0

    with open(bin_path, "wb") as file:
        for name, info in attributes.items():
            data = info["data"]
            components = int(info["componentCount"])
            scalar_type = info.get("scalarType", "float")
            pack_code = SCALAR_PACK_CODES.get(scalar_type)

            if pack_code is None:
                raise ValueError("不支持的 Mesh scalarType: {}".format(scalar_type))
            if len(data) != vertex_count:
                raise ValueError("Mesh attribute 顶点数量不一致: {}".format(name))

            pack_format = "<{}{}".format(components, pack_code)
            for value in data:
                file.write(struct.pack(pack_format, *value))

            json_attributes[name] = {
                "components": components,
                "type": scalar_type,
                "offset": byte_offset,
            }
            byte_offset += vertex_count * components * 4

    return json_attributes, vertex_count

def write_mesh_json(json_path, bin_path, instances, attributes, vertex_count):
    payload = {
        "buffer": os.path.basename(bin_path),
        "vertices": vertex_count,
        "attributes": attributes,
        "instances": [
            [item["vertexOffset"], item["vertexCount"]]
            for item in instances
        ],
    }
    with open(json_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, ensure_ascii=False)

def write_mesh_files(json_path, bin_path, instances, attributes):
    json_attributes, vertex_count = write_mesh_bin(bin_path, attributes)
    write_mesh_json(json_path, bin_path, instances, json_attributes, vertex_count)
    return bin_path, json_path

# ============================================================
# Constant Buffer 数据导出
# ============================================================

def is_null_resource_id(resource_id):
    """RenderDoc ResourceId 的空值在不同绑定中可能有不同字符串表现。"""
    return str(resource_id) in {
        "",
        "0",
        "ResourceId::0",
        "ResourceId()",
    }

def get_int_field(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default

def find_pixel_constant_block(reflection, slot):
    """将 HLSL bN 映射到 ShaderReflection.constantBlocks 的索引。"""
    matches = []

    for block_index, block in enumerate(reflection.constantBlocks):
        bind_slot = get_int_field(block.fixedBindNumber, -1)
        bind_space = get_int_field(block.fixedBindSetOrSpace, 0)

        if bind_slot == slot and bind_space == 0:
            matches.append((block_index, block))

    if not matches:
        raise RuntimeError(
            "Pixel Shader reflection 中找不到 b{}".format(slot)
        )

    if len(matches) > 1:
        raise RuntimeError(
            "Pixel Shader reflection 中 b{} 匹配到多个 Constant Block".format(
                slot
            )
        )

    return matches[0]

def choose_constant_buffer_read_size(block, descriptor, row_byte_length):
    """使用 Shader reflection 声明大小，避免导出底层大 Buffer 的无关范围。"""
    bound_size = get_int_field(descriptor.byteSize, 0)
    declared_size = get_int_field(block.byteSize, 0)

    if declared_size > 0:
        if declared_size > row_byte_length:
            raise RuntimeError(
                "Constant Buffer 大小超出 CBData 单行容量："
                "declaredSize={}, rowCapacity={}".format(
                    declared_size,
                    row_byte_length
                )
            )

        if 0 < bound_size < declared_size:
            raise RuntimeError(
                "Constant Buffer 绑定范围小于 Shader 声明大小："
                "boundSize={}, declaredSize={}".format(
                    bound_size,
                    declared_size
                )
            )

        return declared_size

    if 0 < bound_size <= row_byte_length:
        return bound_size

    raise RuntimeError(
        "无法确定 Constant Buffer 导出大小："
        "boundSize={}, declaredSize={}, rowCapacity={}".format(
            bound_size,
            declared_size,
            row_byte_length
        )
    )

def make_constant_buffer_result(required_slots):
    slots = sorted({int(slot) for slot in required_slots})
    return {
        "enabled": bool(slots),
        "complete": True,
        "requiredSlots": slots,
        "filePath": None,
        "missingSlots": [],
        "sizes": [],
        "warnings": [],
    }

def export_constant_buffers(controller, event_id, paths, required_slots):
    """Pack each required bN into row N of one RGBA8 byte texture."""
    result = make_constant_buffer_result(required_slots)
    if not result["enabled"]:
        return result

    controller.SetFrameEvent(event_id, True)
    pipe = controller.GetPipelineState()
    reflection = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
    if reflection is None:
        result["complete"] = False
        result["missingSlots"] = list(result["requiredSlots"])
        return result

    data_dir = make_data_dir(paths, event_id)
    os.makedirs(data_dir, exist_ok=True)
    row_size = CONVERTER_DATA_WIDTH * 4
    height = max(result["requiredSlots"]) + 1
    output = bytearray(row_size * height)

    for slot in result["requiredSlots"]:
        try:
            block_index, block = find_pixel_constant_block(reflection, slot)
            descriptor = pipe.GetConstantBlock(
                rd.ShaderStage.Pixel, block_index, 0
            ).descriptor
            if is_null_resource_id(descriptor.resource):
                raise RuntimeError("b{} 未绑定".format(slot))

            size = choose_constant_buffer_read_size(block, descriptor, row_size)
            raw = bytes(controller.GetBufferData(
                descriptor.resource,
                get_int_field(descriptor.byteOffset, 0),
                size
            ))
            if len(raw) < size:
                raise RuntimeError("b{} 数据不足".format(slot))

            start = slot * row_size
            output[start:start + size] = raw[:size]
            result["sizes"].append([slot, size])

        except Exception as exception:
            result["complete"] = False
            result["missingSlots"].append(slot)
            result["warnings"].append("b{}: {}".format(slot, exception))

    result["filePath"] = make_cbdata_path(data_dir)
    write_binary_file(result["filePath"], output)

    log("CBData: {} width={} missing={}".format(
        ", ".join("b{}={}B".format(slot, size) for slot, size in result["sizes"]) or "无",
        CONVERTER_DATA_WIDTH,
        len(result["missingSlots"])
    ))
    for message in result["warnings"]:
        warn(message)
    return result

# ============================================================
# Buffer SRV 数据导出
# ============================================================

def align_up(value, alignment):
    return ((value + alignment - 1) // alignment) * alignment

def find_pixel_readonly_buffer(reflection, slot):
    """将 HLSL tN 映射到 ShaderReflection.readOnlyResources。"""
    matches = []

    for resource_index, resource in enumerate(
        reflection.readOnlyResources
    ):
        bind_slot = get_int_field(resource.fixedBindNumber, -1)
        bind_space = get_int_field(
            resource.fixedBindSetOrSpace,
            0
        )
        is_texture = bool(resource.isTexture)

        if bind_slot == slot and bind_space == 0 and not is_texture:
            matches.append((resource_index, resource))

    if not matches:
        raise RuntimeError(
            "Pixel Shader reflection 中找不到 Buffer t{}".format(slot)
        )

    if len(matches) > 1:
        raise RuntimeError(
            "Pixel Shader reflection 中 t{} 匹配到多个 Buffer".format(
                slot
            )
        )

    return matches[0]

def find_used_readonly_descriptor(pipe, resource_index):
    """根据 reflection index 找到当前 draw 的真实 SRV descriptor。"""
    matches = []

    for used_descriptor in pipe.GetReadOnlyResources(
        rd.ShaderStage.Pixel
    ):
        access = used_descriptor.access

        if (
            get_int_field(access.index, -1) == resource_index and
            get_int_field(access.arrayElement, 0) == 0
        ):
            matches.append(used_descriptor)

    if not matches:
        raise RuntimeError(
            "当前 draw 找不到 readOnlyResources[{}] 的 descriptor".format(
                resource_index
            )
        )

    if len(matches) > 1:
        raise RuntimeError(
            "readOnlyResources[{}] 匹配到多个 descriptor".format(
                resource_index
            )
        )

    return matches[0]

def collect_buffer_lengths(controller):
    return {
        str(buffer.resourceId): get_int_field(buffer.length, 0)
        for buffer in controller.GetBuffers()
    }

def choose_buffer_view_size(descriptor, resource_length):
    bound_offset = get_int_field(descriptor.byteOffset, 0)
    bound_size = get_int_field(descriptor.byteSize, 0)

    if bound_offset < 0 or bound_offset > resource_length:
        raise RuntimeError(
            "Buffer view offset 超出资源范围：offset={}, length={}".format(
                bound_offset,
                resource_length
            )
        )

    available_size = resource_length - bound_offset

    if bound_size <= 0:
        return available_size, None

    if bound_size > available_size:
        warning_message = (
            "descriptor.byteSize={} 超出底层 Buffer 剩余范围 {}，"
            "按 {} bytes 导出".format(
                bound_size,
                available_size,
                available_size
            )
        )
        return available_size, warning_message

    return bound_size, None

def resolve_buffer_stride(input_info, descriptor):
    buffer_type = str(input_info.get("bufferType") or "Buffer")
    layout_stride = get_int_field(input_info.get("stride"), 0)
    descriptor_stride = get_int_field(
        descriptor.elementByteSize,
        0
    )

    if buffer_type == "StructuredBuffer":
        stride = layout_stride or descriptor_stride
        if stride <= 0:
            raise RuntimeError("StructuredBuffer stride 未知")
        return stride

    if buffer_type == "ByteAddressBuffer":
        return None

    stride = descriptor_stride or layout_stride
    if stride <= 0:
        raise RuntimeError("typed Buffer elementByteSize 未知")

    return stride

def pack_buffer_data(raw_bytes, width):
    bytes_per_pixel = 4
    source_byte_length = len(raw_bytes)
    packed_source_length = align_up(source_byte_length, bytes_per_pixel)
    row_byte_length = width * bytes_per_pixel
    height = max(
        1,
        (packed_source_length + row_byte_length - 1) // row_byte_length
    )
    byte_length = width * height * bytes_per_pixel

    packed_bytes = bytearray(byte_length)
    packed_bytes[:source_byte_length] = raw_bytes

    return packed_bytes, height, byte_length

def make_buffer_result(required_inputs):
    inputs = {int(item["slot"]): dict(item) for item in required_inputs}
    return {
        "enabled": bool(inputs),
        "complete": True,
        "requiredInputs": list(inputs.values()),
        "buffers": [],
        "missingSlots": [],
        "warnings": [],
    }

def make_buffer_metadata(input_info, file_path):
    return {
        "slot": get_int_field(input_info.get("slot"), -1),
        "filePath": file_path,
    }

def export_buffer_resources(controller, event_id, paths, required_inputs):
    """Export the exact SRV view required by each TNData input."""
    result = make_buffer_result(required_inputs)
    if not result["enabled"]:
        return result

    controller.SetFrameEvent(event_id, True)
    pipe = controller.GetPipelineState()
    reflection = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
    if reflection is None:
        result["complete"] = False
        result["missingSlots"] = [item["slot"] for item in result["requiredInputs"]]
        return result

    data_dir = make_data_dir(paths, event_id)
    os.makedirs(data_dir, exist_ok=True)
    lengths = collect_buffer_lengths(controller)

    for input_info in result["requiredInputs"]:
        slot = get_int_field(input_info.get("slot"), -1)
        try:
            resource_index, _resource = find_pixel_readonly_buffer(reflection, slot)
            descriptor = find_used_readonly_descriptor(pipe, resource_index).descriptor
            resource_length = lengths.get(str(descriptor.resource))
            if resource_length is None:
                raise RuntimeError("t{} 不是可读取 Buffer".format(slot))

            size, size_warning = choose_buffer_view_size(descriptor, resource_length)
            if size_warning:
                result["warnings"].append("t{}: {}".format(slot, size_warning))
            if size <= 0:
                raise RuntimeError("SRV view 大小为 0")

            resolve_buffer_stride(input_info, descriptor)
            raw = bytes(controller.GetBufferData(
                descriptor.resource,
                get_int_field(descriptor.byteOffset, 0),
                size
            ))
            if len(raw) < size:
                raise RuntimeError("原始数据不足")

            packed, height, _byte_length = pack_buffer_data(raw[:size], CONVERTER_DATA_WIDTH)
            file_path = make_buffer_data_path(data_dir, slot)
            write_binary_file(file_path, packed)
            result["buffers"].append(make_buffer_metadata(input_info, file_path))
            log("T{}Data: {}B / {} row".format(slot, size, height))

        except Exception as exception:
            result["complete"] = False
            result["missingSlots"].append(slot)
            result["warnings"].append("T{}Data: {}".format(slot, exception))

    for message in result["warnings"]:
        warn(message)
    return result

# ============================================================
# Texture / Sampler 导出
# ============================================================

def enum_text(value):
    return str(value)

def collect_texture_descriptions(controller):
    return {
        str(texture.resourceId): texture
        for texture in controller.GetTextures()
    }

def find_pixel_readonly_texture(reflection, slot):
    """将 HLSL tN 映射到 Pixel Shader 的 Texture resource。"""
    matches = []

    for resource_index, resource in enumerate(
        reflection.readOnlyResources
    ):
        bind_slot = get_int_field(resource.fixedBindNumber, -1)
        bind_space = get_int_field(
            resource.fixedBindSetOrSpace,
            0
        )

        if bind_slot == slot and bind_space == 0 and bool(resource.isTexture):
            matches.append((resource_index, resource))

    if not matches:
        raise RuntimeError(
            "Pixel Shader reflection 中找不到 Texture t{}".format(slot)
        )

    if len(matches) > 1:
        raise RuntimeError(
            "Pixel Shader reflection 中 t{} 匹配到多个 Texture".format(
                slot
            )
        )

    return matches[0]

def find_pixel_sampler(reflection, slot):
    """将 HLSL sN 映射到 ShaderReflection.samplers。"""
    matches = []

    for sampler_index, sampler in enumerate(reflection.samplers):
        bind_slot = get_int_field(sampler.fixedBindNumber, -1)
        bind_space = get_int_field(
            sampler.fixedBindSetOrSpace,
            0
        )

        if bind_slot == slot and bind_space == 0:
            matches.append((sampler_index, sampler))

    if not matches:
        raise RuntimeError(
            "Pixel Shader reflection 中找不到 Sampler s{}".format(slot)
        )

    if len(matches) > 1:
        raise RuntimeError(
            "Pixel Shader reflection 中 s{} 匹配到多个 Sampler".format(
                slot
            )
        )

    return matches[0]

def find_used_sampler_descriptor(pipe, sampler_index):
    matches = []

    for used_descriptor in pipe.GetSamplers(
        rd.ShaderStage.Pixel
    ):
        access = used_descriptor.access

        if (
            get_int_field(access.index, -1) == sampler_index and
            get_int_field(access.arrayElement, 0) == 0
        ):
            matches.append(used_descriptor)

    if not matches:
        raise RuntimeError(
            "当前 draw 找不到 samplers[{}] 的 descriptor".format(
                sampler_index
            )
        )

    if len(matches) > 1:
        raise RuntimeError(
            "samplers[{}] 匹配到多个 descriptor".format(
                sampler_index
            )
        )

    return matches[0]

def save_texture(
    controller,
    texture_id,
    texture_path,
    mip,
    slice_index
):
    # 同一个资源 view 在多个 draw 中复用时不重复导出。
    if os.path.isfile(texture_path):
        return texture_path

    os.makedirs(os.path.dirname(texture_path), exist_ok=True)

    texsave = rd.TextureSave()
    texsave.resourceId = texture_id
    texsave.mip = mip
    texsave.slice.sliceIndex = slice_index
    texsave.alpha = rd.AlphaMapping.Preserve
    texsave.destType = rd.FileType.PNG

    controller.SaveTexture(texsave, texture_path)

    if not os.path.isfile(texture_path):
        raise RuntimeError(
            "RenderDoc 未生成纹理文件: {}".format(texture_path)
        )

    return texture_path

def sampler_filter_json(texture_filter):
    return [
        enum_text(texture_filter.minify),
        enum_text(texture_filter.magnify),
        enum_text(texture_filter.mip),
        enum_text(texture_filter.filter),
    ]

def make_sampler_metadata(slot, sampler_index, shader_sampler, descriptor):
    sampler = descriptor.sampler
    return {
        "slot": slot,
        "filter": sampler_filter_json(sampler.filter),
        "address": [
            enum_text(sampler.addressU),
            enum_text(sampler.addressV),
            enum_text(sampler.addressW),
        ],
        "anisotropy": float(sampler.maxAnisotropy),
    }

def make_texture_metadata(requirement, descriptor, texture_description, texture_path):
    source_format = str(texture_description.format).upper()
    view_format = str(descriptor.format).upper()
    is_data = bool(requirement.get("isDataTexture"))
    return {
        "slot": int(requirement["slot"]),
        "filePath": texture_path,
        "srgb": bool(("SRGB" in source_format or "SRGB" in view_format) and not is_data),
    }

def make_texture_result(texture_inputs, sampler_inputs, texture_sampler_uses):
    return {
        "complete": True,
        "textures": [],
        "missingSlots": [],
        "samplers": [],
        "missingSamplerSlots": [],
        "samplerUses": list(texture_sampler_uses),
        "warnings": [],
    }

def export_texture_resources(
    controller,
    event_id,
    paths,
    texture_inputs,
    sampler_inputs,
    texture_sampler_uses
):
    """Export only Texture Objects referenced by the compact layout."""
    result = make_texture_result(texture_inputs, sampler_inputs, texture_sampler_uses)
    if not texture_inputs and not sampler_inputs:
        return result

    controller.SetFrameEvent(event_id, True)
    pipe = controller.GetPipelineState()
    reflection = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
    if reflection is None:
        result["complete"] = False
        result["missingSlots"] = [item["slot"] for item in texture_inputs]
        result["missingSamplerSlots"] = [item["slot"] for item in sampler_inputs]
        return result

    descriptions = collect_texture_descriptions(controller)

    for requirement in texture_inputs:
        slot = int(requirement["slot"])
        try:
            resource_index, _shader_resource = find_pixel_readonly_texture(reflection, slot)
            descriptor = find_used_readonly_descriptor(pipe, resource_index).descriptor
            description = descriptions.get(str(descriptor.resource))
            if description is None:
                raise RuntimeError("纹理描述不存在")

            mip = get_int_field(descriptor.firstMip, 0)
            slice_index = get_int_field(descriptor.firstSlice, 0)
            texture_path = make_texture_path(paths, descriptor.resource, mip, slice_index)
            save_texture(controller, descriptor.resource, texture_path, mip, slice_index)
            result["textures"].append(
                make_texture_metadata(requirement, descriptor, description, texture_path)
            )

            if requirement.get("isDataTexture"):
                result["warnings"].append(
                    "T{} 使用 Load，PNG 仅用于预览".format(slot)
                )

        except Exception as exception:
            result["complete"] = False
            result["missingSlots"].append(slot)
            result["warnings"].append("T{}: {}".format(slot, exception))

    for requirement in sampler_inputs:
        slot = int(requirement["slot"])
        try:
            sampler_index, shader_sampler = find_pixel_sampler(reflection, slot)
            descriptor = find_used_sampler_descriptor(pipe, sampler_index)
            result["samplers"].append(
                make_sampler_metadata(slot, sampler_index, shader_sampler, descriptor)
            )
        except Exception as exception:
            result["complete"] = False
            result["missingSamplerSlots"].append(slot)
            result["warnings"].append("s{}: {}".format(slot, exception))

    log("Texture: {} sampler={} missing={}/{}".format(
        len(result["textures"]),
        len(result["samplers"]),
        len(result["missingSlots"]),
        len(result["missingSamplerSlots"])
    ))
    for message in result["warnings"]:
        warn(message)
    return result

def write_material_json(
    material_json_path,
    texture_result,
    shader_result,
    mesh_result,
    cb_result,
    buffer_result
):
    """Write only values the UE importer consumes."""
    if os.path.isfile(material_json_path):
        os.remove(material_json_path)

    ready = (
        shader_result.get("converted") and
        not mesh_result.get("missingVaryings") and
        cb_result.get("complete", True) and
        buffer_result.get("complete", True) and
        texture_result.get("complete", True)
    )

    required_files = [
        mesh_result.get("jsonPath"),
        mesh_result.get("binPath"),
        shader_result.get("hlslPath"),
        shader_result.get("layoutPath"),
    ]
    if cb_result.get("enabled"):
        required_files.append(cb_result.get("filePath"))
    required_files.extend(item.get("filePath") for item in buffer_result.get("buffers", []))
    required_files.extend(item.get("filePath") for item in texture_result.get("textures", []))
    ready = ready and all(path and os.path.isfile(path) for path in required_files)

    if not ready:
        reasons = []
        if not shader_result.get("converted"):
            reasons.append("Shader")
        if mesh_result.get("missingVaryings"):
            reasons.append(
                "Varying={}".format(
                    ",".join(mesh_result["missingVaryings"])
                )
            )
        if not cb_result.get("complete", True):
            reasons.append("CB")
        if not buffer_result.get("complete", True):
            reasons.append("Buffer")
        if not texture_result.get("complete", True):
            reasons.append("Texture/Sampler")

        missing_files = [
            os.path.basename(path) if path else "<none>"
            for path in required_files
            if not path or not os.path.isfile(path)
        ]
        if missing_files:
            reasons.append("File={}".format(",".join(missing_files)))

        warn("材质输入不完整，跳过 {}: {}".format(
            os.path.basename(material_json_path),
            "; ".join(reasons) or "unknown"
        ))
        return None

    material_dir = os.path.dirname(material_json_path)
    payload = {
        "mesh": make_rel_path(mesh_result["jsonPath"], material_dir),
        "shader": make_rel_path(shader_result["hlslPath"], material_dir),
        "layout": make_rel_path(shader_result["layoutPath"], material_dir),
    }

    if cb_result.get("enabled"):
        payload["cb"] = make_rel_path(cb_result["filePath"], material_dir)

    if buffer_result.get("buffers"):
        payload["buffers"] = {
            "T{}Data".format(item["slot"]): make_rel_path(item["filePath"], material_dir)
            for item in sorted(buffer_result["buffers"], key=lambda value: value["slot"])
        }

    texture_entries = {
        item["slot"]: {
            "file": make_rel_path(item["filePath"], material_dir),
            "srgb": item["srgb"],
        }
        for item in texture_result.get("textures", [])
    }
    samplers = {item["slot"]: item for item in texture_result.get("samplers", [])}

    for usage in texture_result.get("samplerUses", []):
        owner = usage.get("samplerOwnerSlot")
        sampler = samplers.get(usage.get("samplerSlot"))
        if owner in texture_entries and sampler:
            texture_entries[owner]["sampler"] = {
                "filter": sampler["filter"],
                "address": sampler["address"],
                "anisotropy": sampler["anisotropy"],
            }

    if texture_entries:
        payload["textures"] = {
            "T{}".format(slot): texture_entries[slot]
            for slot in sorted(texture_entries)
        }

    with open(material_json_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, ensure_ascii=False)
    return payload

# ============================================================
# Scene 写入
# ============================================================

def write_scene_json(scene_path, draw_results):
    scene_dir = os.path.dirname(scene_path)
    draws = [
        {
            "eid": result["eventId"],
            "mesh": make_rel_path(result["jsonPath"], scene_dir),
            "material": make_rel_path(result["materialJsonPath"], scene_dir),
        }
        for result in draw_results
        if result.get("ready")
    ]

    with open(scene_path, "w", encoding="utf-8") as file:
        json.dump({"draws": draws}, file, indent=2, ensure_ascii=False)

# ============================================================
# Pixel Shader 提取与反编译
# ============================================================

def write_binary_file(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)

    with open(path, "wb") as f:
        f.write(data)

def get_pixel_shader_blob(controller):
    pipe = controller.GetPipelineState()
    reflection = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
    if reflection is None:
        raise RuntimeError("当前 draw 没有 Pixel Shader reflection")
    if "DXBC" not in str(reflection.encoding).upper():
        raise RuntimeError("当前仅支持 DXBC Pixel Shader")

    raw = bytes(reflection.rawBytes)
    if not raw:
        raise RuntimeError("Pixel Shader 原始字节为空")
    return {"rawBytes": raw, "shaderHash": hashlib.sha256(raw).hexdigest()}

def write_pixel_shader_bytecode(shader_dir, raw_bytes):
    bytecode_path = make_shader_bytecode_path(shader_dir)
    write_binary_file(bytecode_path, raw_bytes)
    return bytecode_path

def run_hlsl_decompiler(bytecode_path, shader_dir):
    if not os.path.isfile(HLSL_DECOMPILER_EXE):
        raise FileNotFoundError(
            "未找到 HLSLDecompiler: {}".format(
                HLSL_DECOMPILER_EXE
            )
        )

    generated_path = os.path.splitext(bytecode_path)[0] + ".hlsl"
    output_path = make_decompiled_hlsl_path(shader_dir)

    # 删除旧结果，避免本次反编译失败时误用上一次文件。
    for path in {generated_path, output_path}:
        if os.path.isfile(path):
            os.remove(path)

    completed = subprocess.run(
        [HLSL_DECOMPILER_EXE, "-D", bytecode_path],
        cwd=os.path.dirname(HLSL_DECOMPILER_EXE),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT
    )

    output_text = completed.stdout.decode(
        errors="replace"
    ).strip()

    if completed.returncode != 0:
        raise RuntimeError(
            "HLSLDecompiler 返回码 {}: {}".format(
                completed.returncode,
                output_text or "无输出"
            )
        )

    if not os.path.isfile(generated_path):
        raise RuntimeError(
            "HLSLDecompiler 未生成文件: {}".format(
                generated_path
            )
        )

    os.replace(generated_path, output_path)
    return output_path

# ============================================================
# HLSL Converter
# ============================================================

def load_json(json_path):
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)

def parse_converter_layout(layout_path):
    """Expand the compact layout into the names used by the exporter."""
    layout = load_json(layout_path)

    buffers = [
        {
            "slot": item["slot"],
            "name": "T{}Data".format(item["slot"]),
            "bufferType": item.get("type", "Buffer"),
            "stride": item.get("stride"),
        }
        for item in layout.get("buffers", [])
    ]
    textures = [
        {
            "slot": item["slot"],
            "name": "T{}".format(item["slot"]),
            "isDataTexture": bool(item.get("data")),
        }
        for item in layout.get("textures", [])
    ]
    sampler_uses = [
        {
            "textureSlot": item["texture"],
            "samplerSlot": item["sampler"],
            "samplerOwnerSlot": item["owner"],
        }
        for item in layout.get("samplers", [])
    ]
    sampler_slots = sorted({item["samplerSlot"] for item in sampler_uses})
    varyings = [
        {
            "name": item.get("input") or item.get("register", "").upper(),
            "register": item.get("register"),
            "semantic": item.get("semantic", ""),
            "components": item.get("components", 4),
            "type": item.get("type", "float"),
        }
        for item in layout.get("varyings", [])
    ]

    return layout, {
        "dataTextureWidth": layout.get("width"),
        "constantBufferSlots": layout.get("cb", []),
        "bufferInputs": buffers,
        "textureInputs": textures,
        "samplerInputs": [
            {"slot": slot, "name": "S{}".format(slot)}
            for slot in sampler_slots
        ],
        "textureSamplerUses": sampler_uses,
        "varyingInputs": varyings,
        "systemInputs": [{"name": name} for name in layout.get("system", [])],
    }

def log_converter_requirements(requirements):
    def names(items, key="name"):
        return ", ".join(str(item[key]) for item in items) or "无"

    log("输入 CB: {}".format(
        ", ".join("b{}".format(slot) for slot in requirements["constantBufferSlots"]) or "无"
    ))
    log("输入 Buffer: {}".format(names(requirements["bufferInputs"])))
    log("输入 Texture: {}".format(names(requirements["textureInputs"])))
    log("输入 Varying: {}".format(names(requirements["varyingInputs"])))

def reload_hlsl_converter():
    """重新加载当前扩展目录中的 converter，并清除旧模块状态。"""
    global _HLSL_CONVERTER

    from . import rdc2ue_hlsl_converter

    _HLSL_CONVERTER = importlib.reload(
        rdc2ue_hlsl_converter
    )

    log("Converter 模块: {}".format(
        os.path.abspath(_HLSL_CONVERTER.__file__)
    ))

    return _HLSL_CONVERTER

def get_hlsl_converter():
    if _HLSL_CONVERTER is None:
        return reload_hlsl_converter()

    return _HLSL_CONVERTER

def run_hlsl_converter(input_hlsl_path, shader_dir):
    input_hlsl_path = ensure_path_inside(input_hlsl_path, shader_dir, "反编译 HLSL")
    converter = get_hlsl_converter()
    converted = converter.convert_hlsl_file(
        input_hlsl_path,
        shader_dir,
        CONVERTER_DATA_WIDTH
    )

    hlsl_path = ensure_path_inside(
        os.path.join(shader_dir, "ue_custom_shader.hlsl"), shader_dir, "Custom HLSL"
    )
    layout_path = ensure_path_inside(
        os.path.join(shader_dir, "ue_custom_layout.json"), shader_dir, "Custom Layout"
    )
    if not os.path.isfile(hlsl_path) or not os.path.isfile(layout_path):
        raise RuntimeError("Converter 输出不完整")

    layout, requirements = parse_converter_layout(layout_path)
    log_converter_requirements(requirements)
    return {
        "hlslPath": hlsl_path,
        "layoutPath": layout_path,
        "requirements": requirements,
        "warnings": list(converted.get("warnings", [])),
    }

def make_shader_result():
    return {
        "converted": False,
        "shaderHash": None,
        "cacheHit": False,
        "bytecodePath": None,
        "sourcePath": None,
        "hlslPath": None,
        "layoutPath": None,
        "requirements": None,
        "warnings": [],
        "error": None,
    }

def set_shader_error(result, stage, exception):
    result["error"] = OrderedDict([
        ("stage", stage),
        ("message", str(exception)),
    ])

    warn("Shader 处理失败 stage={}, 原因：{}".format(
        stage,
        exception
    ))

    return result

def clone_cached_shader_result(cached_result, shader_hash):
    result = copy.deepcopy(cached_result)
    result["shaderHash"] = shader_hash
    result["cacheHit"] = True
    return result

def cache_shader_result(shader_cache, shader_hash, result, event_id):
    shader_cache[shader_hash] = {
        "result": copy.deepcopy(result),
        "firstEventId": event_id,
    }

def export_pixel_shader(controller, event_id, paths, shader_cache):
    result = make_shader_result()
    try:
        blob = get_pixel_shader_blob(controller)
        shader_hash = blob["shaderHash"]
        result["shaderHash"] = shader_hash
    except Exception as exception:
        return set_shader_error(result, "shader_extract", exception)

    cached = shader_cache.get(shader_hash)
    if cached:
        log("Shader 复用 {} eid={} 首次eid={}".format(
            shader_hash[:16], event_id, cached["firstEventId"]
        ))
        return clone_cached_shader_result(cached["result"], shader_hash)

    shader_dir = make_shared_shader_dir(paths, shader_hash)
    os.makedirs(shader_dir, exist_ok=True)
    log("Shader 新建 {} eid={}".format(shader_hash[:16], event_id))

    try:
        result["bytecodePath"] = write_pixel_shader_bytecode(shader_dir, blob["rawBytes"])
        result["sourcePath"] = run_hlsl_decompiler(result["bytecodePath"], shader_dir)
        converted = run_hlsl_converter(result["sourcePath"], shader_dir)
        result.update(converted)
        result["converted"] = True
    except Exception as exception:
        set_shader_error(result, "shader_pipeline", exception)

    cache_shader_result(shader_cache, shader_hash, result, event_id)
    return result

def export_mesh(controller, draw, mesh_json_path, mesh_bin_path, varying_inputs):
    event_id = draw.eventId
    controller.SetFrameEvent(event_id, True)
    log("EventId={} indexCount={} instanceCount={}".format(
        event_id, draw.numIndices, draw.numInstances
    ))

    pixel_inputs = collect_pixel_input_signature(controller)
    vertex_outputs, postvs_stride = collect_vertex_output_layout(controller)
    standard_layout = build_standard_layout(vertex_outputs)
    varying_layout, missing, warnings = build_varying_layout(
        varying_inputs, pixel_inputs, vertex_outputs
    )

    mesh_data = create_empty_mesh_data(varying_layout)
    for instance_id in range(draw.numInstances):
        append_instance(
            controller,
            instance_id,
            draw.numIndices,
            postvs_stride,
            standard_layout,
            varying_layout,
            mesh_data
        )

    attributes = build_mesh_attributes(mesh_data, varying_layout)
    bin_path, json_path = write_mesh_files(
        mesh_json_path,
        mesh_bin_path,
        mesh_data["instances"],
        attributes
    )

    for message in warnings:
        warn(message)
    return {
        "eventId": event_id,
        "vertexCount": len(mesh_data["positions"]),
        "instanceCount": draw.numInstances,
        "jsonPath": json_path,
        "binPath": bin_path,
        "missingVaryings": missing,
    }

def export_material(
    material_json_path,
    texture_result,
    shader_result,
    mesh_result,
    cb_result,
    buffer_result
):
    payload = write_material_json(
        material_json_path,
        texture_result,
        shader_result,
        mesh_result,
        cb_result,
        buffer_result
    )
    return bool(payload)

def export_draw(controller, draw, paths, shader_cache):
    event_id = draw.eventId
    log("开始导出 EventId = {}".format(event_id))
    controller.SetFrameEvent(event_id, True)

    shader = export_pixel_shader(controller, event_id, paths, shader_cache)
    requirements = shader.get("requirements") or {}

    cb = export_constant_buffers(
        controller, event_id, paths, requirements.get("constantBufferSlots", [])
    )
    buffers = export_buffer_resources(
        controller, event_id, paths, requirements.get("bufferInputs", [])
    )
    textures = export_texture_resources(
        controller,
        event_id,
        paths,
        requirements.get("textureInputs", []),
        requirements.get("samplerInputs", []),
        requirements.get("textureSamplerUses", [])
    )
    mesh = export_mesh(
        controller,
        draw,
        make_mesh_json_path(paths, event_id),
        make_mesh_bin_path(paths, event_id),
        requirements.get("varyingInputs", [])
    )

    material_path = make_material_json_path(paths, event_id)
    ready = export_material(material_path, textures, shader, mesh, cb, buffers)
    mesh["materialJsonPath"] = material_path if ready else None
    mesh["ready"] = ready
    mesh["shaderHash"] = shader.get("shaderHash")
    mesh["cacheHit"] = shader.get("cacheHit", False)
    return mesh

def export_draw_range(controller, start_eid, end_eid, output_dir):
    paths = create_export_paths(output_dir, start_eid, end_eid)
    if os.path.isdir(paths["range"]):
        shutil.rmtree(paths["range"])
    ensure_export_dirs(paths)
    reload_hlsl_converter()

    draws = get_draw_actions(controller, start_eid, end_eid)
    log("导出范围: {}-{}，共 {} 个 drawcall".format(start_eid, end_eid, len(draws)))

    results = []
    shader_cache = {}
    failed = []

    for draw in draws:
        try:
            result = export_draw(controller, draw, paths, shader_cache)
            results.append(result)
            log("导出完成 eid={} ready={} vertices={} instances={} shader={} cacheHit={}".format(
                result["eventId"],
                result["ready"],
                result["vertexCount"],
                result["instanceCount"],
                (result.get("shaderHash") or "")[:16],
                result.get("cacheHit", False)
            ))
        except Exception as exception:
            failed.append(draw.eventId)
            warn("导出失败 eid={}, 原因：{}".format(draw.eventId, exception))

    write_scene_json(paths["scene"], results)
    ready_count = sum(1 for result in results if result.get("ready"))
    skipped_count = len(results) - ready_count
    log("导出完成：ready={} skipped={} failed={} uniqueShaders={}".format(
        ready_count, skipped_count, len(failed), len(shader_cache)
    ))

    return {
        "outputDir": paths["range"],
        "scenePath": paths["scene"],
        "readyCount": ready_count,
        "skippedCount": skipped_count,
        "failedCount": len(failed),
        "failed": failed,
    }

# ============================================================
# 插件入口
# ============================================================

def export_from_plugin(
    ctx,
    start_eid=RANGE_START_EID,
    end_eid=RANGE_END_EID,
    output_dir=None
):
    if output_dir is None:
        output_dir = DEFAULT_OUTPUT_DIR

    output_dir = os.path.abspath(os.path.normpath(output_dir))
    os.makedirs(output_dir, exist_ok=True)

    log("准备导出")
    log("输出目录: {}".format(output_dir))

    result_holder = {"result": None}

    def replay_task(controller):
        result_holder["result"] = export_draw_range(controller, start_eid, end_eid, output_dir)
    
    try:
        ctx.Replay().BlockInvoke(replay_task)
    except Exception:
        print_exception("导出失败")
    
    return result_holder["result"]

