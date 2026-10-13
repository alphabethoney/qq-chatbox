"""Configuration round-trip and precedence for persona evidence opt-in."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qq_chatbot.config import Config, load_config, save_config


class PersonaRetrievalConfigTests(unittest.TestCase):
    def test_default_and_round_trip(self):
        self.assertEqual(Config().retrieval.min_score, 0.35)
        self.assertFalse(Config().retrieval.persona_inject_retrieval)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            path = Path(tmp) / "config.json"
            config = Config()
            config.retrieval.persona_inject_retrieval = True
            save_config(config, path)
            self.assertTrue(load_config(path).retrieval.persona_inject_retrieval)

    def test_file_environment_and_override_precedence(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"retrieval": {"persona_inject_retrieval": True}}), encoding="utf-8")
            with patch.dict(os.environ, {"QQ_CHATBOT_PERSONA_INJECT_RETRIEVAL": "false"}):
                self.assertFalse(load_config(path).retrieval.persona_inject_retrieval)
                self.assertTrue(load_config(path, overrides={"retrieval": {
                    "persona_inject_retrieval": "true"}}).retrieval.persona_inject_retrieval)

    def test_invalid_values_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            for value in ("yes", 1, None, []):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    load_config(overrides={"retrieval": {"persona_inject_retrieval": value}})


if __name__ == "__main__":
    unittest.main()
