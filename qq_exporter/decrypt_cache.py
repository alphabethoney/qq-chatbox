"""Locked incremental decryption of a *stable offline* NTQQ main database.

No WAL is merged. Both source and sidecars are checked before/after reading;
these checks cannot turn a live writer into a stable snapshot. Consumers MUST
keep ``open_decrypted_cache`` entered for their entire read and must not write
the yielded database. The cache directory is private application state, not an
output directory for unrelated files.

SQLite stores per-page cipher/plain SHA256 and a durable undo/redo set. PREPARE
commits all old bytes (including truncated tails) before any plaintext overwrite.
APPLY writes only changed pages, fsyncs, checks SQLite and rechecks the source.
COMMIT atomically installs hashes/identity and removes undo. Recovery with undo
always restores the old size/bytes, fsyncs, then removes undo; it is idempotent.
Initialization/forced or identity rebuilds may stage an entire database (and
old baseline) in SQLite. Normal updates stage only changed/removed pages.
Metadata contains fingerprints, never encryption keys or passphrases. Local
plaintext is fully hashed on reuse; the metadata directory itself is trusted.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import time

from . import sqlcipher as sc

_PAGE = sc.PAGE_SIZE
_SIDECARS = ("-wal", "-shm", "-journal")


def _checkpoint(name: str) -> None:
    """Fault-injection seam; deliberately has no environment-variable trigger."""


def _digest(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _stamp(st) -> tuple:
    # Windows Python versions disagree on ctime (birth vs change time) between
    # stat and fstat. Page/sidecar rehashes still detect restored-mtime writes.
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
            st.st_ctime_ns if os.name != "nt" else None)


def _sidecars(src: Path) -> tuple:
    result = []
    for suffix in _SIDECARS:
        path = Path(str(src) + suffix)
        try:
            with path.open("rb") as fh:
                before = _stamp(os.fstat(fh.fileno()))
                digest = hashlib.sha256()
                for block in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(block)
                if before != _stamp(os.fstat(fh.fileno())) or before != _stamp(path.stat()):
                    raise ValueError("sidecar 在读取期间发生变化；请使用稳定的离线快照")
                result.append((suffix, before, digest.digest()))
        except FileNotFoundError:
            result.append((suffix, None))
    return tuple(result)


def _guard_paths(src: Path, root: Path) -> None:
    protected = [src] + [Path(str(src) + s) for s in _SIDECARS]
    owned = [root / name for name in ("cache.lock", "plain.db", "metadata.sqlite3")]
    owned += [Path(str(p) + s) for p in owned[1:] for s in _SIDECARS]
    for path in owned:
        if path.is_symlink() or (path.exists() and path.stat().st_nlink > 1):
            raise ValueError("缓存路径不能使用链接别名")
        for source in protected:
            if path.resolve() == source.resolve() or (
                path.exists() and source.exists() and path.samefile(source)
            ):
                raise ValueError("缓存路径不能覆盖源文件或 sidecar（包括链接别名）")
    if any(Path(str(root / "plain.db") + s).exists() for s in _SIDECARS):
        raise ValueError("缓存明文存在 SQLite sidecar；消费者必须只读")


@contextmanager
def _lock(path: Path):
    # A separate OS handle per caller also serializes threads in this process.
    with path.open("a+b") as fh:
        if os.fstat(fh.fileno()).st_size == 0:
            fh.write(b"\0")
            fh.flush()
        if os.name == "nt":
            import msvcrt
            while True:
                fh.seek(0)
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in (13, 11, 36):
                        raise
                    time.sleep(0.05)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _connect(path: Path):
    fresh = not path.exists() or path.stat().st_size == 0
    conn = sqlite3.connect(path)
    try:
        if fresh:
            conn.execute("PRAGMA auto_vacuum=FULL")
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS hashes (pg INTEGER PRIMARY KEY, cipher BLOB NOT NULL, plain BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS pending (id INTEGER PRIMARY KEY CHECK(id=1), old_size INTEGER NOT NULL, existed INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS changes (pg INTEGER PRIMARY KEY, old BLOB, new BLOB, cipher BLOB, plain BLOB);
        """)
        return conn
    except BaseException:
        conn.close()
        raise


