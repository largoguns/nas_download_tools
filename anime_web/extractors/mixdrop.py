"""Port de `lib/mixdrop-extractor` (MixDropExtractor.kt).

Mixdrop empaqueta la configuracion con el *packer* de Dean Edwards; tras
desempaquetar, la URL del video esta en ``MDCore.wurl`` (sin protocolo).
"""

from __future__ import annotations

import requests
from bs4 import BeautifulSoup

from anime_sources.base import VideoStream

from ._common import DEFAULT_USER_AGENT, after, before, unpack

_DEFAULT_REFERER = "https://mixdrop.co/"


def get_streams(
    url: str,
    session: requests.Session,
    prefix: str = "",
    lang: str = "",
    headers: dict | None = None,
    referer: str = _DEFAULT_REFERER,
) -> list[VideoStream]:
    request_headers = {
        "Referer": referer,
        "User-Agent": DEFAULT_USER_AGENT,
    }
    response = session.get(url, headers=request_headers)
    response.raise_for_status()
    document = BeautifulSoup(response.text, "html.parser")

    packed = None
    for element in document.find_all("script"):
        data = element.string or element.get_text()
        if data and "eval(" in data and "MDCore" in data:
            packed = unpack(data)
            break
    if not packed:
        return []

    video_url = "https:" + before(after(packed, 'Core.wurl="'), '"')

    name = f"{_prefix(prefix)}MixDrop"
    if lang:
        name += f"({lang})"
    return [VideoStream(url=video_url, label=name, quality=name, extension="mp4", headers=request_headers)]


def _prefix(prefix: str) -> str:
    prefix = (prefix or "").strip()
    return f"{prefix} " if prefix else ""
