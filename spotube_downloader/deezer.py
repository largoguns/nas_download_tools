"""Busqueda de catalogo via la API publica de Deezer (sin auth, sin Premium).

Se usa solo para MOSTRAR resultados (titulo/artista/caratula). La descarga la
sigue haciendo spotdl a partir de una query "artista - titulo" (spotdl busca la
mejor coincidencia). Para albumes, se expanden sus pistas.

No requiere credenciales ni configuracion. Solo stdlib (urllib).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

_BASE = "https://api.deezer.com"
_HEADERS = {"User-Agent": "spotube-downloader"}


class CatalogError(RuntimeError):
    pass


def _get(url: str) -> dict:
    request = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise CatalogError(f"Deezer respondio HTTP {exc.code}.") from exc
    except Exception as exc:  # noqa: BLE001
        raise CatalogError(f"No se pudo contactar con Deezer: {exc}") from exc
    if isinstance(data, dict) and data.get("error"):
        raise CatalogError("Deezer devolvio un error en la busqueda.")
    return data


def _query(artist: str, title: str) -> str:
    return f"{artist} - {title}".strip(" -").strip()


def search(query: str, track_limit: int = 15, album_limit: int = 6) -> list[dict]:
    tracks = _get(f"{_BASE}/search?{urllib.parse.urlencode({'q': query, 'limit': track_limit})}")
    albums = _get(f"{_BASE}/search/album?{urllib.parse.urlencode({'q': query, 'limit': album_limit})}")

    results: list[dict] = []
    for track in tracks.get("data", []) or []:
        artist = (track.get("artist") or {}).get("name", "")
        title = track.get("title", "")
        results.append(
            {
                "kind": "track",
                "title": title,
                "artist": artist,
                "image": (track.get("album") or {}).get("cover_medium", ""),
                "query": _query(artist, title),
            },
        )
    for album in albums.get("data", []) or []:
        artist = (album.get("artist") or {}).get("name", "")
        results.append(
            {
                "kind": "album",
                "title": album.get("title", ""),
                "artist": artist,
                "image": album.get("cover_medium", ""),
                "album_id": album.get("id"),
                "tracks": album.get("nb_tracks"),
            },
        )
    return results


def album_tracks(album_id: int) -> list[dict]:
    data = _get(f"{_BASE}/album/{int(album_id)}/tracks?limit=200")
    out: list[dict] = []
    for track in data.get("data", []) or []:
        artist = (track.get("artist") or {}).get("name", "")
        title = track.get("title", "")
        out.append({"title": title, "artist": artist, "query": _query(artist, title)})
    return out