def _sync(fh):
    fh.flush()
    os.fsync(fh.fileno())


def _recover(conn, plain: Path) -> bool:
    row = conn.execute("SELECT old_size, existed FROM pending WHERE id=1").fetchone()
    if row is None:
        return False
    old_size, existed = row
    with plain.open("r+b" if plain.exists() else "w+b") as fh:
        for pg, old in conn.execute("SELECT pg, old FROM changes WHERE old IS NOT NULL ORDER BY pg"):
            fh.seek((pg - 1) * _PAGE)
            fh.write(old)
            _checkpoint("recovery_page")
        fh.truncate(old_size)
        _sync(fh)
    if not existed:
        plain.unlink()
    _checkpoint("recovery_synced")
    with conn:
        conn.execute("DELETE FROM pending")
        conn.execute("DELETE FROM changes")
    return True


def _verify_source(fin, src, stamp, sides, header, conn, total):
    if _stamp(os.fstat(fin.fileno())) != stamp or _stamp(src.stat()) != stamp:
        raise ValueError("源文件在读取期间发生变化或被替换；请使用稳定的离线快照")
    fin.seek(0)
    if fin.read(sc.EXT_HEADER) != header:
        raise ValueError("源文件头在读取期间发生变化")
    # Re-read hashes to catch in-place modifications even with restored mtime.
    for pg in range(1, total + 1):
        row = conn.execute("SELECT cipher FROM changes WHERE pg=? UNION ALL SELECT cipher FROM hashes WHERE pg=? AND NOT EXISTS(SELECT 1 FROM changes WHERE pg=?)", (pg, pg, pg)).fetchone()
        if row is None or _digest(fin.read(_PAGE)) != row[0]:
            raise ValueError("源页面在读取期间发生变化；请使用稳定的离线快照")
    if (fin.read(1) or _stamp(os.fstat(fin.fileno())) != stamp
            or _stamp(src.stat()) != stamp or _sidecars(src) != sides):
        raise ValueError("源文件或 sidecar 在读取期间发生变化；请使用稳定的离线快照")


