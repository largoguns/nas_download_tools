"""Port de `lib/uqload-extractor` (UqloadExtractor.kt).

La pagina contiene un script con ``sources: ["URL"]`` apuntando al MP4.
"""

from __future__ import annotations

import requests
from bs4 import BeautifulSoup

from anime_sources.base import VideoStream

from ._common import DEFAULT_USER_AGENT, after, before, name_with_prefix


def get_streams(
    url: str,
    session: requests.Session,
    prefix: str = "",
    headers: dict | None = None,
) -> list[VideoStream]:
    request_headers = dict(headers or {})
    request_headers.setdefault("User-Agent", DEFAULT_USER_AGENT)

    response = session.get(url, headers=request_headers)
    response.raise_for_status()
    document = BeautifulSoup(response.text, "html.parser")

    script = None
    for element in document.find_all("script"):
        data = element.string or element.get_text()
        if data and "sources:" in data:
            script = data
            break
    if not script:
        return []

    video_url = before(after(script, 'sources: ["', ""), '"', "")
    if not video_url.startswith("http"):
        return []

    name = name_with_prefix(prefix, "Uqload")
    return [
        VideoStream(
            url=video_url,
            label=name,
            quality=name,
            extension="mp4",
            headers={"Referer": "https://uqload.ws/"},
        ),
    ]
