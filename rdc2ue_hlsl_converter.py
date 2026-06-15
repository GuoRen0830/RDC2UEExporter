#!/usr/bin/env python3
"""
RenderDoc/3DMigoto decompiled pixel shader -> UE4 Custom node HLSL.

The generated HLSL follows the data layout used by the working manual shader:

1. CBData
   - RGBA8 byte texture.
   - One 32-bit scalar is packed into one RGBA8 pixel.
   - Constant-buffer slot is the texture row.
   - float4 cbN[index] occupies four consecutive pixels.

2. tN StructuredBuffer / ByteAddressBuffer data
   - Connected as TNData Texture Object.
   - One 32-bit word is packed into one RGBA8 pixel.
   - The data texture width is a generated compile-time constant, so no
     BufferWidthTN material pin is required.

3. Unsupported volume/cube resource reads
   - Replaced by safe non-zero-W fallbacks to avoid NaN/Inf propagation.

Only standard-library modules are required.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union


TOOL_VERSION = "2.0"
DEFAULT_OUTPUT_SUFFIX = "_ue_custom_bridge"
DEFAULT_DATA_TEXTURE_WIDTH = 4096

INTERPOLATION_TOKENS = {
    "linear",
    "centroid",
    "nointerpolation",
    "noperspective",
    "sample",
}

RESOURCE_TYPES = (
    "Texture1D",
    "Texture1DArray",
    "Texture2D",
    "Texture2DArray",
    "Texture2DMS",
    "Texture2DMSArray",
    "Texture3D",
    "TextureCube",
    "TextureCubeArray",
    "Buffer",
    "StructuredBuffer",
    "ByteAddressBuffer",
    "RWTexture1D",
    "RWTexture1DArray",
    "RWTexture2D",
    "RWTexture2DArray",
    "RWTexture3D",
    "RWBuffer",
    "RWStructuredBuffer",
    "RWByteAddressBuffer",
)

TEXTURE_TYPES = {name for name in RESOURCE_TYPES if "Texture" in name}
BUFFER_TYPES = {name for name in RESOURCE_TYPES if "Buffer" in name}

BUFFER_OPCODE_TYPES = {
    "ld_structured_indexable": "StructuredBuffer",
    "ld_raw_indexable": "ByteAddressBuffer",
    "ld_typed_indexable": "Buffer",
    "ld_buffer_indexable": "Buffer",
}

PLACEHOLDER_RE = re.compile(
    r"^\s*"
    r"(?P<dest>[A-Za-z_]\w*\.[xyzw])"
    r"\s*=\s*"
    r"no_StructuredBufferName"
    r"\[no_srcAddressRegister\]"
    r"\.no_srcByteOffsetName\.swiz"
    r"\s*;\s*$"
)


# -----------------------------------------------------------------------------
# Basic text helpers
# -----------------------------------------------------------------------------


def read_text(path: Union[str, os.PathLike]) -> str:
    return Path(path).read_text(encoding="utf-8-sig")


def write_text(path: Union[str, os.PathLike], text: str) -> None:
    """Write UTF-8 text using an API compatible with RenderDoc's Python."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(str(path), "w", encoding="utf-8", newline="\n") as file:
        file.write(text)


def write_json(path: Union[str, os.PathLike], payload: Any) -> None:
    write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def mask_comments(text: str) -> str:
    """Replace comments with spaces while preserving character positions."""

    result = list(text)
    i = 0

    while i < len(text):
        next_ch = text[i + 1] if i + 1 < len(text) else ""

        if text[i] == "/" and next_ch == "/":
            result[i] = result[i + 1] = " "
            i += 2
            while i < len(text) and text[i] != "\n":
                result[i] = " "
                i += 1
            continue

        if text[i] == "/" and next_ch == "*":
            result[i] = result[i + 1] = " "
            i += 2
            while i < len(text):
                if text[i] == "*" and i + 1 < len(text) and text[i + 1] == "/":
                    result[i] = result[i + 1] = " "
                    i += 2
                    break
                if text[i] != "\n":
                    result[i] = " "
                i += 1
            continue

        i += 1

    return "".join(result)


def find_matching(text: str, open_pos: int, open_char: str, close_char: str) -> int:
    depth = 0

    for i in range(open_pos, len(text)):
        if text[i] == open_char:
            depth += 1
        elif text[i] == close_char:
            depth -= 1
            if depth == 0:
                return i

    raise ValueError(f"No matching {close_char!r} found")


def split_top_level(text: str) -> List[str]:
    """Split a comma-separated list without splitting nested expressions."""

    result: List[str] = []
    start = 0
    round_depth = 0
    square_depth = 0
    angle_depth = 0

    for i, ch in enumerate(text):
        if ch == "(":
            round_depth += 1
        elif ch == ")":
            round_depth -= 1
        elif ch == "[":
            square_depth += 1
        elif ch == "]":
            square_depth -= 1
        elif ch == "<":
            angle_depth += 1
        elif ch == ">":
            angle_depth = max(0, angle_depth - 1)
        elif ch == "," and round_depth == square_depth == angle_depth == 0:
            result.append(text[start:i].strip())
            start = i + 1

    tail = text[start:].strip()
    if tail:
        result.append(tail)

    return result


def replace_token(text: str, old: str, new: str) -> str:
    return re.sub(rf"\b{re.escape(old)}\b", new, text)


def parse_literal(text: str) -> str:
    value = text.strip()
    match = re.fullmatch(r"l\s*\(\s*(.*?)\s*\)", value)
    return match.group(1).strip() if match else value


def cast_int(text: str) -> str:
    value = parse_literal(text)
    if re.fullmatch(r"[+-]?(?:\d+|0[xX][0-9A-Fa-f]+)", value):
        return value
    return f"(int)({value})"


