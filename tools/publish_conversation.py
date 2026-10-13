"""Publish a complete private-chat index without replacing other conversations."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from qq_chatbot.index.embeddings import HashEmbedding
from qq_chatbot.index.store import load_store
from tools.sync_index import _publish, _sync_lock

FILES = ("corpus.json", "embeddings.json", "manifest.json")


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def _hashes(blobs):
    return {name: hashlib.sha256(data).hexdigest() for name, data in blobs.items()}


def _read_blobs(directory):
    return {name: (directory / name).read_bytes() for name in FILES}


def _unchanged(directory, hashes):
    if _hashes(_read_blobs(directory)) != hashes:
        raise ValueError(f"index changed during publication: {directory}")


def _write_durable(path, data):
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_dir(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _validate(directory, blobs):
    corpus, embeddings, manifest = (json.loads(blobs[name]) for name in FILES)
    if not all(isinstance(x, dict) for x in (corpus, embeddings, manifest)):
        raise ValueError("index documents must be objects")
    dim = manifest.get("dim")
    if (manifest.get("format_version") != 2 or embeddings.get("format_version") != 2
            or manifest.get("embedding_provider") != "hash"
            or manifest.get("embedding_model") != "fnv1a-ngram-v1"
            or type(dim) is not int or dim <= 0):
        raise ValueError("only persisted hash fnv1a-ngram-v1 vector space is supported")
    if manifest.get("fallback_embedding") is not None or embeddings.get("fallback_vectors") is not None:
        raise ValueError("fallback vector spaces are not supported")
    generation = manifest.get("generation_id")
    if not isinstance(generation, str) or not generation or any(
            x.get("generation_id") != generation for x in (corpus, embeddings)):
        raise ValueError("index generation mismatch")
    turns, speakers, meta = (corpus.get(k) for k in ("turns", "speakers", "meta"))
    if not isinstance(turns, list) or not isinstance(speakers, dict) or not isinstance(meta, dict):
        raise ValueError("invalid raw corpus schema")
    titles = meta.get("conversation_titles", [])
    if not isinstance(titles, (dict, list)) or any(not isinstance(title, str) for title in
            (titles.values() if isinstance(titles, dict) else titles)):
        raise ValueError("invalid conversation_titles")
    for key, info in speakers.items():
        if not isinstance(key, str) or not isinstance(info, dict):
            raise ValueError("invalid speaker metadata")
        if not isinstance(info.get("name", key), str) or type(info.get("is_self", False)) is not bool:
            raise ValueError("invalid speaker metadata")
    for turn in turns:
        if not isinstance(turn, dict) or any(not isinstance(turn.get(k), str) for k in
                ("id", "speaker_key", "conversation_key", "kind", "text", "time_str")):
            raise ValueError("invalid raw turn schema")
        if not turn["id"] or turn["speaker_key"] not in speakers:
            raise ValueError("empty turn id or missing speaker")
        if (turn["kind"] not in ("c2c", "group") or not turn["conversation_key"].startswith(turn["kind"] + ":")
                or type(turn.get("time")) not in (int, float) or not math.isfinite(turn["time"])
                or not isinstance(turn.get("msg_ids"), list)
                or any(type(mid) not in (str, int) or str(mid) == "" for mid in turn["msg_ids"])):
            raise ValueError("invalid raw turn fields")
    store, _, _ = load_store(directory, HashEmbedding(dim))
    if len(store.turns) != len(turns):
        raise ValueError("runtime corpus content mismatch")
    return corpus, embeddings, manifest


def _snapshot(directory, snapshot):
    blobs = _read_blobs(directory)
    hashes = _hashes(blobs)
    for name, data in blobs.items():
        _write_durable(snapshot / name, data)
    docs = _validate(snapshot, blobs)
    _unchanged(directory, hashes)
    return blobs, hashes, docs


def _content_digest(docs):
    docs = copy.deepcopy(docs)
    docs[2].get("publication", {}).pop("target_digest", None)
    return hashlib.sha256(_json_bytes(docs)).hexdigest()


def publish(source: Path, target: Path, conversation: str, backup_root: Path) -> dict:
    """Return status published/skipped, or raise without committing on validation failure.

    Requires persisted version-2 hash indexes with equal dimensions. Locks are
    nonblocking and shared with sync_index. All writers must honor .sync/lock.
    """
    if not isinstance(conversation, str) or not re.fullmatch(r"c2c:[0-9]+", conversation):
        raise ValueError("conversation must be c2c:ID")
    source, target, backup_root = (Path(p).resolve() for p in (source, target, backup_root))
    if not source.is_dir() or not target.is_dir():
        raise ValueError("source and target must be existing index directories")
    if source.is_relative_to(target) or target.is_relative_to(source):
        raise ValueError("source and target must be separate directory trees")
    if any(backup_root.is_relative_to(p) or p.is_relative_to(backup_root) for p in (source, target)):
        raise ValueError("backup root must be separate from source and target")
    first, second = sorted((source, target), key=lambda p: os.path.normcase(str(p)))
    with _sync_lock(first), _sync_lock(second), tempfile.TemporaryDirectory(prefix=".conversation-publish-") as temporary:
        work = Path(temporary)
        src, dst, stage = (work / name for name in ("source", "target", "stage"))
        for directory in (src, dst, stage):
            directory.mkdir()
        _, source_hashes, source_docs = _snapshot(source, src)
        target_blobs, target_hashes, target_docs = _snapshot(target, dst)
        sc, se, sm = source_docs
        tc, te, tm = target_docs
        if sm["dim"] != tm["dim"]:
            raise ValueError("source and target vector space mismatch")
        if not sc["turns"]:
            raise ValueError("empty source is not a complete conversation")
        if len({t["id"] for t in sc["turns"]}) != len(sc["turns"]):
            raise ValueError("duplicate source turn IDs")
        if any(t["conversation_key"] != conversation or t["kind"] != "c2c" for t in sc["turns"]):
            raise ValueError("source contains wrong conversation scope")
        old = [t for t in tc["turns"] if t["conversation_key"] == conversation]
        old_ids = {str(mid) for t in old for mid in t["msg_ids"]}
        new_ids = {str(mid) for t in sc["turns"] for mid in t["msg_ids"]}
        if not old_ids <= new_ids:
            raise ValueError("source missing old message IDs; refusing history loss")
        source_digest = hashlib.sha256(_json_bytes(source_hashes)).hexdigest()
        previous = tm.get("publication", {})
        result = {"conversation": conversation, "source_digest": source_digest}
        if (isinstance(previous, dict) and previous.get("source_digest") == source_digest
                and previous.get("conversation") == conversation
                and previous.get("target_digest") == _content_digest(target_docs)):
            _unchanged(source, source_hashes)
            _unchanged(target, target_hashes)
            return {**result, "status": "skipped", "backup": previous.get("backup")}
        corpus, embeddings, manifest = copy.deepcopy(target_docs)
        retained = [(t, v) for t, v in zip(tc["turns"], te["vectors"])
                    if t["conversation_key"] != conversation]
        used = {t["id"] for t, _ in retained}
        reserved = used | {t["id"] for t in sc["turns"]}
        incoming = copy.deepcopy(sc["turns"])
        for turn in incoming:
            if turn["id"] in used:
                seed = hashlib.sha256(_json_bytes([conversation, turn["id"]])).hexdigest()
                candidate, suffix = "publication:" + seed, 0
                while candidate in reserved:
                    suffix += 1
                    candidate = f"publication:{seed}:{suffix}"
                turn["id"] = candidate
                reserved.add(candidate)
            used.add(turn["id"])
        corpus["turns"] = [t for t, _ in retained] + incoming
        embeddings["vectors"] = [v for _, v in retained] + se["vectors"]
        embeddings["turn_ids"] = [t["id"] for t in corpus["turns"]]
        protected = {t["speaker_key"] for t, _ in retained}
        for key in {t["speaker_key"] for t in incoming}:
            if key not in protected:
                corpus["speakers"][key] = copy.deepcopy(sc["speakers"][key])
        source_titles = sc["meta"].get("conversation_titles", [])
        target_titles = corpus["meta"].get("conversation_titles", [])
        if isinstance(target_titles, dict):
            if isinstance(source_titles, dict) and conversation in source_titles:
                target_titles[conversation] = source_titles[conversation]
            elif isinstance(source_titles, list) and len(source_titles) == 1:
                target_titles[conversation] = source_titles[0]
            elif source_titles:
                raise ValueError("ambiguous source titles for keyed target metadata")
        else:
            # ingest stores an unkeyed redaction-name list. Never remove an old
            # title: it may belong to another conversation or be a shared alias.
            # The source has already been verified to contain only this c2c.
            additions = ([source_titles[conversation]] if conversation in source_titles else []) if isinstance(source_titles, dict) else source_titles
            for title in additions:
                if title not in target_titles:
                    target_titles.append(title)
            if target_titles or "conversation_titles" in corpus["meta"]:
                corpus["meta"]["conversation_titles"] = target_titles
        manifest["turn_count"] = len(corpus["turns"])
        generation = uuid.uuid4().hex
        docs = [corpus, embeddings, manifest]
        for doc in docs:
            doc["generation_id"] = generation
        backup_root.mkdir(parents=True, exist_ok=True)
        backup = backup_root / ("publication-" + uuid.uuid4().hex)
        backup.mkdir()
        manifest["publication"] = {**result, "source_hashes": source_hashes,
                                   "backup": str(backup), "generation_id": generation}
        manifest["publication"]["target_digest"] = _content_digest(docs)
        for name, doc in zip(FILES, docs):
            _write_durable(stage / name, _json_bytes(doc))
        _validate(stage, _read_blobs(stage))
        for name, data in target_blobs.items():
            _write_durable(backup / name, data)
        _write_durable(backup / "hashmanifest.json", _json_bytes({
            "sha256": target_hashes, "conversation": conversation,
            "source_digest": source_digest, "source": str(source),
            "source_hashes": source_hashes, "source_generation_id": sm["generation_id"],
            "target": str(target)}))
        _fsync_dir(backup)
        _fsync_dir(backup_root)
        if _hashes(_read_blobs(backup)) != target_hashes:
            raise ValueError("backup verification failed")
        _validate(backup, _read_blobs(backup))
        _unchanged(source, source_hashes)
        _unchanged(target, target_hashes)
        _publish(stage, target)
        return {**result, "status": "published", "backup": str(backup),
                "turn_count": len(corpus["turns"]), "generation_id": generation}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--conversation", required=True)
    parser.add_argument("--backup-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = publish(args.source, args.target, args.conversation, args.backup_root)
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(1, f"publication failed: {error}\n")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
