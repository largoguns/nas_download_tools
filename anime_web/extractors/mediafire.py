"""Extractor de Mediafire.

La pagina ``/file/{id}/`` contiene el enlace directo al fichero en
``a#downloadButton[href]`` (a veces ofuscado en ``data-scrambled-url`` como
base64). Es un MP4 directo, ideal para descarga sin pasos intermedios.
"""

from __future__ import annotations

import base64
import re

import requests
from bs4 import BeautifulSoup

from anime_sources.base import VideoStream

from ._common import DEFAULT_USER_AGENT, name_with_prefix

_FALLBACK_RE = re.compile(r'https?://download[^"\'\s]+\.mediafire\.com/[^"\'\s]+')


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

    download_url = ""
    button = document.select_one("a#downloadButton")
    if button:
        download_url = (button.get("href") or "").strip()
        if not download_url.startswith("http"):
            scrambled = button.get("data-scrambled-url")
            if scrambled:
                try:
                    download_url = base64.b64decode(scrambled).decode("utf-8").strip()
                except Exception:
                    download_url = ""

    if not download_url:
        match = _FALLBACK_RE.search(response.text)
        download_url = match.group(0) if match else ""

    if not download_url.startswith(("http://", "https://")):
        return []

    tail = download_url.split("?", 1)[0].rsplit("/", 1)[-1]
    extension = tail.rsplit(".", 1)[-1].lower() if "." in tail else "mp4"
    if not extension.isalnum() or len(extension) > 4:
        extension = "mp4"

    name = name_with_prefix(prefix, "Mediafire")
    return [VideoStream(url=download_url, label=name, quality=name, extension=extension)]