def remove_outer_indent(body: str) -> str:
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    indents = [len(line) - len(line.lstrip()) for line in lines if line.strip()]
    if indents:
        indent = min(indents)
        lines = [line[indent:] for line in lines]

    return "\n".join(lines)


def remove_final_void_return(body: str) -> str:
    # A decompiler may place the final `return;` on its own line or after
    # another statement. A UE Custom expression needs only the value-return
    # appended by this converter.
    return re.sub(r"\breturn\s*;", "", body).rstrip()


# -----------------------------------------------------------------------------
# Main function, parameters and declarations
# -----------------------------------------------------------------------------


def find_main_shell(source: str) -> Dict[str, str]:
    masked = mask_comments(source)
    match = re.search(r"(?:\[[^\]]+\]\s*)*\bvoid\s+main\s*\(", masked)

    if not match:
        raise ValueError("void main(...) was not found")

    open_paren = masked.find("(", match.start())
    close_paren = find_matching(masked, open_paren, "(", ")")
    open_brace = masked.find("{", close_paren)

    if open_brace < 0:
        raise ValueError("main body was not found")

    close_brace = find_matching(masked, open_brace, "{", "}")

    return {
        "prefix": source[: match.start()],
        "parameters": source[open_paren + 1 : close_paren],
        "body": source[open_brace + 1 : close_brace],
        "suffix": source[close_brace + 1 :],
    }


def parse_parameter(raw: str) -> Optional[Dict[str, Any]]:
    text = " ".join(raw.split())
    if not text or text == "void":
        return None

    if ":" in text:
        left, semantic = text.rsplit(":", 1)
    else:
        left, semantic = text, ""

    tokens = left.strip().split()
    direction = "in"
    interpolation: List[str] = []

    for candidate in ("inout", "out", "in"):
        if candidate in tokens:
            direction = candidate
            tokens.remove(candidate)
            break

    while tokens and tokens[0] in INTERPOLATION_TOKENS:
        interpolation.append(tokens.pop(0))

    if len(tokens) < 2:
        return {"raw": text}

    return {
        "name": tokens[-1],
        "type": " ".join(tokens[:-1]),
        "direction": direction,
        "semantic": semantic.strip(),
        "interpolation": interpolation,
    }


def parse_parameters(text: str) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for raw in split_top_level(text):
        parameter = parse_parameter(raw)
        if parameter:
            result.append(parameter)
    return result


def parse_hlsl_type(type_name: str) -> Optional[Tuple[str, int]]:
    match = re.fullmatch(
        r"(float|half|double|int|uint|bool)([1-4])?",
        "".join(type_name.split()),
    )
    if not match:
        return None
    return match.group(1), int(match.group(2) or "1")


def zero_value(type_name: str) -> str:
    info = parse_hlsl_type(type_name)
    if not info:
        return "0"

    base, count = info
    scalar = "0u" if base == "uint" else "false" if base == "bool" else "0.0" if base in {"float", "half", "double"} else "0"

    if count == 1:
        return scalar
    return f"{base}{count}({', '.join([scalar] * count)})"


def semantic_matches(semantic: str, base: str) -> bool:
    return re.fullmatch(rf"{re.escape(base.upper())}\d*", semantic.upper()) is not None


def target_index(semantic: str) -> Optional[int]:
    semantic = semantic.strip().upper()
    if semantic == "SV_TARGET":
        return 0
    match = re.fullmatch(r"SV_TARGET(\d+)", semantic)
    return int(match.group(1)) if match else None


# -----------------------------------------------------------------------------
# Resource reflection
# -----------------------------------------------------------------------------


def collect_bad_buffer_specs(body: str) -> Dict[int, Dict[str, Any]]:
    result: Dict[int, Dict[str, Any]] = {}

    for match in re.finditer(
        r"dcl_resource_structured\s+t(?P<slot>\d+)\s*,\s*(?P<stride>\d+)",
        body,
        re.IGNORECASE,
    ):
        result[int(match.group("slot"))] = {
            "type": "StructuredBuffer",
            "stride": int(match.group("stride")),
        }

    for match in re.finditer(
        r"dcl_resource_raw\s+t(?P<slot>\d+)",
        body,
        re.IGNORECASE,
    ):
        result.setdefault(
            int(match.group("slot")),
            {"type": "ByteAddressBuffer", "stride": None},
        )

    opcode_re = re.compile(
        r"(?P<opcode>"
        r"ld_structured_indexable|"
        r"ld_raw_indexable|"
        r"ld_typed_indexable|"
        r"ld_buffer_indexable"
        r")[^\n]*?\bt(?P<slot>\d+)(?:\.[xyzw]{1,4})?",
        re.IGNORECASE,
    )

    for match in opcode_re.finditer(body):
        slot = int(match.group("slot"))
        stride_match = re.search(r"stride\s*=\s*(\d+)", match.group(0), re.IGNORECASE)
        result.setdefault(
            slot,
            {
                "type": BUFFER_OPCODE_TYPES[match.group("opcode").lower()],
                "stride": int(stride_match.group(1)) if stride_match else None,
            },
        )

    return result


def type_byte_size(type_name: Optional[str]) -> Optional[int]:
    match = re.fullmatch(r"(?:float|half|int|uint)([1-4])?", str(type_name or "").strip())
    if not match:
        return None
    return int(match.group(1) or "1") * 4


