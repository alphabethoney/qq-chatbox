"""Offline regression tests for evidence admitted to persona generation."""
from __future__ import annotations

import copy
import unittest

from qq_chatbot.chat import Chatbot
from qq_chatbot.config import Config
from qq_chatbot.schema import PersonaProfile, RetrievedTurn, Speaker, Turn
from qq_chatbot.session import Session


PERSON = "c2c:fictional-filter-person"


class FixedRetriever:
    def __init__(self, hits):
        self.hits = hits

    def retrieve(self, query, **filters):
        return list(self.hits)


class RecordingLLM:
    def __init__(self):
        self.requests = []

    def chat(self, messages, **kwargs):
        self.requests.append(copy.deepcopy(messages))
        return "fictional reply"


def hit(text, score):
    return RetrievedTurn(Turn(text, Speaker(PERSON, "虚构人物"), PERSON, "c2c",
                              1700000000, "2023-11-14 22:13:20", text, [text]), score)


class PersonaRetrievalFilterTests(unittest.TestCase):
    def make_bot(self, hits, threshold=0.35, inject=True):
        config = Config()
        config.retrieval.min_score = threshold
        config.retrieval.persona_inject_retrieval = inject
        return Chatbot(config, FixedRetriever(hits), RecordingLLM(), profiles={
            PERSON + "#": PersonaProfile(speaker=PERSON, style={"tone": "fictional_profile_tone"})})

    def test_mixed_scores_filter_prompt_and_citations_at_inclusive_boundary(self):
        hits = [hit("fiction_low_030", 0.30), hit("fiction_boundary_035", 0.35),
                hit("fiction_high_080", 0.80), hit("fiction_low_016", 0.16)]
        bot = self.make_bot(hits)
        answer = bot.answer("fictional query", mode="persona", person=PERSON)
        prompt = bot.llm.requests[0][-1]["content"]
        self.assertNotIn("fiction_low_030", prompt)
        self.assertNotIn("fiction_low_016", prompt)
        self.assertIn("fiction_boundary_035", prompt)
        self.assertIn("fiction_high_080", prompt)
        self.assertLess(prompt.index("fiction_boundary_035"), prompt.index("fiction_high_080"))
        self.assertEqual([c.text for c in answer.citations],
                         ["fiction_boundary_035", "fiction_high_080"])
        self.assertTrue(answer.grounded)

    def test_all_low_scores_still_chat_with_profile_and_session(self):
        bot = self.make_bot([hit("fiction_low_only", 0.30)])
        session = Session(bot.config.session.max_turns)
        session.add("fiction_history_user", "fiction_history_reply")
        answer = bot.answer("fiction_current_query", mode="persona", person=PERSON, session=session)
        self.assertEqual(answer.text, "fictional reply")
        self.assertFalse(answer.grounded)
        self.assertEqual(answer.citations, [])
        self.assertEqual(len(bot.llm.requests), 1)
        messages = bot.llm.requests[0]
        self.assertIn("fictional_profile_tone", messages[0]["content"])
        self.assertEqual(messages[1:-1], session.as_messages(query="fiction_current_query"))
        self.assertIn("fiction_current_query", messages[-1]["content"])
        self.assertNotIn("fiction_low_only", str(messages))

    def test_configured_zero_threshold_admits_low_scores(self):
        bot = self.make_bot([hit("fiction_zero_score", 0.0)], threshold=0)
        answer = bot.answer("fictional query", mode="persona", person=PERSON)
        self.assertIn("fiction_zero_score", bot.llm.requests[0][-1]["content"])
        self.assertTrue(answer.grounded)

    def test_default_off_omits_high_scores_but_keeps_profile_history_and_query(self):
        self.assertFalse(Config().retrieval.persona_inject_retrieval)
        bot = self.make_bot([hit("fiction_high_hidden", 0.99)], inject=False)
        session = Session(bot.config.session.max_turns)
        session.add("history user", "history reply")
        answer = bot.answer("current query", mode="persona", person=PERSON, session=session)
        messages = bot.llm.requests[0]
        self.assertNotIn("fiction_high_hidden", str(messages))
        self.assertIn("fictional_profile_tone", messages[0]["content"])
        self.assertEqual(messages[1:-1], session.as_messages(query="current query"))
        self.assertIn("current query", messages[-1]["content"])
        self.assertEqual(answer.text, "fictional reply")
        self.assertFalse(answer.grounded)
        self.assertEqual(answer.citations, [])

    def test_qa_high_scores_ignore_persona_switch(self):
        for inject in (False, True):
            with self.subTest(inject=inject):
                bot = self.make_bot([hit("qa_low", 0.30), hit("qa_boundary", 0.35)], inject=inject)
                answer = bot.answer("query", mode="qa")
                self.assertNotIn("qa_low", str(bot.llm.requests))
                self.assertIn("qa_boundary", str(bot.llm.requests))
                self.assertEqual([c.text for c in answer.citations], ["qa_boundary"])
                self.assertTrue(answer.grounded)

    def test_qa_without_evidence_keeps_early_return(self):
        bot = self.make_bot([hit("fiction_qa_low", 0.30)])
        answer = bot.answer("fictional query", mode="qa")
        self.assertEqual(answer.text, "记录里没有找到相关证据。")
        self.assertFalse(answer.grounded)
        self.assertEqual(answer.citations, [])
        self.assertEqual(bot.llm.requests, [])


if __name__ == "__main__":
    unittest.main()
