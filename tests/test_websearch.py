"""인터넷 검색 도구. 네트워크는 쓰지 않고 응답을 흉내 내어 코드의 경계만 검사한다."""

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from local_agent_lab import websearch
from local_agent_lab.context import tools_for
from local_agent_lab.features import DEFAULT_FEATURES, normalize_features
from local_agent_lab.harness import execute_tool
from local_agent_lab.trace import Run

NEWS = """<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>
<item><title>예전 기사</title><link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;url=https%3a%2f%2fold.example%2fa&amp;c=1</link>
<description>지난 소식</description><pubDate>Mon, 03 Aug 2026 01:00:00 GMT</pubDate></item>
<item><title>엔비디아 &lt;b&gt;실적&lt;/b&gt; 발표</title><link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;url=https%3a%2f%2fnew.example%2fb&amp;c=2</link>
<description>매출이   증가했습니다.</description><pubDate>Fri, 06 Nov 2026 03:28:01 GMT</pubDate></item>
<item><title>중간 주소만 있는 기사</title><link>http://www.bing.com/news/other</link><description>x</description></item>
</channel></rss>""".encode()
WEB = """<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>
<item><title>회사 소개</title><link>https://www.nvidia.com/ko-kr/</link><description>설명</description><pubDate>월, 09 11월 2026 12:44:00 GMT</pubDate></item>
<item><title>기사 중복</title><link>https://new.example/b</link><description>겹침</description></item>
<item><title>위험한 주소</title><link>javascript:alert(1)</link><description>x</description></item>
</channel></rss>""".encode()


class Reply:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return self.body[:limit]


def opener_for(news=NEWS, web=WEB, status=200):
    def opener(request, timeout):
        return Reply(news if "/news/" in request.full_url else web, status)

    return opener


