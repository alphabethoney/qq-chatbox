"""Output scope guards and atomic export checkpoints."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

SCOPE_FILE = ".export-selection.json"


def scope_payload(db_path, selection, effective_self_qq=None, *, artifacts=None):
    result = {"version": 1, "source_db": str(Path(db_path).resolve()),
              "selection": selection.to_dict(),
              "effective_self_qq": str(effective_self_qq) if effective_self_qq is not None else None}
    if artifacts is not None:
        result["artifacts"] = artifacts
    return result


def validate_scope(out_dir, payload, *, active):
    """Read-only validation; call before creating or replacing any output."""
    out_dir = Path(out_dir)
    marker = out_dir / SCOPE_FILE
    if marker.exists():
        try:
            previous = json.loads(marker.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise ValueError("输出范围标记损坏，请使用新的 --out 目录") from exc
        if previous != payload:
            raise ValueError("输出范围/source/selection 不匹配，请使用新的 --out 目录")
    elif active and out_dir.exists() and any(out_dir.iterdir()):
        raise ValueError("已有输出没有选择范围标记，请使用新的 --out 目录")


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_scope(out_dir, payload, *, active):
    """Persist validated scope before output writes, retaining identity on failure."""
    if active or (Path(out_dir) / SCOPE_FILE).exists():
        atomic_json(Path(out_dir) / SCOPE_FILE, payload)


def load_checkpoint(path, payload, *, active):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError("增量状态损坏，请使用新的 --out 目录") from exc
    if not isinstance(state, dict) or not isinstance(state.get("last_ids"), dict):
        raise ValueError("增量状态格式无效，请使用新的 --out 目录")
    if type(state.get("version")) is int and state["version"] == 2:
        if state.get("scope") != payload:
            raise ValueError("增量状态范围不匹配，请使用新的 --out 目录")
    elif "version" in state or active:
        raise ValueError("旧增量状态或未知版本不能用于此导出，请使用新的 --out 目录")
    last_ids = state["last_ids"]
    if any(not isinstance(k, str) or type(v) is not int for k, v in last_ids.items()):
        raise ValueError("增量状态 last_ids 必须为表名到整数 ID 的映射")
    return dict(last_ids)


def save_checkpoint(path, payload, last_ids):
    atomic_json(path, {"version": 2, "scope": payload, "last_ids": last_ids})
