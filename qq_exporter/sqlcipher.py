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
import os
import sqlite3
import struct
import tempfile
from pathlib import Path
from typing import BinaryIO, Callable, Iterable

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
    if len(page) != PAGE_SIZE:
        raise ValueError("SQLCipher 页面长度不完整")
    if len(enc_key) != KEY_SIZE:
        raise ValueError("SQLCipher AES-256 密钥必须为 32 字节")
    if skip_salt not in (0, SALT_SIZE):
        raise ValueError("不支持的 SQLCipher 盐偏移")
    if reserve < IV_SIZE or reserve >= PAGE_SIZE - SALT_SIZE or reserve % 16:
        raise ValueError("不支持的 SQLCipher 保留区布局")
    body = page[skip_salt:]
    usable = len(body) - reserve
    ciphertext = body[:usable]
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
        if reserve < IV_SIZE or reserve % 16 or reserve + IV_SIZE > len(page1) - SALT_SIZE:
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

# Historical public names retained; 0x...82 uses LITTLE-endian checksum words,
# 0x...83 uses BIG-endian words. All WAL header/frame fields are big-endian.
WAL_MAGIC_BE = 0x377F0682
WAL_MAGIC_LE = 0x377F0683
WAL_HEADER_SIZE = 32
WAL_FRAME_HEADER_SIZE = 24
WAL_VERSION = 3007000


def _wal_checksum(data: bytes, state: tuple[int, int], order: str) -> tuple[int, int]:
    s0, s1 = state
    for x0, x1 in struct.iter_unpack(order + "II", data):
        s0 = (s0 + x0 + s1) & 0xFFFFFFFF
        s1 = (s1 + x1 + s0) & 0xFFFFFFFF
    return s0, s1


def _file_stamp(fh: BinaryIO) -> tuple[int, int, int, int]:
    st = os.fstat(fh.fileno())
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def _decode_checked_page(
    page: bytes, enc_key: bytes, pgno: int, reserve: int,
    hmac_key: bytes | None = None, salt: bytes | None = None,
) -> bytes:
    skip = SALT_SIZE if pgno == 1 else 0
    plain = decrypt_page(page, enc_key, skip_salt=skip, reserve=reserve)
    if hmac_key is not None:
        if reserve < IV_SIZE + HMAC_SIZE:
            raise ValueError("保留区不足以容纳 HMAC-SHA1")
        end = PAGE_SIZE - reserve
        stored = page[end + IV_SIZE:end + IV_SIZE + HMAC_SIZE]
        computed = hmac_mod.new(
            hmac_key, page[skip:end + IV_SIZE] + struct.pack("<I", pgno), hashlib.sha1
        ).digest()
        if not hmac_mod.compare_digest(computed, stored):
            raise ValueError(f"SQLCipher 第 {pgno} 页 HMAC-SHA1 校验失败（损坏或不支持的格式）")
    if pgno == 1:
        if salt is not None and page[:SALT_SIZE] != salt:
            raise ValueError("WAL 第 1 页 SQLCipher salt 与主库不符")
        if not looks_like_sqlite_page1(plain):
            raise ValueError("SQLCipher 第 1 页解密头部无效")
        plain = SQLITE_MAGIC + plain
    return plain + b"\x00" * (PAGE_SIZE - len(plain))


