"""Utilidades compartidas por los extractores portados desde las librerias
Kotlin de `extensions-source` (`lib/` y `lib-multisrc/`).

Incluye:

- Helpers de manipulacion de cadenas equivalentes a las extensiones de Kotlin
  (`substringBefore`, `substringAfter`, ...), respetando su semantica de
  "devolver la cadena completa cuando falta el delimitador".
- `Unpacker`: port del desempaquetador de codigo JS comprimido con el
  *packer* de Dean Edwards (usado por mixdrop, mp4upload y streamwish).
- `PlaylistUtils.extract_from_hls`: port de la extraccion de variantes desde
  un master playlist HLS (.m3u8), usado por voe, okru, filemoon y streamwish.
"""

from __future__ import annotations

import re
from typing import Callable
from urllib.parse import urlparse

import requests

from anime_sources.base import VideoStream

# User-Agent por defecto, equivalente al que usan las extensiones para imitar
# un navegador real y evitar respuestas vacias de los hosts.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

HLS_MIME = "application/vnd.apple.mpegurl"


# ---------------------------------------------------------------------------
# Helpers de cadenas (semantica de Kotlin)
# ---------------------------------------------------------------------------
# En Kotlin `substringAfter`/`substringBefore` devuelven la cadena original
# cuando el delimitador no aparece (a menos que se indique otro valor). Estos
# helpers replican ese comportamiento para que los ports sean fieles.

def after(text: str, delimiter: str, missing: str | None = None) -> str:
    index = text.find(delimiter)
    if index == -1:
        return text if missing is None else missing
    return text[index + len(delimiter):]


def before(text: str, delimiter: str, missing: str | None = None) -> str:
    index = text.find(delimiter)
    if index == -1:
        return text if missing is None else missing
    return text[:index]


def after_last(text: str, delimiter: str, missing: str | None = None) -> str:
    index = text.rfind(delimiter)
    if index == -1:
        return text if missing is None else missing
    return text[index + len(delimiter):]


def before_last(text: str, delimiter: str, missing: str | None = None) -> str:
    index = text.rfind(delimiter)
    if index == -1:
        return text if missing is None else missing
    return text[:index]


def name_with_prefix(prefix: str, body: str) -> str:
    """Concatena un prefijo de idioma (ej. ``[JAP]``) con el nombre del video.

    Equivale al patron ``"$prefix $body"`` de las extensiones, devolviendo solo
    ``body`` cuando el prefijo esta vacio.
    """
    prefix = (prefix or "").strip()
    return f"{prefix} {body}" if prefix else body


