"""Port de `extractors/JkanimeExtractor.kt` (reproductores propios de Jkanime).

Resuelve los hosts internos ``Desuka``, ``Nozomi`` y ``Desu`` que sirve la
propia jkanime.net.
"""

from __future__ import annotations

import json

import requests
from bs4 import BeautifulSoup

from anime_sources.base import VideoStream

from ._common import after, before


def get_nozomi(url: str, session: requests.Session, prefix: str = "") -> list[VideoStream]:
    headers = {"Referer": url}
    response = session.get(url, headers=headers)
    response.raise_for_status()
    document = BeautifulSoup(response.text, "html.parser")

    data_input = document.select_one("form input[value]")
    data_key = (data_input.get("value") if data_input else "") or ""

    redirect = session.post(
        "https://jkanime.net/gsplay/redirect_post.php",
        headers=headers,
        data={"data": data_key},
        allow_redirects=True,
    )
    location = redirect.url
    post_key = after(location, "player.html#")

    api_response = session.post("https://jkanime.net/gsplay/api.php", data={"v": post_key})
    try:
        file_url = (api_response.json() or {}).get("file")
    except (json.JSONDecodeError, ValueError):
        file_url = None
    if not file_url:
        return []

    name = f"{_prefix(prefix)}Nozomi"
    return [VideoStream(url=file_url, label=name, quality=name)]


def get_desu(url: str, session: requests.Session, prefix: str = "") -> list[VideoStream]:
    response = session.get(url)
    response.raise_for_status()
    document = BeautifulSoup(response.text, "html.parser")

    script = _find_script(document, "var parts = {")
    if not script:
        return []
    stream_url = before(after(script, "url: '"), "'")
    if not stream_url:
        return []

    name = f"{_prefix(prefix)}Desu"
    return [VideoStream(url=stream_url, label=name, quality=name)]


def get_desuka(url: str, session: requests.Session, prefix: str = "") -> list[VideoStream]:
    response = session.get(url)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "")

    name = f"{_prefix(prefix)}Desuka"
    if content_type.startswith("video/"):
        return [VideoStream(url=response.url, label=name, quality=name, extension="mp4")]

    document = BeautifulSoup(response.text, "html.parser")
    script = _find_script(document, "new DPlayer({")
    if not script:
        return []
    stream_url = before(after(script, "url: '"), "'")
    if not stream_url:
        return []
    return [VideoStream(url=stream_url, label=name, quality=name)]


def _find_script(document: BeautifulSoup, marker: str) -> str | None:
    for element in document.find_all("script"):
        data = element.string or element.get_text()
        if data and marker in data:
            return data
    return None


def _prefix(prefix: str) -> str:
    prefix = (prefix or "").strip()
    return f"{prefix} " if prefix else ""
