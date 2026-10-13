"""R4 tests: all identities, messages and model responses are synthetic/offline."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qq_chatbot.profile import build_profile, load_profiles, profile_cache_key, save_profile
from qq_chatbot.prompt import build_persona_messages
from qq_chatbot.schema import Corpus, PersonaProfile, Speaker, Turn
from qq_chatbot.timeline import update_timeline


def turn(i, text=None, *, conversation="c2c:fiction", kind="c2c"):
    return Turn(str(i), Speaker("c2c:fiction", "虚构角色"), conversation, kind,
                1700000000 + i, f"2023-11-14 22:13:{20+i:02}", text or f"我现在住在虚构城{i}", [f"fake-msg-{i}"])


class FakeLLM:
    name = "offline-fixture"

    def __init__(self, alter=None):
        self.calls = []
        self.alter = alter

    def chat(self, messages, **kwargs):
        prompt = messages[-1]["content"]
        self.calls.append(prompt)
        if "资料：\n" not in prompt:
            return '{"style":{"tone":"虚构语气"}}'
        rows = json.loads(prompt.split("资料：\n", 1)[1])
        entries = [{"topic": "居住城市", "event": row["text"], "source_turn_ids": [row["turn_id"]],
                    "evidence": [{"turn_id": row["turn_id"], "quote": row["text"]}],
                    "event_time": None, "temporal_scope": "current", "certainty": "explicit", "confidence": 0.95}
                   for row in rows]
        if self.alter:
            self.alter(entries)
        return json.dumps({"timeline": entries}, ensure_ascii=False)


class TimelineTests(unittest.TestCase):
    def test_segments_preserve_every_source_and_exact_time(self):
        turns = [turn(i) for i in range(125)]
        llm = FakeLLM()
        entries, state = update_timeline(llm, turns, batch_size=30)
        self.assertEqual(len(llm.calls), 5)
        self.assertEqual(len(entries), 125)
        self.assertEqual(len(state["fingerprints"]), 125)
        first = next(e for e in entries if e["source_turn_ids"] == ["0"])
        self.assertEqual(first["sources"][0]["time_str"], turns[0].time_str)
        self.assertEqual(first["sources"][0]["time"], turns[0].time)
        self.assertEqual(first["source_msg_ids"], ["fake-msg-0"])
        self.assertIsNone(first["event_time"])

    def test_incremental_only_new_and_changed(self):
        llm = FakeLLM()
        turns = [turn(1), turn(2)]
        history, state = update_timeline(llm, turns)
        llm.calls.clear()
        same, checkpoint = update_timeline(llm, turns, history, state)
        self.assertEqual(llm.calls, [])
        self.assertEqual(same, history)
        changed = [turn(1, "我现在住在虚构新城"), turn(2), turn(3)]
        result, _ = update_timeline(llm, changed, history, checkpoint)
        rows = json.loads(llm.calls[0].split("资料：\n", 1)[1])
        self.assertEqual({r["turn_id"] for r in rows}, {"1", "3"})
        self.assertEqual(len(result), 4)
        self.assertEqual(next(e for e in result if e["event"] == turns[0].text)["status"], "source_changed")
        self.assertNotIn("status", history[0]["sources"][0])

    def test_forged_sources_and_quotes_rejected_and_retried(self):
        for alteration in (lambda es: es[0].update(source_turn_ids=["nonexistent"]),
                           lambda es: es[0]["evidence"][0].update(quote="没有出现的原文"),
                           lambda es: es[0].update(event_time="2040-02-01", event_time_quote="我现在")):
            llm = FakeLLM(alteration)
            entries, state = update_timeline(llm, [turn(1)])
            self.assertEqual(entries, [])
            self.assertEqual(state["fingerprints"], {})
            entries, _ = update_timeline(FakeLLM(), [turn(1)], entries, state)
            self.assertEqual(len(entries), 1)

    def test_latest_uncertainty_cannot_replace_supported_state(self):
        entries, _ = update_timeline(FakeLLM(), [turn(1), turn(2), turn(3, "我可能住在虚构城3")])
        by_id = {e["source_turn_ids"][0]: e for e in entries}
        self.assertEqual(by_id["1"]["status"], "superseded")
        self.assertEqual(by_id["2"]["status"], "latest_supported")
        self.assertEqual(by_id["3"]["status"], "uncertain")

    def test_retrospective_event_does_not_replace_current(self):
        def change(entries):
            entries[-1].update(temporal_scope="past", event_time="2011-02-03", event_time_quote="2011年2月3日")
        entries, _ = update_timeline(FakeLLM(change), [turn(1), turn(2, "2011年2月3日我住在虚构旧城")])
        self.assertEqual(entries[0]["status"], "latest_supported")
        self.assertEqual(entries[1]["status"], "historical")
        self.assertEqual(entries[1]["event_time"], "2011-02-03")
        self.assertEqual(entries[1]["time"], turn(2).time_str)

    def test_equal_time_conflict_is_disputed(self):
        first, second = turn(1), turn(2)
        second.time = first.time
        entries, _ = update_timeline(FakeLLM(), [first, second])
        self.assertTrue(all(e["status"] == "disputed" for e in entries))

    def test_missing_sources_retained_but_not_current(self):
        history, state = update_timeline(FakeLLM(), [turn(1)])
        entries, _ = update_timeline(None, [], history, state)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "source_missing")

    def test_restored_source_can_be_current_again(self):
        history, state = update_timeline(FakeLLM(), [turn(1)])
        missing, state = update_timeline(None, [], history, state)
        restored, _ = update_timeline(None, [turn(1)], missing, state)
        self.assertEqual(restored[0]["status"], "latest_supported")

    def test_event_time_cannot_invent_seconds(self):
        def alter(entries):
            entries[0].update(event_time="2011-02-03T12:34:56", event_time_quote="2011年2月3日")
        entries, state = update_timeline(FakeLLM(alter), [turn(1, "2011年2月3日搬家")])
        self.assertEqual(entries, [])
        self.assertEqual(state["fingerprints"], {})

    def test_profile_scope_and_unchanged_skip_all_llm_calls(self):
        a = turn(1, conversation="group:fake-a", kind="group")
        b = turn(2, conversation="group:fake-b", kind="group")
        llm = FakeLLM()
        corpus = Corpus(turns=[a, b, turn(3)])
        profile = build_profile(corpus, "c2c:fiction", llm, kind="group", conversation="group:fake-a")
        self.assertEqual(len(profile.timeline), 1)
        self.assertEqual(profile.timeline[0]["source_turn_ids"], ["1"])
        llm.calls.clear()
        newer = build_profile(corpus, "c2c:fiction", llm, kind="group", conversation="group:fake-a", previous=profile)
        self.assertEqual(llm.calls, [])
        self.assertEqual(newer.timeline_state, profile.timeline_state)
        build_profile(corpus, "c2c:fiction", llm, kind="group", conversation="group:fake-a", previous=newer)
        self.assertEqual(llm.calls, [])

    def test_kind_scope_roundtrip_backup_and_idempotence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = PersonaProfile(speaker="c2c:fiction", name="虚构昵称")
            profile.timeline_state = {"version": 1, "fingerprints": {"1": "fake"}}
            a = save_profile(root, profile, kind="c2c")
            b = save_profile(root, profile, kind="group", conversation="group:fake")
            self.assertNotEqual(a, b)
            save_profile(root, profile, kind="c2c")
            self.assertFalse((a.parent / "history").exists())
            original = a.read_bytes()
            profile.name = "虚构新昵称"
            save_profile(root, profile, kind="c2c")
            backups = list((a.parent / "history").glob("*.json"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
            loaded = load_profiles(root)
            self.assertEqual(loaded[profile_cache_key(profile.speaker, "c2c")].name, profile.name)
            self.assertIn(profile_cache_key(profile.speaker, "group", "group:fake"), loaded)
            self.assertEqual(loaded[profile_cache_key(profile.speaker, "c2c")].timeline_state, profile.timeline_state)

    def test_atomic_failure_keeps_original_and_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = PersonaProfile(speaker="c2c:fiction")
            path = save_profile(root, profile)
            original = path.read_bytes()
            profile.name = "虚构改名"
            with patch("qq_chatbot.profile.os.replace", side_effect=OSError("synthetic failure")):
                with self.assertRaises(OSError):
                    save_profile(root, profile)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(len(list((path.parent / "history").glob("*.json"))), 1)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_legacy_cache_remains_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = save_profile(root, PersonaProfile(speaker="c2c:fiction", timeline=[{"time": "2010-01", "event": "虚构旧事"}]))
            loaded = load_profiles(root)
            self.assertIs(loaded["c2c:fiction"], loaded["c2c:fiction#"])
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