def parse_resources(prefix: str, body: str) -> Dict[str, Any]:
    type_pattern = "|".join(sorted(RESOURCE_TYPES, key=len, reverse=True))
    resource_re = re.compile(
        rf"\b(?P<type>{type_pattern})\s*"
        r"(?:<\s*(?P<element>[^>]+?)\s*>)?\s+"
        r"(?P<name>[A-Za-z_]\w*)\s*:\s*"
        r"register\s*\(\s*(?P<kind>[tus])(?P<slot>\d+)\s*\)\s*;"
    )

    resources: List[Dict[str, Any]] = []
    declared_t_slots: Set[int] = set()

    for match in resource_re.finditer(mask_comments(prefix)):
        resource_type = match.group("type")
        slot = int(match.group("slot"))
        kind = match.group("kind")

        if kind == "t":
            declared_t_slots.add(slot)

        resources.append(
            {
                "name": match.group("name"),
                "type": resource_type,
                "elementType": (match.group("element") or "").strip() or None,
                "kind": kind,
                "slot": slot,
                "category": "texture" if resource_type in TEXTURE_TYPES else "buffer" if resource_type in BUFFER_TYPES else "resource",
                "stride": None,
            }
        )

    inferred = collect_bad_buffer_specs(body)

    for resource in resources:
        if resource["category"] == "buffer" and resource["slot"] in inferred:
            resource["stride"] = inferred[resource["slot"]].get("stride")

    for slot, spec in inferred.items():
        if slot in declared_t_slots:
            continue
        resources.append(
            {
                "name": f"t{slot}",
                "type": spec["type"],
                "elementType": None,
                "kind": "t",
                "slot": slot,
                "category": "buffer",
                "stride": spec.get("stride"),
            }
        )

    sampler_re = re.compile(
        r"\b(?P<type>SamplerState|SamplerComparisonState)\s+"
        r"(?P<name>[A-Za-z_]\w*)\s*:\s*"
        r"register\s*\(\s*s(?P<slot>\d+)\s*\)\s*;"
    )

    samplers = {
        match.group("name"): int(match.group("slot"))
        for match in sampler_re.finditer(mask_comments(prefix))
    }

    return {
        "textures": [item for item in resources if item["category"] == "texture"],
        "buffers": [item for item in resources if item["category"] == "buffer"],
        "samplers": samplers,
        "constantBuffers": parse_cbuffers(prefix),
    }


def parse_cbuffers(prefix: str) -> List[Dict[str, Any]]:
    masked = mask_comments(prefix)
    cbuffer_re = re.compile(
        r"\bcbuffer\s+(?P<name>[A-Za-z_]\w*)\s*:\s*"
        r"register\s*\(\s*b(?P<slot>\d+)\s*\)\s*\{"
    )
    member_re = re.compile(
        r"\b(?P<type>[A-Za-z_]\w*)\s+"
        r"(?P<name>[A-Za-z_]\w*)\s*"
        r"(?:\[\s*(?P<count>\d+)\s*\])?\s*;"
    )

    result: List[Dict[str, Any]] = []

    for match in cbuffer_re.finditer(masked):
        open_brace = masked.find("{", match.start())
        close_brace = find_matching(masked, open_brace, "{", "}")
        block = prefix[open_brace + 1 : close_brace]
        members = []

        for member in member_re.finditer(mask_comments(block)):
            members.append(
                {
                    "name": member.group("name"),
                    "type": member.group("type"),
                    "arrayCount": int(member.group("count")) if member.group("count") else None,
                }
            )

        result.append(
            {
                "name": match.group("name"),
                "slot": int(match.group("slot")),
                "members": members,
            }
        )

    return result


# -----------------------------------------------------------------------------
# Texture call conversion
# -----------------------------------------------------------------------------


def texture_input_name(slot: int) -> str:
    return f"T{slot}"


def safe_resource_value(element_type: Optional[str]) -> str:
    """Safe fallback with a non-zero last component."""

    info = parse_hlsl_type(element_type or "float4")
    if not info:
        return "float4(0.0, 0.0, 0.0, 1.0)"

    base, count = info
    one = "1u" if base == "uint" else "1" if base in {"int", "bool"} else "1.0"
    zero = "0u" if base == "uint" else "0" if base in {"int", "bool"} else "0.0"

    if count == 1:
        return one

    values = [zero] * count
    values[-1] = one
    return f"{base}{count}({', '.join(values)})"


def find_resource_calls(body: str, resource: Dict[str, Any]) -> List[Dict[str, Any]]:
    masked = mask_comments(body)
    pattern = re.compile(
        rf"\b{re.escape(resource['name'])}\s*\.\s*"
        r"(?P<operation>[A-Za-z_]\w*)\s*\("
    )
    calls: List[Dict[str, Any]] = []

    for match in pattern.finditer(masked):
        open_paren = masked.find("(", match.start())
        close_paren = find_matching(masked, open_paren, "(", ")")
        calls.append(
            {
                "start": match.start(),
                "end": close_paren + 1,
                "operation": match.group("operation"),
                "arguments": split_top_level(body[open_paren + 1 : close_paren]),
            }
        )

    return calls


def sampler_input_for_call(
    texture: Dict[str, Any],
    arguments: List[str],
    sampler_slots: Dict[str, int],
    texture_slots: Set[int],
) -> Tuple[str, Optional[int]]:
    if arguments:
        sampler_token = arguments[0].strip()
        slot = sampler_slots.get(sampler_token)
        if slot is not None and slot in texture_slots:
            return f"T{slot}Sampler", slot

    return f"T{texture['slot']}Sampler", texture["slot"]


