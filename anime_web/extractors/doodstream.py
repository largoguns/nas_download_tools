from __future__ import annotations

import random
import re
import time
from urllib.parse import urljoin

import requests

from anime_sources.base import VideoStream


def get_streams(url: str, session: requests.Session) -> list[VideoStream]:
    """Extrae streams de video de una URL de Doodstream.

    La logica se basa en obtener un token de la pagina y usarlo para
    conseguir el enlace de video final.
    """
    # Doodstream usa varios dominios, normalizamos a uno comun
    url = re.sub(r"doodstream\.com|dood\.(?:la|watch|so|to|ws)", "dood.so", url)
    headers = {"Referer": "https://dood.so/"}
    response = session.get(url, headers=headers)
    response.raise_for_status()

    # El enlace al video final se obtiene a traves de una ruta /pass_md5/...
    pass_md5_match = re.search(r"/pass_md5/([^']*)", response.text)
    if not pass_md5_match:
        return []

    pass_url = urljoin(response.url, pass_md5_match.group(0))
    pass_response = session.get(pass_url, headers={"Referer": url})
    video_url_part = pass_response.text

    random_str = "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=10))
    final_url = f"{video_url_part}?token={pass_md5_match.group(1)}&expiry={int(time.time() * 1000)}"
    return [VideoStream(url=final_url, label="Doodstream", quality="auto", extension="mp4")]