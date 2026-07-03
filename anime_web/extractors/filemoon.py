"""Port de `lib/filemoon-extractor` (FilemoonExtractor.kt).

Flujo actual de Filemoon basado en su API:

1. ``GET /api/videos/{id}/embed/details`` -> URL del frame embebido.
2. ``GET https://{embedHost}/api/videos/{id}/embed/playback`` con cabeceras
   especificas -> ``sources`` directas o un bloque ``playback`` cifrado.
3. El bloque cifrado se descifra con AES-256-GCM (clave en ``key_parts``).
4. Cada ``source`` se resuelve como HLS.

El descifrado AES-GCM usa ``cryptography`` si esta disponible; si no, se omite
el ramo cifrado y se intentan las ``sources`` en claro.
"""

from __future__ import annotations

import base64
import json
from urllib.parse import quote, urlparse

import requests

from anime_sources.base import VideoStream

from ._common import DEFAULT_USER_AGENT, after, before, extract_from_hls

try:  # pragma: no cover - depende del entorno
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    _HAS_AESGCM = True
except Exception:  # pragma: no cover
    _HAS_AESGCM = False


def get_streams(
    url: str,
    session: requests.Session,
    prefix: str = "Filemoon:",
    headers: dict | None = None,
) -> list[VideoStream]:
    try:
        parsed = urlparse(url)
        host = parsed.netloc
        segments = [seg for seg in parsed.path.split("/") if seg]
        if not segments:
            return []
        media_id = segments[1] if segments[0] == "e" and len(segments) > 1 else segments[-1]

        user_agent = (headers or {}).get("User-Agent", DEFAULT_USER_AGENT)

        details = session.get(f"https://{host}/api/videos/{media_id}/embed/details").text
        embed_url = before(after(after(after(details, "embed_frame_url", ""), ":"), '"'), '"')
        if not embed_url:
            return []
        embed_host = urlparse(embed_url).netloc

        playback_headers = dict(headers or {})
        playback_headers.update(
            {
                "Referer": embed_url,
                "X-Embed-Origin": host,
                "X-Embed-Parent": _encode_url_path(url),
                "X-Embed-Referer": url,
                "Accept": "*/*",
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "User-Agent": user_agent,
            },
        )

        playback_url = f"https://{embed_host}/api/videos/{media_id}/embed/playback"
        playback = session.get(playback_url, headers=playback_headers).json()

        sources = playback.get("sources")
        if not sources and playback.get("playback"):
            if not _HAS_AESGCM:
                return []
            decrypted = json.loads(_decrypt(playback["playback"]))
            sources = decrypted.get("sources")
        if not sources:
            return []

        video_headers = dict(headers or {})
        video_headers["Referer"] = f"https://{host}/"
        video_headers["User-Agent"] = user_agent
        video_headers.pop("Origin", None)

        streams: list[VideoStream] = []
        for source in sources:
            stream_url = source.get("url") or source.get("file")
            if not stream_url:
                continue
            quality = source.get("label") or "Unknown"
            streams.extend(
                extract_from_hls(
                    session,
                    stream_url,
                    base_headers=video_headers,
                    name_gen=lambda res, q=quality: f"{prefix}{res if res != 'Video' else q}p",
                ),
            )
        return streams
    except Exception:
        return []


def _decrypt(playback: dict) -> str:
    key_bytes = b"".join(_b64url(part) for part in playback["key_parts"])
    iv = _b64url(playback["iv"])
    payload = _b64url(playback["payload"])
    aesgcm = AESGCM(key_bytes)
    # AES/GCM/NoPadding con tag de 128 bits anexado al final del payload.
    return aesgcm.decrypt(iv, payload, None).decode("utf-8")


def _b64url(value: str) -> bytes:
    normalized = value.replace("-", "+").replace("_", "/")
    padding = (-len(normalized)) % 4
    return base64.b64decode(normalized + "=" * padding)


def _encode_url_path(url: str) -> str:
    parsed = urlparse(url)
    encoded_path = "/".join(
        quote(segment, safe="") if segment else "" for segment in parsed.path.split("/")
    )
    rebuilt = f"{parsed.scheme}://{parsed.netloc}{encoded_path}"
    if parsed.query:
        rebuilt += f"?{parsed.query}"
    if parsed.fragment:
        rebuilt += f"#{parsed.fragment}"
    return rebuilt