def _scan_wal(
    wal_path: Path, spool: BinaryIO, enc_key: bytes, reserve: int,
    page_size: int, progress: Progress | None,
    hmac_key: bytes | None = None, salt: bytes | None = None,
) -> tuple[int, int]:
    """Validate into a disk spool; return last committed frame count and db size.

    Strict policy: malformed/truncated frames (even after a commit) fail the whole
    merge. Complete, valid but uncommitted tail frames are ignored. This deliberately
    does not guess whether a damaged tail is stale SQLite WAL storage or corruption.
    Only standard SQLite WAL checksums over encrypted page bytes are supported;
    real QQ-specific WAL codec conventions have not been established by fixtures.
    """
    if page_size != PAGE_SIZE:
        raise ValueError("仅支持 4096 字节 SQLCipher WAL 页面")
    try:
        fin = open(wal_path, "rb")
    except FileNotFoundError:
        return 0, 0
    with fin:
        before = _file_stamp(fin)
        if before[2] == 0:
            return 0, 0
        header = fin.read(WAL_HEADER_SIZE)
        if len(header) != WAL_HEADER_SIZE:
            raise ValueError("WAL 头部截断")
        magic, version, wal_page_size, _, salt1, salt2, c0, c1 = struct.unpack(">8I", header)
        if magic not in (WAL_MAGIC_BE, WAL_MAGIC_LE):
            raise ValueError("不支持的 WAL magic")
        if version != WAL_VERSION or wal_page_size != page_size:
            raise ValueError("不支持的 WAL 版本或页大小")
        order = "<" if magic == WAL_MAGIC_BE else ">"
        state = _wal_checksum(header[:24], (0, 0), order)
        if state != (c0, c1):
            raise ValueError("WAL 头部 checksum 校验失败")
        frame_size = WAL_FRAME_HEADER_SIZE + page_size
        if (before[2] - WAL_HEADER_SIZE) % frame_size:
            raise ValueError("WAL 帧截断")
        total = (before[2] - WAL_HEADER_SIZE) // frame_size
        committed = db_size = 0
        for index in range(1, total + 1):
            fh = fin.read(WAL_FRAME_HEADER_SIZE)
            page = fin.read(page_size)
            if len(fh) != WAL_FRAME_HEADER_SIZE or len(page) != page_size:
                raise ValueError("WAL 帧读取不完整")
            pgno, size, fs1, fs2, fc0, fc1 = struct.unpack(">6I", fh)
            if not 1 <= pgno <= 0xFFFFFFFE or size > 0xFFFFFFFE:
                raise ValueError("WAL 页号或提交大小无效")
            if (fs1, fs2) != (salt1, salt2):
                raise ValueError("WAL 帧 salt 校验失败")
            state = _wal_checksum(fh[:8] + page, state, order)
            if state != (fc0, fc1):
                raise ValueError("WAL 帧 checksum 校验失败")
            plain = _decode_checked_page(page, enc_key, pgno, reserve, hmac_key, salt)
            spool.write(struct.pack(">II", pgno, size))
            spool.write(plain)
            if size:
                committed, db_size = index, size
            if progress and (index % 256 == 0 or index == total):
                progress(index, total)
        if _file_stamp(fin) != before:
            raise ValueError("WAL 在读取期间发生变化；请使用稳定的离线快照")
    spool.seek(0)
    return committed, db_size


def decrypt_wal(
    wal_path: Path,
    enc_key: bytes,
    reserve: int,
    page_size: int = PAGE_SIZE,
    progress: Progress | None = None,
) -> tuple[list[tuple[int, bytes]], int]:
    """返回已提交的最终页镜像和提交后页数；损坏/未知 WAL 抛 ValueError。

    此兼容便捷接口仍返回列表（内存随页面数增长）；decrypt_database 使用磁盘
    暂存实现有限内存重放。这里仅验证 WAL checksum/salt；缺少主库 salt，不能
    验证 SQLCipher HMAC。主库合并路径默认另行校验所有页的 HMAC-SHA1。
    """
    with tempfile.TemporaryFile() as spool:
        committed, db_size = _scan_wal(
            Path(wal_path), spool, enc_key, reserve, page_size, progress
        )
        pages: dict[int, bytes] = {}
        for _ in range(committed):
            pgno, size = struct.unpack(">II", spool.read(8))
            pages[pgno] = spool.read(page_size)
            if size:
                pages = {n: page for n, page in pages.items() if n <= size}
        return list(pages.items()), db_size


def _apply_wal_file(fout, frames: list[tuple[int, bytes]], db_size: int, page_size: int) -> None:
    """Apply final committed images (legacy materialized interface)."""
    for pgno, plain in frames:
        if not 1 <= pgno <= db_size or len(plain) > page_size:
            raise ValueError("WAL 最终页面无效")
        fout.seek((pgno - 1) * page_size)
        fout.write(plain + b"\x00" * (page_size - len(plain)))
    if db_size:
        fout.truncate(db_size * page_size)


def _merge_wal(
    fout: BinaryIO, wal_path: Path, enc_key: bytes, reserve: int,
    progress: Progress | None, hmac_key: bytes | None, salt: bytes,
) -> int:
    with tempfile.TemporaryFile() as spool:
        committed, _ = _scan_wal(
            wal_path, spool, enc_key, reserve, PAGE_SIZE, progress, hmac_key, salt
        )
        for _ in range(committed):
            pgno, size = struct.unpack(">II", spool.read(8))
            page = spool.read(PAGE_SIZE)
            fout.seek((pgno - 1) * PAGE_SIZE)
            fout.write(page)
            if size:
                # Each commit matters: a later growth must not resurrect pages
                # removed by an earlier truncating transaction.
                fout.truncate(size * PAGE_SIZE)
        return committed


# ---- 整库解密 -------------------------------------------------------------


