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

DEFAULT_EXPORT_ATTRS = [
    ("POSITION", "ATTRIBUTE0", 3),
    ("TEXCOORD_0", "ATTRIBUTE5", 2),
]

# ==================== 数据读取函数 ================

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

def unpack_vertex_value(fmt, raw_bytes):
    """解码顶点数据为 tuple """
    format_chars = {
        1: {
            rd.CompType.UInt: 'B',
            rd.CompType.SInt: 'b',
            rd.CompType.UNorm: 'B',
            rd.CompType.SNorm: 'b',
        },
        2: {
            rd.CompType.UInt: 'H',
            rd.CompType.SInt: 'h',
            rd.CompType.UNorm: 'H',
            rd.CompType.SNorm: 'h',
            rd.CompType.Float: 'e',
        },
        4: {
            rd.CompType.UInt: 'I',
            rd.CompType.SInt: 'i',
            rd.CompType.Float: 'f',
        },
    }

    comp_width = fmt.compByteWidth
    comp_type = fmt.compType
    comp_count = fmt.compCount

    if comp_width not in format_chars:
        raise RuntimeError("不支持的字节宽度：compByteWidth={}".format(comp_width))
    if comp_type not in format_chars[comp_width]:
        raise RuntimeError("不支持的组件类型：compType={}, compByteWidth={}".format(comp_type,comp_width))
    
    char = format_chars[comp_width][comp_type]
    struct_fmt = "<{}{}".format(comp_count, char)
    value = struct.unpack_from(struct_fmt, raw_bytes, 0)

    # 映射 UNorm 和 SNorm
    if comp_type == rd.CompType.UNorm:
        divisor = float((1 << (comp_width * 8)) - 1)
        value = tuple(v / divisor for v in value)
    elif comp_type == rd.CompType.SNorm:
        max_val = float((1 << (comp_width * 8 - 1)) - 1)
        value = tuple(max(v / max_val, -1.0) for v in value)
    
    return value

def get_index_byte_width(draw, ib_info):
    """获取索引字节宽度"""
    bw = getattr(ib_info, "byteStride", None)
    if bw:
        return bw
    bw = getattr(draw, "indexByteWidth", None)
    if bw is not None:
        return bw
    flags = getattr(draw, "flags", 0)
    if hasattr(rd, "ActionFlags") and (flags & rd.ActionFlags.Indexed):
        return 4
    return 0


def get_indices(controller, draw, ib_info):
    """获取索引数据"""
    num_indices = draw.numIndices
    index_byte_width = get_index_byte_width(draw, ib_info)

    # 非索引绘制
    if index_byte_width == 0:
        return [draw.baseVertex + i for i in range(num_indices)]
    
    # 索引绘制
    ib_bytes = controller.GetBufferData(
        ib_info.resourceId,
        ib_info.byteOffset,
        0
    )

    if index_byte_width == 2:
        fmt = 'H'
    else:
        fmt = 'I'

    indices = []
    for i in range(num_indices):
        byte_offset = (draw.indexOffset + i) * index_byte_width
        index_value = struct.unpack_from(fmt, ib_bytes, byte_offset)[0]
        indices.append(index_value + draw.baseVertex)

    return indices

# ==================== 导出函数 ====================

def write_mesh_bin(bin_path, attributes):
    """将顶点数据写入 BIN 文件"""
    first_attr_name = next(iter(attributes))
    vertex_count = len(attributes[first_attr_name]["data"])

    json_attributes = OrderedDict()
    byte_offset = 0

    with open(bin_path, "wb") as f:
        for name, info in attributes.items():
            data = info["data"]
            component_type = info.get("componentType", "FLOAT32")
            component_count = info.get("componentCount", len(data[0]))

            byte_stride = component_count * 4
            pack_fmt = "<{}f".format(component_count)

            for value in data:
                comps = list(value[:component_count])

                while len(comps) < component_count:
                    comps.append(0.0)

                f.write(struct.pack(pack_fmt, *[float(c) for c in comps]))
            
            byte_length = vertex_count * byte_stride

            json_attributes[name] = OrderedDict([
                ("componentType", component_type),
                ("componentCount", component_count),
                ("byteStride", byte_stride),
                ("byteOffset", byte_offset),
                ("byteLength", byte_length),
                ("count", vertex_count),
            ])

            byte_offset += byte_length

    return json_attributes, byte_offset, vertex_count

