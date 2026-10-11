"""纯 Python 实现的 SQLCipher 解密（不依赖 sqlcipher3 扩展）。

NTQQ 数据库的文件格式
---------------------
* 文件开头有 ``1024`` 字节自定义头部（明文，内含库版本、HMAC 算法等元数据），
  其后才是 SQLCipher 数据；
* 页大小 ``4096``；
* 每页尾部的「保留区」存放 IV(16B) + HMAC(20B) + 填充，SQLCipher 4 默认 48 字节；
* 密钥为 32 字节，直接作为 AES-256 密钥（也可能由 16 字符口令经
  PBKDF2-HMAC-SHA512/4000 派生而来）；
* 盐值 16 字节，就存在第 1 页开头，因此解密时不需要额外提供。

本模块会在解密时**自动探测保留区大小**：用第 1 页试几个候选值，
哪个能还原出合法的 SQLite 页头就用哪个。
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import sqlite3
import struct
from pathlib import Path
from typing import Callable, Iterable

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# ---- 常量 -----------------------------------------------------------------

EXT_HEADER = 1024      # NTQQ 在 SQLCipher 数据前附加的头部长度
PAGE_SIZE = 4096
SALT_SIZE = 16
KEY_SIZE = 32
IV_SIZE = 16
HMAC_SIZE = 20         # HMAC-SHA1
RESERVE = 48           # SQLCipher 4 默认保留区

HMAC_MASK = 0x3A       # SQLCipher 中 HMAC key 的掩码（与盐异或）
KDF_ITER = 4000        # NTQQ 使用的 PBKDF2 迭代次数
FAST_ITER = 2          # HMAC key 派生所用的极少迭代数

SQLITE_MAGIC = b"SQLite format 3\x00"

RESERVE_CANDIDATES = (48, 80, 32, 64, 16, 96, 0, 24, 40, 56)

Progress = Callable[[int, int], None]


# ---- 密钥派生与校验 -------------------------------------------------------


def derive_hmac_key(enc_key: bytes, salt: bytes) -> bytes:
    """按 SQLCipher 规则派生 HMAC-SHA1 密钥。"""
    hmac_salt = bytes(b ^ HMAC_MASK for b in salt)
    return hashlib.pbkdf2_hmac("sha512", enc_key, hmac_salt, FAST_ITER, KEY_SIZE)


def verify_page1_hmac(page1: bytes, enc_key: bytes, reserve: int = RESERVE) -> bool:
    """校验第 1 页的 HMAC-SHA1，用于确认密钥正确。"""
    if len(page1) < PAGE_SIZE:
        return False
    salt = page1[:SALT_SIZE]
    hmac_key = derive_hmac_key(enc_key, salt)
    data_end = PAGE_SIZE - reserve
    content = page1[SALT_SIZE:data_end]
    iv = page1[data_end : data_end + IV_SIZE]
    stored = page1[data_end + IV_SIZE : data_end + IV_SIZE + HMAC_SIZE]
    computed = hmac_mod.new(
        hmac_key, content + iv + struct.pack("<I", 1), hashlib.sha1
    ).digest()
    return hmac_mod.compare_digest(computed, stored)


def derive_key_from_passphrase(
    passphrase: bytes, salt: bytes, iterations: int = KDF_ITER
) -> bytes:
    """把口令（而非原始密钥）派生成 AES 密钥。"""
    return hashlib.pbkdf2_hmac("sha512", passphrase, salt, iterations, KEY_SIZE)


# ``global`` 目录下的库（login.db / sign.db）使用硬编码在 wrapper.node 里的固定口令。
# 这一点已在本机 QQ 9.9.30 的 login.db 上实测通过（HMAC 校验 + quick_check 均通过）。
PRE_LOGIN_PASSPHRASE = b"BD156D6710D54D8782F4"


def global_db_key(db_path: Path, iterations: int = KDF_ITER) -> bytes:
    """推导 ``global/nt_db`` 下数据库的密钥。"""
    salt = extract_salt(db_path)
    return derive_key_from_passphrase(PRE_LOGIN_PASSPHRASE, salt, iterations)


def key_from_passphrase(db_path: Path, passphrase: str, iterations: int = KDF_ITER) -> bytes:
    """从 16 字符 ASCII 口令推导 AES 密钥（账号库的标准路径）。

    这个口令正是 NapCat / qq-win-db-key 在登录时捕获的那个 16 字节字符串。
    """
    salt = extract_salt(db_path)
    return derive_key_from_passphrase(passphrase.encode("utf-8"), salt, iterations)


# ---- 页面解密 -------------------------------------------------------------


def decrypt_page(
    page: bytes, enc_key: bytes, skip_salt: int = 0, reserve: int = RESERVE
) -> bytes:
    """解密单个 SQLCipher 页，返回明文页数据（长度 = page_size - reserve）。"""
    body = page[skip_salt:]
    usable = len(body) - reserve
    if usable <= 0 or usable + IV_SIZE > len(body):
        return b""
    ciphertext = body[: usable - (usable % 16)]
    iv = body[usable : usable + IV_SIZE]
    decryptor = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


def looks_like_sqlite_page1(
    plain: bytes, skip_salt: int = 16, page_size: int = PAGE_SIZE
) -> bool:
    """判断解密结果是否是合法的 SQLite 第 1 页（与 IV 无关的头部特征）。

    ``plain`` 的第 0 字节对应 SQLite 页内偏移 ``skip_salt``，
    所以页内偏移 X 对应 ``plain[X - skip_salt]``。
    """
    if skip_salt < 0 or skip_salt > 100:
        return False
    if len(plain) < 100 - skip_salt:
        return False
    o = skip_salt
    try:
        declared = int.from_bytes(plain[16 - o : 18 - o], "big")
        return (
            declared == page_size
            and plain[18 - o] in (1, 2)
            and plain[19 - o] in (1, 2)
            and plain[21 - o] == 64
            and plain[22 - o] == 32
            and plain[23 - o] == 32
        )
    except IndexError:
        return False


def detect_reserve(
    page1: bytes,
    enc_key: bytes,
    candidates: Iterable[int] = RESERVE_CANDIDATES,
) -> int | None:
    """自动探测保留区大小。"""
    for reserve in candidates:
        if reserve < 0 or reserve + IV_SIZE > len(page1) - SALT_SIZE:
            continue
        plain = decrypt_page(page1, enc_key, skip_salt=SALT_SIZE, reserve=reserve)
        if looks_like_sqlite_page1(plain, skip_salt=SALT_SIZE):
            return reserve
    return None


def read_page1_raw(db_path: Path) -> bytes:
    """读取 NTQQ 数据库第 1 页的原始（仍加密）数据。"""
    with open(db_path, "rb") as fh:
        head = fh.read(EXT_HEADER + PAGE_SIZE)
    if len(head) < EXT_HEADER + PAGE_SIZE:
        raise ValueError(f"文件过小，不像是 NTQQ 数据库: {db_path}")
    return head[EXT_HEADER : EXT_HEADER + PAGE_SIZE]


def extract_salt(db_path: Path) -> bytes:
    """从数据库文件第 1 页读出 16 字节盐值。"""
    return read_page1_raw(db_path)[:SALT_SIZE]


def is_ntqq_database(db_path: Path) -> bool:
    """判断是否带 NTQQ 自定义头部的 SQLCipher 库。"""
    try:
        with open(db_path, "rb") as fh:
            head = fh.read(EXT_HEADER + PAGE_SIZE)
    except OSError:
        return False
    if len(head) < EXT_HEADER + PAGE_SIZE:
        return False
    return head[:16] == b"SQLite header 3\x00"


def read_header_meta(db_path: Path) -> dict:
    """解析 1024 字节头部里的元数据（明文）。"""
    from . import wire

    with open(db_path, "rb") as fh:
        head = fh.read(EXT_HEADER)
    if head[:16] != b"SQLite header 3\x00":
        raise ValueError("不是 NTQQ 数据库头部")
    marker = head[32:40]
    length = struct.unpack_from("<I", head, 40)[0]
    payload = head[44 : 44 + length]
    raw: dict = {}
    try:
        for number, wtype, value in wire.parse_fields(payload):
            if wtype == wire.WT_LEN:
                text = wire.decode_text(value)
                raw[number] = text if text else value.hex()
            else:
                raw[number] = value
    except Exception:  # noqa: BLE001
        pass
    return {
        "marker": marker.decode("latin1", "replace"),
        "format_version": raw.get(3),
        "hmac_algorithm": raw.get(4),
        "key_id": raw.get(2),
        "raw": raw,
    }


# ---- WAL 合并 -------------------------------------------------------------

WAL_MAGIC_BE = 0x377F0682
WAL_MAGIC_LE = 0x377F0683
WAL_HEADER_SIZE = 32
WAL_FRAME_HEADER_SIZE = 24


def decrypt_wal(
    wal_path: Path,
    enc_key: bytes,
    reserve: int,
    page_size: int = PAGE_SIZE,
    progress: Progress | None = None,
) -> tuple[list[tuple[int, bytes]], int]:
    """解密 WAL 中的页面镜像。

    返回 ``([(页号, 明文页数据), ...], 提交后的数据库页数)``。
    WAL 是「未写入主库的最新改动」，先解密主库再按页号覆盖即可得到最新状态。
    """
    wal_path = Path(wal_path)
    if not wal_path.is_file():
        return [], 0
    blob = wal_path.read_bytes()
    if len(blob) < WAL_HEADER_SIZE:
        return [], 0

    magic = int.from_bytes(blob[0:4], "big")
    if magic not in (WAL_MAGIC_BE, WAL_MAGIC_LE):
        return [], 0
    big_endian = magic == WAL_MAGIC_BE
    order = "big" if big_endian else "little"
    wal_page_size = int.from_bytes(blob[8:12], order)
    if wal_page_size not in (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536):
        wal_page_size = page_size
    page_size = wal_page_size

    frames: list[tuple[int, bytes]] = []
    db_size = 0
    offset = WAL_HEADER_SIZE
    frame_size = WAL_FRAME_HEADER_SIZE + page_size
    total = max(0, (len(blob) - WAL_HEADER_SIZE) // frame_size)
    index = 0

    while offset + frame_size <= len(blob):
        header = blob[offset : offset + WAL_FRAME_HEADER_SIZE]
        pgno = int.from_bytes(header[0:4], order)
        size_after_commit = int.from_bytes(header[4:8], order)
        frame_page = blob[
            offset + WAL_FRAME_HEADER_SIZE : offset + WAL_FRAME_HEADER_SIZE + page_size
        ]
        skip = SALT_SIZE if pgno == 1 else 0
        try:
            plain = decrypt_page(frame_page, enc_key, skip_salt=skip, reserve=reserve)
        except Exception:  # noqa: BLE001
            plain = b""
        if plain:
            if pgno == 1:
                # 与主库一致：页 1 的密文不含开头的 16 字节魔数，这里补回来
                page = bytearray(SQLITE_MAGIC + plain)
                if len(page) > page_size:
                    page = page[:page_size]
                page += b"\x00" * (page_size - len(page))
                plain = bytes(page[:page_size])
            frames.append((pgno, plain))
            if size_after_commit:
                db_size = size_after_commit
        offset += frame_size
        index += 1
        if progress and index % 256 == 0:
            progress(index, total)

    return frames, db_size


def _apply_wal_file(fout, frames: list[tuple[int, bytes]], db_size: int, page_size: int) -> None:
    """把 WAL 页面覆盖到已解密的主库文件上（随机访问 seek+write）。"""
    for pgno, plain in frames:
        if pgno <= 0:
            continue
        start = (pgno - 1) * page_size
        page = plain + b"\x00" * (page_size - len(plain))
        fout.seek(start)
        fout.write(page[:page_size])
    if db_size > 0:
        want = db_size * page_size
        fout.flush()
        cur = fout.seek(0, 2)
        if want < cur:
            fout.truncate(want)


# ---- 整库解密 -------------------------------------------------------------


def decrypt_database(
    src: Path,
    dst: Path,
    enc_key: bytes,
    *,
    reserve: int | None = None,
    merge_wal: bool = True,
    progress: Progress | None = None,
) -> dict:
    """把 NTQQ 的加密数据库解密成标准 SQLite 文件。

    采用**流式**：按页读取 → 按页解密 → 按页写出，内存占用恒定（与库大小无关）。

    :param merge_wal: 是否把 ``-wal`` 里尚未写入主库的页面合并进来
    """
    src = Path(src)
    dst = Path(dst)

    # 1) 读第 1 页，探测保留区大小
    with open(src, "rb") as fin:
        fin.seek(EXT_HEADER)
        page1_enc = fin.read(PAGE_SIZE)
    if len(page1_enc) < PAGE_SIZE:
        raise ValueError(f"数据库内容不足一页: {src}")

    if reserve is None:
        reserve = detect_reserve(page1_enc, enc_key)
        if reserve is None:
            raise ValueError(
                "无法确定 SQLCipher 保留区大小：密钥可能不对，或库格式与预期不同。"
            )

    total_pages = max(0, (src.stat().st_size - EXT_HEADER) // PAGE_SIZE)
    sqlite_reserved_byte = 0

    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fin.seek(EXT_HEADER)
        for pgno in range(1, total_pages + 1):
            page = fin.read(PAGE_SIZE)
            if not page:
                break
            if len(page) < PAGE_SIZE:
                page += b"\x00" * (PAGE_SIZE - len(page))

            skip = SALT_SIZE if pgno == 1 else 0
            plain = decrypt_page(page, enc_key, skip_salt=skip, reserve=reserve)

            if pgno == 1:
                page1 = bytearray(SQLITE_MAGIC + plain)
                # 注意：SQLite 头部的「保留区字节」(偏移 20) 不一定要等于 SQLCipher 的
                # 保留区大小（实测 NTQQ 分别是 80 和 48）。这里保留解密出来的原值。
                sqlite_reserved_byte = page1[20]
                if len(page1) > PAGE_SIZE:
                    page1 = page1[:PAGE_SIZE]
                page1 += b"\x00" * (PAGE_SIZE - len(page1))
                fout.write(bytes(page1[:PAGE_SIZE]))
            else:
                fout.write(plain)
                fout.write(b"\x00" * (PAGE_SIZE - len(plain)))

            if progress and (pgno % 512 == 0 or pgno == total_pages):
                progress(pgno, total_pages)

        # 2) WAL 合并（随机访问覆盖 / 追加）
        wal_pages = 0
        if merge_wal:
            frames, db_size = decrypt_wal(
                Path(str(src) + "-wal"), enc_key, reserve, PAGE_SIZE, progress
            )
            if frames:
                _apply_wal_file(fout, frames, db_size, PAGE_SIZE)
                wal_pages = len(frames)

    # 3) 兜底：若保留区字节与 SQLCipher 保留区不一致导致读不出，原地改这一个字节再验
    integrity = quick_check(dst)
    if integrity != "ok" and sqlite_reserved_byte != (reserve & 0xFF):
        try:
            with open(dst, "r+b") as fh:
                fh.seek(20)
                fh.write(bytes([reserve & 0xFF]))
            alt_integrity = quick_check(dst)
            if alt_integrity == "ok":
                sqlite_reserved_byte = reserve & 0xFF
                integrity = alt_integrity
            else:
                with open(dst, "r+b") as fh:
                    fh.seek(20)
                    fh.write(bytes([sqlite_reserved_byte]))
        except OSError:
            pass

    return {
        "ok": integrity == "ok",
        "pages": total_pages,
        "wal_pages": wal_pages,
        "reserve": reserve,
        "sqlite_reserved_byte": sqlite_reserved_byte,
        "bytes": dst.stat().st_size,
        "integrity": integrity,
    }


def quick_check(db_path: Path) -> str:
    """用 sqlite3 打开解密结果并做一次快速完整性检查。"""
    try:
        conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
        try:
            row = conn.execute("PRAGMA quick_check(1)").fetchone()
            return row[0] if row else "unknown"
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return f"sqlite error: {exc}"


def list_tables(db_path: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()
