"""Mega.nz: no soportado para descarga directa.

Mega cifra el contenido en el cliente (AES-CTR con la clave incluida en el
fragmento ``#...`` de la URL). El descargador de la app descarga por HTTP y
escribe los bytes tal cual, sin descifrar, por lo que el fichero resultante
seria inservible. Se omite limpiamente para que el pivotado pruebe otro servidor.
"""

from __future__ import annotations

import logging

import requests

from anime_sources.base import VideoStream


def get_streams(
    url: str,
    session: requests.Session,
    prefix: str = "",
    headers: dict | None = None,
) -> list[VideoStream]:
    logging.info(
        "Mega omitido (descarga cifrada en cliente, no soportada por descarga HTTP): %s",
        url,
    )
    return []
