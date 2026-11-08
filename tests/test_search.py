import gzip
import json
import unittest

from local_agent_lab.search import search
from local_agent_lab.trace import ROOT


def load_sample(sample_id):
    with gzip.open(ROOT / "samples" / (sample_id + ".json.gz"), "rb") as handle:
        return json.loads(handle.read())


class SearchTests(unittest.TestCase):
    def test_rule_sentence_is_located_inside_actual_model_requests_with_message_origin(self):
        snapshot = load_sample("5eb8a17eef4a4594")  # verified-write (실제 4B 기록)
        rules = next(e for e in snapshot["events"] if e["kind"] == "rules.wrap")
        sentence = rules["input"]["raw_text"].strip().splitlines()[0].lstrip("-# ").strip()
        result = search(snapshot, sentence)
        self.assertGreater(result["count"], 0)
        requests = [h for h in result["hits"] if h["kind"] == "model.request" and h["field"].startswith("input.messages")]
        self.assertTrue(requests, "규칙 문장은 실제 모델 요청의 messages 안에서 찾혀야 한다")
        first = requests[0]
        self.assertEqual(first["role"], "system")
        self.assertEqual(first["message_index"], 0)
        self.assertEqual(first["request_number"], 1)
        self.assertIn(sentence, first["snippet"])
        self.assertIn("field", first)
        # 파일 원문(file.decode)에서도 같은 문장이 찾혀 '존재'와 '요청 포함'을 구분해 볼 수 있다.
        self.assertTrue(any(h["kind"] == "file.decode" for h in result["hits"]))
        self.assertEqual(result["by_kind"]["model.request"], len(requests))

    def test_tool_result_bodies_and_kind_filter_and_limits(self):
        snapshot = load_sample("5eb8a17eef4a4594")
        result = search(snapshot, "practice", kinds={"tool.result"})
        self.assertTrue(result["hits"])
        self.assertTrue(all(h["kind"] == "tool.result" for h in result["hits"]))
        self.assertTrue(all("snippet" in h and "event_id" in h for h in result["hits"]))
        everything = search(snapshot, "e", limit=5)
        self.assertEqual(everything["count"], 5)
        self.assertTrue(everything["truncated"])
        self.assertEqual(search(snapshot, "zzz-not-present-zzz")["count"], 0)
        with self.assertRaises(ValueError):
            search(snapshot, "   ")
        with self.assertRaises(ValueError):
            search(snapshot, "x" * 201)

    def test_summary_request_hits_carry_model_purpose_and_agent(self):
        snapshot = load_sample("2abcb810c4364fdb")  # compaction (요약 요청은 model_purpose=compaction)
        result = search(snapshot, "요약", kinds={"model.request"})
        purposes = {h.get("model_purpose") for h in result["hits"]}
        self.assertIn("compaction", purposes)
        self.assertTrue(all(h["agent_id"] for h in result["hits"]))


if __name__ == "__main__":
    unittest.main()