def _update(src, root, enc_key, reserve, progress, force_rebuild):
    plain = root / "plain.db"
    conn = _connect(root / "metadata.sqlite3")
    try:
        recovered = _recover(conn, plain)
        row = conn.execute("SELECT value FROM state WHERE id=1").fetchone()
        previous = json.loads(row[0]) if row else None
        if previous is None and plain.exists():
            raise ValueError("缓存缺少元数据，拒绝覆盖现有明文文件")
        sides = _sidecars(src)
        with src.open("rb") as fin:
            stamp = _stamp(os.fstat(fin.fileno()))
            if stamp != _stamp(src.stat()):
                raise ValueError("源文件被替换；请使用稳定的离线快照")
            size = stamp[2] - sc.EXT_HEADER
            if size < _PAGE or size % _PAGE:
                raise ValueError("数据库内容不足一页或末页截断")
            total = size // _PAGE
            header = fin.read(sc.EXT_HEADER)
            if header[:16] != b"SQLite header 3\x00":
                raise ValueError("不是 NTQQ 数据库头部")
            first = fin.read(_PAGE)
            # Authenticate key/layout without an AES call on a fully reused page.
            if reserve is None:
                reserve = next((r for r in sc.RESERVE_CANDIDATES
                                if r >= sc.IV_SIZE + sc.HMAC_SIZE and r % 16 == 0
                                and sc.verify_page1_hmac(first, enc_key, r)), None)
            if (reserve is None or reserve < sc.IV_SIZE + sc.HMAC_SIZE
                    or reserve >= _PAGE - sc.SALT_SIZE or reserve % 16
                    or not sc.verify_page1_hmac(first, enc_key, reserve)):
                raise ValueError("第 1 页 HMAC 校验失败：密钥或布局不匹配")
            salt = first[:sc.SALT_SIZE]
            hmac_key = sc.derive_hmac_key(enc_key, salt)
            identity = hashlib.sha256(os.path.normcase(str(src.resolve())).encode("utf-8")
                                      + b"\0" + header + salt + str(reserve).encode()).hexdigest()
            key_id = hmac.new(enc_key, b"qq-exporter-cache-key-v1\0" + salt, hashlib.sha256).hexdigest()
            current = {"version": 1, "source_identity": identity, "key_fingerprint": key_id,
                       "reserve": reserve, "pages": total}
            rebuilt = force_rebuild or previous is None or any(
                previous.get(k) != current[k] for k in ("version", "source_identity", "key_fingerprint", "reserve")
            ) or not plain.exists()
            existed = plain.exists()
            old_size = plain.stat().st_size if existed else 0
            old_pages = old_size // _PAGE
            if previous and not rebuilt and old_size != previous["pages"] * _PAGE:
                raise ValueError("缓存明文长度损坏；请显式重建")
            # No writes to plaintext until the entire durable preparation commits.
            changed = reused = journal_bytes = written = 0
            old_fh = plain.open("rb") if existed else None
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM changes")
                fin.seek(sc.EXT_HEADER)
                for pg in range(1, max(total, old_pages) + 1):
                    old = old_fh.read(_PAGE) if old_fh and pg <= old_pages else None
                    known = conn.execute("SELECT cipher, plain FROM hashes WHERE pg=?", (pg,)).fetchone()
                    if not rebuilt and pg <= old_pages and (known is None or _digest(old) != known[1]):
                        raise ValueError(f"缓存明文第 {pg} 页完整性校验失败；请显式重建")
                    if pg <= total:
                        encrypted = fin.read(_PAGE)
                        if len(encrypted) != _PAGE:
                            raise ValueError("源页面在读取期间发生变化")
                        cipher_hash = _digest(encrypted)
                        if not rebuilt and known and old is not None and cipher_hash == known[0]:
                            reused += 1
                        else:
                            decoded = sc._decode_checked_page(encrypted, enc_key, pg, reserve, hmac_key, salt)
                            conn.execute("INSERT INTO changes VALUES(?,?,?,?,?)", (pg, old, decoded, cipher_hash, _digest(decoded)))
                            changed += 1
                            journal_bytes += len(old or b"") + len(decoded)
                    else:
                        conn.execute("INSERT INTO changes VALUES(?,?,?,?,?)", (pg, old, None, None, None))
                        journal_bytes += len(old or b"")
                    if progress and pg <= total:
                        progress(pg, total)
                # Back up any partial damaged tail during explicit rebuild too.
                if old_fh and old_size % _PAGE:
                    tail = old_fh.read()
                    pg = old_pages + 1
                    existing = conn.execute("SELECT 1 FROM changes WHERE pg=?", (pg,)).fetchone()
                    if existing:
                        conn.execute("UPDATE changes SET old=? WHERE pg=?", (tail, pg))
                    else:
                        conn.execute("INSERT INTO changes VALUES(?,?,?,?,?)", (pg, tail, None, None, None))
                    journal_bytes += len(tail)
                _verify_source(fin, src, stamp, sides, header, conn, total)
                conn.execute("INSERT INTO pending VALUES(1,?,?)", (old_size, int(existed)))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                if old_fh:
                    old_fh.close()
            try:
                _checkpoint("prepared")
                with plain.open("r+b" if existed else "w+b") as fout:
                    for pg, decoded in conn.execute("SELECT pg,new FROM changes WHERE new IS NOT NULL ORDER BY pg"):
                        fout.seek((pg - 1) * _PAGE)
                        fout.write(decoded)
                        written += len(decoded)
                        _checkpoint("applied_page")
                    if old_size != total * _PAGE:
                        fout.truncate(total * _PAGE)
                    _checkpoint("truncated")
                    _sync(fout)
                _checkpoint("plain_synced")
                integrity = sc.quick_check(plain)
                if integrity != "ok":
                    first_change = conn.execute("SELECT new FROM changes WHERE pg=1").fetchone()
                    if first_change and first_change[0] is not None and first_change[0][20] != reserve:
                        fixed = bytearray(first_change[0])
                        fixed[20] = reserve
                        fixed = bytes(fixed)
                        # Old page was durably journaled already. Persist the new
                        # hash before overwriting; recovery continues using old.
                        with conn:
                            conn.execute("UPDATE changes SET new=?,plain=? WHERE pg=1", (fixed, _digest(fixed)))
                        with plain.open("r+b") as fout:
                            fout.write(fixed)
                            written += len(fixed)
                            _sync(fout)
                        integrity = sc.quick_check(plain)
                if integrity != "ok":
                    raise ValueError(f"缓存 SQLite quick_check 失败: {integrity}")
                _verify_source(fin, src, stamp, sides, header, conn, total)
                _checkpoint("before_commit")
                with conn:
                    if rebuilt:
                        conn.execute("DELETE FROM hashes")
                    conn.execute("INSERT OR REPLACE INTO hashes SELECT pg,cipher,plain FROM changes WHERE new IS NOT NULL")
                    conn.execute("DELETE FROM hashes WHERE pg>?", (total,))
                    conn.execute("INSERT OR REPLACE INTO state VALUES(1,?)", (json.dumps(current, sort_keys=True),))
                    conn.execute("DELETE FROM changes")
                    conn.execute("DELETE FROM pending")
                _checkpoint("committed")
            except BaseException:
                conn.rollback()
                _recover(conn, plain)
                raise
        return plain, {"ok": True, "pages": total, "decrypted_pages": changed,
                       "reused_pages": reused, "removed_pages": max(0, old_pages - total),
                       "integrity": integrity, "rebuilt": rebuilt, "recovered": recovered,
                       "source_identity": identity, "reserve": reserve, "hmac_verified": True,
                       "wal_pages": 0, "bytes": total * _PAGE,
                       "journal_bytes": journal_bytes, "written_bytes": written}
    finally:
        conn.close()


