# RDC2UE RenderDoc Exporter
# 从 RenderDoc 中批量导出：
#   scene.json
#   meshes/mesh_eid_xxx.json + mesh_eid_xxx.bin
#   materials/mat_eid_xxx.json
#   textures/tex_xxx.png

import os
import json
import struct
import traceback
from collections import OrderedDict

import renderdoc as rd

# ============================================================
# 全局配置
# ============================================================

RANGE_START_EID = 7890
RANGE_END_EID = 13075

RANGE_START_EID = 9197
RANGE_END_EID = 9892

DEFAULT_OUTPUT_DIR = r"F:\RDC2UE\ExportResults"

EXPORT_PROFILE = "pc" # "pc" | "mobile"

FLIP_WINDING = False

# VS Output layout
if EXPORT_PROFILE == "pc":
    # slot0: SV_POSITION.xyzw
    # slot1: TEXCOORD10.xyzw   -> Tangent.xyz + Handedness
    # slot2: TEXCOORD11.xyzw   -> Normal.xyz
    # slot3: COLOR.xyzw
    # slot4: TEXCOORD0.xyzw    -> UV0.xy
    VSOUT_SLOT_SV_POSITION = 0
    VSOUT_SLOT_TANGENT     = 1
    VSOUT_SLOT_NORMAL      = 2
    VSOUT_SLOT_UV0         = 4

elif EXPORT_PROFILE == "mobile":
    # mobile 端仅 slot 0 有效
    # slot 0: SV_Position
    VSOUT_SLOT_SV_POSITION = 0
    VSOUT_SLOT_TANGENT     = 1
    VSOUT_SLOT_NORMAL      = 2
    VSOUT_SLOT_UV0         = 3

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
    range_dir = os.path.join(
        output_dir,
        "range_{}_{}".format(start_eid, end_eid)
    )

    return {
        "range": range_dir,
        "scene": os.path.join(range_dir, "scene.json"),
        "meshes": os.path.join(range_dir, "meshes"),
        "materials": os.path.join(range_dir, "materials"),
        "textures": os.path.join(range_dir, "textures"),
    }


def ensure_export_dirs(paths):
    os.makedirs(paths["range"], exist_ok=True)
    os.makedirs(paths["meshes"], exist_ok=True)
    os.makedirs(paths["materials"], exist_ok=True)
    os.makedirs(paths["textures"], exist_ok=True)


def make_rel_path(path, base_dir):
    rel_path = os.path.relpath(path, base_dir)
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


def make_texture_filename(texture_id):
    texture_id_text = str(texture_id)
    texture_id_text = texture_id_text.replace("ResourceId::", "")

    return "T_{}_rdc.png".format(texture_id_text)


def make_texture_path(paths, texture_id):
    return os.path.join(
        paths["textures"],
        make_texture_filename(texture_id)
    )


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
# VS Output 读取
# ============================================================

def read_float4(raw_bytes, vertex_index, vertex_stride, float4_slot):
    offset = vertex_index * vertex_stride + float4_slot * 16
    return struct.unpack_from("<4f", raw_bytes, offset)


def read_vsout_vertex(raw_bytes, vertex_index, vertex_stride):
    sv_position = read_float4(raw_bytes, vertex_index, vertex_stride, VSOUT_SLOT_SV_POSITION)
    tangent4 = read_float4(raw_bytes, vertex_index, vertex_stride, VSOUT_SLOT_TANGENT)
    normal4 = read_float4(raw_bytes, vertex_index, vertex_stride, VSOUT_SLOT_NORMAL)
    uv4 = read_float4(raw_bytes, vertex_index, vertex_stride, VSOUT_SLOT_UV0)

    position = clip_to_world(sv_position)
    tangent = tangent4[:3]
    binormal_sign = tangent4[3]
    normal = normal4[:3]
    uv0 = (uv4[0], uv4[1])

    return position, tangent, binormal_sign, normal, uv0


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


# ============================================================
# Mesh 数据组装
# ============================================================

def create_empty_mesh_data():
    return {
        "positions": [],
        "tangents": [],
        "binormalSigns": [],
        "normals": [],
        "uvs": [],
        "instances": [],
    }


def append_vertex_from_vsout(raw_bytes, vertex_stride, vertex_index, mesh_data):
    position, tangent, binormal_sign, normal, uv = read_vsout_vertex(
        raw_bytes,
        vertex_index,
        vertex_stride
    )

    mesh_data["positions"].append(position)
    mesh_data["tangents"].append(tangent)
    mesh_data["binormalSigns"].append((binormal_sign,))
    mesh_data["normals"].append(normal)
    mesh_data["uvs"].append(uv)