def replace_texture_accesses(body: str, resources: Dict[str, Any]) -> Tuple[str, Set[int], Set[int], List[str]]:
    replacements: List[Tuple[int, int, str]] = []
    used_texture_slots: Set[int] = set()
    sampler_owner_slots: Set[int] = set()
    warnings: List[str] = []

    texture_slots = {
        item["slot"]
        for item in resources["textures"]
        if item["type"] == "Texture2D"
    }

    for texture in resources["textures"]:
        calls = find_resource_calls(body, texture)
        if not calls:
            continue

        for call in calls:
            operation = call["operation"]
            arguments = call["arguments"]

            if texture["type"] != "Texture2D":
                replacement = safe_resource_value(texture.get("elementType"))
                warnings.append(
                    f"{texture['name']}.{operation} was replaced by {replacement}"
                )
            else:
                input_name = texture_input_name(texture["slot"])
                sampler_name, sampler_owner = sampler_input_for_call(
                    texture,
                    arguments,
                    resources["samplers"],
                    texture_slots,
                )

                used_texture_slots.add(texture["slot"])
                if sampler_owner is not None:
                    sampler_owner_slots.add(sampler_owner)

                if operation == "Sample" and len(arguments) == 2:
                    replacement = f"Texture2DSample({input_name}, {sampler_name}, {arguments[1]})"
                elif operation == "SampleBias" and len(arguments) == 3:
                    replacement = f"Texture2DSampleBias({input_name}, {sampler_name}, {arguments[1]}, {arguments[2]})"
                elif operation == "SampleLevel" and len(arguments) == 3:
                    replacement = f"Texture2DSampleLevel({input_name}, {sampler_name}, {arguments[1]}, {arguments[2]})"
                elif operation == "SampleGrad" and len(arguments) == 4:
                    replacement = f"Texture2DSampleGrad({input_name}, {sampler_name}, {arguments[1]}, {arguments[2]}, {arguments[3]})"
                elif operation == "Load":
                    location = f"(int3)({arguments[0]})"

                    if len(arguments) == 1:
                        replacement = (f"{input_name}.Load({location})")
                    else:
                         replacement = (
                            f"{input_name}.Load("
                            f"{location}, "
                            f"(int2)({arguments[1]}))"
                        )
                else:
                    warnings.append(
                        f"Unsupported call left unchanged: {texture['name']}.{operation}"
                    )
                    continue

            replacements.append((call["start"], call["end"], replacement))

    result = body
    for start, end, replacement in sorted(replacements, reverse=True):
        result = result[:start] + replacement + result[end:]

    return result, used_texture_slots, sampler_owner_slots, warnings


# -----------------------------------------------------------------------------
# Constant-buffer conversion
# -----------------------------------------------------------------------------


def replace_cb_accesses(body: str, cbuffers: List[Dict[str, Any]]) -> Tuple[str, Set[int]]:
    member_to_slot: Dict[str, int] = {}

    for cb in cbuffers:
        for member in cb["members"]:
            if member["arrayCount"] is not None:
                member_to_slot[member["name"]] = cb["slot"]

    # Common decompiler naming fallback: cb0[index], cb1[index], ...
    for slot in range(32):
        if re.search(rf"\bcb{slot}\s*\[", body):
            member_to_slot.setdefault(f"cb{slot}", slot)

    if not member_to_slot:
        return body, set()

    name_pattern = "|".join(
        sorted((re.escape(name) for name in member_to_slot), key=len, reverse=True)
    )
    used_slots: Set[int] = set()
    result = body

    while True:
        masked = mask_comments(result)
        match = re.search(rf"\b(?P<name>{name_pattern})\s*\[", masked)
        if not match:
            break

        open_bracket = masked.find("[", match.start())
        close_bracket = find_matching(masked, open_bracket, "[", "]")
        expression = result[open_bracket + 1 : close_bracket].strip()
        slot = member_to_slot[match.group("name")]
        used_slots.add(slot)

        replacement = f"RDC_LOAD_CB({slot}, {cast_int(expression)})"
        result = result[: match.start()] + replacement + result[close_bracket + 1 :]

    return result, used_slots


# -----------------------------------------------------------------------------
# Decompiled buffer-instruction conversion
# -----------------------------------------------------------------------------


def parse_register_operand(text: str) -> Optional[Dict[str, str]]:
    match = re.fullmatch(
        r"\s*(?P<name>[A-Za-z_]\w*)(?:\.(?P<mask>[xyzw]{1,4}))?\s*",
        text,
    )
    if not match:
        return None
    return {"name": match.group("name"), "mask": match.group("mask") or "xyzw"}


def parse_bad_buffer_instruction(line: str) -> Optional[Dict[str, Any]]:
    match = re.match(
        r"^(?P<indent>\s*)"
        r"(?P<opcode>"
        r"ld_structured_indexable|"
        r"ld_raw_indexable|"
        r"ld_typed_indexable|"
        r"ld_buffer_indexable"
        r")",
        line,
        re.IGNORECASE,
    )
    if not match:
        return None

    opcode = match.group("opcode").lower()
    position = match.end()
    headers: List[str] = []

    while True:
        while position < len(line) and line[position].isspace():
            position += 1
        if position >= len(line) or line[position] != "(":
            break
        close_pos = find_matching(line, position, "(", ")")
        headers.append(line[position : close_pos + 1])
        position = close_pos + 1

    operands = split_top_level(line[position:].strip())
    expected = {
        "ld_structured_indexable": 4,
        "ld_raw_indexable": 3,
        "ld_typed_indexable": 3,
        "ld_buffer_indexable": 3,
    }[opcode]

    if len(operands) != expected:
        raise ValueError(f"Cannot parse buffer instruction: {line.strip()}")

    destination = parse_register_operand(operands[0])
    resource = parse_register_operand(operands[-1])
    if not destination or not resource:
        raise ValueError(f"Cannot parse buffer operands: {line.strip()}")

    slot_match = re.fullmatch(r"t(\d+)", resource["name"], re.IGNORECASE)
    if not slot_match:
        raise ValueError(f"Buffer register was not tN: {line.strip()}")

    stride_match = re.search(r"stride\s*=\s*(\d+)", "".join(headers), re.IGNORECASE)

    result: Dict[str, Any] = {
        "indent": match.group("indent"),
        "opcode": opcode,
        "bufferType": BUFFER_OPCODE_TYPES[opcode],
        "slot": int(slot_match.group(1)),
        "destination": destination["name"],
        "destinationMask": destination["mask"],
        "resourceMask": resource["mask"],
        "stride": int(stride_match.group(1)) if stride_match else None,
    }

    if opcode == "ld_structured_indexable":
        result["index"] = parse_literal(operands[1])
        result["byteOffset"] = parse_literal(operands[2])
    elif opcode == "ld_raw_indexable":
        result["byteOffset"] = parse_literal(operands[1])
    else:
        result["index"] = parse_literal(operands[1])

    return result


