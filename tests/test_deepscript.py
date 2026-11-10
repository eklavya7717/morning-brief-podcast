import json
import unittest
from pathlib import Path
from unittest import mock

from src import deepscript

ROOT = Path(__file__).resolve().parents[1]


class DeepScriptTests(unittest.TestCase):
    def setUp(self):
        deepscript._cache.clear()
        self.brief = json.loads((ROOT / "docs" / "sample_brief.json").read_text(encoding="utf-8"))
        for s in self.brief["sections"]:
            for i in s["items"]:
                i["body"] = "The Court examined the question in detail. " * 40

    def test_llm_text_is_used_and_attributed(self):
        fake = " ".join(["Reasoned"] * 300)
        with mock.patch.object(deepscript, "_chat", return_value=fake), mock.patch("time.sleep"):
            script = deepscript.build_deep_script(self.brief, {"audio": {"target_minutes": 20}})
        self.assertIn("Reasoned", script)
        self.assertIn("reported by", script)
        self.assertGreater(len(script.split()), 1500)

    def test_falls_back_without_llm(self):
        with mock.patch.object(deepscript, "_chat", return_value=None), mock.patch("time.sleep"):
            script = deepscript.build_deep_script(self.brief, {"audio": {}})
        self.assertTrue(script and "Good morning" in script)

    def test_no_legal_items_returns_none(self):
        self.assertIsNone(deepscript.build_deep_script({"sections": []}, {}))


if __name__ == "__main__":
    unittest.main()