def append_triangle(raw_bytes, vertex_stride, indices, tri_start, order, mesh_data):
    for local_index in order:
        index_pos = tri_start + local_index
        vertex_index = indices[index_pos]

        append_vertex_from_vsout(
            raw_bytes,
            vertex_stride,
            vertex_index,
            mesh_data
        )


def append_instance(controller, instance_id, index_count, mesh_data):
    # 当前 instance 的 VS Output buffer
    vertex_offset = len(mesh_data["positions"])

    postvs = controller.GetPostVSData(
        instance_id,
        0,
        rd.MeshDataStage.VSOut
    )

    vertex_stride = postvs.vertexByteStride

    raw_bytes = controller.GetBufferData(
        postvs.vertexResourceId,
        postvs.vertexByteOffset,
        0
    )

    # 当前 instance 的 index buffer
    indices = read_postvs_indices(controller, postvs, index_count)

    # 根据索引展开顶点
    order = (0, 2, 1) if FLIP_WINDING else (0, 1, 2)
    triangle_index_count = (index_count // 3) * 3

    for tri_start in range(0, triangle_index_count, 3):
        append_triangle(
            raw_bytes,
            vertex_stride,
            indices,
            tri_start,
            order,
            mesh_data
        )

    vertex_count = len(mesh_data["positions"]) - vertex_offset

    mesh_data["instances"].append(OrderedDict([
        ("vertexOffset", vertex_offset),
        ("vertexCount", vertex_count),
    ]))


def build_mesh_attributes(mesh_data):
    return OrderedDict([
        ("POSITION", {
            "data": mesh_data["positions"],
            "componentCount": 3,
        }),
        ("TANGENT", {
            "data": mesh_data["tangents"],
            "componentCount": 3,
        }),
        ("BINORMAL_SIGN", {
            "data": mesh_data["binormalSigns"],
            "componentCount": 1,
        }),
        ("NORMAL", {
            "data": mesh_data["normals"],
            "componentCount": 3,
        }),
        ("TEXCOORD_0", {
            "data": mesh_data["uvs"],
            "componentCount": 2,
        }),
    ])


# ============================================================
# Mesh 文件写入
# ============================================================

def write_mesh_bin(bin_path, attributes):
    first_attr_name = next(iter(attributes))
    vertex_count = len(attributes[first_attr_name]["data"])

    json_attributes = OrderedDict()
    byte_offset = 0

    with open(bin_path, "wb") as f:
        # attribute-major layout
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

    payload["instances"] = instances

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def write_mesh_files(json_path, bin_path, event_id, instances, attributes):
    json_attributes, byte_length, _vertex_count = write_mesh_bin(
        bin_path,
        attributes
    )

    write_mesh_json(
        json_path,
        bin_path,
        event_id,
        instances,
        json_attributes,
        byte_length
    )

    return bin_path, json_path


# ============================================================
# Texture / Material 导出
# ============================================================

def collect_all_texture_ids(controller):
    # 收集所有 texture 的 ID
    # 后续遍历 shader resource 时，用来过滤非纹理资源
    texture_ids = set()

    textures = controller.GetTextures()

    for texture in textures:
        texture_ids.add(str(texture.resourceId))

    return texture_ids


def save_texture(controller, texture_id, texture_path):
    # 避免重复导出
    if os.path.exists(texture_path):
        return True

    os.makedirs(os.path.dirname(texture_path), exist_ok=True)

    # 导出为 PNG
    texsave = rd.TextureSave()

    texsave.resourceId = texture_id
    texsave.mip = 0
    texsave.slice.sliceIndex = 0
    texsave.alpha = rd.AlphaMapping.Preserve
    texsave.destType = rd.FileType.PNG

    controller.SaveTexture(texsave, texture_path)

    return True


def collect_texture_bindings(controller, paths, all_texture_ids):
    pipe = controller.GetPipelineState()
    ps_resources = pipe.GetReadOnlyResources(rd.ShaderStage.Pixel)

    texture_bindings = []

    for slot, used_descriptor in enumerate(ps_resources):
        # 当前 slot 绑定的 GPU resourceId
        texture_id = used_descriptor.descriptor.resource
        texture_id_text = str(texture_id)

        if texture_id_text not in all_texture_ids:
            continue

        texture_path = make_texture_path(paths, texture_id)

        save_texture(controller, texture_id, texture_path)

        texture_bindings.append(OrderedDict([
            ("slot", slot),
            ("texture", make_rel_path(texture_path, paths["materials"])),
        ]))

    return texture_bindings


def write_material_json(material_json_path, texture_bindings):
    payload = OrderedDict()
    payload["textures"] = texture_bindings

    with open(material_json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


# ============================================================
# Scene 写入
# ============================================================

def write_scene_json(scene_path, draw_results):
    scene_dir = os.path.dirname(scene_path)
    draws = []

    for result in draw_results:
        draws.append(OrderedDict([
            ("mesh", make_rel_path(result["jsonPath"], scene_dir)),
            ("material", make_rel_path(result["materialJsonPath"], scene_dir)),
        ]))

    payload = OrderedDict()
    payload["draws"] = draws

    with open(scene_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


# ============================================================
# 主流程
# ============================================================

def export_mesh(controller, draw, mesh_json_path, mesh_bin_path):
    # 切换到当前 draw
    event_id = draw.eventId

    controller.SetFrameEvent(event_id, True)

    index_count = draw.numIndices
    instance_count = draw.numInstances

    log("EventId={} indexCount={} instanceCount={}".format(
        event_id,
        index_count,
        instance_count
    ))

    # 所有 instance 存在同一数组
    mesh_data = create_empty_mesh_data()

    for instance_id in range(instance_count):
        append_instance(
            controller,
            instance_id,
            index_count,
            mesh_data
        )

    # 写入文件
    attributes = build_mesh_attributes(mesh_data)

    bin_path, json_path = write_mesh_files(
        mesh_json_path,
        mesh_bin_path,
        event_id,
        mesh_data["instances"],
        attributes
    )

    return {
        "eventId": event_id,
        "vertexCount": len(mesh_data["positions"]),
        "instanceCount": instance_count,
        "jsonPath": json_path,
        "binPath": bin_path,
    }


def export_material(controller, event_id, material_json_path, paths, all_texture_ids):
    controller.SetFrameEvent(event_id, True)

    texture_bindings = collect_texture_bindings(
        controller,
        paths,
        all_texture_ids
    )

    write_material_json(material_json_path, texture_bindings)

    return {
        "materialJsonPath": material_json_path,
        "textureCount": len(texture_bindings),
    }


def export_draw(controller, draw, paths, all_texture_ids):
    event_id = draw.eventId

    log("开始导出 EventId = {}".format(event_id))

    mesh_json_path = make_mesh_json_path(paths, event_id)
    mesh_bin_path = make_mesh_bin_path(paths, event_id)
    material_json_path = make_material_json_path(paths, event_id)

    mesh_result = export_mesh(
        controller,
        draw,
        mesh_json_path,
        mesh_bin_path
    )

    material_result = export_material(
        controller,
        event_id,
        material_json_path,
        paths,
        all_texture_ids
    )

    result = mesh_result
    result["materialJsonPath"] = material_result["materialJsonPath"]
    result["textureCount"] = material_result["textureCount"]

    return result


def export_draw_range(controller, start_eid, end_eid, output_dir):
    paths = create_export_paths(output_dir, start_eid, end_eid)
    ensure_export_dirs(paths)

    draws = get_draw_actions(controller, start_eid, end_eid)
    log("导出范围: {}-{}，共 {} 个 drawcall".format(
        start_eid,
        end_eid,
        len(draws)
    ))

    all_texture_ids = collect_all_texture_ids(controller)

    results = []
    failed = []

    for draw in draws:
        event_id = draw.eventId

        try:
            result = export_draw(
                controller,
                draw,
                paths,
                all_texture_ids
            )

            results.append(result)

            log("导出成功 eid={} vertices={} instances={} textures={}".format(
                result["eventId"],
                result["vertexCount"],
                result["instanceCount"],
                result["textureCount"]
            ))

        except Exception as e:
            warn("导出失败 eid={}, 原因：{}".format(event_id, e))
            failed.append(event_id)

    write_scene_json(paths["scene"], results)

    log("scene.json 已写入: {}".format(paths["scene"]))
    log("导出完成：成功 {} 个，失败 {} 个".format(
        len(results),
        len(failed)
    ))

    return {
        "startEid": start_eid,
        "endEid": end_eid,
        "outputDir": paths["range"],
        "scenePath": paths["scene"],
        "successCount": len(results),
        "failedCount": len(failed),
        "results": results,
        "failed": failed,
    }


# ============================================================
# 插件入口
# ============================================================

def export_from_plugin(
    ctx, 
    start_eid = RANGE_START_EID, 
    end_eid = RANGE_END_EID, 
    output_dir=DEFAULT_OUTPUT_DIR
):
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

