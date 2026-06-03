# RDC2UE RenderDoc Mesh Exporter
# 从 RenderDoc 指定的 drawcall 中导出 mesh 顶点数据为 JSON + BIN

import os
import json
import struct
import traceback
from collections import OrderedDict

import renderdoc as rd

# ==================== 日志函数 ====================
def log(message):
    print("[RDC2UE] {}".format(message))


def warn(message):
    print("[RDC2UE][warn] {}".format(message))


def error(message):
    print("[RDC2UE][error] {}".format(message))


def print_exception(prefix="Exception"):
    error(prefix)
    print(traceback.format_exc())

# ==================== 全局配置 ====================

DEFAULT_OUTPUT_DIR = r"F:\RDC2UE\ExportResults"

# 人工记录 ViewProjection 矩阵
# cb1[9-11]
VIEW_PROJ = [
    [0.98146,   0.00537,   0.0,        0.19162],
    [0.19164,  -0.02735,   0.0,       -0.98135],
    [-0.00001,  1.77765,   0.0,       -0.01555],
    [0.0,       0.0,       1.0,        0.0],
]

# VS Output
# SV_POSITION.xyzw
# TEXCOORD10.xyzw   -> Tangent.xyz + Handedness
# TEXCOORD11.xyzw   -> Normal.xyz
# COLOR.xyzw
# TEXCOORD0.xyzw    -> UV0.xy
VSOUT_SLOT_SV_POSITION = 0
VSOUT_SLOT_TANGENT     = 1
VSOUT_SLOT_NORMAL      = 2
VSOUT_SLOT_UV0         = 4

FLIP_WINDING = False
WRITE_DEBUG_TXT = False

# ==================== Drawcall 查找 ================

def find_action_by_eid(actions, target_eid):
    """递归匹配查找 action """
    for action in actions:
        if action.eventId == target_eid:
            return action
        
        found = find_action_by_eid(action.children, target_eid)
        if found is not None:
            return found

    return None

def get_draw_action(controller, event_id):
    """根据 eid 找到对应 drawcall """
    root_actions = controller.GetRootActions()
    action = find_action_by_eid(root_actions, event_id)
    if action is None:
        raise RuntimeError("未找到 eventId = {} 的 drawcall".format(event_id))
    
    return action

# ==================== 数学工具 ====================
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

def normalize3(v):
    x, y, z = v
    length = x * x + y * y + z * z

    if length <= 0.0:
        return (0.0, 0.0, 1.0)
    
    inv_len = length ** -0.5
    return (x * inv_len, y * inv_len, z * inv_len)

# ==================== VS Output 读取函数 ====================

def read_float4(raw_bytes, vertex_index, vertex_stride, float4_slot):
    offset = vertex_index * vertex_stride + float4_slot * 16
    return struct.unpack_from("<4f", raw_bytes, offset)

def read_vsout_vertex(raw_bytes, vertex_index, vertex_stride):
    sv_position = read_float4(raw_bytes, vertex_index, vertex_stride, VSOUT_SLOT_SV_POSITION)
    normal4 = read_float4(raw_bytes, vertex_index, vertex_stride, VSOUT_SLOT_NORMAL)
    uv4 = read_float4(raw_bytes, vertex_index, vertex_stride, VSOUT_SLOT_UV0)

    position = clip_to_world(sv_position)
    normal = normalize3(normal4[:3])
    uv0 = (uv4[0], uv4[1])

    return position, normal, uv0

def read_postvs_indices(controller, postvs, index_count):
    index_stride = postvs.indexByteStride

    index_bytes = controller.GetBufferData(
        postvs.indexResourceId,
        postvs.indexByteOffset,
        index_count * index_stride
    )

    fmt = "<H" if index_stride == 2 else "<I"

    indices = []
    for i in range(index_count):
        offset = i * index_stride
        indices.append(struct.unpack_from(fmt, index_bytes, offset)[0])
    
    return indices

# ==================== 导出函数 ====================

def write_mesh_bin(bin_path, attributes):
    first_attr_name = next(iter(attributes))
    vertex_count = len(attributes[first_attr_name]["data"])

    json_attributes = OrderedDict()
    byte_offset = 0

    with open(bin_path, "wb") as f:
        for name, info in attributes.items():
            data = info["data"]
            component_count = info["componentCount"]

            byte_stride = component_count * 4
            byte_length = vertex_count * byte_stride
            pack_fmt = "<{}f".format(component_count)

            for value in data:
                f.write(struct.pack(pack_fmt, *value))
            
            json_attributes[name] = OrderedDict([
                ("componentCount", component_count),
                ("byteOffset", byte_offset),
                ("count", vertex_count),
            ])

            byte_offset += byte_length
    
    return json_attributes, byte_offset, vertex_count

def write_mesh_json(json_path, bin_path, event_id, instances, json_attributes, byte_length):
    payload = OrderedDict()

    payload["eventId"] = event_id

    payload["buffer"] = OrderedDict([
        ("uri", os.path.basename(bin_path)),
        ("byteLength", byte_length),
    ])

    payload["attributes"] = json_attributes

    if len(instances) > 1:
        payload["instances"] = instances

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

