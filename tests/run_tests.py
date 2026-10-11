"""自测套件（不依赖 pytest，直接运行）：

    python tests/run_tests.py

覆盖：
1. Protobuf wire 解析器；
2. SQLCipher 解密器 + 保留区自动探测（构造真实格式的加密文件做往返）；
3. 「读取 -> 归类 -> 导出」链路（用 NTQQ 风格的明文样本库）。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: E402

from qq_exporter import sqlcipher, wire  # noqa: E402
from qq_exporter.collect import build_conversations  # noqa: E402
from qq_exporter.exporters import export_json, export_txt  # noqa: E402
from make_fixture import build as build_fixture  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [OK]   {name}")
    else:
        FAILED.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


# --------------------------------------------------------------------------
# 1. wire 解析
# --------------------------------------------------------------------------


def test_wire() -> None:
    print("\n== Protobuf wire 解析 ==")

    def varint(v: int) -> bytes:
        out = bytearray()
        while True:
            b = v & 0x7F
            v >>= 7
            if v:
                out.append(b | 0x80)
            else:
                out.append(b)
                return bytes(out)

    # varint 边界
    check("varint 0", wire.read_varint(varint(0), 0)[0] == 0)
    check("varint 127", wire.read_varint(varint(127), 0)[0] == 127)
    check("varint 128", wire.read_varint(varint(128), 0)[0] == 128)
    check("varint 2^32", wire.read_varint(varint(2**32), 0)[0] == 2**32)
    check("varint 2^63", wire.read_varint(varint(2**63), 0)[0] == 2**63)

    # 嵌套 + 文本抽取
    text_field = 45101
    sample_text = "你好"
    content = (
        varint((text_field << 3) | 2)
        + varint(len(sample_text.encode()))
        + sample_text.encode()
    )
    body = varint((1 << 3) | 2) + varint(len(content)) + content
    check("嵌套文本抽取", wire.extract_text_segments(body) == [sample_text])

    # 多层嵌套
    inner = varint((text_field << 3) | 2) + varint(2) + b"hi"
    mid = varint((7 << 3) | 2) + varint(len(inner)) + inner
    outer = varint((3 << 3) | 2) + varint(len(mid)) + mid
    check("深层嵌套文本抽取", wire.extract_text_segments(outer, max_depth=8) == ["hi"])

    # 二进制内容不应被当成文本
    blob = varint((9 << 3) | 2) + varint(4) + b"\x00\x01\x02\x03"
    check("二进制不被当文本", wire.decode_text(b"\x00\x01\x02\x03") is None)
    check("非法数据抛错", _raises(lambda: wire.parse_fields(b"\xff\xff\xff")))

    # 多个同名 repeated 字段
    rep = b"".join(
        varint((text_field << 3) | 2) + varint(1) + s for s in (b"a", b"b", b"c")
    )
    check("repeated 字段顺序", wire.extract_text_segments(rep) == ["a", "b", "c"])

    # fixed32 / fixed64 / 未知长度
    misc = varint((1 << 3) | 5) + b"\x01\x02\x03\x04" + varint((2 << 3) | 1) + b"\x00" * 8
    fields = wire.parse_fields(misc)
    check("fixed32/fixed64 解析", len(fields) == 2 and fields[0][1] == wire.WT_I32)

    # to_dict 可序列化
    try:
        json.dumps(wire.to_dict(body), ensure_ascii=False)
        check("to_dict 可 JSON 序列化", True)
    except TypeError as exc:
        check("to_dict 可 JSON 序列化", False, repr(exc))


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:  # noqa: BLE001
        return True
    return False


# --------------------------------------------------------------------------
# 2. SQLCipher 往返
# --------------------------------------------------------------------------


def build_encrypted(
    plain_path: Path, enc_path: Path, key: bytes, reserve: int = 48
) -> bytes:
    """把一份明文页数据封装成 NTQQ 格式的 SQLCipher 文件。"""
    plain = plain_path.read_bytes()
    page_size = sqlcipher.PAGE_SIZE
    assert len(plain) % page_size == 0
    salt = os.urandom(16)
    out = bytearray()
    out += b"SQLite header 3\x00"
    out += struct.pack("<H", 4) + struct.pack("<I", 16) + b"\x00" * 10
    payload = (
        b"\x12\x07ABCDEFG"
        + b"\x1a\x071.1.0.1"
        + b"\x22\x09HMAC_SHA1"
        + b"\x28\x01"
    )
    out += b"QQ_NT DB" + struct.pack("<I", len(payload)) + payload
    out += b"\x00" * (sqlcipher.EXT_HEADER - len(out))

    pages = len(plain) // page_size
    for pgno in range(1, pages + 1):
        page = plain[(pgno - 1) * page_size : pgno * page_size]
        skip = 16 if pgno == 1 else 0
        usable = page_size - reserve - skip
        usable -= usable % 16
        iv = os.urandom(16)
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        ct = encryptor.update(page[skip : skip + usable]) + encryptor.finalize()
        hmac_key = sqlcipher.derive_hmac_key(key, salt)
        import hmac as hmac_mod

        mac = hmac_mod.new(
            hmac_key,
            ct + iv + struct.pack("<I", pgno),
            hashlib.sha1,
        ).digest()[: sqlcipher.HMAC_SIZE]
        frame = bytearray()
        frame += salt if pgno == 1 else b""
        frame += ct
        frame += iv
        frame += mac
        frame += b"\x00" * (page_size - len(frame))
        out += bytes(frame[:page_size])
    enc_path.write_bytes(bytes(out))
    return salt


def test_crypto(tmp: Path) -> None:
    print("\n== SQLCipher 解密与参数探测 ==")

    page_size = sqlcipher.PAGE_SIZE
    # 造 3 页明文。注意：加密只覆盖每页前 (page_size - reserve) 字节，
    # 所以每页末尾 48 字节必须为 0，往返才能逐字节相等。
    p1 = bytearray(page_size)
    p1[0:16] = sqlcipher.SQLITE_MAGIC
    p1[16:18] = struct.pack(">H", page_size)
    p1[18] = 1
    p1[19] = 1
    p1[20] = 48
    p1[21] = 64
    p1[22] = 32
    p1[23] = 32
    p1[44:48] = struct.pack(">I", 4)
    p1[56:60] = struct.pack(">I", 1)
    p1[100] = 0x0D
    p1[103:105] = struct.pack(">H", 2)
    p1[120:180] = (b"CREATE TABLE t(a INTEGER);" * 2).ljust(60, b" ")
    p1[4000:4048] = b"Z" * 48
    pages = [bytes(p1)]
    for i in range(2, 4):
        p = bytearray(page_size)
        p[0] = 0x0D
        p[100:4000] = bytes(((i * 37 + j) % 251 for j in range(3900)))
        pages.append(bytes(p))
    plain = b"".join(pages)

    plain_path = tmp / "plain_pages.bin"
    plain_path.write_bytes(plain)
    key = os.urandom(32)
    enc_path = tmp / "fake_nt_msg.db"
    build_encrypted(plain_path, enc_path, key, reserve=48)

    check("识别为 NTQQ 库", sqlcipher.is_ntqq_database(enc_path))
    page1 = sqlcipher.read_page1_raw(enc_path)
    check("盐值读取", len(sqlcipher.extract_salt(enc_path)) == 16)
    check("HMAC 校验（正确密钥）", sqlcipher.verify_page1_hmac(page1, key))
    check("HMAC 校验（错误密钥）", not sqlcipher.verify_page1_hmac(page1, os.urandom(32)))
    check("保留区自动探测", sqlcipher.detect_reserve(page1, key) == 48)
    check("错误密钥->探测失败", sqlcipher.detect_reserve(page1, os.urandom(32)) is None)

    out = tmp / "decrypted.db"
    stats = sqlcipher.decrypt_database(enc_path, out, key)
    check("解密页数", stats["pages"] == 3, str(stats))
    check("解密保留区", stats["reserve"] == 48, str(stats["reserve"]))

    recovered = out.read_bytes()
    check("解密内容与原文一致", recovered == plain, f"len {len(recovered)} vs {len(plain)}")

    # 头部元数据
    meta = sqlcipher.read_header_meta(enc_path)
    check("头部 HMAC 算法", meta["hmac_algorithm"] == "HMAC_SHA1", str(meta))
    check("头部格式版本", meta["format_version"] == "1.1.0.1", str(meta))


# --------------------------------------------------------------------------
# 3. 导出链路
# --------------------------------------------------------------------------


def test_pipeline(tmp: Path) -> None:
    print("\n== 导出链路 ==")
    from make_fixture import C2C_ROWS, GROUP_ROWS

    db = build_fixture(tmp / "fixture_plain.db")
    meta, convs = build_conversations(db)
    check("会话数量", len(convs) == 3, str([c.key for c in convs]))
    check("消息总数", meta["message_count"] == len(C2C_ROWS) + len(GROUP_ROWS))

    keys = {c.key for c in convs}
    check("私聊会话键", "c2c:10001" in keys and "c2c:10002" in keys, str(keys))
    check("群聊会话键", "group:123456" in keys, str(keys))

    group = next(c for c in convs if c.kind == "group")
    check("群消息数", group.count == 3)
    check("群消息文本", group.messages[0]["text"] == "大家好")
    check("方向标记", [m["direction"] for m in group.messages] == ["in", "out", "in"])
    check("群发送者", group.messages[1]["sender_qq"] == 88888888)

    c2c = next(c for c in convs if c.key == "c2c:10001")
    check("私聊消息数", c2c.count == 3, str(c2c.count))
    check("私聊按时间排序", c2c.messages[0]["time"] < c2c.messages[-1]["time"])

    out_dir = tmp / "out"
    index = export_json(convs, out_dir / "json", meta)
    check("JSON 索引文件存在", (out_dir / "json" / "index.json").is_file())
    check("JSON 会话文件数", len([p for p in (out_dir / "json").glob("*.json")]) == 4)
    loaded = json.loads((out_dir / "json" / "index.json").read_text(encoding="utf-8"))
    check("JSON 索引内容", loaded["meta"]["message_count"] == meta["message_count"])

    export_txt(convs, out_dir / "txt", meta)
    txt_files = sorted((out_dir / "txt").glob("*.txt"))
    check("TXT 文件数", len(txt_files) == 4)
    sample = next(p for p in txt_files if "123456" in p.name).read_text(encoding="utf-8")
    check("TXT 含消息文本", "今天的会议改到三点" in sample)
    check("TXT 含方向箭头", "->" in sample and "<-" in sample)

    # 名称映射
    meta2, convs2 = build_conversations(db, names={"group:123456": "测试群", "10001": "爱丽丝"})
    titles = {c.key: c.title for c in convs2}
    check("名称映射（key）", titles["group:123456"] == "测试群", str(titles))
    check("名称映射（裸号）", titles["c2c:10001"] == "爱丽丝", str(titles))

    # 非文本消息不应报错
    conn = sqlite3.connect(db)
    conn.execute(
        'INSERT INTO c2c_msg_table VALUES (?,?,?,?,?,?,?,?,?,?)',
        (9999, 1737009999, 0, "u_x", 10009, "u_me", 88888888, 2, 0, b"\xff\xfe\xfd"),
    )
    conn.commit()
    conn.close()
    meta3, convs3 = build_conversations(db)
    check("非法消息体不崩溃", meta3["message_count"] == 9, str(meta3["message_count"]))
    # 消息体为空时也要能导出
    conn = sqlite3.connect(db)
    conn.execute(
        'INSERT INTO c2c_msg_table VALUES (?,?,?,?,?,?,?,?,?,?)',
        (9998, 1737009998, 0, "u_x", 10009, "u_me", 88888888, 2, 0, None),
    )
    conn.commit()
    conn.close()
    meta4, convs4 = build_conversations(db)
    check("空消息体不崩溃", meta4["message_count"] == 10, str(meta4["message_count"]))

    # 文件名安全性
    from qq_exporter.exporters import safe_name

    check("文件名转义", safe_name('a/b:c*?"<>|', "x") == "a_b_c______", safe_name('a/b:c*?"<>|', "x"))
    check("文件名去尾部点", safe_name("abc... ", "x") == "abc", safe_name("abc... ", "x"))
    check("空名回退", safe_name("   ", "fallback") == "fallback")


# --------------------------------------------------------------------------


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="qqex_test_"))
    try:
        test_wire()
        test_crypto(tmp)
        test_pipeline(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'='*60}")
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for f in FAILED:
        print(f"  FAILED: {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
