"""Port aproximado de `lib/universal-extractor` (UniversalExtractor.kt).

El extractor universal original usa un ``WebView`` de Android para interceptar
la primera peticion de red a un ``.mp4/.m3u8/.mpd``. En un entorno Python puro
sin navegador headless no es posible replicar esa intercepcion dinamica, asi que
aqui se hace un mejor esfuerzo: descargar la pagina y buscar por regex una URL
de medios directa. Sirve como ultimo recurso cuando ningun extractor especifico
reconoce el host.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import requests

from anime_sources.base import VideoStream

from ._common import DEFAULT_USER_AGENT, HLS_MIME, extract_from_hls

_VIDEO_RE = re.compile(r'https?://[^\s"\'<>\\]+?\.(mp4|m3u8|mpd)(\?[^\s"\'<>\\]*)?', re.I)


def get_streams(
    url: str,
    session: requests.Session,
    prefix: str = "",
    headers: dict | None = None,
) -> list[VideoStream]:
    request_headers = dict(headers or {})
    request_headers.setdefault("User-Agent", DEFAULT_USER_AGENT)

    try:
        response = session.get(url, headers=request_headers)
        response.raise_for_status()
    except Exception:
        return []

    match = _VIDEO_RE.search(response.text)
    if not match:
        return []

    media_url = match.group(0)
    host = urlparse(url).netloc.split(".")[0].title()
    label = f"{_prefix(prefix)}- {host}: Mirror"

    if ".m3u8" in media_url:
        return extract_from_hls(
            session,
            media_url,
            referer=url,
            base_headers=request_headers,
            name_gen=lambda quality: f"{_prefix(prefix)}- {host}: {quality}",
        )

    download_headers = dict(request_headers)
    download_headers["referer"] = url
    mime = HLS_MIME if ".mpd" in media_url else "video/mp4"
    return [VideoStream(url=media_url, label=label, quality=label, mime_type=mime, headers=download_headers)]


def _prefix(prefix: str) -> str:
    prefix = (prefix or "").strip()
    return f"{prefix} " if prefix else ""
