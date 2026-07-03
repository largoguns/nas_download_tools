"""Port de `lib/yourupload-extractor` (YourUploadExtractor.kt).

La pagina del reproductor contiene un script ``jwplayerOptions`` con la URL del
fichero en ``file: '...'``.
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
    request_headers["referer"] = "https://www.yourupload.com/"

    try:
        response = session.get(url, headers=request_headers)
        response.raise_for_status()
    except Exception:
        return []

    document = BeautifulSoup(response.text, "html.parser")
    script = None
    for element in document.find_all("script"):
        data = element.string or element.get_text()
        if data and "jwplayerOptions" in data:
            script = data
            break
    if not script:
        return []

    file_url = before(after(script, "file: '"), "',")
    if not file_url.startswith(("http://", "https://")):
        return []

    name = name_with_prefix(prefix, "YourUpload")
    return [VideoStream(url=file_url, label=name, quality=name, extension="mp4", headers=request_headers)]
