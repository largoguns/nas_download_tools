"""Escaneo de biblioteca de Navidrome al completar descargas.

Cuando termina una descarga, se lanza un escaneo de Navidrome via su API Subsonic
(`startScan`). Varias descargas seguidas se agrupan (debounce) en un unico escaneo.

Configuracion por entorno (si falta URL/usuario/contrasena, no hace nada):
  NAVIDROME_URL                       p.ej. http://navidrome:4533
  NAVIDROME_USER                      usuario de Navidrome
  NAVIDROME_PASSWORD                  contrasena de ese usuario
  NAVIDROME_SCAN_DEBOUNCE_SECONDS     espera tras la ultima descarga (def. 30)

Autenticacion Subsonic por token: se envia salt + md5(password+salt), nunca la
contrasena en claro. Usa solo la stdlib (urllib/hashlib): no anade dependencias.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request

NAVIDROME_URL = os.environ.get("NAVIDROME_URL", "").strip().rstrip("/")
NAVIDROME_USER = os.environ.get("NAVIDROME_USER", "").strip()
NAVIDROME_PASSWORD = os.environ.get("NAVIDROME_PASSWORD", "").strip()
DEBOUNCE_SECONDS = max(0.0, float(os.environ.get("NAVIDROME_SCAN_DEBOUNCE_SECONDS", "30")))

_CLIENT = "spotube-downloader"
_API_VERSION = "1.16.1"

_lock = threading.Lock()
_timer: threading.Timer | None = None


def enabled() -> bool:
    return bool(NAVIDROME_URL and NAVIDROME_USER and NAVIDROME_PASSWORD)


def notify() -> None:
    """Programa (con debounce) un escaneo de biblioteca de Navidrome."""
    if not enabled():
        return
    global _timer
    with _lock:
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(DEBOUNCE_SECONDS, _scan)
        _timer.daemon = True
        _timer.start()


def _scan() -> None:
    salt = os.urandom(8).hex()
    token = hashlib.md5((NAVIDROME_PASSWORD + salt).encode("utf-8")).hexdigest()
    query = urllib.parse.urlencode(
        {
            "u": NAVIDROME_USER,
            "t": token,
            "s": salt,
            "v": _API_VERSION,
            "c": _CLIENT,
            "f": "json",
        },
    )
    url = f"{NAVIDROME_URL}/rest/startScan.view?{query}"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            logging.info("Navidrome: escaneo lanzado (HTTP %s).", response.status)
    except urllib.error.HTTPError as exc:
        logging.warning("Navidrome: fallo al lanzar escaneo (HTTP %s: %s).", exc.code, exc.reason)
    except Exception as exc:  # noqa: BLE001 - el escaneo nunca debe tumbar el worker
        logging.warning("Navidrome: no se pudo contactar (%s).", exc)
