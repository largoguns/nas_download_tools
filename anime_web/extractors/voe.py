"""Port de `lib/voe-extractor` (VoeExtractor.kt).

Voe entrega los datos del reproductor dentro de un ``<script type=application/json>``
cifrados con una cadena de transformaciones (`decryptF7`): rot13, sustitucion de
patrones, base64, desplazamiento de caracteres, inversion y un segundo base64.
El JSON resultante contiene la URL del manifiesto HLS (`source`) y, opcionalmente,
una URL MP4 directa (`direct_access_url`).
"""

from __future__ import annotations

import base64
import json
import re

import requests
from bs4 import BeautifulSoup

from anime_sources.base import VideoStream

from ._common import DEFAULT_USER_AGENT, after, before_last, extract_from_hls

_REDIRECT_RE = re.compile(r"window\.location\.href\s*=\s*'([^']+)';")
_PATTERNS_RE = re.compile("|".join(re.escape(p) for p in ("@$", "^^", "~@", "%?", "*~", "!!", "#&")))


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

    # Algunas paginas hacen una redireccion via JS antes de exponer el JSON.
    first_script = document.find("script")
    if first_script and first_script.string:
        redirect = _REDIRECT_RE.search(first_script.string)
        if redirect:
            response = session.get(redirect.group(1), headers=request_headers)
            response.raise_for_status()
            document = BeautifulSoup(response.text, "html.parser")

    json_script = document.select_one("script[type=application/json]")
    if not json_script or not json_script.string:
        return []

    raw = json_script.string.strip()
    encoded = before_last(after(raw, '["'), '"]')
    if not encoded:
        return []

    decrypted = _decrypt_f7(encoded)
    if decrypted is None:
        return []

    streams: list[VideoStream] = []
    m3u8 = decrypted.get("source")
    mp4 = decrypted.get("direct_access_url")

    if m3u8:
        streams.extend(
            extract_from_hls(
                session,
                m3u8,
                base_headers=request_headers,
                name_gen=lambda quality: f"{_prefix(prefix)}Voe:{quality}",
            ),
        )
    if mp4:
        name = f"{_prefix(prefix)}Voe:MP4"
        streams.append(VideoStream(url=mp4, label=name, quality=name, extension="mp4"))

    return streams


def _prefix(prefix: str) -> str:
    prefix = (prefix or "").strip()
    return f"{prefix} " if prefix else ""


def _decrypt_f7(payload: str) -> dict | None:
    try:
        step = _rot13(payload)
        step = _PATTERNS_RE.sub("_", step)
        step = step.replace("_", "")
        step = _base64_latin1(step)
        step = "".join(chr((ord(ch) - 3) % 256) for ch in step)
        step = step[::-1]
        decoded = base64.b64decode(step.encode("latin-1")).decode("utf-8")
        return json.loads(decoded)
    except Exception:
        return None


def _rot13(text: str) -> str:
    result = []
    for char in text:
        if "A" <= char <= "Z":
            result.append(chr((ord(char) - ord("A") + 13) % 26 + ord("A")))
        elif "a" <= char <= "z":
            result.append(chr((ord(char) - ord("a") + 13) % 26 + ord("a")))
        else:
            result.append(char)
    return "".join(result)


def _base64_latin1(text: str) -> str:
    return base64.b64decode(text.encode("latin-1")).decode("latin-1")
