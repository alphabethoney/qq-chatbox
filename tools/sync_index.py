"""定时同步：把导出账号的新消息增量并入机器人索引，并推送到主机。

流程（每步可独立跑）：
    全量或缓存差分页解密 → SQL 增量严格导出 → 与持久累计/base 合并去重 → 暂存重建索引 → 提交水位线

水位线存入 --out/manifest.json 的 sync，累计导出存入 --out/.sync。
默认重读最近 300 秒，--full 可补窗口外迟到记录；--decrypt-cache 可复用未变页面，
但仍需完整明文库磁盘空间，累计合并和索引重建也仍是全量操作。
时间列没有索引时 SQLite 仍可能全表扫描。

用法:
    python tools/sync_index.py --passphrase <16字符口令> --person <QQ号> \
        [--base-json out/json] [--live-db <当前库路径>] [--out .chatbot] \
        [--host <用户>@<主机IP>] [--push]

口令可用 --passphrase 传，或用 --passphrase-file 指向一个只含口令的文件（避免出现在命令行历史里）。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 不内置默认账号路径（避免在仓库里泄露账号信息），调用方需显式传 --live-db。
DEFAULT_LIVE_DB: str | None = None


def _run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def decrypt(live_db: Path, plain_db: Path, *, key: str = "", passphrase: str = "",
            integrity_mode: str = "full", conversation: str | None = None,
            source_identity_path: Path | None = None) -> dict:
    from qq_exporter import sqlcipher

    if key:
        enc_key = bytes.fromhex(key)
    else:
        enc_key = sqlcipher.key_from_passphrase(live_db, passphrase)
    # 注意：QQ 9.9.x 的 -wal 文件用现有逻辑合并会损坏整库（quick_check 报 malformed），
    # 这里跳过 WAL，只解主库；任何完整性错误都会阻止提交水位线。
    options = {} if integrity_mode == "full" and conversation is None else {
        "integrity_mode": integrity_mode, "conversation": conversation}
    if source_identity_path is not None:
        options["source_identity_path"] = source_identity_path
    stats = sqlcipher.decrypt_database(live_db, plain_db, enc_key, merge_wal=False, progress=None, **options)
    print(f"解密完成: 页数={stats['pages']}  quick_check={stats['integrity']}", flush=True)
    if (stats.get("accepted") is not True if integrity_mode == "selected" else stats["integrity"] != "ok"):
        raise RuntimeError("完整性验收未通过，拒绝推进同步状态")
    return stats


def export_fault_tolerant(plain_db: Path, out_dir: Path, *, since: float | None = None,
                          immutable: bool = False, conversation: str | None = None,
                          self_qq: str | None = None) -> None:
    """Compatibility name; sync always requests a complete, strict export."""
    cmd = [sys.executable, str(ROOT / "tools/export_large.py"),
           "--db", str(plain_db), "--out", str(out_dir), "--format", "json", "--strict"]
    if conversation is not None:
        cmd += ["--conversation", conversation]
    if self_qq is not None:
        cmd += ["--self-qq", str(self_qq)]
    if immutable:
        cmd += ["--immutable"]
    if since is not None:
        cmd += ["--since", str(since)]
    _run(cmd)


def _load_json_dir(d: Path) -> dict[str, dict]:
    """读一个 export 目录，返回 {conversation_key: {meta, messages}}。"""
    index = json.loads((d / "index.json").read_text(encoding="utf-8"))
    convs: dict[str, dict] = {}
    if not isinstance(index.get("conversations"), list):
        raise ValueError(f"无效导出索引: {d}")
    for entry in index["conversations"]:
        key = entry.get("id")
        fname = entry.get("file")
        if not key or not fname or key in convs:
            raise ValueError(f"无效或重复会话条目: {entry}")
        fp = (d / fname).resolve()
        if not fp.is_relative_to(d.resolve()):
            raise ValueError("会话文件必须在导出目录内")
        data = json.loads(fp.read_text(encoding="utf-8"))
        if not isinstance(data.get("messages"), list):
            raise ValueError(f"无效消息列表: {fp}")
        if any(not isinstance(m, dict) for m in data["messages"]):
            raise ValueError(f"无效消息: {fp}")
        convs[key] = {
            "entry": {k: v for k, v in entry.items() if k != "file"},
            "messages": data.get("messages", []),
        }
    return convs


def merge(base_dir: Path, live_dir: Path, merged_dir: Path) -> None:
    """合并两个导出目录：按会话合并消息、按 msg_id 去重、按时间排序。"""
    base = _load_json_dir(base_dir)
    live = _load_json_dir(live_dir)
    merged_dir.mkdir(parents=True, exist_ok=True)

    all_keys = dict.fromkeys(list(base) + list(live))
    idx = []
    for i, key in enumerate(all_keys):
        b = base.get(key, {"entry": {}, "messages": []})
        l = live.get(key, {"entry": {}, "messages": []})
        entry = {**b["entry"], **l["entry"], "id": key, "kind": l["entry"].get("kind") or b["entry"].get("kind")}
        entry.setdefault("title", str(key))
        seen: set = set()
        msgs: list[dict] = []
        for m in b["messages"] + l["messages"]:
            mid = m.get("msg_id")
            identity = ("id", str(mid)) if mid is not None else ("body", json.dumps(m, sort_keys=True, ensure_ascii=False))
            if identity in seen:
                continue
            seen.add(identity)
            msgs.append(m)
        msgs.sort(key=lambda m: (_message_time(m.get("time")) or 0, str(m.get("msg_id") or "")))
        entry["message_count"] = len(msgs)
        entry["first_time"] = msgs[0].get("time") if msgs else None
        entry["last_time"] = msgs[-1].get("time") if msgs else None

        stem = f"{i:04d}_{_safe(entry['title'], key)}"
        entry["file"] = f"{stem}.json"
        (merged_dir / entry["file"]).write_text(
            json.dumps({"conversation": entry, "messages": msgs}, ensure_ascii=False),
            encoding="utf-8",
        )
        idx.append(entry)

    total = sum(e["message_count"] for e in idx)
    (merged_dir / "index.json").write_text(
        json.dumps({"conversation_count": len(idx), "message_count": total, "conversations": idx},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"合并完成: {len(idx)} 会话 / {total:,} 条 -> {merged_dir}", flush=True)


def _safe(name: str, fallback: str) -> str:
    import re

    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or "")).strip(". ")
    return (name or fallback)[:60]


def build(person: str, json_dir: Path, out: Path, *, config: Path | None = None) -> None:
    cmd = [sys.executable, "-m", "qq_chatbot", "build",
           "--json", str(json_dir), "--out", str(out)]
    if person:
        cmd += ["--person", person]
    if config is not None:
        cmd += ["--config", str(config)]
    _run(cmd)


def push(host: str, out: Path) -> None:
    _run(["scp", "-q", "-r", str(out), f"{host}:~/qq-chatbot/"])
    # 重启远端服务，加载新索引
    subprocess.run(["ssh", host, "systemctl --user restart qq-chatbot"], check=True)


def _message_time(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("无效消息时间戳")
    return value / 1000 if value > 10**12 else value


def _archive_digest(directory: Path) -> str:
    """Validate and fingerprint all referenced data, not just index metadata."""
    _load_json_dir(directory)
    index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256()
    for name in ["index.json"] + sorted(e["file"] for e in index["conversations"]):
        digest.update(name.encode("utf-8"))
        with (directory / name).open("rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    """Same-directory replace, with file contents flushed before publication."""
    fd, temporary = tempfile.mkstemp(prefix=".sync-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _publish(stage: Path, out: Path) -> None:
    # Existing readers use fixed filenames. Replace the commit record LAST;
    # interrupted publication can only cause a safe replay from the old state.
    # All writes/backups precede replacement; rollback needs no extra disk space.
    paths = sorted(p for p in stage.iterdir() if p.is_file() and p.name != "manifest.json")
    paths.append(stage / "manifest.json")
    with tempfile.TemporaryDirectory(prefix=".sync-publish-", dir=out, ignore_cleanup_errors=True) as temporary:
        transaction = Path(temporary)
        for source in paths:
            _atomic_write(transaction / source.name, source.read_bytes())
            target = out / source.name
            if target.exists():
                shutil.copyfile(target, transaction / (source.name + ".previous"))
        changed = []
        try:
            for source in paths:
                target = out / source.name
                os.replace(transaction / source.name, target)
                changed.append(target)
        except BaseException:
            for target in reversed(changed):
                previous = transaction / (target.name + ".previous")
                if previous.exists():
                    os.replace(previous, target)
                else:
                    target.unlink(missing_ok=True)
            raise


def _legacy_sync(*, live_db: Path, base_json: Path, work: Path, out: Path,
         person: str = "", key: str = "", passphrase: str = "",
         overlap_seconds: int = 300, full: bool = False, host: str | None = None,
         build_config: Path | None = None, decrypt_cache: Path | None = None,
         rebuild_decrypt_cache: bool = False, adopt_existing_history: bool = False) -> dict:
    """Commit manifest.sync only after complete export, cumulative merge and build.

    State timestamps use Unix seconds. --full repairs arrivals older than the
    bounded overlap; it does not force cache decryption. Plaintext storage and
    index rebuild remain full-sized. Source/base/person/overlap changes force
    a full read. A changed source identity starts a new lineage and preserves
    the old generation for recovery. Unknown legacy identity requires explicit
    adopt_existing_history before it can be retained with a cached source.
    """
    if (rebuild_decrypt_cache or adopt_existing_history) and decrypt_cache is None:
        raise ValueError("rebuild_decrypt_cache / adopt_existing_history 需要 decrypt_cache")
    if overlap_seconds < 1:
        raise ValueError("overlap_seconds 必须至少为 1，确保同秒消息不遗漏")
    live_db, base_json, work, out = (Path(p).resolve() for p in (live_db, base_json, work, out))
    if decrypt_cache is not None:
        decrypt_cache = Path(decrypt_cache).resolve()
        if decrypt_cache == out or decrypt_cache.is_relative_to(out):
            raise ValueError("decrypt_cache 必须位于索引输出目录之外")
    work.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    state_dir = out / ".sync"
    state_dir.mkdir(exist_ok=True)
    # OS locks are released on process death; no stale lock-file deletion needed.
    with (state_dir / "lock").open("a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            return _sync_locked(live_db=live_db, base_json=base_json, work=work, out=out,
                                person=person, key=key, passphrase=passphrase,
                                overlap_seconds=overlap_seconds, full=full, host=host,
                                build_config=Path(build_config).resolve() if build_config is not None else None,
                                decrypt_cache=decrypt_cache, rebuild_decrypt_cache=rebuild_decrypt_cache,
                                adopt_existing_history=adopt_existing_history)
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _sync_locked(*, live_db, base_json, work, out, person, key, passphrase,
                 overlap_seconds, full, host, build_config, decrypt_cache, rebuild_decrypt_cache,
                 adopt_existing_history):
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    previous = manifest.get("sync")
    config = {"live_db": str(live_db), "base_json": str(base_json), "person": person,
              "base_digest": _archive_digest(base_json) if base_json.exists() else None,
              "overlap_seconds": overlap_seconds,
              "build_config": str(build_config) if build_config is not None else None,
              "build_config_digest": hashlib.sha256(build_config.read_bytes()).hexdigest() if build_config is not None else None,
              "embedding_env_digest": hashlib.sha256(json.dumps({k: v for k, v in os.environ.items()
                  if k.startswith("QQ_CHATBOT_EMBEDDING_")}, sort_keys=True).encode()).hexdigest()}
    archive = None
    last = None
    if previous is not None:
        required = {"version", "archive", "archive_digest", "last_msg_time", "config"}
        if (not isinstance(previous, dict) or previous.get("version") != 1
                or not required.issubset(previous) or not isinstance(previous.get("config"), dict)
                or not isinstance(previous.get("archive"), str)):
            raise ValueError("未知或损坏的同步状态")
        archive = (out / previous["archive"]).resolve()
        if archive.parent != (out / ".sync").resolve() or not archive.name.startswith("data-"):
            raise ValueError("无效累计快照路径")
        if _archive_digest(archive) != previous["archive_digest"]:
            raise ValueError("累计快照校验失败，拒绝推进水位线")
        last = _message_time(previous["last_msg_time"])
        if previous["config"].get("live_db") != str(live_db):
            archive, last = None, None
    compatible = previous is not None and previous["config"] == config
    since = last - overlap_seconds if compatible and not full and last is not None else None
    source_identity = None
    generation = out / ".sync" / f"data-{uuid.uuid4().hex}"
    committed = False
    try:
        with tempfile.TemporaryDirectory(prefix="run-", dir=work, ignore_cleanup_errors=True) as temporary:
            run = Path(temporary)
            if decrypt_cache is None:
                plain_db = run / "plain" / "nt_msg.db"
                plain_db.parent.mkdir()
                decrypt(live_db, plain_db, key=key, passphrase=passphrase)
                print(f"同步模式: {'全量' if since is None else '增量'} since={since}", flush=True)
                export_fault_tolerant(plain_db, run / "live", since=since)
            else:
                from qq_exporter import sqlcipher
                from qq_exporter.decrypt_cache import open_decrypted_cache

                enc_key = bytes.fromhex(key) if key else sqlcipher.key_from_passphrase(live_db, passphrase)
                with open_decrypted_cache(live_db, decrypt_cache, enc_key,
                                          force_rebuild=rebuild_decrypt_cache) as (plain_db, stats):
                    source_identity = stats.get("source_identity")
                    if not isinstance(source_identity, str) or not source_identity:
                        raise ValueError("解密缓存缺少源身份，拒绝推进同步状态")
                    prior_identity = None
                    if archive is not None:
                        identity_path = archive / "source-identity.json"
                        if identity_path.exists():
                            prior_identity = json.loads(identity_path.read_text(encoding="utf-8")).get("source_identity")
                        if prior_identity is None:
                            if not adopt_existing_history:
                                raise ValueError("已有累计历史缺少源身份；请使用新 --out，或确认同源后指定 --adopt-existing-history")
                            since = None
                        elif source_identity != prior_identity:
                            # Compare to the message commit, not the last cache
                            # update: build failure may leave a refreshed cache.
                            # Preserve the old generation for explicit recovery.
                            archive, last, since = None, None, None
                            print("源身份变化：全量导出并开启新的累计快照；旧快照保留供恢复", flush=True)
                    print(f"解密缓存: 总页={stats['pages']} 解密={stats['decrypted_pages']} "
                          f"复用={stats['reused_pages']}", flush=True)
                    print(f"同步模式: {'全量' if since is None else '增量'} since={since}", flush=True)
                    export_fault_tolerant(plain_db, run / "live", since=since, immutable=True)
            # Cache lock is released before cumulative merging and index build.
            live_json = run / "live" / "json"
            live_index = json.loads((live_json / "index.json").read_text(encoding="utf-8"))
            report = live_index.get("export", {})
            if report.get("complete") is not True or report.get("errors") or report.get("since") != since:
                raise ValueError("导出不完整或过滤条件不匹配，拒绝推进水位线")
            live = _load_json_dir(live_json)
            times = [_message_time(m.get("time")) for c in live.values() for m in c["messages"]]
            if any(t is None for t in times):
                raise ValueError("导出含无时间戳消息，拒绝推进水位线")
            exported_last = max(times, default=None)
            if report.get("last_msg_time") != exported_last:
                raise ValueError("导出水位线与消息不一致")
            cumulative = archive or live_json
            if base_json.exists():
                merge(cumulative, base_json, run / "base")
                cumulative = run / "base"
            merge(cumulative, live_json, run / "merged")
            # Keep durable cumulative data outside disposable --work.
            stage = run / "index" / out.name
            if build_config is None:
                build(person, run / "merged", stage)
            else:
                build(person, run / "merged", stage, config=build_config)
            built_manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
            shutil.copytree(run / "merged", generation)
            if source_identity is not None:
                (generation / "source-identity.json").write_text(
                    json.dumps({"source_identity": source_identity}), encoding="utf-8")
            for path in generation.iterdir():
                with path.open("r+b") as fh:
                    os.fsync(fh.fileno())
            watermark = max((t for t in (last, exported_last) if t is not None), default=None)
            state = {"version": 1, "config": config, "last_msg_time": watermark,
                     "archive": generation.relative_to(out).as_posix(),
                     "archive_digest": _archive_digest(generation)}
            built_manifest["sync"] = state
            (stage / "manifest.json").write_text(json.dumps(built_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            if host is not None:
                remote_stage = run / "remote" / out.name
                remote_stage.mkdir(parents=True)
                for name in ("corpus.json", "embeddings.json"):
                    if (stage / name).exists():
                        shutil.copyfile(stage / name, remote_stage / name)
                remote_manifest = {k: v for k, v in built_manifest.items() if k != "sync"}
                (remote_stage / "manifest.json").write_text(
                    json.dumps(remote_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
                push(host, remote_stage)
            _publish(stage, out)
            committed = True
    finally:
        # Only remove the exact generation created by this run.
        if not committed and generation.exists():
            shutil.rmtree(generation)
    # Prune only the previously committed generation after the new commit.
    if archive is not None and archive != generation:
        try:
            shutil.rmtree(archive)
        except OSError as exc:
            print(f"警告: 旧累计快照清理失败: {exc}", flush=True)
    return state


def _scope_load(directory, conversation, self_qq, *, exclusive=False):
    """Read index metadata and only the selected body; never touch other bodies."""
    if conversation is None:
        return _load_json_dir(directory)
    from tools.export_large import conv_key_of
    index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
    entries = index.get("conversations")
    if not isinstance(entries, list):
        raise ValueError("无效会话索引")
    selected = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or entry["id"] in seen:
            raise ValueError("无效或重复会话索引")
        seen.add(entry["id"])
        if entry["id"] == conversation:
            selected.append(entry)
        elif exclusive:
            raise ValueError("累计/导出会话范围不匹配")
    result = {}
    for entry in selected:
        if entry.get("kind") != "c2c" or not isinstance(entry.get("file"), str):
            raise ValueError("会话范围不匹配")
        target = (directory / entry["file"]).resolve()
        if not target.is_relative_to(directory.resolve()) or target == directory.resolve():
            raise ValueError("会话文件路径越界")
        data = json.loads(target.read_text(encoding="utf-8"))
        if data.get("conversation", {}).get("id", conversation) != conversation:
            raise ValueError("会话正文元数据范围不匹配")
        messages = data.get("messages")
        if not isinstance(messages, list):
            raise ValueError("无效消息列表")
        for message in messages:
            if not isinstance(message, dict) or message.get("direction") not in ("in", "out"):
                raise ValueError("消息缺少可验证的会话身份")
            if self_qq is not None:
                expected = "out" if str(message.get("sender_qq")) == str(self_qq) else "in"
                if message["direction"] != expected:
                    raise ValueError("消息账号方向不匹配")
                # NTQQ may store the conversation partner, not the recipient,
                # in peer_qq on incoming rows. Accept both documented layouts;
                # archive account provenance still belongs to the caller.
                if expected == "in" and str(message.get("peer_qq")) not in (str(self_qq), str(message.get("sender_qq"))):
                    raise ValueError("入站消息的收件账号/会话对象无法验证或不匹配")
            try:
                actual = conv_key_of("c2c", message)[0]
            except (KeyError, TypeError) as exc:
                raise ValueError("消息缺少可验证的会话身份") from exc
            if actual != conversation:
                raise ValueError("消息会话范围不匹配")
        result[conversation] = {"entry": {k: v for k, v in entry.items() if k != "file"}, "messages": messages}
    return result


def _scope_digest(directory, conversation, self_qq, *, exclusive=False):
    if conversation is None:
        return _archive_digest(directory)
    data = _scope_load(directory, conversation, self_qq, exclusive=exclusive)
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _project(directory, data):
    directory.mkdir(parents=True, exist_ok=True)
    entries = []
    for number, item in enumerate(data.values()):
        entry = {**item["entry"], "file": f"{number}.json"}
        (directory / entry["file"]).write_text(json.dumps({"conversation": entry,
            "messages": item["messages"]}, ensure_ascii=False), encoding="utf-8")
        entries.append(entry)
    (directory / "index.json").write_text(json.dumps({"conversations": entries}), encoding="utf-8")


def _check_corpus(stage, conversation):
    if conversation is None:
        return
    corpus = json.loads((stage / "corpus.json").read_text(encoding="utf-8"))
    if not isinstance(corpus, dict) or not isinstance(corpus.get("turns"), list):
        raise ValueError("构建产物缺少真实 corpus.turns")
    if any(not isinstance(turn, dict) or turn.get("conversation_key") != conversation
           or turn.get("kind") != "c2c" for turn in corpus["turns"]):
        raise ValueError("构建产物会话范围不匹配")


def _read_key_file(path, source, self_qq):
    text = Path(path).read_text(encoding="utf-8-sig").strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise ValueError("无效密钥文件 JSON") from exc
        key = data.get("key")
        # The caller must explicitly name the source; metadata never redirects it.
        if data.get("db") and Path(data["db"]).resolve() != source:
            raise ValueError("密钥文件数据库路径与显式源不匹配")
        if self_qq is not None and data.get("account") is not None and str(data["account"]) != str(self_qq):
            raise ValueError("密钥文件账号不匹配")
    else:
        key = text
    try:
        if not isinstance(key, str) or len(bytes.fromhex(key)) != 32:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("密钥文件必须包含 32 字节 hex 密钥") from None
    return key


@contextlib.contextmanager
def _sync_lock(out):
    state = out / ".sync"
    state.mkdir(parents=True, exist_ok=True)
    with (state / "lock").open("a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def sync(*, live_db: Path, base_json: Path, work: Path, out: Path,
         person: str = "", key: str = "", passphrase: str = "",
         overlap_seconds: int = 300, full: bool = False, host: str | None = None,
         build_config: Path | None = None, decrypt_cache: Path | None = None,
         rebuild_decrypt_cache: bool = False, adopt_existing_history: bool = False,
         conversation: str | None = None, self_qq: str | None = None,
         key_file: Path | None = None, snapshot_mode: str = "provided",
         integrity_mode: str = "full") -> dict:
    """Sync a single private conversation, or preserve legacy full-range behavior.

    API defaults to provided for backward compatibility; CLI defaults to
    offline-copy. provided requires a caller-supplied stable database. selected
    validates the requested conversation and is not whole-database integrity.
    Scope/policy changes require a new --out; migrate history via --base-json.
    """
    from qq_exporter.selection import Selection
    if snapshot_mode not in ("offline-copy", "provided") or integrity_mode not in ("full", "selected"):
        raise ValueError("无效快照/完整性策略")
    selection = Selection(conversations=(conversation,) if conversation else (), self_qq=self_qq)
    conversation = selection.conversations[0] if selection.conversations else None
    self_qq = selection.self_qq
    if conversation is not None and not conversation.startswith("c2c:"):
        raise ValueError("sync 仅支持单个 c2c:ID 范围")
    if integrity_mode == "selected" and conversation is None:
        raise ValueError("selected 需要 conversation")
    if sum(bool(v) for v in (key, passphrase, key_file)) > 1:
        raise ValueError("key / passphrase / key_file 互斥")
    if snapshot_mode == 'offline-copy' and Path(live_db).absolute() != Path(live_db).resolve():
        raise ValueError('离线快照要求源数据库实际路径，不接受链接别名')
    live_db, base_json, work, out = (Path(p).resolve() for p in (live_db, base_json, work, out))
    if key_file is not None:
        key = _read_key_file(key_file, live_db, self_qq)
    # Preserve historical signatures and mock-based callers of the unscoped API.
    prior_path = out / "manifest.json"
    prior = json.loads(prior_path.read_text(encoding="utf-8")).get("sync") if prior_path.exists() else None
    if conversation is None and self_qq is None and snapshot_mode == "provided" and (not prior or prior.get("version") != 2):
        return _legacy_sync(live_db=live_db, base_json=base_json, work=work, out=out,
            person=person, key=key, passphrase=passphrase, overlap_seconds=overlap_seconds,
            full=full, host=host, build_config=build_config, decrypt_cache=decrypt_cache,
            rebuild_decrypt_cache=rebuild_decrypt_cache, adopt_existing_history=adopt_existing_history)
    if overlap_seconds < 1:
        raise ValueError("overlap_seconds 必须至少为 1")
    if (rebuild_decrypt_cache or adopt_existing_history) and decrypt_cache is None:
        raise ValueError("缓存选项需要 decrypt_cache")
    if decrypt_cache is not None:
        decrypt_cache = Path(decrypt_cache).resolve()
        if decrypt_cache == out or decrypt_cache.is_relative_to(out):
            raise ValueError("decrypt_cache 必须位于索引目录之外")
    if build_config is not None:
        build_config = Path(build_config).resolve()
    work.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    report_path = work / f"sync-report-{uuid.uuid4().hex}.json"
    report = {"version": 1, "stage": "validate", "status": "running",
              "conversation": conversation, "self_qq": self_qq,
              "snapshot_mode": snapshot_mode, "integrity_mode": integrity_mode}
    stage_started = time.perf_counter()
    report["durations_seconds"] = {}
    def progress(stage):
        nonlocal stage_started
        now = time.perf_counter()
        previous_stage = report["stage"]
        report["durations_seconds"][previous_stage] = report["durations_seconds"].get(previous_stage, 0) + round(now - stage_started, 6)
        stage_started = now
        report["stage"] = stage
        _atomic_write(report_path, json.dumps(report, ensure_ascii=False, indent=2).encode())
    generation = out / ".sync" / f"data-{uuid.uuid4().hex}"
    committed = False
    publication_backup = None
    try:
        progress("validate")
        with _sync_lock(out):
            manifest = json.loads(prior_path.read_text(encoding="utf-8")) if prior_path.exists() else {}
            previous = manifest.get("sync")
            scope = {"source": str(live_db), "conversation": conversation, "self_qq": self_qq,
                     "person": person, "integrity_mode": integrity_mode, "snapshot_mode": snapshot_mode,
                     "validation_policy": 1}
            config = {"scope": scope, "base_json": str(base_json), "overlap_seconds": overlap_seconds,
                      "base_digest": _scope_digest(base_json, conversation, self_qq) if base_json.exists() else None,
                      "build_config_digest": hashlib.sha256(build_config.read_bytes()).hexdigest() if build_config else None,
                      "embedding_env_digest": hashlib.sha256(json.dumps({k: v for k, v in os.environ.items()
                          if k.startswith("QQ_CHATBOT_EMBEDDING_")}, sort_keys=True).encode()).hexdigest()}
            archive, last = None, None
            if previous is not None and isinstance(previous, dict) and previous.get("version") == 1 and conversation is None and self_qq is None:
                # Unscoped CLI upgrades keep the old archive. Narrowing a v1
                # archive is forbidden and must use a fresh output + base.
                previous = {**previous, "version": 2, "scope": scope}
            if previous is not None:
                if not isinstance(previous, dict) or previous.get("version") != 2 or previous.get("scope") != scope:
                    raise ValueError("同步范围/源身份/策略变化；请使用新 --out 并通过 --base-json 迁移旧历史")
                if not isinstance(previous.get("archive"), str) or not isinstance(previous.get("config"), dict):
                    raise ValueError("无效同步状态")
                archive = (out / previous["archive"]).resolve()
                if archive.parent != (out / ".sync").resolve() or not archive.name.startswith("data-"):
                    raise ValueError("无效累计快照路径")
                if _scope_digest(archive, conversation, self_qq, exclusive=True) != previous.get("archive_digest"):
                    raise ValueError("累计快照校验失败")
                last = _message_time(previous.get("last_msg_time"))
            elif manifest and conversation is not None:
                raise ValueError("现有索引缺少范围状态；请使用新 --out")
            since = last - overlap_seconds if previous and previous["config"] == config and last is not None and not full else None
            from qq_exporter.snapshot import open_snapshot
            progress("snapshot")
            # Snapshot context spans export, build and publication. A plaintext
            # cache rebuild may temporarily retain both old and new plaintext.
            needed = live_db.stat().st_size * (2 if decrypt_cache else 1) + 16 * 1024 * 1024
            with contextlib.ExitStack() as snapshots:
                snapshot = snapshots.enter_context(open_snapshot(live_db, work, mode=snapshot_mode, required_free_bytes=needed))
                report["snapshot"] = snapshot.metadata
                with tempfile.TemporaryDirectory(prefix="run-", dir=work, ignore_cleanup_errors=True) as temporary:
                    run = Path(temporary)
                    progress("decrypt")
                    identity = None
                    options = {"integrity_mode": integrity_mode, "conversation": conversation}
                    export_options = {"since": since, "immutable": True, "conversation": conversation, "self_qq": self_qq}
                    if decrypt_cache is None:
                        plain = run / "plain.db"
                        stats = decrypt(snapshot.path, plain, key=key, passphrase=passphrase,
                                        source_identity_path=live_db, **options)
                        identity = (stats or {}).get("source_identity")
                        if not isinstance(identity, str) or not identity:
                            raise ValueError("解密缺少源身份")
                        if previous and previous.get("source_identity") != identity and (conversation is not None or "source_identity" in previous):
                            raise ValueError("源身份变化；请使用新 --out")
                        progress("export")
                        export_fault_tolerant(plain, run / "live", **export_options)
                    else:
                        from qq_exporter import sqlcipher
                        from qq_exporter.decrypt_cache import open_decrypted_cache
                        enc_key = bytes.fromhex(key) if key else sqlcipher.key_from_passphrase(snapshot.path, passphrase)
                        with open_decrypted_cache(snapshot.path, decrypt_cache, enc_key,
                                force_rebuild=rebuild_decrypt_cache, source_identity_path=live_db, **options) as (plain, stats):
                            identity = stats.get("source_identity")
                            if not isinstance(identity, str) or not identity:
                                raise ValueError("缓存缺少源身份")
                            if previous and previous.get("source_identity") != identity and (conversation is not None or "source_identity" in previous):
                                raise ValueError("源身份变化；请使用新 --out")
                            if stats.get("accepted") is not True:
                                raise ValueError("缓存完整性验收未通过")
                            progress("export")
                            export_fault_tolerant(plain, run / "live", **export_options)
                    report["integrity"] = {k: v for k, v in (stats or {}).items() if k in (
                        "accepted", "integrity", "full_integrity_ok", "selected_integrity_ok", "pages",
                        "decrypted_pages", "reused_pages", "nonzero_pages_hmac_verified", "zero_pages", "hmac_verified")}
                    live_json = run / "live" / "json"
                    exported = json.loads((live_json / "index.json").read_text(encoding="utf-8")).get("export", {})
                    if (exported.get("complete") is not True or exported.get("errors") or exported.get("since") != since
                            or exported.get("selection") != selection.to_dict()
                            or (self_qq is not None and str(exported.get("effective_self_qq")) != self_qq)):
                        raise ValueError("导出不完整或范围条件不匹配")
                    effective_self = exported.get("effective_self_qq")
                    if previous and "effective_self_qq" in previous and previous.get("effective_self_qq") != effective_self:
                        raise ValueError("导出账号身份变化；请使用新 --out")
                    live = _scope_load(live_json, conversation, effective_self, exclusive=True)
                    times = [_message_time(m.get("time")) for c in live.values() for m in c["messages"]]
                    if any(t is None for t in times):
                        raise ValueError("导出消息时间无效")
                    exported_last = max(times, default=None)
                    if exported.get("last_msg_time") != exported_last:
                        raise ValueError("导出水位线与消息不一致")
                    progress("merge")
                    _project(run / "cumulative", _scope_load(archive, conversation, effective_self, exclusive=True) if archive else {})
                    cumulative = run / "cumulative"
                    if base_json.exists():
                        _project(run / "base", _scope_load(base_json, conversation, effective_self))
                        merge(cumulative, run / "base", run / "with-base")
                        cumulative = run / "with-base"
                    merge(cumulative, live_json, run / "merged")
                    merged = _scope_load(run / "merged", conversation, effective_self, exclusive=True)
                    if not any(c["messages"] for c in merged.values()):
                        snapshot.verify()
                        report["status"] = "no_target"
                        progress("complete")
                        return previous or {"status": "no_target", "last_msg_time": None}
                    progress("build")
                    stage = run / "index" / out.name
                    build(person, run / "merged", stage, **({"config": build_config} if build_config else {}))
                    _check_corpus(stage, conversation)
                    built = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
                    shutil.copytree(run / "merged", generation)
                    state = {"version": 2, "scope": scope, "config": config,
                             "source_identity": identity, "effective_self_qq": effective_self,
                             "archive": generation.relative_to(out).as_posix(),
                             "archive_digest": _scope_digest(generation, conversation, effective_self, exclusive=True),
                             "last_msg_time": max((t for t in (last, exported_last) if t is not None), default=None)}
                    built["sync"] = state
                    (stage / "manifest.json").write_text(json.dumps(built, ensure_ascii=False, indent=2), encoding="utf-8")
                    progress("verify")
                    snapshot.verify()
                    if host is not None:
                        remote = run / "remote" / out.name
                        remote.mkdir(parents=True)
                        for name in ("corpus.json", "embeddings.json"):
                            if (stage / name).exists():
                                shutil.copyfile(stage / name, remote / name)
                        public = {k: v for k, v in built.items() if k in ("format_version", "generation_id",
                            "embedding_provider", "embedding_model", "dim", "turn_count", "fallback_embedding")}
                        public["scope"] = {"conversation": conversation, "self_qq": effective_self,
                                           "person": person, "integrity_mode": integrity_mode}
                        (remote / "manifest.json").write_text(json.dumps(public), encoding="utf-8")
                        progress("push")
                        push(host, remote)
                        snapshot.verify()
                    progress("publish")
                    snapshots.close()  # final source verification MUST precede publication
                    _publish(stage, out)
                    committed = True
            report["status"] = "ok"
            try:
                progress("complete")
            except OSError:
                pass  # A diagnostic write cannot undo a committed watermark.
            # Old generations remain available for recovery; never delete history
            # on identity or policy rejection.
            return state
    except BaseException as exc:
        report["status"] = "source_busy" if type(exc).__name__ == "SourceBusy" else "failed"
        report["error_type"] = type(exc).__name__
        try:
            progress(report["stage"])
        except OSError:
            pass
        raise
    finally:
        if not committed and generation.exists():
            shutil.rmtree(generation)


def main() -> int:
    ap = argparse.ArgumentParser()
    auth = ap.add_mutually_exclusive_group(required=True)
    auth.add_argument("--passphrase", help="16 字符 ASCII 口令")
    auth.add_argument("--passphrase-file", help="只含口令的文件")
    auth.add_argument("--key", help="32 字节密钥（hex 64 字符）")
    auth.add_argument("--key-file", type=Path, help="现有 key JSON 或 raw hex 文件")
    ap.add_argument("--conversation", help="仅同步单个私聊 c2c:ID，与 --person 独立")
    ap.add_argument("--self-qq", help="显式指定自己的 QQ 号")
    ap.add_argument("--snapshot-mode", choices=("offline-copy", "provided"), default="offline-copy",
                    help="默认 offline-copy；provided 要求调用者提供稳定副本（API 默认 provided 兼容旧调用）")
    ap.add_argument("--integrity-mode", choices=("full", "selected"), default="full",
                    help="selected 需要 --conversation，仅验收选定范围")
    ap.add_argument("--live-db", default=str(DEFAULT_LIVE_DB) if DEFAULT_LIVE_DB else None, help="导出账号当前库路径")
    ap.add_argument("--person", default="", help="只索引这个 QQ 号的人")
    ap.add_argument("--base-json", default="out/json", help="全量历史导出目录（首次同步保留）")
    ap.add_argument("--work", default="_work/sync", help="中间文件目录")
    ap.add_argument("--out", default=".chatbot", help="索引输出目录")
    ap.add_argument("--host", default="", help="机器人主机 SSH 地址")
    ap.add_argument("--push", action="store_true", help="推送到主机并重启服务")
    ap.add_argument("--overlap-seconds", type=int, default=300, help="重读窗口秒数（至少 1，默认 300）")
    ap.add_argument("--config", type=Path, help="透传给索引 build 的配置文件（含 embedding 配置）")
    ap.add_argument("--full", action="store_true", help="强制全量读取，补回窗口外迟到消息")
    ap.add_argument("--decrypt-cache", type=Path, help="持久差分页解密缓存目录（含明文，须在索引目录之外）")
    ap.add_argument("--rebuild-decrypt-cache", action="store_true", help="强制重建解密缓存，需要 --decrypt-cache")
    ap.add_argument("--adopt-existing-history", action="store_true", help="确认现有无源身份的累计历史与当前源同属一个账号，保留并全量合并；需要 --decrypt-cache")
    args = ap.parse_args()
    if (args.rebuild_decrypt_cache or args.adopt_existing_history) and args.decrypt_cache is None:
        ap.error("--rebuild-decrypt-cache / --adopt-existing-history 需要 --decrypt-cache")
    if args.overlap_seconds < 1:
        ap.error("--overlap-seconds 必须至少为 1")
    if args.push and not args.host:
        ap.error("--push 需要 --host")

    if args.integrity_mode == "selected" and not args.conversation:
        ap.error("--integrity-mode selected 需要 --conversation")
    if not args.live_db:
        ap.error("需要显式 --live-db；不会使用密钥文件中的路径作为源")
    try:
        passphrase = args.passphrase or ""
        if args.passphrase_file:
            passphrase = Path(args.passphrase_file).read_text(encoding="utf-8").strip()
        sync(live_db=Path(args.live_db), base_json=Path(args.base_json), work=Path(args.work),
             out=Path(args.out), person=args.person, key=args.key or "", passphrase=passphrase or "",
             overlap_seconds=args.overlap_seconds, full=args.full,
             host=args.host if args.push else None, build_config=args.config,
             decrypt_cache=args.decrypt_cache, rebuild_decrypt_cache=args.rebuild_decrypt_cache,
             adopt_existing_history=args.adopt_existing_history,
             conversation=args.conversation, self_qq=args.self_qq, key_file=args.key_file,
             snapshot_mode=args.snapshot_mode, integrity_mode=args.integrity_mode)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"同步失败（未提交新水位线）: {type(exc).__name__}", file=sys.stderr, flush=True)
        return 3 if type(exc).__name__ == "SourceBusy" else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
