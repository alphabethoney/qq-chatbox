"""生成一个 NTQQ 风格的**明文** SQLite 样本库，用于离线自测。

表结构、列名（协议字段号）都模仿真实 nt_msg.db，消息体用真实的
Protobuf wire 编码，因此可以完整验证「读取 -> 解析 -> 归类 -> 导出」链路。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


# ---- 极简 Protobuf 编码器 -------------------------------------------------


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def tag(number: int, wire_type: int) -> bytes:
    return varint((number << 3) | wire_type)


def ld(number: int, payload: bytes) -> bytes:
    """length-delimited 字段。"""
    return tag(number, 2) + varint(len(payload)) + payload


def vi(number: int, value: int) -> bytes:
    """varint 字段。"""
    return tag(number, 0) + varint(value)


def encode_body(text: str, content_type: int = 1) -> bytes:
    """构造一条消息体：MsgBody { repeated MsgContent }，文本在 45101。"""
    content = ld(45101, text.encode("utf-8")) + vi(45002, content_type)
    return ld(1, content)


SCHEMA = """
CREATE TABLE c2c_msg_table (
    "40001" INTEGER, "40050" INTEGER, "40013" INTEGER,
    "40020" TEXT, "40033" INTEGER, "40021" TEXT, "40030" INTEGER,
    "40011" INTEGER, "40012" INTEGER, "40800" BLOB
);
CREATE TABLE group_msg_table (
    "40001" INTEGER, "40050" INTEGER, "40013" INTEGER,
    "40020" TEXT, "40033" INTEGER, "40021" TEXT, "40030" INTEGER,
    "40011" INTEGER, "40012" INTEGER, "40800" BLOB
);
"""

C2C_ROWS = [
    (1001, 1737000000, 0, "u_alice", 10001, "u_me", 88888888, 2, 0, encode_body("你好，在吗？")),
    (1002, 1737000060, 1, "u_me", 88888888, "u_alice", 10001, 2, 0, encode_body("在的，怎么了")),
    (1003, 1737000120, 0, "u_alice", 10001, "u_me", 88888888, 2, 0, encode_body("周末一起吃饭？")),
    (1004, 1737000600, 0, "u_bob", 10002, "u_me", 88888888, 2, 0, encode_body("文件发你了")),
    (1005, 1737000700, 1, "u_me", 88888888, "u_bob", 10002, 2, 0, encode_body("收到，谢谢！")),
]

GROUP_ROWS = [
    (2001, 1737100000, 0, "u_carol", 10003, "123456", 123456, 2, 0, encode_body("大家好")),
    (2002, 1737100100, 1, "u_me", 88888888, "123456", 123456, 2, 0, encode_body("欢迎新人")),
    (2003, 1737100200, 0, "u_dave", 10004, "123456", 123456, 2, 0, encode_body("今天的会议改到三点")),
]


def build(path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            'INSERT INTO c2c_msg_table VALUES (?,?,?,?,?,?,?,?,?,?)', C2C_ROWS
        )
        conn.executemany(
            'INSERT INTO group_msg_table VALUES (?,?,?,?,?,?,?,?,?,?)', GROUP_ROWS
        )
        conn.commit()
    finally:
        conn.close()
    return path


if __name__ == "__main__":
    import sys

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("_work/fixture_plain.db")
    print(build(target))
