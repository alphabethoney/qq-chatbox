"""Offline R8 scope, identity, roster and memory-boundary regression tests.

Run: python tests/test_group_scope.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qq_chatbot.bridge.onebot import OneBotClient
from qq_chatbot.bridge.policy import BridgeRuntime, _at_self, _mentions
from qq_chatbot.config import Config
from qq_chatbot.schema import Answer, Citation
from qq_chatbot.session import Session


def event(text="hello", *, group=10, user=1, **extra):
    result = dict(post_type="message", message_type="group" if group is not None else "private",
                  user_id=user, self_id=99, message=text)
    if group is not None:
        result["group_id"] = group
    result.update(extra)
    return result


class Retriever:
    def __init__(self):
        self.calls = []

    def retrieve(self, query, **filters):
        self.calls.append((query, filters))
        # All data is synthetic: emulate the real retriever's metadata filtering.
        turns = [SimpleNamespace(conversation_key="group:10", kind="group", speaker=SimpleNamespace(name="one", key="group:10:1"), time_str="now", text="LOCAL"),
                 SimpleNamespace(conversation_key="group:20", kind="group", speaker=SimpleNamespace(name="one", key="group:20:1"), time_str="now", text="FOREIGN"),
                 SimpleNamespace(conversation_key="c2c:1", kind="c2c", speaker=SimpleNamespace(name="one", key="c2c:1"), time_str="now", text="PRIVATE")]
        return [SimpleNamespace(turn=t, score=1.0) for t in turns
                if all(getattr(t, k) == filters[k] for k in ("kind",) if k in filters)
                and ("conversation" not in filters or t.conversation_key == filters["conversation"])
                and ("person_qq" not in filters or t.speaker.key.split(":")[-1] == filters["person_qq"])]


class Bot:
    def __init__(self):
        self.config = Config()
        self.config.bridge.group_trigger = "all"
        self.config.bridge.rate_limit_per_min = 0
        self.retriever = Retriever()
        self.calls = []
        self.answer_value = Answer("answer", [Citation("group:10", "now", "one", "evidence", 1)], True)
        self.scopes = []
        self.store = None

    def answer(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return self.answer_value

    def create_session(self, scope):
        self.scopes.append(scope)
        return Session(self.config.session.max_turns, memory_store=self.store, scope=scope)


def runtime(**settings):
    bot = Bot()
    for k, v in settings.items():
        setattr(bot.config.bridge, k, v)
    return BridgeRuntime(bot, bot.config), bot


def message(actions):
    return "\n".join(a.params.get("message", "") for a in actions)


class GroupScopeTests(unittest.TestCase):
    def test_search_never_leaks_other_group_or_private(self):
        rt, bot = runtime()
        text = message(rt.handle(event("/search hello")))
        self.assertIn("LOCAL", text)
        self.assertNotIn("FOREIGN", text)
        self.assertNotIn("PRIVATE", text)
        self.assertEqual(bot.retriever.calls[-1][1], dict(top_k=5, kind="group", conversation="group:10"))

    def test_search_obeys_active_person_and_kind(self):
        rt, bot = runtime()
        rt.handle(event("/persona c2c:1", group=None))
        rt.handle(event("/kind c2c", group=None))
        rt.handle(event("/search hello", group=None))
        self.assertEqual(bot.retriever.calls[-1][1], dict(top_k=5, person_qq="1", kind="c2c"))
        rt.handle(event("/search hello --kind group --person group:20:2 --conversation group:20", group=None))
        self.assertEqual(bot.retriever.calls[-1][1], dict(top_k=5, person_qq="2", kind="group", conversation="group:20"))

    def test_cross_scope_commands_are_rejected_before_retrieval(self):
        rt, bot = runtime()
        for command in ("/search hello --conversation group:20", "/search hello --kind=c2c",
                        "/search hello --person group:20:1", "/kind c2c", "/persona group:20:1"):
            with self.subTest(command=command):
                self.assertIn("拒绝", message(rt.handle(event(command))))
        self.assertEqual(bot.retriever.calls, [])
        self.assertEqual(bot.calls, [])

    def test_unknown_options_fail_closed(self):
        rt, bot = runtime()
        for command in ("/search hello --conversation", "/search hello --kind=all", "/search hello --scope all"):
            rt.handle(event(command))
        self.assertFalse(bot.retriever.calls)

    def test_group_answers_are_scoped_including_persona(self):
        rt, bot = runtime()
        rt.handle(event("/persona c2c:1"))
        rt.handle(event())
        self.assertEqual(bot.calls[-1][1]["conversation"], "group:10")
        self.assertEqual(bot.calls[-1][1]["kind"], "group")
        self.assertEqual(bot.calls[-1][1]["person"], "c2c:1")

    def test_citations_clear_on_switch_and_no_evidence(self):
        rt, bot = runtime()
        for command in ("/qa", "/persona c2c:1", "/kind group"):
            rt.handle(event())
            self.assertTrue(rt.last_cites)
            rt.handle(event(command))
            self.assertFalse(rt.last_cites)
            self.assertFalse(rt.sessions)
            self.assertIn("尚无出处", message(rt.handle(event("/cite"))))
        rt.handle(event())
        bot.answer_value = Answer("no evidence", [], False)
        rt.handle(event())
        self.assertFalse(rt.last_cites)

    def test_session_factory_and_identity_isolation(self):
        rt, bot = runtime()
        rt.handle(event(user=1))
        first = bot.calls[-1][1]["session"]
        rt.handle(event(user=2))
        self.assertIsNot(first, bot.calls[-1][1]["session"])
        rt.handle(event(user=1, self_id=98))
        self.assertEqual(len(set(bot.scopes)), 3)
        rt.handle(event("/persona c2c:1", user=1))
        rt.handle(event(user=1))
        self.assertEqual(len(set(bot.scopes)), 4)

    def test_legacy_session_fallback(self):
        rt, bot = runtime()
        bot.create_session = None
        rt.handle(event())
        self.assertIsInstance(bot.calls[-1][1]["session"], Session)

    def test_sender_mismatch_and_self_ignored(self):
        rt, bot = runtime()
        self.assertEqual(rt.handle(event(sender={"user_id": 2})), [])
        self.assertEqual(rt.handle(event(user="99")), [])
        self.assertFalse(bot.calls)

    def test_mentions_do_not_authorize_sender(self):
        rt, bot = runtime(group_member_check=True)
        rt.update_group_members(10, [{"user_id": 2, "nickname": "member"}])
        actions = rt.handle(event("[CQ:at,qq=99] [CQ:at,qq=2] /search hello"))
        self.assertIn("拒绝", message(actions))
        self.assertFalse(bot.retriever.calls)
        self.assertFalse(bot.calls)

    def test_mentions_narrow_search_and_preserve_identity(self):
        rt, bot = runtime(group_member_check=True)
        rt.update_group_members(10, [{"user_id": 1, "card": "trusted"}, {"user_id": 2}])
        segments = [{"type": "at", "data": {"qq": "99"}},
                    {"type": "at", "data": {"qq": "2"}},
                    {"type": "text", "data": {"text": " /search hello"}}]
        rt.handle(event(segments, sender={"user_id": "1", "nickname": "untrusted"}))
        self.assertEqual(bot.retriever.calls[-1][1]["person_qq"], "2")
        context = next(iter(rt.event_context.values()))
        self.assertEqual((context.sender_id, context.sender_name, context.mentions), ("1", "trusted", ("2",)))
        self.assertTrue(context.sender_is_member)

    def test_mention_does_not_override_persona_search_target(self):
        rt, bot = runtime(group_member_check=True)
        rt.update_group_members(10, [{"user_id": 1}, {"user_id": 2}])
        rt.handle(event("/persona c2c:1"))
        rt.handle(event("[CQ:at,qq=99] [CQ:at,qq=2] /search hello"))
        self.assertEqual(bot.retriever.calls[-1][1]["person_qq"], "1")
        self.assertEqual(bot.retriever.calls[-1][1]["conversation"], "group:10")

    def test_string_mentions_use_message_not_raw_message(self):
        e = event("[CQ:at,qq=99] [CQ:at,qq=all] hi")
        self.assertTrue(_at_self(e, 99))
        self.assertEqual(_mentions(e), ("99", "all"))
        self.assertFalse(_at_self(event("hello", raw_message="[CQ:at,qq=99]"), 99))

    def test_memory_write_failure_preserves_answer_and_reports_failure(self):
        import sqlite3
        rt, bot = runtime(split_sentences=True, max_sentences=1)
        failed = SimpleNamespace(add=lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
        bot.store = failed
        actions = rt.handle(event())
        self.assertEqual(actions[0].params["message"], "answer")
        self.assertIn("记忆保存失败", message(actions))
        self.assertEqual(next(iter(rt.sessions.values())).history, [])
        self.assertTrue(rt.last_cites)

    def test_unrelated_session_bug_is_not_swallowed(self):
        rt, bot = runtime()
        failed = SimpleNamespace(add=lambda *args: (_ for _ in ()).throw(RuntimeError("bug")))
        bot.store = failed
        with self.assertRaises(RuntimeError):
            rt.handle(event())

    def test_forget_failure_does_not_claim_persistent_deletion(self):
        import sqlite3
        rt, bot = runtime()
        bot.store = SimpleNamespace(delete=lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
        actions = rt.handle(event("/forget"))
        self.assertIn("删除失败", message(actions))
        self.assertFalse(rt.sessions)

    def test_forget_after_restart_is_scoped(self):
        from qq_chatbot.memory import MemoryStore
        with tempfile.TemporaryDirectory() as tmp:
            rt, bot = runtime()
            bot.store = MemoryStore(Path(tmp) / "synthetic.sqlite")
            rt.handle(event(user=1))
            scope1 = bot.scopes[-1]
            rt.handle(event(user=2))
            scope2 = bot.scopes[-1]
            rt.handle(event("/persona c2c:1", user=1))
            rt.handle(event(user=1))
            persona_scope = bot.scopes[-1]
            fresh = BridgeRuntime(bot, bot.config)
            self.assertIn("已清除", message(fresh.handle(event("/forget", user=1))))
            self.assertFalse(bot.store.retrieve(scope1))
            self.assertTrue(bot.store.retrieve(scope2))
            self.assertTrue(bot.store.retrieve(persona_scope))
            self.assertFalse(fresh.sessions)
            self.assertFalse(fresh.last_cites)


class MemberTransportTests(unittest.TestCase):
    def setUp(self):
        self.rt, self.bot = runtime(group_member_check=True, group_member_cache_ttl=30, group_member_request_timeout=5)
        self.client = OneBotClient("ws://unused.invalid")

    def response(self, echo, **extra):
        return dict(status="ok", retcode=0, echo=echo, data=[{"user_id": 1, "group_id": 10}], **extra)

    def test_default_sends_no_member_request(self):
        rt, bot = runtime()
        actions = self.client.process_event(event(), rt.handle)
        self.assertEqual([a.action for a in actions], ["send_group_msg"])
        self.assertTrue(bot.calls)

    def test_async_request_response_cache_and_expiry(self):
        with patch("time.monotonic", return_value=100):
            actions = self.client.process_event(event(), self.rt.handle)
            self.assertEqual(actions[0].action, "get_group_member_list")
            self.assertFalse(self.bot.calls)
            self.assertEqual(actions[0].params, {"group_id": 10})
            self.assertNotIn("get_group_member_list", [a.action for a in self.client.process_event(event(), self.rt.handle)])
            self.client.process_event(self.response(actions[0].echo), self.rt.handle)
            self.client.process_event(event(), self.rt.handle)
            self.assertEqual(len(self.bot.calls), 1)
        with patch("time.monotonic", return_value=131):
            actions = self.client.process_event(event(), self.rt.handle)
            self.assertEqual(actions[0].action, "get_group_member_list")
            self.assertEqual(len(self.bot.calls), 1)

    def test_failed_unknown_and_malformed_responses_never_authorize(self):
        self.client.process_event(self.response("unknown"), self.rt.handle)
        self.assertIsNone(self.rt.group_members(10))
        for data, status, retcode in (([{"user_id": 1}], "failed", 1),
                                      ({"user_id": 1}, "ok", 0),
                                      ([{"user_id": 1, "group_id": 20}], "ok", 0)):
            actions = self.client.process_event(event(), self.rt.handle)
            self.client.process_event(dict(echo=actions[0].echo, status=status, retcode=retcode, data=data), self.rt.handle)
            self.assertIsNone(self.rt.group_members(10))

    def test_timeout_ignores_late_response(self):
        with patch("time.monotonic", return_value=100):
            old = self.client.process_event(event(), self.rt.handle)[0].echo
        with patch("time.monotonic", return_value=106):
            new = self.client.process_event(event(), self.rt.handle)[0].echo
            self.assertNotEqual(old, new)
            self.client.process_event(self.response(old), self.rt.handle)
            self.assertIsNone(self.rt.group_members(10))
            self.client.process_event(self.response(new), self.rt.handle)
            self.assertIn("1", self.rt.group_members(10))

    def test_group_departure_invalidates_roster(self):
        self.rt.update_group_members(10, [{"user_id": 1}])
        self.client.process_event(dict(post_type="notice", notice_type="group_decrease", group_id=10, user_id=1), self.rt.handle)
        self.assertIsNone(self.rt.group_members(10))
        self.assertEqual(self.client.process_event(event(), self.rt.handle)[0].action, "get_group_member_list")

    def test_group_caches_do_not_authorize_other_groups(self):
        self.rt.update_group_members(10, [{"user_id": 1}])
        self.client.process_event(event(group=20), self.rt.handle)
        self.assertFalse(self.bot.calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
