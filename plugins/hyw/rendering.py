from __future__ import annotations

import html
import json
import re
import tempfile
from pathlib import Path

from otae_bot.infrastructure.rendering.browser import screenshot_web_element

from .agent import Answer


def wants_card(text: str) -> bool:
    return len(text) > 200 or bool(re.search(r"(?m)^#{1,6} |```|\*\*|<summary>|\|.+\|", text))


def prepare_html(answer: Answer) -> str:
    # The upstream component accepts raw HTML: permit only its plain summary marker.
    markdown = re.sub(
        r"<[^>]*>", lambda match: match[0] if match[0] in {"<summary>", "</summary>"} else html.escape(match[0]),
        answer.text,
    )
    data = {
        "markdown": markdown, "references": answer.sources, "page_references": [], "image_references": [],
        "stages": [], "stats": {"operation_rounds": answer.turns}, "total_time": 0, "theme_color": "#ef4444",
    }
    serialized = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    # No remote images, scripts, favicons or model-supplied network requests during rendering.
    csp = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; base-uri 'none'; form-action 'none'"
    injection = f'<meta http-equiv="Content-Security-Policy" content="{csp}"><style>img[src^="http"]{{display:none}}</style>'
    template = (Path(__file__).parent / "assets/card.html").read_text(encoding="utf-8")
    initial = "window.RENDER_DATA = {};"
    if template.count(initial) != 1:
        raise ValueError("Unsupported upstream card bootstrap")
    return template.replace("<head>", "<head>" + injection, 1).replace(initial, f"window.RENDER_DATA = {serialized};", 1)


async def render_answer(answer: Answer) -> bytes:
    with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as file:
        file.write(prepare_html(answer))
        path = Path(file.name)
    try:
        return await screenshot_web_element(
            path.as_uri(), "#main-container", viewport=(1100, 900), max_height=12000,
            strict_max_height=True, wait_for_images=True, wait_for_fonts=True, settle_ms=350,
        )
    finally:
        path.unlink(missing_ok=True)


def source_links(answer: Answer) -> str:
    cited = [item for item in answer.sources if f'[{item["index"]}]' in answer.text]
    if not cited:
        return ""
    return "参考资料：\n" + "\n".join(f'[{item["index"]}] {item["title"]}\n{item["url"]}' for item in cited)
