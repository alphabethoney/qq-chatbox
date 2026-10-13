"""Offline integration checks; every person/message/response here is fictional."""
from __future__ import annotations

import copy
import json
import multiprocessing
import tempfile
import threading
import unittest
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from qq_chatbot.chat import Chatbot, load_chatbot
from qq_chatbot.config import Config
from qq_chatbot.index import HybridRetriever, build_store, make_embedding, save_store
from qq_chatbot.profile import load_profiles, profile_cache_key, profile_path, save_profile
from qq_chatbot.schema import Corpus, PersonaProfile, Speaker, Turn

PERSON = "c2c:fictional-person"
GROUP_A = "group:fiction-a"
GROUP_B = "group:fiction-b"
A_TEXT = "fiction_group_a_orchid 我现在住在虚构甲城"
B_TEXT = "fiction_group_b_saffron 我现在住在虚构乙城"
PRIVATE_TEXT = "fiction_private_cobalt 我现在住在虚构私城"
ANSWER = "fiction_generated_reply 这是虚构回复"


def fictional_corpus():
    rows = [("a", GROUP_A, "group", A_TEXT), ("b", GROUP_B, "group", B_TEXT),
            ("p", PERSON, "c2c", PRIVATE_TEXT)]
    turns = [Turn(tid, Speaker(PERSON, "虚构人物"), conversation, kind,
                  1700000000 + i, f"2023-11-14 22:13:{20+i:02}", text, [f"fiction-msg-{tid}"])
             for i, (tid, conversation, kind, text) in enumerate(rows)]
    return Corpus(turns=turns)


class FictionalLLM:
    name = "fictional-offline"

    def __init__(self):
        self.requests = []
        self._lock = threading.Lock()

    def chat(self, messages, **kwargs):
        with self._lock:
            self.requests.append(copy.deepcopy(messages))
        prompt = messages[-1]["content"]
        if "资料：\n" in prompt:
            rows = json.loads(prompt.split("资料：\n", 1)[1])
            return json.dumps({"timeline": [
                {"topic": "居住城市", "event": row["text"], "source_turn_ids": [row["turn_id"]],
                 "evidence": [{"turn_id": row["turn_id"], "quote": row["text"]}],
                 "event_time": None, "temporal_scope": "current", "certainty": "explicit", "confidence": 0.9}
                for row in rows]}, ensure_ascii=False)
        if "只提炼说话风格" in prompt:
            return '{"style":{"tone":"虚构平实"}}'
        return ANSWER

    @property
    def extraction_requests(self):
        return [r for r in self.requests if "资料：\n" in r[-1]["content"] or "只提炼说话风格" in r[-1]["content"]]


def make_bot(root, *, corpus=None, config=None, llm=None):
    config = config or Config()
    config.store.dir = Path(root)
    corpus = corpus if corpus is not None else fictional_corpus()
    llm = llm or FictionalLLM()
    embed = make_embedding(config.embedding)
    store = build_store(corpus.turns, embed)
    save_store(store, Path(root), corpus.meta, embedding_provider="hash", dim=config.embedding.dim)
    retriever = HybridRetriever(store, embed, config.retrieval)
    return Chatbot(config, retriever, llm, corpus=corpus, profiles=load_profiles(Path(root)))


def save_fictional_version(args):
    """Top-level spawn target for cross-process archive preservation."""
    root, name = args
    save_profile(Path(root), PersonaProfile(speaker=PERSON, name=name), kind="group", conversation=GROUP_A)
    return name


