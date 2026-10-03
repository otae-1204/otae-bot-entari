"""Offline source labels from tool evidence, never assistant-authored link text."""
import json
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

from .media import discover


def source_key(url: str) -> str:
    try:
        return unquote(urlsplit(url)._replace(fragment="").geturl())
    except ValueError:
        return url


class _TitleParser(HTMLParser):
    """Extract text from provider title markup without interpreting it as Markdown."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str):
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs):
        if tag == "br":
            self.parts.append(" ")


def plain_title(title: str) -> str:
    parser = _TitleParser()
    parser.feed(title)
    parser.close()
    return " ".join("".join(parser.parts).split())


def source_titles(messages: list[dict]) -> dict[str, str]:
    titles: dict[str, str] = {}
    image_pages: dict[str, str] = {}
    for message in messages:
        name = message.get("toolName")
        if (message.get("role") != "toolResult" or message.get("isError")
                or name not in {"web_search", "search_images", "jina_read_url", "reverse_image_search"}):
            continue
        content = message.get("content", [])
        if not content or content[0].get("type") != "text":
            continue
        try:
            data = json.loads(content[0]["text"])
        except (ValueError, KeyError):
            continue
        if not isinstance(data, dict):
            continue
        for batch in data.get("results", []):
            if not batch.get("ok"):
                continue
            rows = batch.get("results", []) if name in {"web_search", "search_images"} else [batch]
            if name == "reverse_image_search":
                rows = [source for match in batch.get("matches", []) for source in match.get("sources", [match])]
            for row in rows:
                url = row.get("url", "")
                if not url:
                    continue
                title = plain_title(row.get("title") or "")
                if not title or title in {url, row.get("source_url")}:
                    title = urlsplit(url).hostname or url
                for address in (url, row.get("source_url", "")):
                    if address:
                        key = source_key(address)
                        if name == "jina_read_url" or key not in titles:
                            titles[key] = title
                if row.get("image_url"):
                    image_pages[source_key(row["image_url"])] = source_key(url)
                text = row.get("snippet", row.get("content", ""))
                for image in discover(text, url, title, 0):
                    image_pages.setdefault(source_key(image.url), source_key(url))
        for image in data.get("media_images", []):
            if image.get("url") and image.get("source_url"):
                image_pages[source_key(image["url"])] = source_key(image["source_url"])
    for image, page in image_pages.items():
        titles[image] = titles.get(page) or urlsplit(page).hostname or page
    return titles