# ---------------------------------------------------------------------------
# Unpacker (port de lib/unpacker)
# ---------------------------------------------------------------------------
class _SubstringExtractor:
    """Port de `SubstringExtractor`: lee subcadenas avanzando un cursor."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._start = 0

    def skip_over(self, value: str) -> None:
        index = self._text.find(value, self._start)
        if index == -1:
            return
        self._start = index + len(value)

    def substring_before(self, value: str) -> str:
        index = self._text.find(value, self._start)
        if index == -1:
            return ""
        result = self._text[self._start:index]
        self._start = index + len(value)
        return result

    def substring_between(self, left: str, right: str) -> str:
        index = self._text.find(left, self._start)
        if index == -1:
            return ""
        left_index = index + len(left)
        right_index = self._text.find(right, left_index)
        if right_index == -1:
            return ""
        self._start = right_index + len(right)
        return self._text[left_index:right_index]


_WORD_RE = re.compile(r"[0-9A-Za-z]+")


def _parse_radix62(token: str) -> int:
    result = 0
    for char in token:
        code = ord(char)
        if code <= ord("9"):
            digit = code - ord("0")
        elif code >= ord("a"):
            digit = code - (ord("a") - 10)
        else:
            digit = code - (ord("A") - 36)
        result = result * 62 + digit
    return result


def unpack(script: str, left: str | None = None, right: str | None = None) -> str:
    """Desempaqueta codigo JS comprimido con el *packer* de Dean Edwards.

    Port fiel de `Unpacker.unpack`. Devuelve cadena vacia si no encuentra un
    bloque empaquetado.
    """
    parser = _SubstringExtractor(script)
    packed = parser.substring_between("}('", ".split('|'),0,{}))").replace("\\'", '"')
    if not packed:
        return ""

    inner = _SubstringExtractor(packed)
    if left is not None and right is not None:
        data = inner.substring_between(left, right)
        inner.skip_over("',")
    else:
        data = inner.substring_before("',")
    if not data:
        return ""

    dictionary = inner.substring_between("'", "'").split("|")
    size = len(dictionary)

    def replace(match: re.Match[str]) -> str:
        key = match.group(0)
        index = _parse_radix62(key)
        if index >= size:
            return key
        return dictionary[index] or key

    return _WORD_RE.sub(replace, data)


def unpack_and_combine(script: str) -> str:
    """Equivalente aproximado a `JsUnpacker.unpackAndCombine`.

    Desempaqueta todos los bloques `eval(function(p,a,c,k,e,d){...})` presentes
    y concatena el resultado. En las paginas objetivo suele haber uno solo.
    """
    combined: list[str] = []
    for block in re.findall(r"eval\(function\(p,a,c,k,e,d\).*?\}\([^\n]*?\.split\('\|'\),0,\{\}\)\)", script, re.S):
        result = unpack(block)
        if result:
            combined.append(result)
    if combined:
        return "".join(combined)
    # Fallback: intentar desempaquetar el script completo.
    return unpack(script)


# ---------------------------------------------------------------------------
# PlaylistUtils (port de lib/playlist-utils, solo HLS)
# ---------------------------------------------------------------------------
_PLAYLIST_SEPARATOR = "#EXT-X-STREAM-INF:"


def generate_master_headers(base_headers: dict | None, referer: str) -> dict:
    headers = dict(base_headers or {})
    headers["Accept"] = "*/*"
    if referer:
        host = urlparse(referer).netloc
        if host:
            headers["Origin"] = f"https://{host}"
            headers["Referer"] = referer
    return headers


def _absolute_url(url: str, playlist_url: str, master_base: str) -> str | None:
    if not url:
        return None
    if url.startswith("http"):
        return url
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("/"):
        parsed = urlparse(playlist_url)
        return f"{parsed.scheme}://{parsed.netloc}{url}"
    return master_base + url


def extract_from_hls(
    session: requests.Session,
    playlist_url: str,
    referer: str = "",
    base_headers: dict | None = None,
    name_gen: Callable[[str], str] | None = None,
    timeout: int = 20,
) -> list[VideoStream]:
    """Port de `PlaylistUtils.extractFromHls`.

    Descarga el master playlist y devuelve un `VideoStream` por cada variante de
    calidad. Si no es un master (no contiene `#EXT-X-STREAM-INF:`) devuelve un
    unico stream apuntando al propio playlist.
    """
    generator = name_gen or (lambda quality: quality)
    master_headers = generate_master_headers(base_headers, referer)

    response = session.get(playlist_url, headers=master_headers, timeout=timeout)
    response.raise_for_status()
    master = response.text

    if _PLAYLIST_SEPARATOR not in master:
        name = generator("Video")
        return [
            VideoStream(
                url=playlist_url,
                label=name,
                quality=name,
                mime_type=HLS_MIME,
                headers=master_headers,
            ),
        ]

    parsed = urlparse(playlist_url)
    base_path = parsed.path.rsplit("/", 1)[0] + "/"
    master_base = f"{parsed.scheme}://{parsed.netloc}{base_path}"

    streams: list[VideoStream] = []
    for chunk in master.split(_PLAYLIST_SEPARATOR)[1:]:
        codec = before(after(chunk, 'CODECS="', ""), '"', "")
        if codec.startswith("mp4a"):
            # Pista de solo audio, se descarta como en el original.
            continue

        resolution = before(after(after(chunk, "RESOLUTION="), "x"), ",")
        resolution = resolution.split("\n", 1)[0].strip()

        video_line = chunk.split("\n", 2)
        raw_url = video_line[1].strip() if len(video_line) > 1 else ""
        absolute = _absolute_url(raw_url, playlist_url, master_base)
        if not absolute:
            continue

        name = generator(resolution or "Video")
        streams.append(
            VideoStream(
                url=absolute.rstrip(),
                label=name,
                quality=name,
                mime_type=HLS_MIME,
                headers=master_headers,
            ),
        )

    return streams