@contextmanager
def open_decrypted_cache(src: Path, cache_dir: Path, enc_key: bytes, *,
                         reserve=None, progress=None, force_rebuild=False):
    """Yield ``(plain_path, stats)`` under an exclusive cross-process lock.

    HMAC verification cannot be disabled. Failure before commit preserves the old
    baseline. Consumer exceptions after yield leave the committed cache reusable.
    A bad local plaintext hash fails closed; ``force_rebuild=True`` repairs it.
    Source must be a stable offline snapshot, including unchanged sidecars.
    Read SQLite through ``mode=ro&immutable=1`` to avoid creating WAL/SHM.

    Each successful update reads source pages three times (prepare, pre-apply
    validation, pre-commit validation) and hashes all existing plaintext pages.
    ``decrypted_pages`` counts actual codec calls, including page 1; HMAC layout
    probing does not decrypt. ``journal_bytes`` counts old/new page payloads,
    excluding SQLite index/rollback-journal overhead; ``written_bytes`` counts
    plaintext page writes, including a possible reserve-byte correction.
    Auto-vacuum reclaims staged payloads at commit, leaving per-page hash metadata.
    """
    src, root = Path(src).absolute(), Path(cache_dir).absolute()
    if len(enc_key) != sc.KEY_SIZE:
        raise ValueError("SQLCipher AES-256 密钥必须为 32 字节")
    _guard_paths(src, root)
    root.mkdir(parents=True, exist_ok=True)
    with _lock(root / "cache.lock"):
        _guard_paths(src, root)
        result = _update(src, root, enc_key, reserve, progress, force_rebuild)
        yield result
