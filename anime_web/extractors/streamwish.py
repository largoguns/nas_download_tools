"""Port de `lib/streamwish-extractor` (StreamWishExtractor.kt).

StreamWish entrega el manifiesto HLS dentro de un script (a veces empaquetado
con el *packer* de Dean Edwards). Se extrae el ``master.m3u8`` mediante regex y
luego se resuelven las variantes con HLS.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import requests

from anime_sources.base import VideoStream

from ._common import DEFAULT_USER_AGENT, extract_from_hls, name_with_prefix, unpack_and_combine

_EMBED_ID_RE = re.compile(r".*/(?:e|f|d|embed)/([a-zA-Z0-9]+)")
_M3U8_RE = re.compile(r'https[^"\']*m3u8[^"\']*')
# Dominios alternativos por si la URL original no responde. La familia StreamWish
# rota de dominio con frecuencia; probamos SIEMPRE primero la URL original.
_DOMAINS = ["streamwish.com", "sfastwish.com", "flaswish.com", "niramirus.com", "medixiru.com"]


def get_streams(
    url: str,
    session: requests.Session,
    prefix: str = "",
    headers: dict | None = None,
) -> list[VideoStream]:
    request_headers = dict(headers or {})
    request_headers.setdefault("User-Agent", DEFAULT_USER_AGENT)

    embed_id = _embed_id(url)
    # Probar primero la URL tal cual (sigue redirecciones al dominio vivo) y
    # luego reconstruirla con dominios alternativos usando el id del embed.
    candidates = [url]
    if embed_id:
        candidates += [f"https://{domain}/e/{embed_id}" for domain in _DOMAINS]

    for full_url in candidates:
        try:
            response = session.get(full_url, headers=request_headers)
            if not response.ok or not response.text:
                continue

            script_body = _find_script(response.text)
            if not script_body:
                continue

            match = _M3U8_RE.search(script_body)
            if not match:
                continue

            referer = f"https://{urlparse(response.url).netloc}/"
            streams = extract_from_hls(
                session,
                match.group(0),
                referer=referer,
                base_headers=request_headers,
                name_gen=lambda quality: name_with_prefix(prefix, f"StreamWish:{quality}"),
            )
            if streams:
                return streams
        except Exception:
            continue

    return []


def _embed_id(url: str) -> str:
    match = _EMBED_ID_RE.match(url)
    return match.group(1) if match else ""


def _find_script(body: str) -> str | None:
    # Los scripts empaquetados (packer de Dean Edwards) NO contienen "m3u8"
    # literal hasta desempaquetarlos, asi que hay que desempaquetar primero.
    for block in re.findall(r"<script[^>]*>(.*?)</script>", body, re.S | re.I):
        if "eval(function(p,a,c" in block:
            unpacked = unpack_and_combine(block)
            if unpacked and "m3u8" in unpacked:
                return unpacked
        elif "m3u8" in block:
            return block
    return None