def buffer_byte_address(info: Dict[str, Any], spec: Dict[str, Any]) -> str:
    buffer_type = spec.get("type") or info["bufferType"]
    stride = info.get("stride") or spec.get("stride")

    if buffer_type == "StructuredBuffer":
        if not isinstance(stride, int):
            raise ValueError(f"t{info['slot']} StructuredBuffer stride is unknown")
        return (
            f"((int)({info['index']})) * {stride} + "
            f"((int)({info.get('byteOffset', '0')}))"
        )

    if buffer_type == "ByteAddressBuffer":
        return f"((int)({info['byteOffset']}))"

    if not isinstance(stride, int):
        stride = type_byte_size(spec.get("elementType"))
    if not isinstance(stride, int):
        raise ValueError(f"t{info['slot']} typed Buffer stride is unknown")

    return f"((int)({info['index']})) * {stride}"


def result_swizzle(info: Dict[str, Any]) -> str:
    count = len(info["destinationMask"])
    return (info["resourceMask"] + "xyzw")[:count]


def try_fuse_raw_consumer(
    next_line: str,
    destination: str,
    raw_expression: str,
    indent: str,
) -> Optional[str]:
    escaped = re.escape(destination)

    bitwise = re.fullmatch(
        rf"\s*{escaped}\s*=\s*"
        rf"(?:\(\s*(?:int|uint)\s*\)\s*)?{escaped}\s*"
        r"(?P<op>&|\||\^|<<|>>)\s*(?P<rhs>[^;]+)\s*;\s*",
        next_line,
    )
    if bitwise:
        op = bitwise.group("op")
        rhs = bitwise.group("rhs").strip()
        return f"{indent}{destination} = (float)({raw_expression} {op} (uint)({rhs}));"

    compare_zero = re.fullmatch(
        rf"\s*{escaped}\s*=\s*"
        rf"(?P<neg>-)?\(float\)\(\s*{escaped}\s*"
        r"(?P<op>==|!=)\s*0(?:\.0+)?\s*\)\s*;\s*",
        next_line,
    )
    if compare_zero:
        neg = "-" if compare_zero.group("neg") else ""
        op = compare_zero.group("op")
        return f"{indent}{destination} = {neg}(float)({raw_expression} {op} 0u);"

    return None


def replace_buffer_instructions(
    body: str,
    resources: Dict[str, Any],
) -> Tuple[str, Dict[int, Dict[str, Any]]]:
    specs = {item["slot"]: dict(item) for item in resources["buffers"]}
    lines = body.splitlines()
    output: List[str] = []
    used_specs: Dict[int, Dict[str, Any]] = {}
    i = 0

    while i < len(lines):
        info = parse_bad_buffer_instruction(lines[i])
        if info is None:
            output.append(lines[i])
            i += 1
            continue

        spec = specs.get(
            info["slot"],
            {
                "slot": info["slot"],
                "type": info["bufferType"],
                "stride": info.get("stride"),
                "elementType": None,
            },
        )
        if info.get("stride") and not spec.get("stride"):
            spec["stride"] = info["stride"]

        address = buffer_byte_address(info, spec)
        swizzle = result_swizzle(info)
        destination = f"{info['destination']}.{info['destinationMask']}"
        float_expr = f"RDC_T{info['slot']}_LOAD_F4({address}).{swizzle}"
        uint_expr = f"RDC_T{info['slot']}_LOAD_U4({address}).{swizzle}"

        # Skip the decompiler's placeholder assignments following the opcode.
        j = i + 1
        while j < len(lines) and PLACEHOLDER_RE.fullmatch(lines[j]):
            j += 1

        fused = None
        if len(info["destinationMask"]) == 1 and j < len(lines):
            fused = try_fuse_raw_consumer(
                lines[j],
                destination,
                uint_expr,
                info["indent"],
            )

        output.append(fused or f"{info['indent']}{destination} = {float_expr};")
        used_specs[info["slot"]] = spec
        i = j + 1 if fused else j

    transformed = "\n".join(output)

    residual = re.search(
        r"\b(?:ld_structured_indexable|ld_raw_indexable|ld_typed_indexable|ld_buffer_indexable)\b|"
        r"no_StructuredBufferName",
        transformed,
        re.IGNORECASE,
    )
    if residual:
        raise ValueError("Some decompiler buffer placeholders were not converted")

    return transformed, used_specs


# -----------------------------------------------------------------------------
# Varying and temporary-register conversion
# -----------------------------------------------------------------------------


def required_component_count(name: str, body: str, fallback_type: str) -> int:
    maximum = 0

    for match in re.finditer(rf"\b{re.escape(name)}\.([xyzw]{{1,4}})\b", body):
        for component in match.group(1):
            maximum = max(maximum, "xyzw".index(component) + 1)

    if maximum:
        return maximum

    parsed = parse_hlsl_type(fallback_type)
    return parsed[1] if parsed else 4


