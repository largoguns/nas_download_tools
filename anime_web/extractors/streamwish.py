"""Port de `lib/streamwish-extractor` (StreamWishExtractor.kt).

StreamWish entrega el manifiesto HLS dentro de un script (a veces empaquetado
con el *packer* de Dean Edwards). Se extrae el ``master.m3u8`` mediante regex y
luego se resuelven las variantes con HLS.
"""

from __future__ import annotations

import re

import requests

from anime_sources.base import VideoStream

from ._common import DEFAULT_USER_AGENT, extract_from_hls, name_with_prefix, unpack_and_combine

_EMBED_ID_RE = re.compile(r".*/(?:e|f|d)/([a-zA-Z0-9]+)")
_M3U8_RE = re.compile(r'https[^"]*m3u8[^"]*')
_DOMAINS = ["streamwish.com", "niramirus.com", "medixiru.com"]


def get_streams(
    url: str,
    session: requests.Session,
    prefix: str = "",
    headers: dict | None = None,
) -> list[VideoStream]:
    request_headers = dict(headers or {})
    request_headers.setdefault("User-Agent", DEFAULT_USER_AGENT)

    embed_id = _embed_id(url)
    is_full_url = embed_id.startswith("https://")

    for domain in _DOMAINS:
        full_url = embed_id if is_full_url else f"https://{domain}/{embed_id}"
        try:
            response = session.get(full_url, headers=request_headers)
            if not response.ok:
                continue
            body = response.text
            if not body:
                continue

            script_body = _find_script(body)
            if not script_body:
                continue

            match = _M3U8_RE.search(script_body)
            if not match:
                continue

            master_url = match.group(0)
            from urllib.parse import urlparse

            referer = f"https://{urlparse(full_url).netloc}/"
            return extract_from_hls(
                session,
                master_url,
                referer=referer,
                base_headers=request_headers,
                name_gen=lambda quality: name_with_prefix(prefix, f"StreamWish:{quality}"),
            )
        except Exception:
            if is_full_url:
                return []
            continue

    return []


def _embed_id(url: str) -> str:
    match = _EMBED_ID_RE.match(url)
    return match.group(1) if match else url


def _find_script(body: str) -> str | None:
    for block in re.findall(r"<script[^>]*>(.*?)</script>", body, re.S | re.I):
        if "m3u8" not in block:
            continue
        if "eval(function(p,a,c" in block:
            return unpack_and_combine(block)
        return block
    return None
