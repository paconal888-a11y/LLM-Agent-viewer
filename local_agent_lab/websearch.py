"""인터넷 검색의 네트워크 부분.

요청 주소는 여기에 고정한다. 모델은 검색어만 정할 수 있고 주소를 고를 수 없으므로
사설망이나 임의의 사이트로 요청이 나가지 않는다. 검색 서비스를 바꾸려면 이 파일만 고친다.

공개 RSS 결과를 읽는다. 자동 요청을 막는 페이지를 우회하거나 브라우저로 위장하지 않는다.
"""

import html
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

PROVIDER = "Bing"
WEB_ENDPOINT = "https://www.bing.com/search"
NEWS_ENDPOINT = "https://www.bing.com/news/search"
USER_AGENT = "AgentLens/0.9 (local agent learning tool)"
MAX_QUERY_CHARACTERS = 200
MAX_WEB_RESULTS = 4
MAX_NEWS_RESULTS = 4
MAX_SEARCHES_PER_RUN = 5
MAX_RESPONSE_BYTES = 1_000_000
TIMEOUT_SECONDS = 15


class SearchFailed(ValueError):
    """검색 서비스에서 결과를 받지 못했다. 실패 내용은 모델에게도 그대로 전달된다."""


def result_url(link):
    """뉴스 RSS의 중간 주소에서 기사 주소를 꺼낸다. 검색 서비스 자체의 주소는 버린다."""
    parts = urllib.parse.urlsplit(link.strip())
    url = urllib.parse.parse_qs(parts.query).get("url", [""])[0] if parts.path.endswith("apiclick.aspx") else link.strip()
    target = urllib.parse.urlsplit(url)
    if target.scheme not in {"http", "https"} or target.netloc.endswith("bing.com"):
        return ""
    return url[:300]


def clean(text, limit):
    plain = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return " ".join(plain.split())[:limit]


def published(text):
    try:
        return parsedate_to_datetime(text).date().isoformat()
    except (TypeError, ValueError):
        return ""


def feed_items(body):
    # 차단 안내 같은 HTML이나 DTD가 있는 문서는 XML로 해석하지 않는다.
    if b"<!doctype" in body[:2048].lower():
        raise SearchFailed(f"{PROVIDER}가 RSS 대신 다른 문서를 보냈습니다.")
    try:
        return ET.fromstring(body).findall("./channel/item")
    except ET.ParseError as error:
        raise SearchFailed(f"{PROVIDER}의 응답을 읽지 못했습니다: {error}") from error


def parse_feed(body, kind):
    results = []
    for item in feed_items(body):
        title = clean(item.findtext("title"), 120)
        url = result_url(item.findtext("link") or "")
        if not title or not url:
            continue
        entry = {"type": kind, "title": title, "url": url, "snippet": clean(item.findtext("description"), 200)}
        date = published(item.findtext("pubDate")) if kind == "news" else ""
        if date:
            entry["date"] = date
        results.append(entry)
    return results


def for_model(results):
    """모델에게는 사이트 이름만 준다. 긴 주소는 토큰만 쓰고, 전체 주소는 기록에 남는다."""
    return [
        {
            **{key: item[key] for key in ("type", "title", "snippet", "date") if key in item},
            "source": urllib.parse.urlsplit(item["url"]).netloc.removeprefix("www."),
        }
        for item in results
    ]


def fetch(endpoint, query, open_url):
    values = {"q": query, "format": "rss"}
    if re.search("[가-힣]", query):
        values["setlang"] = "ko"
    address = endpoint + "?" + urllib.parse.urlencode(values)
    request = urllib.request.Request(address, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml"})
    try:
        with open_url(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read(MAX_RESPONSE_BYTES + 1)[:MAX_RESPONSE_BYTES]
    except (urllib.error.URLError, OSError) as error:
        raise SearchFailed(f"{PROVIDER}에 연결하지 못했습니다: {error}") from error


def search(query, open_url=urllib.request.urlopen):
    """뉴스(최신순)와 일반 웹 결과를 함께 돌려준다. 한쪽만 실패하면 다른 쪽 결과를 쓴다."""
    collected, failures, statuses, size = {}, [], [], 0
    for kind, endpoint in (("news", NEWS_ENDPOINT), ("web", WEB_ENDPOINT)):
        try:
            status, body = fetch(endpoint, query, open_url)
            statuses.append(status)
            size += len(body)
            if status != 200:
                raise SearchFailed(f"{PROVIDER}가 결과를 주지 않았습니다(HTTP {status}).")
            collected[kind] = parse_feed(body, kind)
        except SearchFailed as error:
            failures.append(str(error))
    if failures and not any(collected.values()):
        raise SearchFailed(failures[0])
    news = sorted(collected.get("news", []), key=lambda item: item.get("date", ""), reverse=True)[:MAX_NEWS_RESULTS]
    seen = {item["url"] for item in news}
    web = [item for item in collected.get("web", []) if item["url"] not in seen][:MAX_WEB_RESULTS]
    return {"provider": PROVIDER, "status": statuses, "bytes": size, "results": news + web, "partial": bool(failures)}