def write_mesh_files(path_prefix, event_id, instances, attributes):
    bin_path = path_prefix + ".bin"
    json_path = path_prefix + ".json"

    json_attributes, byte_length, _vertex_count = write_mesh_bin(bin_path, attributes)

    write_mesh_json(
        json_path,
        bin_path,
        event_id,
        instances,
        json_attributes,
        byte_length
    )

    return bin_path, json_path

def write_debug_txt(txt_path, event_id, positions, normals, uvs):
    vertex_count = len(positions)

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("# EventId = {}\n".format(event_id))
        f.write("# VertexCount = {}\n".format(vertex_count))
        f.write("# Columns: i, POSITION.xyz, NORMAL.xyz, UV.xy\n")

        for i in range(vertex_count):
            p = positions[i]
            n = normals[i]
            uv = uvs[i]

            f.write(
                "{}, "
                "{:.6f}, {:.6f}, {:.6f}, "
                "{:.6f}, {:.6f}, {:.6f}, "
                "{:.6f}, {:.6f}\n".format(
                    i,
                    p[0], p[1], p[2],
                    n[0], n[1], n[2],
                    uv[0], uv[1],
                )
            )

def append_triangle(raw_bytes, vertex_stride, indices, tri_start, order, positions, normals, uvs):
    for local_index in order:
        index_pos = tri_start + local_index
        vertex_index = indices[index_pos]

        position, normal, uv = read_vsout_vertex(raw_bytes, vertex_index, vertex_stride)

        positions.append(position)
        normals.append(normal)
        uvs.append(uv)

def append_instance(controller, instance_id, index_count, positions, normals, uvs, instances):
    vertex_offset = len(positions)
    
    postvs = controller.GetPostVSData(instance_id, 0, rd.MeshDataStage.VSOut)

    vertex_stride = postvs.vertexByteStride

    raw_bytes = controller.GetBufferData(
        postvs.vertexResourceId,
        postvs.vertexByteOffset,
        0
    )

    indices = read_postvs_indices(controller, postvs, index_count)

    log("Instance {}: vertexStride={}, vertexRawBytes={}, maxIndex={}".format(
        instance_id, vertex_stride, len(raw_bytes), max(indices)
    ))
    
    order = (0, 2, 1) if FLIP_WINDING else (0, 1, 2)
    triangle_index_count = (index_count // 3) * 3

    for tri_start in range(0, triangle_index_count, 3):
        append_triangle(
            raw_bytes, 
            vertex_stride, 
            indices, 
            tri_start, 
            order, 
            positions, 
            normals, 
            uvs
        )
    
    vertex_count = len(positions) - vertex_offset
    
    instances.append(OrderedDict([
        ("vertexOffset", vertex_offset),
        ("vertexCount", vertex_count),
    ]))

def export_mesh(controller, event_id, output_dir):
    print("=" * 70)
    log("开始导出 EventId = {}".format(event_id))

    # 获取 drawcall
    draw = get_draw_action(controller, event_id)

    controller.SetFrameEvent(event_id, True)

    index_count = draw.numIndices
    instance_count = draw.numInstances
    log("indexCount={}, instanceCount={}".format(index_count, instance_count))

    # 拼装三角形
    positions = []
    normals = []
    uvs = []
    instances = []

    for instance_id in range(instance_count):
        append_instance(
            controller,
            instance_id,
            index_count,
            positions,
            normals,
            uvs,
            instances
        )

    # 导出 mesh 文件
    attributes = OrderedDict([
        ("POSITION", {
            "data": positions,
            "componentCount": 3,
        }),
        ("NORMAL", {
            "data": normals,
            "componentCount": 3,
        }),
        ("TEXCOORD_0", {
            "data": uvs,
            "componentCount": 2,
        }),
    ])

    mesh_prefix = os.path.join(output_dir, "eid_{}".format(event_id))
    bin_path, json_path = write_mesh_files(
        mesh_prefix,
        event_id, 
        instances, 
        attributes
    )

    txt_path = None
    if WRITE_DEBUG_TXT:
        txt_path = mesh_prefix + ".txt"
        write_debug_txt(txt_path, event_id, positions, normals, uvs)
        log("调试 TXT 已写入: {}".format(txt_path))
    
    log("done eid={} verts={} inst={}".format(event_id, len(positions), instance_count))

    return {
        "eventId": event_id,
        "vertexCount": len(positions),
        "instanceCount": instance_count,
        "jsonPath": json_path,
        "binPath": bin_path,
    }

# ==================== 插件入口 ====================

def export_current_draw_from_plugin(ctx, output_dir=DEFAULT_OUTPUT_DIR):
    """插件调用入口函数"""
    os.makedirs(output_dir, exist_ok=True)
    
    event_id = ctx.CurEvent()
    if event_id == 0:
        log("当前没有选中有效 event")
        return None
    
    log("插件入口，准备导出当前 drawcall")
    log("当前 event id: {}".format(event_id))
    log("输出目录: {}".format(output_dir))

    result_holder = {"result": None}

    def replay_task(controller):
        result_holder["result"] = export_mesh(controller, event_id, output_dir)
    
    try:
        ctx.Replay().BlockInvoke(replay_task)
    except Exception:
        print_exception("导出失败")

    return result_holder["result"]