def adapt_inputs(
    body: str,
    parameters: List[Dict[str, Any]],
) -> Tuple[str, List[str], List[Dict[str, Any]]]:
    result = body
    setup_lines: List[str] = []
    layout: List[Dict[str, Any]] = []

    for parameter in parameters:
        if parameter.get("direction") == "out" or not parameter.get("name"):
            continue

        name = parameter["name"]
        semantic = parameter.get("semantic", "")
        type_name = parameter.get("type", "float4")

        if not re.search(rf"\b{re.escape(name)}\b", result):
            continue

        if semantic_matches(semantic, "SV_POSITION"):
            replacement = "RDC_SVPosition"
            if not any("RDC_SVPosition" in line for line in setup_lines):
                setup_lines.append("float4 RDC_SVPosition = Parameters.SvPosition;")

        elif semantic_matches(semantic, "VELOCITY_PREV_POS"):
            replacement = "RDC_PreviousPosition"
            if not any("RDC_SVPosition" in line for line in setup_lines):
                setup_lines.append("float4 RDC_SVPosition = Parameters.SvPosition;")
            if not any("RDC_PreviousPosition" in line for line in setup_lines):
                setup_lines.append("float4 RDC_PreviousPosition = RDC_SVPosition;")

        elif semantic_matches(semantic, "SV_ISFRONTFACE"):
            replacement = "RDC_IsFrontFace"
            setup_lines.append("uint RDC_IsFrontFace = (FrontFace > 0.0) ? 1u : 0u;")
            layout.append(
                {
                    "name": "FrontFace",
                    "inputType": "CMOT_Float1",
                    "semantic": semantic,
                }
            )

        else:
            replacement = name.upper()
            count = required_component_count(name, result, type_name)
            layout.append(
                {
                    "name": replacement,
                    "inputType": f"CMOT_Float{count}",
                    "semantic": semantic,
                    "sourceType": type_name,
                }
            )

        result = replace_token(result, name, replacement)

    return result, setup_lines, layout


def initialize_register_declarations(body: str) -> str:
    declaration_re = re.compile(
        r"^(?P<indent>\s*)"
        r"(?P<type>(?:float|half|double|int|uint|bool)[1-4]?)\s+"
        r"(?P<vars>[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)+)\s*;\s*$"
    )

    output: List[str] = []

    for line in body.splitlines():
        match = declaration_re.fullmatch(line)
        if not match:
            output.append(line)
            continue

        names = [item.strip() for item in match.group("vars").split(",")]
        if not all(re.fullmatch(r"r\d+|bitmask|uiDest|fDest", name) for name in names):
            output.append(line)
            continue

        initial = zero_value(match.group("type"))
        declarations = ", ".join(f"{name} = {initial}" for name in names)
        output.append(f"{match.group('indent')}{match.group('type')} {declarations};")

    return "\n".join(output)


def clean_decompiler_comments(body: str) -> str:
    lines = body.splitlines()
    output: List[str] = []

    for line in lines:
        if re.search(r"dcl_resource_(?:structured|raw|typed|buffer)", line, re.IGNORECASE):
            continue
        if re.search(r"Needs manual fix|unknown dcl_|Known bad code|Missing reflection info", line, re.IGNORECASE):
            continue
        output.append(line)

    return "\n".join(output)


# -----------------------------------------------------------------------------
# Generated HLSL preamble and footer
# -----------------------------------------------------------------------------


