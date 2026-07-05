"""Integracion con Navidrome via su API Subsonic (sin dependencias externas).

- Escaneo de biblioteca al completar descargas (`startScan`), con debounce.
- Creacion/actualizacion de una playlist en Navidrome cuando se descarga una
  playlist de Spotify: espera al escaneo, busca cada cancion y crea la playlist.

Configuracion por entorno (si falta URL/usuario/contrasena, no hace nada):
  NAVIDROME_URL                       p.ej. http://navidrome:4533
  NAVIDROME_USER                      usuario de Navidrome
  NAVIDROME_PASSWORD                  contrasena de ese usuario
  NAVIDROME_SCAN_DEBOUNCE_SECONDS     espera tras la ultima descarga (def. 30)
  NAVIDROME_SCAN_WAIT_SECONDS         espera max. a que termine el escaneo (def. 180)

Autenticacion Subsonic por token: salt + md5(password+salt), nunca la contrasena
en claro. Solo stdlib (urllib/hashlib).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

NAVIDROME_URL = os.environ.get("NAVIDROME_URL", "").strip().rstrip("/")
NAVIDROME_USER = os.environ.get("NAVIDROME_USER", "").strip()
NAVIDROME_PASSWORD = os.environ.get("NAVIDROME_PASSWORD", "").strip()
DEBOUNCE_SECONDS = max(0.0, float(os.environ.get("NAVIDROME_SCAN_DEBOUNCE_SECONDS", "30")))
SCAN_WAIT_SECONDS = max(10.0, float(os.environ.get("NAVIDROME_SCAN_WAIT_SECONDS", "180")))

_CLIENT = "spotube-downloader"
_API_VERSION = "1.16.1"

_lock = threading.Lock()
_timer: threading.Timer | None = None


class SubsonicError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(NAVIDROME_URL and NAVIDROME_USER and NAVIDROME_PASSWORD)


def _auth_params() -> dict[str, str]:
    salt = os.urandom(8).hex()
    token = hashlib.md5((NAVIDROME_PASSWORD + salt).encode("utf-8")).hexdigest()
    return {"u": NAVIDROME_USER, "t": token, "s": salt, "v": _API_VERSION, "c": _CLIENT, "f": "json"}


def _call(endpoint: str, params=None, timeout: float = 20.0) -> dict:
    """Llama a un endpoint Subsonic y devuelve el objeto 'subsonic-response'.

    ``params`` puede ser dict o lista de tuplas (para claves repetidas como songId).
    """
    items = list(_auth_params().items())
    if params:
        items += list(params.items()) if isinstance(params, dict) else list(params)
    url = f"{NAVIDROME_URL}/rest/{endpoint}.view?{urllib.parse.urlencode(items)}"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        data = json.load(response)
    resp = data.get("subsonic-response", {})
    if resp.get("status") != "ok":
        message = (resp.get("error") or {}).get("message", "error desconocido")
        raise SubsonicError(message)
    return resp


# --------------------------------------------------------------- escaneo (debounce)
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
    try:
        _call("startScan")
        logging.info("Navidrome: escaneo lanzado.")
    except Exception as exc:  # noqa: BLE001 - el escaneo nunca debe tumbar el worker
        logging.warning("Navidrome: no se pudo lanzar el escaneo (%s).", exc)


# ------------------------------------------------------------------------ playlists
def _norm(value: str) -> str:
    stripped = "".join(
        c for c in unicodedata.normalize("NFKD", value or "") if not unicodedata.combining(c)
    ).lower()
    return re.sub(r"[^a-z0-9]+", " ", stripped).strip()


def wait_for_scan(timeout: float = SCAN_WAIT_SECONDS, poll: float = 3.0) -> bool:
    """Lanza un escaneo y espera a que termine. True si acabo dentro del timeout."""
    try:
        _call("startScan")
    except Exception as exc:  # noqa: BLE001
        logging.warning("Navidrome: no se pudo iniciar el escaneo (%s).", exc)
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(poll)
        try:
            status = _call("getScanStatus").get("scanStatus", {})
        except Exception:  # noqa: BLE001
            continue
        if not status.get("scanning", False):
            return True
    return False


def find_song_id(title: str, artist: str) -> str | None:
    """Busca una cancion en Navidrome por 'artista titulo' y devuelve su id."""
    def _search(query: str) -> list[dict]:
        resp = _call("search3", {"query": query, "songCount": 10, "artistCount": 0, "albumCount": 0})
        return resp.get("searchResult3", {}).get("song", []) or []

    songs = _search(f"{artist} {title}".strip()) or _search(title)
    if not songs:
        return None
    nt, na = _norm(title), _norm(artist)
    for song in songs:
        if _norm(song.get("title", "")) == nt and (not na or na in _norm(song.get("artist", ""))):
            return song.get("id")
    return songs[0].get("id")


def _playlist_id_by_name(name: str) -> str | None:
    resp = _call("getPlaylists")
    target = _norm(name)
    for playlist in resp.get("playlists", {}).get("playlist", []) or []:
        if _norm(playlist.get("name", "")) == target:
            return playlist.get("id")
    return None


def sync_playlist(name: str, tracks: list[dict]) -> None:
    """Crea (o actualiza) una playlist en Navidrome con las pistas dadas.

    ``tracks`` es una lista de {title, artist}. Espera a que termine el escaneo,
    busca cada cancion y crea/actualiza la playlist. Nunca lanza: registra y sigue.
    """
    if not enabled() or not tracks:
        return
    try:
        if not wait_for_scan():
            logging.warning(
                "Navidrome: el escaneo no termino a tiempo; la playlist '%s' puede salir incompleta.",
                name,
            )
        song_ids: list[str] = []
        missing = 0
        for track in tracks:
            song_id = find_song_id(track.get("title", ""), track.get("artist", ""))
            if song_id:
                song_ids.append(song_id)
            else:
                missing += 1

        if not song_ids:
            logging.warning("Navidrome: no se encontro ninguna cancion de la playlist '%s'.", name)
            return

        existing = _playlist_id_by_name(name)
        params: list[tuple[str, str]] = [("songId", sid) for sid in song_ids]
        if existing:
            params.append(("playlistId", existing))
        else:
            params.append(("name", name))
        _call("createPlaylist", params)
        logging.info(
            "Navidrome: playlist '%s' %s (%d canciones, %d no encontradas).",
            name, "actualizada" if existing else "creada", len(song_ids), missing,
        )
    except Exception as exc:  # noqa: BLE001
        logging.warning("Navidrome: no se pudo crear/actualizar la playlist '%s': %s", name, exc)
