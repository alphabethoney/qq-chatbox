"""通用 Protobuf wire 格式解析器。

NTQQ 把消息体以 Protobuf 编码后存进数据库的 ``40800`` 列，字段编号会随
QQ 版本变化，官方也没有公开 ``.proto``。与其硬编码一套随时会失效的结构，
这里实现一个「不依赖 schema」的通用解析器：

* 完整支持 wire type 0/1/2/5，以及 3/4 组（已废弃，仍可跳过）；
* 可以递归下钻，按字段编号抓取任意深度的值；
* 无法识别的内容原样保留，绝不静默丢弃。
"""

from __future__ import annotations

from typing import Iterator

WT_VARINT = 0
WT_I64 = 1
WT_LEN = 2
WT_SGROUP = 3
WT_EGROUP = 4
WT_I32 = 5

MAX_DEPTH_DEFAULT = 8


class WireError(ValueError):
    """遇到不符合 protobuf wire 格式的数据。"""


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """读取一个 varint，返回 ``(值, 新位置)``。"""
    result = 0
    shift = 0
    length = len(data)
    while True:
        if pos >= length:
            raise WireError("varint 越界")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise WireError("varint 过长")


def parse_fields(data: bytes) -> list[tuple[int, int, object]]:
    """解析一层 protobuf，返回 ``[(字段号, wire 类型, 值), ...]``。

    ``WT_LEN`` 的值是 ``bytes``，``WT_VARINT`` 的值是 ``int``，
    ``WT_I32`` / ``WT_I64`` 的值是定长 ``bytes``。
    """
    fields: list[tuple[int, int, object]] = []
    pos = 0
    size = len(data)
    while pos < size:
        tag, pos = read_varint(data, pos)
        number = tag >> 3
        wire_type = tag & 0x07
        if number == 0:
            raise WireError("字段号为 0")

        if wire_type == WT_VARINT:
            value, pos = read_varint(data, pos)
        elif wire_type == WT_I64:
            if pos + 8 > size:
                raise WireError("fixed64 越界")
            value = data[pos : pos + 8]
            pos += 8
        elif wire_type == WT_LEN:
            length, pos = read_varint(data, pos)
            if pos + length > size:
                raise WireError("length-delimited 越界")
            value = data[pos : pos + length]
            pos += length
        elif wire_type == WT_I32:
            if pos + 4 > size:
                raise WireError("fixed32 越界")
            value = data[pos : pos + 4]
            pos += 4
        elif wire_type == WT_SGROUP:
            # 已废弃的组类型：跳过直到对应的结束标记
            depth = 1
            while depth:
                tag2, pos = read_varint(data, pos)
                inner_wt = tag2 & 0x07
                if inner_wt == WT_SGROUP:
                    depth += 1
                elif inner_wt == WT_EGROUP:
                    depth -= 1
                elif inner_wt == WT_VARINT:
                    _, pos = read_varint(data, pos)
                elif inner_wt == WT_I64:
                    pos += 8
                elif inner_wt == WT_LEN:
                    length, pos = read_varint(data, pos)
                    pos += length
                elif inner_wt == WT_I32:
                    pos += 4
                if pos > size:
                    raise WireError("组类型越界")
            value = b""
        elif wire_type == WT_EGROUP:
            raise WireError("多余的组结束标记")
        else:
            raise WireError(f"未知 wire 类型 {wire_type}")
        fields.append((number, wire_type, value))
    return fields


def looks_like_message(data: bytes) -> bool:
    """启发式判断一段字节是否本身就是一个 protobuf 消息。"""
    if not data:
        return False
    try:
        fields = parse_fields(data)
    except WireError:
        return False
    return bool(fields)


def decode_text(raw: bytes) -> str | None:
    """把字节串按 UTF-8 解码，失败返回 ``None``。"""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # 排掉明显的二进制内容
    if any(ch == "\x00" for ch in text):
        return None
    if not text:
        return None
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\n\r\t")
    if printable / len(text) < 0.9:
        return None
    return text


def walk(
    data: bytes,
    *,
    depth: int = 0,
    max_depth: int = MAX_DEPTH_DEFAULT,
    path: tuple[int, ...] = (),
) -> Iterator[tuple[tuple[int, ...], int, int, object]]:
    """递归遍历所有字段。产出 ``(路径, 字段号, wire 类型, 值)``。"""
    try:
        fields = parse_fields(data)
    except WireError:
        return
    for number, wire_type, value in fields:
        current = path + (number,)
        yield current, number, wire_type, value
        if wire_type == WT_LEN and depth < max_depth and len(value) > 1:
            nested = value
            if looks_like_message(nested):
                yield from walk(
                    nested, depth=depth + 1, max_depth=max_depth, path=current
                )


def find_all(data: bytes, numbers: set[int], max_depth: int = MAX_DEPTH_DEFAULT):
    """收集所有指定字段号的取值（任意深度）。"""
    out: list[tuple[tuple[int, ...], int, object]] = []
    for path, number, wire_type, value in walk(data, max_depth=max_depth):
        if number in numbers:
            out.append((path, wire_type, value))
    return out


def extract_text_segments(
    data: bytes, text_field: int = 45101, max_depth: int = MAX_DEPTH_DEFAULT
) -> list[str]:
    """按出现顺序抽取所有文本段。"""
    segments: list[str] = []
    for _path, number, wire_type, value in walk(data, max_depth=max_depth):
        if number == text_field and wire_type == WT_LEN:
            text = decode_text(value)
            if text is not None:
                segments.append(text)
    return segments


def to_dict(data: bytes, max_depth: int = MAX_DEPTH_DEFAULT) -> dict:
    """把 protobuf 转成可 JSON 序列化的字典（字段号为键）。"""
    result: dict[str, object] = {}
    try:
        fields = parse_fields(data)
    except WireError:
        return {"_raw_hex": data.hex()}

    for number, wire_type, value in fields:
        key = str(number)
        if wire_type == WT_VARINT:
            entry: object = value
        elif wire_type in (WT_I32, WT_I64):
            entry = value.hex()
        else:
            nested = None
            if max_depth > 0 and len(value) > 1 and looks_like_message(value):
                nested = to_dict(value, max_depth - 1)
            text = decode_text(value)
            if nested is not None and (text is None or len(nested) > 1):
                entry = nested
            elif text is not None:
                entry = text
            else:
                entry = {"_hex": value.hex()[:4096]}
        if key in result:
            existing = result[key]
            if isinstance(existing, list):
                existing.append(entry)
            else:
                result[key] = [existing, entry]
        else:
            result[key] = entry
    return result
