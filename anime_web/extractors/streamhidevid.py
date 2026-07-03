"""Port de `lib/streamhidevid-extractor` (StreamHideVidExtractor.kt).

Familia vidhide/streamhide: la pagina embed (`/v/`) contiene un script con el
manifiesto HLS (a veces empaquetado con el *packer*). Se extrae ``file:"...m3u8"``
y se resuelven las variantes con HLS.
"""

from __future__ import annotations

import re

import requests

from anime_sources.base import VideoStream

from ._common import (
    DEFAULT_USER_AGENT,
    after,
    before,
    extract_from_hls,
    name_with_prefix,
    unpack_and_combine,
)


def get_streams(
    url: str,
    session: requests.Session,
    prefix: str = "",
    headers: dict | None = None,
) -> list[VideoStream]:
    request_headers = dict(headers or {})
    request_headers.setdefault("User-Agent", DEFAULT_USER_AGENT)

    embed_url = _embed_url(url)
    response = session.get(embed_url, headers=request_headers)
    response.raise_for_status()

    script = None
    for block in re.findall(r"<script[^>]*>(.*?)</script>", response.text, re.S | re.I):
        if "m3u8" not in block:
            continue
        script = unpack_and_combine(block) if "eval(function(p,a,c" in block else block
        break
    if not script:
        return []

    master_url = before(after(after(script, "source", ""), 'file:"', ""), '"', "")
    if not master_url.startswith("http"):
        return []

    return extract_from_hls(
        session,
        master_url,
        referer=url,
        base_headers=request_headers,
        name_gen=lambda quality: name_with_prefix(prefix, f"StreamHideVid:{quality}"),
    )


def _embed_url(url: str) -> str:
    for marker in ("/d/", "/download/", "/file/", "/f/"):
        if marker in url:
            return url.replace(marker, "/v/")
    return url
