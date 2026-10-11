"""从运行中的 NTQQ 进程内存里恢复 SQLCipher 密钥。

NTQQ 把数据放在 SQLCipher（AES-256）加密库里，密钥不落盘，只在打开数据库时
存在于进程内存中。本模块按「由快到慢」的顺序尝试多种策略：

策略 1（盐值锚定，最快）
    SQLCipher 的编解码上下文里同时保存着 16 字节盐值和 32 字节 AES 密钥，
    而盐值本身就明文写在数据库文件第 1 页开头，是随机值，几乎不可能误命中。
    所以先在内存里搜盐值，再在它附近（±若干字节）按 8 字节对齐试每一段
    32 字节，用下面这个 **96 位判据** 验证：

        SQLite 页 1 头部偏移 72..91 是保留区，恒为 0；
        分块 CBC 下  AES_256_ECB_dec(K, C[4])[0:12] == C[3][0:12]

    这个判据与 IV、保留区大小都无关，一次 AES 块解密即可判定，误报率 ~2^-96。

策略 2（旧版特征串）
    早期版本会把 ``x'<64位十六进制密钥><32位十六进制盐值>'`` 这样的
    48 字节原始密钥串留在内存里，直接匹配即可。

策略 3（口令枚举，兜底，很慢）
    把内存里所有 16 字符可打印串（含 UTF-16LE）当候选口令，
    用 PBKDF2-HMAC-SHA512/4000 派生后再用同一判据验证。
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import winmem
from .sqlcipher import (
    EXT_HEADER,
    PAGE_SIZE,
    SALT_SIZE,
    extract_salt,
    is_ntqq_database,
)

# 旧版特征：x'<64hex key><32hex salt>'
KEY_BLOB_RE = re.compile(rb"x'([0-9a-fA-F]{64})([0-9a-fA-F]{32})'")
HEX_BYTES = frozenset(b"0123456789abcdefABCDEF")

# 口令枚举用
PRINTABLE_RUN = re.compile(rb"[ -~]{16,}")
UTF16_RUN = re.compile(rb"(?:[ -~]\x00){16,}")

KDF_ALGO = "sha512"
KDF_ITERS = 4000

Progress = Callable[[str], None]


@dataclass
class KeyHit:
    salt_hex: str
    key_hex: str
    address: int = 0
    verified: bool = False
    method: str = ""
    detail: str = ""

    @property
    def key_bytes(self) -> bytes:
        return bytes.fromhex(self.key_hex)


@dataclass
class ScanStats:
    scanned_bytes: int = 0
    regions: int = 0
    elapsed: float = 0.0
    candidates: int = 0
    hits: list[KeyHit] = field(default_factory=list)


# --------------------------------------------------------------------------
# 判据
# --------------------------------------------------------------------------


class PageOracle:
    """基于 SQLite 页 1 头部保留区（恒为 0）的密钥判据。"""

    def __init__(self, db_path: Path):
        blob = Path(db_path).read_bytes()
        if len(blob) < EXT_HEADER + PAGE_SIZE:
            raise ValueError(f"文件过小: {db_path}")
        self.page1 = blob[EXT_HEADER : EXT_HEADER + PAGE_SIZE]
        # CBC：P[k] = dec(C[k]) XOR C[k-1]；页内偏移 80..92 落在保留区
        self.c4 = self.page1[80:96]
        self.target = self.page1[64:76]
        self.salt = self.page1[:SALT_SIZE]

    def test(self, key: bytes) -> bool:
        if len(key) != 32:
            return False
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        return decryptor.update(self.c4)[:12] == self.target

    def verify(self, key: bytes) -> bool:
        """完整解一页做最终确认（不依赖 IV 的 'CREATE TABLE' 判据）。"""
        reserve = 48
        body = self.page1[SALT_SIZE : len(self.page1) - reserve]
        body = body[: len(body) - (len(body) % 16)]
        decryptor = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16)).decryptor()
        plain = decryptor.update(body) + decryptor.finalize()
        return b"CREATE TABLE" in plain or b"CREATE INDEX" in plain or b"sqlite_" in plain

    def derive_from_passphrase(self, passphrase: bytes, algo: str = KDF_ALGO, iters: int = KDF_ITERS) -> bytes:
        return hashlib.pbkdf2_hmac(algo, passphrase, self.salt, iters, 32)


# --------------------------------------------------------------------------
# 进程定位
# --------------------------------------------------------------------------


def find_qq_main_pid(exe_name: str = "QQ.exe") -> int | None:
    """找到加载了 ``wrapper.node`` 的 QQ 主进程。"""
    pids = winmem.list_processes(exe_name)
    for pid in pids:
        if winmem.process_has_module(pid, "wrapper.node"):
            return pid
    return pids[0] if pids else None


def list_qq_pids(exe_name: str = "QQ.exe") -> list[int]:
    return winmem.list_processes(exe_name)


def qq_is_likely_idle(db_path: Path) -> bool:
    """粗略判断数据库最近是否被写过，用于给出「QQ 可能没登录」的提示。"""
    import os
    import time as _time

    newest = 0.0
    for suffix in ("", "-wal"):
        try:
            newest = max(newest, os.path.getmtime(str(db_path) + suffix))
        except OSError:
            pass
    if newest == 0:
        return True
    return (_time.time() - newest) > 6 * 3600


# --------------------------------------------------------------------------
# 策略 1：盐值锚定
# --------------------------------------------------------------------------


def _salt_anchored(
    mem: winmem.ProcessMemory,
    oracle: PageOracle,
    *,
    radius: int = 1024,
    align: int = 8,
    max_hits: int = 64,
    stats: ScanStats | None = None,
) -> list[KeyHit]:
    salt = oracle.salt
    results: list[KeyHit] = []
    seen_offsets: set[int] = set()

    for address, data in mem.iter_chunks(chunk_size=8 * 1024 * 1024, private_only=True):
        if stats:
            stats.scanned_bytes += len(data)
        start = 0
        while len(results) < max_hits:
            idx = data.find(salt, start)
            if idx < 0:
                break
            start = idx + 1
            lo = max(0, idx - radius)
            hi = min(len(data) - 32, idx + radius)
            for off in range(lo - (lo % align), hi + 1, align):
                key = data[off : off + 32]
                if len(key) < 32 or key in (b"\x00" * 32,):
                    continue
                if stats:
                    stats.candidates += 1
                if oracle.test(key):
                    hit_addr = address + off
                    if hit_addr in seen_offsets:
                        continue
                    seen_offsets.add(hit_addr)
                    results.append(
                        KeyHit(
                            salt_hex=salt.hex(),
                            key_hex=key.hex(),
                            address=hit_addr,
                            method="salt-anchored",
                            detail=f"盐值出现于 0x{address + idx:X}",
                        )
                    )
            if start == 0:
                break
    return results


# --------------------------------------------------------------------------
# 策略 2：旧版 x'key+salt' 特征串
# --------------------------------------------------------------------------


def _legacy_blobs(
    mem: winmem.ProcessMemory, oracle: PageOracle, stats: ScanStats | None = None
) -> list[KeyHit]:
    salt_hex_ascii = oracle.salt.hex().encode()
    results: list[KeyHit] = []
    for address, data in mem.iter_chunks(chunk_size=8 * 1024 * 1024):
        if stats:
            stats.scanned_bytes += len(data)
        start = 0
        while True:
            idx = data.find(salt_hex_ascii, start)
            if idx < 0:
                break
            start = idx + 1
            if (
                idx >= 66
                and idx + 33 <= len(data)
                and data[idx - 66 : idx - 64] == b"x'"
                and data[idx + 32 : idx + 33] == b"'"
                and all(c in HEX_BYTES for c in data[idx - 64 : idx])
            ):
                key_hex = data[idx - 64 : idx].decode("ascii").lower()
                key = bytes.fromhex(key_hex)
                if stats:
                    stats.candidates += 1
                results.append(
                    KeyHit(
                        salt_hex=oracle.salt.hex(),
                        key_hex=key_hex,
                        address=address + idx - 66,
                        method="legacy-blob",
                        detail="x'key+salt' 特征串",
                    )
                )
    return results


# --------------------------------------------------------------------------
# 策略 3：全量内存扫描（96 位判据）
# --------------------------------------------------------------------------


def _full_scan(
    mem: winmem.ProcessMemory,
    oracle: PageOracle,
    *,
    align: int = 8,
    stats: ScanStats | None = None,
    progress: Progress | None = None,
) -> list[KeyHit]:
    AES = algorithms.AES
    ECB = modes.ECB()
    c4, target = oracle.c4, oracle.target
    results: list[KeyHit] = []
    last = time.time()
    for address, data in mem.iter_chunks(chunk_size=8 * 1024 * 1024):
        if stats:
            stats.scanned_bytes += len(data)
        for off in range(0, len(data) - 32, align):
            key = data[off : off + 32]
            decryptor = Cipher(AES(key), ECB).decryptor()
            if decryptor.update(c4)[:12] == target:
                results.append(
                    KeyHit(
                        salt_hex=oracle.salt.hex(),
                        key_hex=key.hex(),
                        address=address + off,
                        method="full-scan",
                    )
                )
        if stats:
            stats.candidates += len(data) // align
        if progress and time.time() - last > 15:
            last = time.time()
            progress(f"  全量扫描中，已试 {stats.candidates:,} 个候选…")
    return results


# --------------------------------------------------------------------------
# 策略 4：口令枚举
# --------------------------------------------------------------------------


def collect_passphrase_candidates(mem: winmem.ProcessMemory, strict: bool = True) -> list[bytes]:
    from .winmem import ProcessMemory

    _ = ProcessMemory  # 仅用于类型提示
    cands: set[bytes] = set()
    for _addr, chunk in mem.iter_chunks(chunk_size=8 * 1024 * 1024):
        for m in PRINTABLE_RUN.finditer(chunk):
            run = m.group()
            if strict:
                cands.add(run[-16:])
            else:
                for i in range(len(run) - 15):
                    cands.add(bytes(run[i : i + 16]))
        for m in UTF16_RUN.finditer(chunk):
            run = m.group()
            for i in range(0, len(run) - 31, 2):
                cands.add(bytes(run[i + j] for j in range(0, 32, 2)))
    return sorted(cands)


def _passphrase_scan(
    mem: winmem.ProcessMemory,
    oracle: PageOracle,
    *,
    algo: str = KDF_ALGO,
    iters: int = KDF_ITERS,
    max_candidates: int | None = 200_000,
    progress: Progress | None = None,
) -> list[KeyHit]:
    cands = collect_passphrase_candidates(mem)
    if max_candidates:
        cands = cands[:max_candidates]
    if progress:
        progress(f"  口令枚举：{len(cands):,} 个候选，预计 {len(cands)*3.1/1000:.0f} 秒（单进程）")
    results: list[KeyHit] = []
    for cand in cands:
        key = oracle.derive_from_passphrase(cand, algo, iters)
        if oracle.test(key):
            results.append(
                KeyHit(
                    salt_hex=oracle.salt.hex(),
                    key_hex=key.hex(),
                    method="passphrase",
                    detail=f"口令={cand.decode('latin1')!r} / {algo}/{iters}",
                )
            )
    return results


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------


def recover_key(
    db_path: Path,
    *,
    pid: int | None = None,
    exe_name: str = "QQ.exe",
    deep: bool = False,
    passphrase: bool = False,
    progress: Progress | None = None,
) -> tuple[KeyHit | None, ScanStats]:
    """为单个数据库恢复密钥。

    :param deep: 额外做一次全量内存扫描（慢，约 10~20 分钟）
    :param passphrase: 额外做口令枚举（更慢）
    """
    db_path = Path(db_path)
    oracle = PageOracle(db_path)
    stats = ScanStats()
    started = time.time()

    # global 目录下的库用固定口令，无需扫描内存
    if "global" in db_path.parts:
        from .sqlcipher import global_db_key, verify_page1_hmac

        try:
            key = global_db_key(db_path)
            page1 = db_path.read_bytes()[EXT_HEADER : EXT_HEADER + PAGE_SIZE]
            if verify_page1_hmac(page1, key):
                hit = KeyHit(
                    salt_hex=oracle.salt.hex(),
                    key_hex=key.hex(),
                    method="pre-login-passphrase",
                    detail="global 库的固定口令",
                    verified=True,
                )
                stats.elapsed = time.time() - started
                stats.hits = [hit]
                return hit, stats
        except Exception:  # noqa: BLE001
            pass

    if pid is None:
        pid = find_qq_main_pid(exe_name)
    if pid is None:
        raise RuntimeError(f"没有找到正在运行的 {exe_name}，请先登录 QQ 再重试。")

    if progress:
        progress(f"  QQ 主进程 PID={pid}，盐值={oracle.salt.hex()}")

    with winmem.ProcessMemory(pid) as mem:
        stats.regions = sum(1 for _ in mem.iter_regions())

        hits = _salt_anchored(mem, oracle, stats=stats)
        if progress:
            progress(f"  盐值锚定：试了 {stats.candidates:,} 个候选，得到 {len(hits)} 个候选密钥")
        if not hits:
            hits = _legacy_blobs(mem, oracle, stats=stats)
            if hits and progress:
                progress(f"  旧版特征串命中 {len(hits)} 个")

        for hit in hits:
            hit.verified = oracle.verify(hit.key_bytes)
        good = [h for h in hits if h.verified]
        if good:
            stats.elapsed = time.time() - started
            stats.hits = good
            return good[0], stats

        if deep:
            if progress:
                progress("  进入全量内存扫描（较慢）…")
            hits = _full_scan(mem, oracle, stats=stats, progress=progress)
            for hit in hits:
                hit.verified = oracle.verify(hit.key_bytes)
            good = [h for h in hits if h.verified]
            if good:
                stats.elapsed = time.time() - started
                stats.hits = good
                return good[0], stats

        if passphrase:
            if progress:
                progress("  进入口令枚举（很慢）…")
            hits = _passphrase_scan(mem, oracle, progress=progress)
            for hit in hits:
                hit.verified = oracle.verify(hit.key_bytes)
            good = [h for h in hits if h.verified]
            if good:
                stats.elapsed = time.time() - started
                stats.hits = good
                return good[0], stats

    stats.elapsed = time.time() - started
    stats.hits = hits
    return None, stats


# --------------------------------------------------------------------------
# 数据库发现
# --------------------------------------------------------------------------


def tencent_files_root() -> Path:
    import os

    return Path(os.path.expandvars(r"%USERPROFILE%\Documents\Tencent Files"))


def discover_databases(qq: str | None = None) -> dict[str, list[Path]]:
    """发现本机 NTQQ 的数据库，返回 ``{账号: [路径, ...]}``。"""
    root = tencent_files_root()
    result: dict[str, list[Path]] = {}
    if not root.is_dir():
        return result

    candidates: list[tuple[str, Path]] = []
    global_dir = root / "nt_qq" / "global" / "nt_db"
    if global_dir.is_dir():
        candidates.append(("global", global_dir))

    for account_dir in sorted(root.iterdir()):
        if not account_dir.is_dir():
            continue
        if qq and account_dir.name != str(qq):
            continue
        db_dir = account_dir / "nt_qq" / "nt_db"
        if db_dir.is_dir():
            candidates.append((account_dir.name, db_dir))

    for account, db_dir in candidates:
        files = sorted(
            p for p in db_dir.glob("*.db") if p.is_file() and is_ntqq_database(p)
        )
        if files:
            result[account] = files
    return result


def find_main_db(qq: str | None = None) -> tuple[str, Path]:
    """定位存放聊天记录的主库 ``nt_msg.db``。"""
    dbs = discover_databases(qq)
    for account, files in dbs.items():
        if account == "global":
            continue
        for path in files:
            if path.name == "nt_msg.db":
                return account, path
    raise FileNotFoundError(
        "没有找到 nt_msg.db。默认路径为 "
        r"%USERPROFILE%\Documents\Tencent Files\<QQ号>\nt_qq\nt_db\nt_msg.db"
    )


def pending_wal(db_path: Path) -> int:
    """返回同名 ``-wal`` 文件大小，用于提示先退出 QQ 以合并最新消息。"""
    try:
        return Path(str(db_path) + "-wal").stat().st_size
    except OSError:
        return 0