def call(name, arguments, number=1):
    return {"id": f"call-{number}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


class ParserTests(unittest.TestCase):
    def test_news_comes_first_newest_first_with_real_addresses_and_dates(self):
        found = websearch.search("엔비디아 최신", open_url=opener_for())
        self.assertEqual(
            found["results"],
            [
                {"type": "news", "title": "엔비디아 실적 발표", "url": "https://new.example/b", "snippet": "매출이 증가했습니다.", "date": "2026-11-06"},
                {"type": "news", "title": "예전 기사", "url": "https://old.example/a", "snippet": "지난 소식", "date": "2026-08-03"},
                {"type": "web", "title": "회사 소개", "url": "https://www.nvidia.com/ko-kr/", "snippet": "설명"},
            ],
        )
        self.assertFalse(found["partial"])

    def test_requests_go_only_to_the_fixed_addresses_with_an_honest_client_name(self):
        seen = []

        def opener(request, timeout):
            seen.append((request.full_url, timeout, request.get_header("User-agent")))
            return Reply(NEWS if "/news/" in request.full_url else WEB)

        websearch.search("엔비디아 최신", open_url=opener)
        self.assertEqual(len(seen), 2)
        self.assertTrue(seen[0][0].startswith(websearch.NEWS_ENDPOINT + "?q="))
        self.assertTrue(seen[1][0].startswith(websearch.WEB_ENDPOINT + "?q="))
        self.assertIn("format=rss", seen[0][0])
        self.assertIn("setlang=ko", seen[0][0])
        for _, timeout, agent in seen:
            self.assertEqual(timeout, websearch.TIMEOUT_SECONDS)
            self.assertTrue(agent.startswith("AgentLens/"))

    def test_english_queries_do_not_force_korean_results(self):
        seen = []

        def opener(request, timeout):
            seen.append(request.full_url)
            return Reply(WEB)

        websearch.search("nvidia news", open_url=opener)
        self.assertNotIn("setlang", seen[0])

    def test_one_failed_feed_keeps_the_other_and_says_so(self):
        def opener(request, timeout):
            if "/news/" in request.full_url:
                raise urllib.error.URLError("offline")
            return Reply(WEB)

        found = websearch.search("q", open_url=opener)
        self.assertTrue(found["partial"])
        self.assertEqual([item["title"] for item in found["results"]], ["회사 소개", "기사 중복"])

    def test_a_block_page_or_broken_reply_is_reported_not_treated_as_no_results(self):
        with self.assertRaises(websearch.SearchFailed):
            websearch.search("q", open_url=opener_for(status=202))
        page = b"<!DOCTYPE html><html><body>verify</body></html>"
        with self.assertRaises(websearch.SearchFailed):
            websearch.search("q", open_url=opener_for(news=page, web=page))
        with self.assertRaises(websearch.SearchFailed):
            websearch.search("q", open_url=opener_for(news=b"<rss", web=b"not xml"))

        def refuse(request, timeout):
            raise urllib.error.URLError("offline")

        with self.assertRaises(websearch.SearchFailed):
            websearch.search("q", open_url=refuse)

    def test_the_model_gets_site_names_instead_of_long_addresses(self):
        view = websearch.for_model([{"type": "news", "title": "t", "url": "https://www.msn.com/ko-kr/a%20b", "snippet": "s", "date": "2026-11-06"}])
        self.assertEqual(view, [{"type": "news", "title": "t", "snippet": "s", "date": "2026-11-06", "source": "msn.com"}])

    def test_a_valid_empty_feed_means_no_results(self):
        empty = b'<?xml version="1.0"?><rss><channel></channel></rss>'
        self.assertEqual(websearch.search("q", open_url=opener_for(news=empty, web=empty))["results"], [])


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)

    def run_for(self, **features):
        return Run("엔비디아 최신 현황", "web-test", features=features)

    def names(self, run):
        return [tool["function"]["name"] for tool in tools_for(run)]

    def execute(self, run, arguments, number=1):
        reply = execute_tool(run, self.workspace, [], call("web_search", arguments, number), set())
        return json.loads(reply["content"])

    def test_search_is_off_unless_the_user_turns_it_on(self):
        self.assertFalse(DEFAULT_FEATURES["web"])
        self.assertNotIn("web_search", self.names(self.run_for()))
        self.assertIn("web_search", self.names(self.run_for(web=True)))
        self.assertFalse(normalize_features({"web": True, "tools": False})["web"])

    def test_a_model_cannot_call_search_when_it_is_off(self):
        run = self.run_for()
        with mock.patch.object(websearch, "search") as search:
            result = self.execute(run, {"query": "엔비디아"})
        search.assert_not_called()
        self.assertFalse(result["executed_successfully"])
        self.assertEqual([e for e in run.events if e["kind"] == "web.request"], [])

    def test_a_search_records_what_left_the_computer_and_what_came_back(self):
        run = self.run_for(web=True)
        found = {"provider": "Bing", "status": [200, 200], "bytes": 10, "partial": False, "results": [{"type": "web", "title": "t", "url": "https://a.example", "snippet": "s"}]}
        with mock.patch.object(websearch, "search", return_value=found) as search:
            result = self.execute(run, {"query": "  엔비디아   최신 "})
        search.assert_called_once_with("엔비디아 최신")
        self.assertEqual(result["results"], [{"type": "web", "title": "t", "snippet": "s", "source": "a.example"}])
        self.assertRegex(result["searched_on"], r"^\d{4}-\d{2}-\d{2}$")
        request = next(e for e in run.events if e["kind"] == "web.request")
        self.assertEqual(
            request["input"],
            {"provider": "Bing", "urls": [websearch.NEWS_ENDPOINT, websearch.WEB_ENDPOINT], "query": "엔비디아 최신"},
        )
        self.assertEqual(request["actor"], "web")
        self.assertEqual(next(e for e in run.events if e["kind"] == "web.response")["output"]["results"], found["results"])

    def test_query_length_and_the_per_run_limit_are_checked_in_code(self):
        run = self.run_for(web=True)
        reply = {"provider": "x", "status": [200], "bytes": 1, "partial": False, "results": []}
        with mock.patch.object(websearch, "search", return_value=reply) as search:
            self.assertFalse(self.execute(run, {"query": "가" * 201})["executed_successfully"])
            self.assertFalse(self.execute(run, {"query": "   "})["executed_successfully"])
            search.assert_not_called()
            for number in range(websearch.MAX_SEARCHES_PER_RUN):
                self.assertEqual(self.execute(run, {"query": "q"}, number)["results"], [])
            over = self.execute(run, {"query": "q"}, 99)
        self.assertEqual(search.call_count, websearch.MAX_SEARCHES_PER_RUN)
        self.assertFalse(over["executed_successfully"])
        self.assertIn("한도", over["error"])

    def test_a_failed_search_reaches_the_model_and_the_record(self):
        run = self.run_for(web=True)
        with mock.patch.object(websearch, "search", side_effect=websearch.SearchFailed("막힘")):
            result = self.execute(run, {"query": "엔비디아"})
        self.assertEqual(result, {"error": "막힘", "executed_successfully": False})
        response = next(e for e in run.events if e["kind"] == "web.response")
        self.assertEqual(response["status"], "error")


if __name__ == "__main__":
    unittest.main()
