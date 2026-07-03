from __future__ import annotations

from urllib.parse import urlparse

import requests

from anime_sources.base import SourceError, VideoStream


DIRECT_VIDEO_EXTENSIONS = {"mp4", "m4v", "mov", "webm", "mkv"}


def supports(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"}


def get_streams(url: str, session: requests.Session) -> list[VideoStream]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise SourceError("La URL directa debe ser HTTP o HTTPS.")

    lower_path = parsed.path.lower()
    if lower_path.endswith(".m3u8"):
        return [
            VideoStream(
                url=url,
                label="HLS",
                quality="direct",
                mime_type="application/vnd.apple.mpegurl",
                extension="mp4",
            ),
        ]

    extension = lower_path.rsplit(".", 1)[-1] if "." in lower_path else "mp4"
    if extension not in DIRECT_VIDEO_EXTENSIONS:
        extension = "mp4"

    return [
        VideoStream(
            url=url,
            label="Directo",
            quality="direct",
            mime_type="video/mp4",
            extension=extension,
        ),
    ]
