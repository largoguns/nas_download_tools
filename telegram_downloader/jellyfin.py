"""Refresco de biblioteca de Jellyfin al completar descargas.

Cuando termina una descarga, se dispara un escaneo de biblioteca de Jellyfin via
su API. Varias descargas seguidas se agrupan (debounce) en un unico escaneo para
no martillear a Jellyfin al completar un lote.

Configuracion por entorno (si falta la URL o la API key, no hace nada):
  JELLYFIN_URL                       p.ej. http://jellyfin:8096
  JELLYFIN_API_KEY                   clave de API (Panel -> API Keys)
  JELLYFIN_LIBRARY_ID                (opcional) ItemId de la biblioteca a escanear.
                                     Vacio = escaneo global de todas las bibliotecas.
  JELLYFIN_REFRESH_DEBOUNCE_SECONDS  espera tras la ultima descarga (def. 30)

Para averiguar el ItemId de una biblioteca:
  curl -s "$JELLYFIN_URL/Library/VirtualFolders?api_key=$JELLYFIN_API_KEY" \
    | python3 -c "import sys,json;[print(v['ItemId'],'->',v['Name']) for v in json.load(sys.stdin)]"

Usa solo la stdlib (urllib): no anade dependencias.
"""
from __future__ import annotations

import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "").strip().rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "").strip()
JELLYFIN_LIBRARY_ID = os.environ.get("JELLYFIN_LIBRARY_ID", "").strip()
DEBOUNCE_SECONDS = max(0.0, float(os.environ.get("JELLYFIN_REFRESH_DEBOUNCE_SECONDS", "30")))

_lock = threading.Lock()
_timer: threading.Timer | None = None


def enabled() -> bool:
    return bool(JELLYFIN_URL and JELLYFIN_API_KEY)


def notify() -> None:
    """Programa (con debounce) un escaneo de biblioteca de Jellyfin."""
    if not enabled():
        return
    global _timer
    with _lock:
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(DEBOUNCE_SECONDS, _refresh)
        _timer.daemon = True
        _timer.start()


def _refresh() -> None:
    if JELLYFIN_LIBRARY_ID:
        # Escaneo de una biblioteca concreta (busca ficheros nuevos, sin rehacer
        # metadatos existentes).
        params = urllib.parse.urlencode(
            {
                "Recursive": "true",
                "metadataRefreshMode": "Default",
                "imageRefreshMode": "Default",
                "replaceAllMetadata": "false",
                "replaceAllImages": "false",
            },
        )
        url = f"{JELLYFIN_URL}/Items/{JELLYFIN_LIBRARY_ID}/Refresh?{params}"
    else:
        url = f"{JELLYFIN_URL}/Library/Refresh"

    request = urllib.request.Request(
        url,
        method="POST",
        headers={"X-Emby-Token": JELLYFIN_API_KEY},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            target = JELLYFIN_LIBRARY_ID or "todas las bibliotecas"
            logging.info("Jellyfin: escaneo lanzado sobre %s (HTTP %s).", target, response.status)
    except urllib.error.HTTPError as exc:
        logging.warning("Jellyfin: fallo al lanzar escaneo (HTTP %s: %s).", exc.code, exc.reason)
    except Exception as exc:  # noqa: BLE001 - el refresco nunca debe tumbar el worker
        logging.warning("Jellyfin: no se pudo contactar (%s).", exc)
