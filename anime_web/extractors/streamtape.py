"""Port de `lib/streamtape-extractor` (StreamTapeExtractor.kt).

La URL del video se reconstruye desde el script que asigna
``document.getElementById('robotlink').innerHTML``: la primera parte mas el
fragmento que sigue a ``+ ('xcd``.
"""

from __future__ import annotations

import requests
from bs4 import BeautifulSoup

from anime_sources.base import VideoStream

from ._common import after, before

_BASE_EMBED = "https://streamtape.com/e/"
_TARGET = "document.getElementById('robotlink')"


def get_streams(
    url: str,
    session: requests.Session,
    quality: str = "Streamtape",
    headers: dict | None = None,
    prefix: str = "",
) -> list[VideoStream]:
    if prefix:
        quality = f"{prefix.strip()} StreamTape"

    if not url.startswith(_BASE_EMBED):
        parts = url.split("/")
        if len(parts) <= 4:
            return []
        url = _BASE_EMBED + parts[4]

    response = session.get(url, headers=dict(headers or {}))
    response.raise_for_status()
    document = BeautifulSoup(response.text, "html.parser")

    script = None
    for element in document.find_all("script"):
        data = element.string or element.get_text()
        if data and _TARGET in data:
            script = after(data, f"{_TARGET}.innerHTML = '")
            break
    if not script:
        return []

    video_url = "https:" + before(script, "'") + before(after(script, "+ ('xcd"), "'")
    return [VideoStream(url=video_url, label=quality, quality=quality, extension="mp4")]
