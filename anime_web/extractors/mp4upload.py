"""Port de `lib/mp4upload-extractor` (Mp4uploadExtractor.kt).

mp4upload sirve la URL dentro de un bloque empaquetado con el *packer* de Dean
Edwards o, en su defecto, en una llamada ``player.src(...)``. La resolucion se
extrae de la marca ``HEIGHT=`` del manifiesto interno.
"""

from __future__ import annotations

import re

import requests
from bs4 import BeautifulSoup

from anime_sources.base import VideoStream

from ._common import DEFAULT_USER_AGENT, after, before, unpack_and_combine

_REFERER = "https://mp4upload.com/"
_QUALITY_RE = re.compile(r"\WHEIGHT=(\d+)")


def get_streams(
    url: str,
    session: requests.Session,
    prefix: str = "",
    headers: dict | None = None,
    suffix: str = "",
) -> list[VideoStream]:
    request_headers = dict(headers or {})
    request_headers.setdefault("User-Agent", DEFAULT_USER_AGENT)
    request_headers["referer"] = _REFERER

    response = session.get(url, headers=request_headers)
    response.raise_for_status()
    document = BeautifulSoup(response.text, "html.parser")

    script = None
    for element in document.find_all("script"):
        data = element.string or element.get_text()
        if not data:
            continue
        if "eval(" in data and "p,a,c,k,e,d" in data:
            script = unpack_and_combine(data)
            break
    if not script:
        for element in document.find_all("script"):
            data = element.string or element.get_text()
            if data and "player.src" in data:
                script = data
                break
    if not script:
        return []

    video_url = _extract_src(script)
    if not video_url:
        return []

    resolution_match = _QUALITY_RE.search(script)
    resolution = f"{resolution_match.group(1)}p" if resolution_match else "Unknown resolution"
    name = f"{_prefix(prefix)}Mp4Upload - {resolution}{suffix}"
    return [VideoStream(url=video_url, label=name, quality=name, extension="mp4", headers=request_headers)]


def _extract_src(script: str) -> str:
    # Equivalente a: substringAfter(".src(").substringBefore(")")
    #                .substringAfter("src:").substringAfter('"').substringBefore('"')
    chunk = before(after(script, ".src("), ")")
    chunk = after(chunk, "src:")
    chunk = after(chunk, '"')
    return before(chunk, '"')


def _prefix(prefix: str) -> str:
    prefix = (prefix or "").strip()
    return f"{prefix} " if prefix else ""
