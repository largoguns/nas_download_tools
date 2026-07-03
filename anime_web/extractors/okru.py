"""Port de `lib/okru-extractor` (OkruExtractor.kt).

ok.ru expone la configuracion del reproductor en el atributo ``data-options`` de
un ``<div>`` como JSON doblemente escapado. Puede contener un playlist HLS
(``ondemandHls``), uno DASH (``ondemandDash``) o una lista de MP4 por calidad.
"""

from __future__ import annotations

import requests
from bs4 import BeautifulSoup

from anime_sources.base import VideoStream

from ._common import after, before, extract_from_hls, name_with_prefix

_QUALITIES = {
    "ultra": "2160p",
    "quad": "1440p",
    "full": "1080p",
    "hd": "720p",
    "sd": "480p",
    "low": "360p",
    "lowest": "240p",
    "mobile": "144p",
}


def get_streams(
    url: str,
    session: requests.Session,
    prefix: str = "",
    headers: dict | None = None,
    fix_qualities: bool = True,
) -> list[VideoStream]:
    response = session.get(url, headers=dict(headers or {}))
    response.raise_for_status()
    document = BeautifulSoup(response.text, "html.parser")

    container = document.select_one("div[data-options]")
    if not container:
        return []
    video_string = container.get("data-options") or ""
    if not video_string:
        return []

    if "ondemandHls" in video_string:
        playlist_url = _extract_link(video_string, "ondemandHls")
        return extract_from_hls(
            session,
            playlist_url,
            base_headers=headers,
            name_gen=lambda quality: name_with_prefix(prefix, f"Okru:{quality}"),
        )
    if "ondemandDash" in video_string:
        playlist_url = _extract_link(video_string, "ondemandDash")
        name = name_with_prefix(prefix, "Okru:DASH")
        return [VideoStream(url=playlist_url, label=name, quality=name)]

    return _videos_from_json(video_string, prefix, fix_qualities)


def _extract_link(video_string: str, attr: str) -> str:
    # En el JSON escapado los valores aparecen como `attr\":\"valor\"`.
    fragment = after(video_string, f'{attr}\\":\\"', "")
    return before(fragment, '\\"', "").replace("\\\\u0026", "&")


def _videos_from_json(video_string: str, prefix: str, fix_qualities: bool) -> list[VideoStream]:
    array_data = before(
        after(video_string, '\\"videos\\":[{\\"name\\":\\"', ""),
        "]",
    )
    if not array_data:
        return []

    streams: list[VideoStream] = []
    for part in reversed(array_data.split('{\\"name\\":\\"')):
        video_url = _extract_link(part, "url")
        quality = before(part, '\\"', "")
        if fix_qualities:
            quality = _QUALITIES.get(quality, quality)
        if video_url.startswith("https://"):
            name = name_with_prefix(prefix, f"Okru:{quality}")
            streams.append(VideoStream(url=video_url, label=name, quality=name, extension="mp4"))
    return streams
