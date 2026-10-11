"""媒体（图片/文件/语音/视频/表情）信息提取与下载。"""

from __future__ import annotations

import shutil
import urllib.request
from pathlib import Path

from . import wire

# 内容类型(45002) -> 媒体类别
MEDIA_TYPE_NAMES = {
    2: "image",
    3: "file",
    4: "voice",
    5: "video",
    7: "sticker",
}

# 本地缓存路径前缀：45403 里的 ``::NTOSFull::D:\\...`` 去掉前缀即本地文件
LOCAL_PREFIX = "::NTOSFull::"

_EXT = {"image": ".png", "voice": ".amr", "video": ".mp4", "sticker": ".png", "file": ""}


def _first(fields, number, wire_type=wire.WT_LEN):
    for n, w, v in fields:
        if n == number and w == wire_type:
            return v
    return None


def extract_media(blob: bytes) -> list[dict]:
    """从消息体 Protobuf 里提取媒体信息。

    返回 ``[{type, filename, local_path, url}]``：

    * ``local_path`` —— QQ 已缓存到本地的文件路径（首选，直接复制即可）；
    * ``url`` —— 下载地址（``45816`` 主机 + ``45802`` 路径），含时效 token，
      旧消息的 token 可能已过期。
    """
    if not blob:
        return []
    try:
        fields = wire.parse_fields(blob)
    except wire.WireError:
        return []

    out: list[dict] = []
    for n, w, v in fields:
        if n != 40800 or w != wire.WT_LEN:
            continue
        try:
            inner = wire.parse_fields(v)
        except wire.WireError:
            continue
        ctype = _first(inner, 45002, wire.WT_VARINT)
        if ctype not in MEDIA_TYPE_NAMES:
            continue

        raw = _first(inner, 45402)
        filename = wire.decode_text(raw) if raw else None

        raw = _first(inner, 45403)
        local = wire.decode_text(raw) if raw else None
        if local and local.startswith(LOCAL_PREFIX):
            local = local[len(LOCAL_PREFIX):]

        url = None
        raw = _first(inner, 45816)
        host = wire.decode_text(raw) if raw else None
        raw = _first(inner, 45802)
        path = wire.decode_text(raw) if raw else None
        if host and path:
            url = path if path.startswith("http") else f"https://{host}{path}"

        out.append(
            {
                "type": MEDIA_TYPE_NAMES[ctype],
                "filename": filename,
                "local_path": local,
                "url": url,
            }
        )
    return out


def _safe_name(media: dict, index: int) -> str:
    name = (media.get("filename") or "").replace("\\", "/")
    name = Path(name).name  # 只取文件名，防路径穿越
    if name:
        return name
    return f"media_{index:04d}{_EXT.get(media.get('type', ''), '')}"


def _md5_of(media: dict) -> str | None:
    """从文件名提取 32 位 MD5（QQ 缓存按 MD5 命名）。"""
    name = media.get("filename") or ""
    base = Path(name).name.split(".")[0]
    if len(base) >= 32 and all(c in "0123456789abcdefABCDEF" for c in base[:32]):
        return base[:32].lower()
    return None


def build_local_index(nt_data_root: Path | str) -> dict[str, str]:
    """扫描 ``nt_data``，建立 ``MD5 -> 最佳本地文件路径`` 索引。

    原图（``Ori``）优先，缩略图（``Thumb``）兜底。一次扫描，多次查找。
    """
    root = Path(nt_data_root)
    index: dict[str, str] = {}
    if not root.is_dir():
        return index
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        stem = p.stem
        if len(stem) < 32:
            continue
        md5 = stem[:32].lower()
        if not all(c in "0123456789abcdef" for c in md5):
            continue
        cur = index.get(md5)
        if cur is None or ("Ori" in str(p) and "Ori" not in str(cur)):
            index[md5] = str(p)
    return index


def download_media(
    media_items: list[dict],
    out_dir: Path,
    *,
    local_index: dict[str, str] | None = None,
    progress=None,
) -> dict:
    """把媒体保存到 ``out_dir``，返回统计 ``{copied, downloaded, skipped, failed}``。

    查找顺序：消息里的本地路径 → ``local_index``（按 MD5）→ HTTP 下载（``url``）。
    同名文件自动去重。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    local_index = local_index or {}
    stats = {"copied": 0, "downloaded": 0, "skipped": 0, "failed": 0}
    seen: set[str] = set()

    for i, m in enumerate(media_items):
        name = _safe_name(m, i)
        dest = out_dir / name
        if name in seen:
            stats["skipped"] += 1
            continue
        seen.add(name)
        if dest.exists() and dest.stat().st_size > 0:
            stats["skipped"] += 1
            continue

        # 1) 消息里的本地路径
        src = m.get("local_path")
        if src and Path(src).is_file():
            try:
                shutil.copy2(src, dest)
                stats["copied"] += 1
                continue
            except OSError:
                pass
        # 2) 按 MD5 在缓存索引里找（原图没了还能拿缩略图）
        md5 = _md5_of(m)
        if md5 and md5 in local_index:
            try:
                shutil.copy2(local_index[md5], dest)
                stats["copied"] += 1
                continue
            except OSError:
                pass
        # 3) HTTP 下载（rkey 时效，旧消息可能过期）
        if m.get("url"):
            try:
                req = urllib.request.Request(
                    m["url"], headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0)"}
                )
                with urllib.request.urlopen(req, timeout=20) as resp, open(dest, "wb") as fh:
                    shutil.copyfileobj(resp, fh)
                if dest.stat().st_size > 0:
                    stats["downloaded"] += 1
                    continue
            except Exception:  # noqa: BLE001
                pass

        stats["failed"] += 1

    return stats