class ChatIntegrationTests(unittest.TestCase):
    def test_group_prompt_profile_and_citations_exclude_other_scopes(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp)
            bot.config.retrieval.min_score = 0
            bot.config.retrieval.persona_inject_retrieval = True
            # First populate a broad/private profile to catch accidental cache reuse.
            bot.answer("fiction", mode="persona", person=PERSON, kind="c2c")
            bot.llm.requests.clear()
            answer = bot.answer("fiction", mode="persona", person=PERSON, kind="group", conversation=GROUP_A)
            all_requests = json.dumps(bot.llm.requests, ensure_ascii=False)
            self.assertIn(A_TEXT, all_requests)
            for forbidden in (B_TEXT, PRIVATE_TEXT):
                self.assertNotIn(forbidden, all_requests)
            self.assertTrue(answer.citations)
            self.assertTrue(all(c.conversation_key == GROUP_A for c in answer.citations))
            profile = bot.profiles[profile_cache_key(PERSON, "group", GROUP_A)]
            self.assertEqual({s["conversation_key"] for e in profile.timeline for s in e["sources"]}, {GROUP_A})
            saved = profile_path(Path(tmp), PERSON, "group", GROUP_A).read_text(encoding="utf-8")
            self.assertNotIn(B_TEXT, saved)
            self.assertNotIn(PRIVATE_TEXT, saved)
            self.assertIn(A_TEXT, saved)

    def test_restart_reuses_profile_without_any_extraction_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp)
            bot.answer("fiction", mode="persona", person=PERSON, kind="group", conversation=GROUP_A)
            self.assertEqual(len(bot.llm.extraction_requests), 2)
            second_llm = FictionalLLM()
            with patch("qq_chatbot.chat.make_llm", return_value=second_llm):
                restarted = load_chatbot(bot.config)
            restarted.answer("fiction", mode="persona", person=PERSON, kind="group", conversation=GROUP_A)
            self.assertEqual(second_llm.extraction_requests, [])
            self.assertEqual(len(second_llm.requests), 1)  # only response generation
            self.assertFalse((Path(tmp) / "profiles" / "history").exists())

    def test_default_persona_without_evidence_is_not_grounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp, corpus=Corpus())
            answer = bot.answer("fiction unrelated", mode="persona", person=PERSON)
            self.assertEqual(answer.text, ANSWER)
            self.assertFalse(answer.grounded)
            self.assertEqual(answer.citations, [])

    def test_memory_create_session_isolates_scope_and_bot_identity_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config()
            config.memory.enabled = True
            config.bridge.self_id = 900001
            bot = make_bot(tmp, config=config)
            first = bot.create_session("group:fiction-a|sender:fiction-user")
            first.add("fiction_memory_shared_a 用户虚构陈述", "fiction_memory_generated_a 生成回复")
            self.assertIsNotNone(first.memory_store)
            restarted = make_bot(tmp, config=copy.deepcopy(config))
            same = restarted.create_session("group:fiction-a|sender:fiction-user")
            other_group = restarted.create_session("group:fiction-b|sender:fiction-user")
            other_person = restarted.create_session("group:fiction-a|sender:fiction-other")
            self.assertIn("fiction_memory_shared_a", json.dumps(same.as_messages(query="fiction_memory_shared_a")))
            self.assertEqual(other_group.as_messages(), [])
            self.assertEqual(other_person.as_messages(), [])
            another_config = copy.deepcopy(config)
            another_config.bridge.self_id = 900002
            another_bot = make_bot(tmp, config=another_config)
            self.assertEqual(another_bot.create_session("group:fiction-a|sender:fiction-user").as_messages(), [])
            restarted.answer("fiction_memory_shared_a", mode="persona", person=PERSON, session=same,
                             kind="group", conversation=GROUP_A)
            response_request = restarted.llm.requests[-1]
            self.assertIn("fiction_memory_shared_a", json.dumps(response_request))
            self.assertNotIn("fiction_memory_shared_a", response_request[0]["content"])

    def test_trace_default_content_redaction_and_evidence_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config()
            config.observability.enabled = True
            config.retrieval.min_score = 0
            self.assertFalse(config.observability.include_content)
            bot = make_bot(tmp, config=config)
            answer = bot.answer("fiction_query_secret", mode="qa", conversation=GROUP_A)
            self.assertTrue(answer.trace_id)
            self.assertTrue(answer.grounded)
            raw = (Path(tmp) / "traces.jsonl").read_text(encoding="utf-8")
            for secret in ("fiction_query_secret", ANSWER, A_TEXT, B_TEXT, PRIVATE_TEXT, GROUP_A):
                self.assertNotIn(secret, raw)
            row = json.loads(raw)
            self.assertEqual(row["id"], answer.trace_id)
            self.assertTrue(row["evidence"])
            self.assertEqual(set(row["evidence"][0]), {"ref", "score", "vector_score", "keyword_score"})
            self.assertNotIn("query", row)
            self.assertNotIn("answer", row)

    def test_concurrent_answers_extract_once_per_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp)
            start = threading.Barrier(4)

            def ask(_):
                start.wait(timeout=10)
                return bot.answer("fiction", mode="persona", person=PERSON, kind="group", conversation=GROUP_A)

            with ThreadPoolExecutor(max_workers=4) as pool:
                answers = list(pool.map(ask, range(4)))
            self.assertEqual(len(answers), 4)
            self.assertEqual(len(bot.llm.extraction_requests), 2)
            self.assertFalse((Path(tmp) / "profiles" / "history").exists())

    def test_concurrent_process_saves_archive_every_published_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            names = [f"fiction-version-{i}" for i in range(8)]
            save_fictional_version((tmp, "fiction-initial"))
            with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
                self.assertEqual(set(pool.map(save_fictional_version, [(tmp, n) for n in names])), set(names))
            current = profile_path(Path(tmp), PERSON, "group", GROUP_A)
            archives = list((current.parent / "history").glob("*.json"))
            self.assertEqual(len(archives), len(names))
            all_versions = [json.loads(p.read_text(encoding="utf-8"))["name"] for p in [current, *archives]]
            self.assertEqual(set(all_versions), {"fiction-initial", *names})
            self.assertEqual(len(all_versions), len(set(all_versions)))


if __name__ == "__main__":
    unittest.main()
