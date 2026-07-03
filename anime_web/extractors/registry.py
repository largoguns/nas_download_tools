"""Registro de extractores y deteccion de host por convenciones de dominio.

Replica la logica de `Jkanime.kt`:

- ``CONVENTIONS`` mapea una clave de extractor a los fragmentos de dominio que la
  identifican (en el mismo orden que la extension Kotlin).
- ``resolve_streams`` decodifica/detecta el host, aplica el prefijo de idioma y
  delega en el extractor adecuado, cayendo en el extractor universal cuando
  ningun host coincide.
"""

from __future__ import annotations

import requests

from anime_sources.base import VideoStream

from . import (
    direct,
    doodstream,
    filemoon,
    jkanime_internal,
    mediafire,
    mega,
    mixdrop,
    mp4upload,
    okru,
    streamhidevid,
    streamtape,
    streamwish,
    universal,
    uqload,
    voe,
    yourupload,
)

# Deteccion de host -> clave de extractor (orden y alias identicos a Jkanime.kt).
CONVENTIONS: list[tuple[str, list[str]]] = [
    ("voe", ["voe", "tubelessceliolymph", "simpulumlamerop", "urochsunloath",
             "nathanfromsubject", "yip.", "metagnathtuggers", "donaldlineelse"]),
    ("okru", ["ok.ru", "okru"]),
    ("filemoon", ["filemoon", "moonplayer", "moviesm4u", "files.im"]),
    ("streamtape", ["streamtape", "stp", "stape", "shavetape"]),
    ("mp4upload", ["mp4upload"]),
    ("mixdrop", ["mixdrop", "mxdrop"]),
    ("streamwish", ["wishembed", "streamwish", "strwish", "wish", "kswplayer",
                    "swhoi", "multimovies", "uqloads", "neko-stream", "swdyu",
                    "iplayerhls", "streamgg", "flaswish", "sfastwish", "lion",
                    "streamhls", "hlswish", "obeywish", "asnwish"]),
    ("uqload", ["uqload"]),
    ("vidhide", ["vidhide", "streamhide", "guccihide", "streamvid", "ahvsh",
                 "kinoger", "smoothpre", "dhtpre", "peytonepre", "earnvids", "ryderjet"]),
    ("yourupload", ["yourupload", "upload.com"]),
    ("mediafire", ["mediafire"]),
    ("mega", ["mega.nz", "mega.co.nz"]),
    ("doodstream", ["dood", "ds2play", "ds2video", "doodstream", "dsvplay",
                    "d000d", "d0000d", "vide0"]),
    ("desuka", ["stream/jkmedia"]),
    ("nozomi", ["um2.php", "nozomi"]),
    ("desu", ["um.php"]),
]

# Idiomas declarados por jkanime en el campo `lang` de cada servidor.
LANGUAGES = {1: "[JAP]", 3: "[LAT]", 4: "[CHIN]"}


def detect_key(url: str) -> str | None:
    """Devuelve la clave de extractor para una URL segun ``CONVENTIONS``."""
    lowered = url.lower()
    for key, names in CONVENTIONS:
        if any(name.lower() in lowered for name in names):
            return key
    return None


def resolve_streams(
    url: str,
    session: requests.Session,
    lang: str = "",
    headers: dict | None = None,
) -> list[VideoStream]:
    """Detecta el host y delega en el extractor adecuado.

    ``lang`` es el prefijo de idioma (ej. ``[JAP]``). Equivale al bloque
    ``when (matched)`` de ``videoListParse`` en la extension Kotlin.
    """
    key = detect_key(url)
    space_prefix = f"{lang} " if lang else ""

    if key == "voe":
        return voe.get_streams(url, session, prefix=space_prefix, headers=headers)
    if key == "okru":
        return okru.get_streams(url, session, prefix=lang, headers=headers)
    if key == "filemoon":
        return filemoon.get_streams(url, session, prefix=f"{space_prefix}Filemoon:", headers=headers)
    if key == "streamwish":
        return streamwish.get_streams(url, session, prefix=lang, headers=headers)
    if key == "uqload":
        return uqload.get_streams(url, session, prefix=lang, headers=headers)
    if key == "vidhide":
        return streamhidevid.get_streams(url, session, prefix=lang, headers=headers)
    if key == "streamtape":
        return streamtape.get_streams(url, session, prefix=lang, headers=headers)
    if key == "mp4upload":
        return mp4upload.get_streams(url, session, prefix=space_prefix, headers=headers)
    if key == "mixdrop":
        return mixdrop.get_streams(url, session, prefix=space_prefix, headers=headers)
    if key == "yourupload":
        return yourupload.get_streams(url, session, prefix=lang, headers=headers)
    if key == "mediafire":
        return mediafire.get_streams(url, session, prefix=lang, headers=headers)
    if key == "mega":
        return mega.get_streams(url, session, prefix=lang, headers=headers)
    if key == "doodstream":
        return doodstream.get_streams(url, session)
    if key == "desuka":
        return jkanime_internal.get_desuka(url, session, prefix=space_prefix)
    if key == "nozomi":
        return jkanime_internal.get_nozomi(url, session, prefix=space_prefix)
    if key == "desu":
        return jkanime_internal.get_desu(url, session, prefix=space_prefix)

    return universal.get_streams(url, session, prefix=lang, headers=headers)


# Mapa por host directo, util para fuentes genericas o pruebas individuales.
EXTRACTORS = {
    "voe": voe.get_streams,
    "okru": okru.get_streams,
    "filemoon": filemoon.get_streams,
    "streamtape": streamtape.get_streams,
    "mp4upload": mp4upload.get_streams,
    "mixdrop": mixdrop.get_streams,
    "streamwish": streamwish.get_streams,
    "uqload": uqload.get_streams,
    "vidhide": streamhidevid.get_streams,
    "yourupload": yourupload.get_streams,
    "mediafire": mediafire.get_streams,
    "dood": doodstream.get_streams,
    "universal": universal.get_streams,
    "direct": direct.get_streams,
}
