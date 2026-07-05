"""Busqueda de catalogo via la API publica de Deezer (sin auth, sin Premium).

Se usa solo para MOSTRAR resultados (titulo/artista/caratula). La descarga la
sigue haciendo spotdl a partir de una query "artista - titulo" (spotdl busca la
mejor coincidencia). Para albumes, se expanden sus pistas.

No requiere credenciales ni configuracion. Solo stdlib (urllib).
"""
from __future__ import annotations

import json
import re
import unicodedata
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


def _norm(value: str) -> str:
    stripped = "".join(
        c for c in unicodedata.normalize("NFKD", value or "") if not unicodedata.combining(c)
    ).lower()
    return re.sub(r"[^a-z0-9]+", " ", stripped).strip()


def _track_result(track: dict, artist_name: str | None = None) -> dict:
    artist = artist_name or (track.get("artist") or {}).get("name", "")
    title = track.get("title", "")
    return {
        "kind": "track",
        "title": title,
        "artist": artist,
        "image": (track.get("album") or {}).get("cover_medium", ""),
        "query": _query(artist, title),
    }


def _album_result(album: dict, artist_name: str) -> dict:
    return {
        "kind": "album",
        "title": album.get("title", ""),
        "artist": artist_name,
        "image": album.get("cover_medium", ""),
        "album_id": album.get("id"),
        "tracks": album.get("nb_tracks"),
    }


def _top_artist(query: str) -> dict | None:
    data = _get(f"{_BASE}/search/artist?{urllib.parse.urlencode({'q': query, 'limit': 1})}")
    items = data.get("data", []) or []
    return items[0] if items else None


def _is_artist_query(query: str, artist_name: str) -> bool:
    """True si la busqueda parece ser un artista (nombre ~ query)."""
    nq, nn = _norm(query), _norm(artist_name)
    if len(nq) < 3 or not nn:
        return False
    return nq == nn or nq in nn or nn in nq


def search(query: str, track_limit: int = 25, album_limit: int = 100) -> list[dict]:
    """Busca en Deezer.

    Si la consulta es realmente un artista, devuelve sus canciones mas populares
    (`/artist/{id}/top`) y TODA su discografia (`/artist/{id}/albums`). Si no, hace
    una busqueda general ordenando las canciones por popularidad (`rank`).

    Para distinguirlo: se considera modo-artista solo si el nombre del artista
    candidato coincide con el artista del primer resultado de canciones (asi
    "Daft Punk" -> discografia, pero "bohemian rhapsody" -> busqueda de cancion,
    aunque exista un "artista" de Deezer con ese nombre).
    """
    track_items = _get(
        f"{_BASE}/search?{urllib.parse.urlencode({'q': query, 'limit': track_limit})}"
    ).get("data", []) or []

    artist = None
    try:
        artist = _top_artist(query)
    except CatalogError:
        artist = None

    if artist and _is_artist_query(query, artist.get("name", "")):
        first_artist = (track_items[0].get("artist") or {}).get("name", "") if track_items else ""
        if _norm(first_artist) == _norm(artist.get("name", "")):
            return _artist_search(artist, track_limit, album_limit)

    # Busqueda general: canciones por popularidad + albumes que coincidan.
    results: list[dict] = []
    track_items.sort(key=lambda t: t.get("rank", 0), reverse=True)
    for track in track_items:
        results.append(_track_result(track))
    albums = _get(
        f"{_BASE}/search/album?{urllib.parse.urlencode({'q': query, 'limit': min(album_limit, 25)})}"
    )
    for album in albums.get("data", []) or []:
        results.append(_album_result(album, (album.get("artist") or {}).get("name", "")))
    return results


def _artist_search(artist: dict, track_limit: int, album_limit: int) -> list[dict]:
    artist_id = artist["id"]
    name = artist.get("name", "")
    results: list[dict] = []

    top = _get(f"{_BASE}/artist/{artist_id}/top?{urllib.parse.urlencode({'limit': track_limit})}")
    for track in top.get("data", []) or []:
        results.append(_track_result(track, name))

    albums = _get(f"{_BASE}/artist/{artist_id}/albums?{urllib.parse.urlencode({'limit': album_limit})}")
    items = albums.get("data", []) or []
    # Mas nuevos primero; sin duplicar por titulo (Deezer repite ediciones).
    items.sort(key=lambda a: a.get("release_date", ""), reverse=True)
    seen: set[str] = set()
    for album in items:
        key = _norm(album.get("title", ""))
        if key in seen:
            continue
        seen.add(key)
        results.append(_album_result(album, name))
    return results


def album_tracks(album_id: int) -> list[dict]:
    data = _get(f"{_BASE}/album/{int(album_id)}/tracks?limit=200")
    out: list[dict] = []
    for track in data.get("data", []) or []:
        artist = (track.get("artist") or {}).get("name", "")
        title = track.get("title", "")
        out.append({"title": title, "artist": artist, "query": _query(artist, title)})
    return out