def extract_source_defines(prefix: str) -> List[str]:
    lines = prefix.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    result: List[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        if not re.match(r"^\s*#\s*define\b", line):
            i += 1
            continue

        if re.match(r"^\s*#\s*define\s+cmp\b", line):
            i += 1
            continue

        result.append(line.strip())
        while line.rstrip().endswith("\\") and i + 1 < len(lines):
            i += 1
            line = lines[i]
            result.append(line.rstrip())
        i += 1

    return result


def build_data_macros(
    cb_slots: Set[int],
    buffer_specs: Dict[int, Dict[str, Any]],
    data_width: int,
) -> List[str]:
    if not cb_slots and not buffer_specs:
        return []

    lines = [
        "// RGBA8 raw-byte decoding shared by CBData and TNData.",
        (
            "#define RDC_LOAD_RGBA8_U32(TEX, X, Y) "
            "(((uint)round(saturate((TEX).Load(int3((X), (Y), 0)).r) * 255.0)) | "
            "(((uint)round(saturate((TEX).Load(int3((X), (Y), 0)).g) * 255.0)) << 8) | "
            "(((uint)round(saturate((TEX).Load(int3((X), (Y), 0)).b) * 255.0)) << 16) | "
            "(((uint)round(saturate((TEX).Load(int3((X), (Y), 0)).a) * 255.0)) << 24))"
        ),
    ]

    if cb_slots:
        lines.extend(
            [
                "#define RDC_LOAD_CB_SCALAR(ROW, PIXEL_X) asfloat(RDC_LOAD_RGBA8_U32(CBData, (PIXEL_X), (ROW)))",
                (
                    "#define RDC_LOAD_CB(ROW, INDEX) float4("
                    "RDC_LOAD_CB_SCALAR((ROW), (INDEX) * 4 + 0), "
                    "RDC_LOAD_CB_SCALAR((ROW), (INDEX) * 4 + 1), "
                    "RDC_LOAD_CB_SCALAR((ROW), (INDEX) * 4 + 2), "
                    "RDC_LOAD_CB_SCALAR((ROW), (INDEX) * 4 + 3))"
                ),
            ]
        )

    if buffer_specs:
        lines.extend(
            [
                f"#define RDC_DATA_TEXTURE_WIDTH {data_width}",
                "#define RDC_DATA_PIXEL_INDEX(BYTE_ADDRESS) (((int)(BYTE_ADDRESS)) >> 2)",
                "#define RDC_DATA_PIXEL_X(BYTE_ADDRESS) (RDC_DATA_PIXEL_INDEX(BYTE_ADDRESS) % RDC_DATA_TEXTURE_WIDTH)",
                "#define RDC_DATA_PIXEL_Y(BYTE_ADDRESS) (RDC_DATA_PIXEL_INDEX(BYTE_ADDRESS) / RDC_DATA_TEXTURE_WIDTH)",
            ]
        )

        for slot in sorted(buffer_specs):
            lines.extend(
                [
                    f"#define RDC_T{slot}_LOAD_U32(BYTE_ADDRESS) RDC_LOAD_RGBA8_U32(T{slot}Data, RDC_DATA_PIXEL_X(BYTE_ADDRESS), RDC_DATA_PIXEL_Y(BYTE_ADDRESS))",
                    (
                        f"#define RDC_T{slot}_LOAD_U4(BYTE_ADDRESS) uint4("
                        f"RDC_T{slot}_LOAD_U32((BYTE_ADDRESS) + 0), "
                        f"RDC_T{slot}_LOAD_U32((BYTE_ADDRESS) + 4), "
                        f"RDC_T{slot}_LOAD_U32((BYTE_ADDRESS) + 8), "
                        f"RDC_T{slot}_LOAD_U32((BYTE_ADDRESS) + 12))"
                    ),
                    f"#define RDC_T{slot}_LOAD_F4(BYTE_ADDRESS) asfloat(RDC_T{slot}_LOAD_U4(BYTE_ADDRESS))",
                ]
            )

    return lines


def build_output_declarations(outputs: List[Dict[str, Any]]) -> List[str]:
    declarations = []
    for output in outputs:
        if output.get("name"):
            type_name = output.get("type", "float4")
            declarations.append(f"{type_name} {output['name']} = {zero_value(type_name)};")
    return declarations


def build_surface_footer(outputs: List[Dict[str, Any]]) -> Tuple[List[str], Dict[str, Any]]:
    targets: Dict[int, str] = {}

    for output in outputs:
        index = target_index(output.get("semantic", ""))
        if index is not None and output.get("name"):
            targets[index] = output["name"]

    normal = targets.get(1)
    mask = targets.get(2)
    base_color = targets.get(3)
    lines = ["", "// Surface outputs"]

    if normal:
        lines.extend(
            [
                f"float2 RDC_Oct = {normal}.xy * 2.0 - 1.0;",
                "float3 RDC_DecodedNormal = float3(RDC_Oct.x, RDC_Oct.y, 1.0 - abs(RDC_Oct.x) - abs(RDC_Oct.y));",
                "if (RDC_DecodedNormal.z < 0.0)",
                "{",
                "    float2 RDC_OctSign = float2(",
                "        RDC_DecodedNormal.x >= 0.0 ? 1.0 : -1.0,",
                "        RDC_DecodedNormal.y >= 0.0 ? 1.0 : -1.0);",
                "    RDC_DecodedNormal.xy = (1.0 - abs(RDC_DecodedNormal.yx)) * RDC_OctSign;",
                "}",
                "Normal = normalize(RDC_DecodedNormal);",
            ]
        )
    else:
        lines.append("Normal = float3(0.0, 0.0, 1.0);")

    lines.append(f"Mask = {mask}.xyz;" if mask else "Mask = float3(0.0, 0.5, 0.5);")
    lines.append(f"return {base_color}.xyz;" if base_color else "return float3(0.0, 0.0, 0.0);")

    return lines, {
        "baseColorSource": base_color,
        "normalSource": normal,
        "maskSource": mask,
    }


def assemble_hlsl(
    source_defines: List[str],
    data_macros: List[str],
    output_declarations: List[str],
    setup_lines: List[str],
    body: str,
    footer: List[str],
) -> str:
    lines = [
        "// RenderDoc -> UE Custom Node HLSL",
        f"// Generated by rdc_custom_material_rewriter.py {TOOL_VERSION}",
        "",
        "#define cmp -",
    ]

    if source_defines:
        lines.extend(source_defines)

    if data_macros:
        lines.extend(["", *data_macros])

    lines.extend(["", *output_declarations])

    if setup_lines:
        lines.extend(["", *setup_lines])

    lines.extend(["", body.rstrip(), *footer, "", "#undef cmp"])

    if data_macros:
        lines.append("#undef RDC_LOAD_RGBA8_U32")

    return "\n".join(lines).rstrip() + "\n"


# -----------------------------------------------------------------------------
# Validation and layout
# -----------------------------------------------------------------------------


def validate_hlsl(hlsl: str) -> None:
    forbidden = {
        "decompiler buffer opcode": r"\b(?:ld_structured_indexable|ld_raw_indexable|ld_typed_indexable|ld_buffer_indexable)\b",
        "decompiler buffer placeholder": r"no_StructuredBufferName",
        "dynamic BufferWidth pin": r"\bBufferWidthT\d+\b",
        "old BufferData input": r"\bBufferDataT\d+\b",
        "void return": r"\breturn\s*;",
    }

    failures = [name for name, pattern in forbidden.items() if re.search(pattern, hlsl)]
    if not re.search(r"\breturn\s+[^;]+;", hlsl):
        failures.append("return statement")

    if failures:
        raise ValueError("Generated HLSL validation failed: " + ", ".join(failures))


def build_layout(
    cb_slots: Set[int],
    buffer_specs: Dict[int, Dict[str, Any]],
    texture_slots: Set[int],
    varying_inputs: List[Dict[str, Any]],
    surface: Dict[str, Any],
    data_width: int,
) -> Dict[str, Any]:
    inputs: List[Dict[str, Any]] = []

    if cb_slots:
        inputs.append(
            {
                "name": "CBData",
                "inputType": "TextureObject",
                "packing": "one_uint32_per_rgba8_pixel; four_pixels_per_float4",
            }
        )

    for slot in sorted(buffer_specs):
        inputs.append(
            {
                "name": f"T{slot}Data",
                "inputType": "TextureObject",
                "register": f"t{slot}",
                "dataTextureWidth": data_width,
                "packing": "one_uint32_per_rgba8_pixel",
                "bufferType": buffer_specs[slot].get("type"),
                "stride": buffer_specs[slot].get("stride"),
            }
        )

    for slot in sorted(texture_slots):
        inputs.append(
            {
                "name": f"T{slot}",
                "inputType": "TextureObject",
                "register": f"t{slot}",
            }
        )

    inputs.extend(varying_inputs)

    return {
        "version": TOOL_VERSION,
        "generatedHlsl": "ue_custom_shader.hlsl",
        "customInputs": inputs,
        "mainOutput": {"name": "BaseColor", "type": "CMOT_Float3"},
        "additionalOutputs": [
            {"name": "Normal", "type": "CMOT_Float3"},
            {"name": "Mask", "type": "CMOT_Float3"},
        ],
        "surfaceMapping": surface,
        "textureSettings": {
            "CBData": {"sRGB": False, "mipmaps": False, "filter": "Nearest"},
            "TNData": {"sRGB": False, "mipmaps": False, "filter": "Nearest"},
        },
        "notes": [
            "No BufferWidthTN scalar input is required.",
            f"All raw data textures are addressed with compile-time width {data_width}.",
        ],
    }


# -----------------------------------------------------------------------------
# Conversion pipeline
# -----------------------------------------------------------------------------


def convert_shader(
    input_path: str,
    output_dir: Optional[str] = None,
    data_width: int = DEFAULT_DATA_TEXTURE_WIDTH,
) -> Dict[str, Any]:
    if data_width <= 0:
        raise ValueError("data_width must be greater than zero")

    input_file = Path(input_path).resolve()
    output_path = (
        Path(output_dir).resolve()
        if output_dir
        else input_file.with_name(input_file.stem + DEFAULT_OUTPUT_SUFFIX)
    )
    output_path.mkdir(parents=True, exist_ok=True)

    source = read_text(input_file)
    shell = find_main_shell(source)
    parameters = parse_parameters(shell["parameters"])
    resources = parse_resources(shell["prefix"], shell["body"])

    body = remove_final_void_return(remove_outer_indent(shell["body"]))
    body = clean_decompiler_comments(body)

    body, used_texture_slots, sampler_owner_slots, texture_warnings = replace_texture_accesses(
        body,
        resources,
    )
    body, buffer_specs = replace_buffer_instructions(body, resources)
    body, cb_slots = replace_cb_accesses(body, resources["constantBuffers"])

    input_parameters = [item for item in parameters if item.get("direction") != "out"]
    output_parameters = [item for item in parameters if item.get("direction") == "out"]

    body, setup_lines, varying_inputs = adapt_inputs(body, input_parameters)
    body = initialize_register_declarations(body)

    texture_slots = used_texture_slots | sampler_owner_slots
    source_defines = extract_source_defines(shell["prefix"])
    data_macros = build_data_macros(cb_slots, buffer_specs, data_width)
    output_declarations = build_output_declarations(output_parameters)
    footer, surface = build_surface_footer(output_parameters)

    final_hlsl = assemble_hlsl(
        source_defines,
        data_macros,
        output_declarations,
        setup_lines,
        body,
        footer,
    )
    validate_hlsl(final_hlsl)

    layout = build_layout(
        cb_slots,
        buffer_specs,
        texture_slots,
        varying_inputs,
        surface,
        data_width,
    )

    hlsl_path = output_path / "ue_custom_shader.hlsl"
    layout_path = output_path / "ue_custom_layout.json"
    write_text(hlsl_path, final_hlsl)
    write_json(layout_path, layout)

    warnings = list(texture_warnings)
    if shell["suffix"].strip():
        warnings.append("Source text after main() was not merged into the Custom node body")

    print(f"[RDC2UE] HLSL:   {hlsl_path}")
    print(f"[RDC2UE] Layout: {layout_path}")
    print(f"[RDC2UE] Data texture width: {data_width} (compile-time constant)")
    print("[RDC2UE] BufferWidthTN pins: not generated")

    for warning in warnings:
        print(f"[RDC2UE][warn] {warning}")

    return {
        "hlsl": str(hlsl_path),
        "layout": str(layout_path),
        "warnings": warnings,
    }



def convert_hlsl_file(
    input_hlsl_path: str,
    output_dir: str,
    data_width: int = DEFAULT_DATA_TEXTURE_WIDTH,
) -> Dict[str, Any]:
    """Stable API used by rdc2ue_exporter when both files share one package."""

    return convert_shader(
        input_hlsl_path,
        output_dir,
        data_width,
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert a decompiled pixel shader to UE4 Custom-node HLSL."
    )
    parser.add_argument("input_hlsl", help="Input decompiled HLSL file")
    parser.add_argument("output_dir", nargs="?", help="Optional output directory")
    parser.add_argument(
        "--data-width",
        type=int,
        default=DEFAULT_DATA_TEXTURE_WIDTH,
        help=f"Raw CB/Buffer data texture width (default: {DEFAULT_DATA_TEXTURE_WIDTH})",
    )
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()

    try:
        convert_hlsl_file(
            args.input_hlsl,
            args.output_dir,
            args.data_width,
        )
        return 0
    except Exception as exc:
        print(f"[RDC2UE][error] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
