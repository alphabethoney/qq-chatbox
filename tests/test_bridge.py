"""qq_chatbot 桥接层（群聊+私聊）自测：纯逻辑，不连 NapCat、不联网。

    python tests/test_bridge.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from make_fixture import build as build_fixture  # noqa: E402

from qq_chatbot.bridge.policy import BridgeRuntime  # noqa: E402
from qq_chatbot.chat import Chatbot  # noqa: E402
from qq_chatbot.config import Config  # noqa: E402
from qq_chatbot.index import HybridRetriever, build_store, make_embedding  # noqa: E402
from qq_chatbot.ingest import load_corpus  # noqa: E402
from qq_chatbot.llm import make_llm  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []

SELF = 88888888  # fixture 中的「我」


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [OK]   {name}")
    else:
        FAILED.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


def make_runtime(tmp: Path, **bridge) -> BridgeRuntime:
    db = build_fixture(tmp / "fixture_plain.db")
    corpus = load_corpus(db=db)
    cfg = Config()
    embed = make_embedding(cfg.embedding)
    store = build_store(corpus.turns, embed)
    retriever = HybridRetriever(store, embed, cfg.retrieval)
    llm = make_llm(cfg.llm)
    bot = Chatbot(cfg, retriever, llm, corpus=corpus)
    for k, v in bridge.items():
        setattr(cfg.bridge, k, v)
    return BridgeRuntime(bot, cfg)


def group_event(raw: str, user_id=10003, group_id=123456) -> dict:
    return {
        "post_type": "message", "message_type": "group", "sub_type": "normal",
        "user_id": user_id, "group_id": group_id, "self_id": SELF,
        "raw_message": raw,
    }


def private_event(raw: str, user_id=10001) -> dict:
    return {
        "post_type": "message", "message_type": "private",
        "user_id": user_id, "self_id": SELF, "raw_message": raw,
    }


def group_event_array(text: str, user_id=10003, group_id=123456) -> dict:
    """NapCat messagePostFormat=array 的群聊事件。"""
    return {
        "post_type": "message", "message_type": "group", "sub_type": "normal",
        "user_id": user_id, "group_id": group_id, "self_id": SELF,
        "message": [
            {"type": "at", "data": {"qq": str(SELF)}},
            {"type": "text", "data": {"text": " " + text}},
        ],
        "raw_message": f"[CQ:at,qq={SELF}] {text}",
    }


def only_action(out):
    return out[0].action if out else None


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="qqcb_bridge_"))
    try:
        rt = make_runtime(tmp)

        print("\n== 触发与作用域 ==")
        out = rt.handle(group_event(f"[CQ:at,qq={SELF}] 会议改到几点"), self_id=SELF)
        check("群聊 @机器人 触发", only_action(out) == "send_group_msg", str(out))
        check("群聊发送到本群", out and out[0].params.get("group_id") == 123456)
        check("群聊检索限制在本群",
              out and out[0].params.get("message", "").startswith("[qa]"))

        out = rt.handle(group_event("没 @ 我"), self_id=SELF)
        check("群聊未 @ 不触发", out == [], str(out))

        out = rt.handle(group_event_array("会议改到几点"), self_id=SELF)
        check("array 格式 @ 触发", only_action(out) == "send_group_msg", str(out))
        check("array 格式提取文本并回复", out and out[0].params["message"].startswith("[qa]"), str(out))

        out = rt.handle(private_event("周末一起吃饭"), self_id=SELF)
        check("私聊直接回复", only_action(out) == "send_private_msg", str(out))
        check("私聊发送给该用户", out and out[0].params.get("user_id") == 10001)

        out = rt.handle(private_event("我自己的消息", user_id=SELF), self_id=SELF)
        check("忽略自己消息", out == [], str(out))

        print("\n== 命令 ==")
        out = rt.handle(private_event("/qa"), self_id=SELF)
        check("/qa 切换", out and "历史问答" in out[0].params["message"], str(out))
        out = rt.handle(private_event("/persona c2c:10001"), self_id=SELF)
        check("/persona 切换", out and "c2c:10001" in out[0].params["message"], str(out))
        out = rt.handle(private_event("/cite"), self_id=SELF)
        check("/cite 显示上一条出处", out and "周末一起吃饭" in out[0].params["message"], str(out))
        out = rt.handle(private_event("/cite", user_id=99999), self_id=SELF)
        check("/cite 无出处提示", out and "尚无出处" in out[0].params["message"], str(out))
        out = rt.handle(private_event("/help"), self_id=SELF)
        check("/help", out and "命令" in out[0].params["message"], str(out))
        out = rt.handle(private_event("/kind group"), self_id=SELF)
        check("/kind group 切换", out and "群聊人格" in out[0].params["message"], str(out))
        out = rt.handle(private_event("/kind auto"), self_id=SELF)
        check("/kind auto 恢复自动", out and "自动" in out[0].params["message"], str(out))

        print("\n== 数字分身与限流 ==")
        rt2 = make_runtime(tmp, mode="persona", persona_person="c2c:10001")
        out = rt2.handle(private_event("在吗"), self_id=SELF)
        check("persona 默认模式生效", out and out[0].params["message"].startswith("[persona]"),
              str(out))

        rt3 = make_runtime(tmp, rate_limit_per_min=1)
        out = rt3.handle(private_event("第一次"), self_id=SELF)
        ok1 = bool(out)
        out = rt3.handle(private_event("第二次"), self_id=SELF)
        check("限流：1 条/分钟后拒绝", ok1 and out == [], str(out))

        rt4 = make_runtime(tmp, group_trigger="all")
        out = rt4.handle(group_event("不用 @ 也回"), self_id=SELF)
        check("group_trigger=all 全量回复", only_action(out) == "send_group_msg", str(out))

        print("\n== 单句单次发出 ==")
        from qq_chatbot.bridge.policy import _split_sentences
        s = _split_sentences("哈哈。真的吗！好的；行吧")
        check("拆句切分", s == ["哈哈。", "真的吗！", "好的；", "行吧"], str(s))

        rt5 = make_runtime(tmp, split_sentences=True, max_sentences=3)
        # mock LLM 回复形如 "[persona]在吗，命中 N 条证据"，无句号 -> 单条
        out = rt5.handle(private_event("在吗"), self_id=SELF)
        check("无句号时单条发出", len(out) == 1, str(len(out)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'='*60}")
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for f in FAILED:
        print(f"  FAILED: {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