def decrypt_database(
    src: Path,
    dst: Path,
    enc_key: bytes,
    *,
    reserve: int | None = None,
    merge_wal: bool = True,
    progress: Progress | None = None,
    verify_hmac: bool = True,
) -> dict:
    """流式解密稳定的离线 NTQQ 快照，完成后原子替换目标文件。

    默认验证现有 SHA1 页 HMAC 布局；未知 codec 需调用者明确选择
    verify_hmac=False，此时仅有 SQLite 结构检查和 WAL checksum，不能认证页面。
    WAL 使用临时磁盘空间，内存与库/WAL 大小无关。异常不会发布部分输出。
    完整解密但 SQLite 检查失败时保留兼容行为：发布诊断文件并返回 ok=False。
    本函数不提供断点恢复：没有稳定快照身份及持久化认证检查点时不能安全跳页。
    """
    src, dst = Path(src), Path(dst)
    wal_path = Path(str(src) + "-wal")
    for protected in (src, wal_path):
        if dst.resolve() == protected.resolve() or (
            dst.exists() and protected.exists() and dst.samefile(protected)
        ):
            raise ValueError("输出路径不能覆盖加密主库或 WAL（包括链接别名）")
    if any(Path(str(dst) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("目标数据库存在 SQLite sidecar；请使用新的离线输出路径")
    if len(enc_key) != KEY_SIZE:
        raise ValueError("SQLCipher AES-256 密钥必须为 32 字节")
    temp_path: Path | None = None
    try:
        with open(src, "rb") as fin:
            source_stamp = _file_stamp(fin)
            payload_size = source_stamp[2] - EXT_HEADER
            if payload_size < PAGE_SIZE or payload_size % PAGE_SIZE:
                raise ValueError("数据库内容不足一页或末页截断")
            total_pages = payload_size // PAGE_SIZE
            fin.seek(EXT_HEADER)
            page1_enc = fin.read(PAGE_SIZE)
            if reserve is None:
                reserve = detect_reserve(page1_enc, enc_key)
                if reserve is None:
                    raise ValueError("无法确定 SQLCipher 保留区大小：密钥或库格式不匹配")
            salt = page1_enc[:SALT_SIZE]
            hmac_key = derive_hmac_key(enc_key, salt) if verify_hmac else None
            # Validate before allocating output and also when reserve was supplied.
            first = _decode_checked_page(page1_enc, enc_key, 1, reserve, hmac_key, salt)
            sqlite_reserved_byte = first[20]
            dst.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w+b", prefix=f".{dst.name}.", suffix=".partial",
                dir=dst.parent, delete=False,
            ) as fout:
                temp_path = Path(fout.name)
                fin.seek(EXT_HEADER)
                for pgno in range(1, total_pages + 1):
                    plain = _decode_checked_page(
                        fin.read(PAGE_SIZE), enc_key, pgno, reserve, hmac_key, salt
                    )
                    fout.write(plain)
                    if progress and (pgno % 512 == 0 or pgno == total_pages):
                        progress(pgno, total_pages)
                wal_pages = 0
                if merge_wal:
                    wal_pages = _merge_wal(
                        fout, wal_path, enc_key, reserve, progress, hmac_key, salt
                    )
                if _file_stamp(fin) != source_stamp:
                    raise ValueError("主库在读取期间发生变化；请使用稳定的离线快照")
                # A committed WAL page 1 can change this byte.
                fout.seek(20)
                sqlite_reserved_byte = fout.read(1)[0]
                fout.flush()
                os.fsync(fout.fileno())

        integrity = quick_check(temp_path)
        if integrity != "ok" and sqlite_reserved_byte != reserve:
            with open(temp_path, "r+b") as fh:
                fh.seek(20)
                fh.write(bytes([reserve]))
            alternate = quick_check(temp_path)
            if alternate == "ok":
                sqlite_reserved_byte, integrity = reserve, alternate
            else:
                with open(temp_path, "r+b") as fh:
                    fh.seek(20)
                    fh.write(bytes([sqlite_reserved_byte]))
        stats = {
            "ok": integrity == "ok",
            "pages": total_pages,
            "wal_pages": wal_pages,
            "reserve": reserve,
            "sqlite_reserved_byte": sqlite_reserved_byte,
            "bytes": temp_path.stat().st_size,
            "integrity": integrity,
            "hmac_verified": verify_hmac,
        }
        with open(temp_path, "r+b") as fh:
            os.fsync(fh.fileno())
        os.replace(temp_path, dst)
        temp_path = None
        return stats
    finally:
        if temp_path is not None:
            # Only our freshly created, exact temporary path is removed.
            temp_path.unlink(missing_ok=True)


def quick_check(db_path: Path) -> str:
    """检查离线解密结果；immutable 防止读写临时文件的 WAL/SHM sidecar。"""
    try:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
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