def write_mesh_json(json_path, bin_path, json_attributes, byte_length, vertex_count):
    """写 JSON 文件"""
    payload = OrderedDict()

    payload["version"] = 3

    payload["geometry"] = OrderedDict([
        ("primitive", "triangles"),
        ("vertexCount", vertex_count),
        ("triangleCount", vertex_count // 3),
    ])

    payload["buffer"] = OrderedDict([
        ("uri", os.path.basename(bin_path)),
        ("byteLength", byte_length),
    ])

    payload["attributes"] = json_attributes

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

def write_mesh_files(path_prefix, attributes):
    """将顶点数据写入 JSON 和 BIN 文件"""
    bin_path = path_prefix + ".bin"
    json_path = path_prefix + ".json"

    json_attributes, byte_length, vertex_count = write_mesh_bin(bin_path, attributes)
    write_mesh_json(json_path, bin_path, json_attributes, byte_length, vertex_count)
    return bin_path, json_path

def write_debug_txt(txt_path, event_id, draw_name, selected_attrs, parsed, indices_in_vb):
    """调试 txt 文件"""

    total = len(indices_in_vb)

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("# EventId = {}\n".format(event_id))
        f.write("# Drawcall = {}\n".format(draw_name))
        f.write("# VertexCount = {}\n".format(total))
        f.write("# TriangleCount = {}\n".format(total // 3))

        header = ["i", "vb_idx"]
        for semantic, _attr, count in selected_attrs:
            for c in range(count):
                header.append("{}_{}".format(semantic, c))

        f.write("# Columns: {}\n".format(", ".join(header)))

        for i, vb_idx in enumerate(indices_in_vb):
            row = [str(i), str(vb_idx)]

            for semantic, _attr, _count in selected_attrs:
                value = parsed[semantic][i]
                row += ["{:.6f}".format(v) for v in value]

            f.write(", ".join(row) + "\n")

# ==================== 主流程 ====================

def export_mesh(controller, event_id, output_dir):
    """导出网格数据"""

    print("=" * 70)
    log("开始导出 EventId = {}".format(event_id))

    # 获取 drawcall
    draw = get_draw_action(controller, event_id)
    draw_name = draw.GetName(controller.GetStructuredFile())
    log("Drawcall: {}".format(draw_name))

    # 读取 pipeline 状态
    controller.SetFrameEvent(event_id, True)

    state = controller.GetPipelineState()
    vbo = state.GetVBuffers()
    ib_info = state.GetIBuffer()
    attrs = state.GetVertexInputs()

    index_byte_width = get_index_byte_width(draw, ib_info)

    log("numIndices={}, indexByteWidth={}, baseVertex={}, indexOffset={}".format(
        draw.numIndices, 
        index_byte_width, 
        draw.baseVertex, 
        draw.indexOffset
    ))

    # 需导出的 attribute
    attr_by_name = {a.name: a for a in attrs if a.used}

    selected = []
    for semantic, attr_name, count in DEFAULT_EXPORT_ATTRS:
        selected.append((semantic, attr_by_name[attr_name], count))
    
    # 读取 vertex buffer
    vb_cache = {}
    needed_vb_slots = sorted({attr.vertexBuffer for _semantic, attr, _count in selected})
    for slot in needed_vb_slots:
        vb = vbo[slot]

        log("读取 VB slot {}: rid={}, byteOffset={}, byteStride={}".format(
            slot,
            int(vb.resourceId),
            vb.byteOffset,
            vb.byteStride
        ))

        vb_bytes = controller.GetBufferData(vb.resourceId, 0, 0)
        vb_cache[slot] = (vb, vb_bytes)

    # 根据 index buffer 得到顶点读取顺序
    indices_in_vb = get_indices(controller, draw, ib_info)
    total = len(indices_in_vb)
    log("顶点总数: {} ({} 个三角形)".format(total, total // 3))

    # 解析所有属性
    parsed = OrderedDict()

    for semantic, attr, expected_count in selected:
        vb, vb_bytes = vb_cache[attr.vertexBuffer]

        base_offset = vb.byteOffset + attr.byteOffset
        stride = vb.byteStride

        values = []

        for vb_idx in indices_in_vb:
            byte_offset = base_offset + vb_idx * stride
            value = unpack_vertex_value(attr.format, vb_bytes[byte_offset:])

            if len(value) > expected_count:
                value = value[:expected_count]
            elif len(value) < expected_count:
                value = value + (0.0,) * (expected_count - len(value))

            values.append(value)

        parsed[semantic] = values

    # 写调试 txt 文件
    txt_path = os.path.join(output_dir, "eid_{}.txt".format(event_id))
    write_debug_txt(txt_path, event_id, draw_name, selected, parsed, indices_in_vb)
    log("调试 txt 文件已写入: {}".format(txt_path))

    # 写 JSON 和 BIN 文件
    attributes_payload = OrderedDict()
    for semantic, _attr, _count in selected:
        attributes_payload[semantic] = {
            "data": parsed[semantic],
            "componentType": "FLOAT32",
            "componentCount": _count,
        }

    mesh_prefix = os.path.join(output_dir, "eid_{}".format(event_id))
    bin_path, json_path = write_mesh_files(mesh_prefix, attributes_payload)

    log("JSON 文件已写入: {}".format(json_path))
    log("BIN 文件已写入: {}".format(bin_path))
    log("导出完成")
    print("=" * 70)

    return {
        "event_id": event_id,
        "drawName": draw_name,
        "vertexCount": total,
        "triangleCount": total // 3,
        "jsonPath": json_path,
        "binPath": bin_path,
        "txtPath": txt_path,
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