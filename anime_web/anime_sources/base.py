from __future__ import annotations

import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# Muchas fuentes (jkanime, animeflv) cierran conexiones de forma transitoria
# (RemoteDisconnected / 429). Reintentamos las peticiones GET ante errores de
# conexion para que una caida puntual no aborte la operacion.
_HTML_RETRIES = max(0, int(os.environ.get("ANIME_HTML_RETRIES", "2")))
_HTML_RETRY_BACKOFF = max(0.0, float(os.environ.get("ANIME_HTML_RETRY_BACKOFF", "1.0")))
_RETRYABLE_FETCH_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


class SourceError(RuntimeError):
    """Raised when a source cannot complete a catalog or stream operation."""


class DownloadsDisabled(SourceError):
    """Raised when a source does not explicitly allow downloads."""


class StreamNotFound(SourceError):
    """Raised when no downloadable stream is available for an episode."""


def _clean_tuple(values: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    return tuple(value.strip() for value in values or () if value and value.strip())


@dataclass(frozen=True)
class AnimeItem:
    title: str
    url: str
    source_id: str = ""
    thumbnail_url: str = ""
    description: str = ""
    genres: tuple[str, ...] = field(default_factory=tuple)
    status: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["genres"] = list(self.genres)
        return data


@dataclass(frozen=True)
class EpisodeItem:
    title: str
    url: str
    source_id: str = ""
    number: float | None = None
    uploaded_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VideoStream:
    url: str
    label: str = "Directo"
    quality: str = ""
    mime_type: str = ""
    extension: str = "mp4"
    headers: dict[str, str] = field(default_factory=dict)
    size_bytes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AnimePage:
    items: list[AnimeItem]
    page: int = 1
    has_next: bool = False
    total_pages: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "page": self.page,
            "has_next": self.has_next,
            "total_pages": self.total_pages,
        }


@dataclass(frozen=True)
class EpisodePage:
    items: list[EpisodeItem]
    page: int = 1
    has_next: bool = False
    total: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "episodes": [item.to_dict() for item in self.items],
            "page": self.page,
            "has_next": self.has_next,
            "total": self.total,
        }


# --------------------------------------------------------------------------- #
# Clasificacion de medios (para organizar las descargas al estilo Jellyfin).
#
# Los scrapers modelan cada temporada/OVA/pelicula como un "anime" independiente
# con la pista en el titulo ("... 2nd Season", "... OVA", "... Movie"). Aqui
# derivamos, solo a partir del titulo, si es una temporada (y cual), un especial
# (OVA/ONA -> Specials, S00) o una pelicula, y el "nombre base" de la serie con
# los marcadores quitados para que Jellyfin agrupe las temporadas y OVAs de un
# mismo anime en la misma carpeta.
# --------------------------------------------------------------------------- #

MEDIA_SERIES = "series"
MEDIA_SPECIAL = "special"
MEDIA_MOVIE = "movie"


@dataclass(frozen=True)
class MediaClassification:
    kind: str = MEDIA_SERIES  # series | special | movie
    season: int = 1           # 1.. para series; 0 para especiales; ignorado en peliculas
    series_name: str = ""     # carpeta/base name (marcadores quitados salvo en peliculas)


_MOVIE_MARKER = re.compile(r"\b(?:the\s+movie|movie|gekijou?ban|pel[ií]cula)\b", re.IGNORECASE)
_SPECIAL_MARKER = re.compile(
    r"\b(?:ova|ovas|oad|oav|special|specials|especial|picture\s+drama)\b",
    re.IGNORECASE,
)
_ORDINAL_SEASON = re.compile(r"\b(\d+)\s*(?:st|nd|rd|th)\s+season\b", re.IGNORECASE)
_SEASON_N = re.compile(r"\bseason\s+(\d+)\b", re.IGNORECASE)
_TEMPORADA_N = re.compile(r"\btemporada\s+(\d+)\b", re.IGNORECASE)
_TRAILING_ROMAN = re.compile(r"\s+(ii|iii|iv|v|vi|vii|viii)\s*$", re.IGNORECASE)
_TRAILING_NUMBER = re.compile(r"\s+(\d{1,2})\s*$")
_ROMAN_VALUES = {"ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8}


def _detect_media_kind(title: str) -> str:
    if _MOVIE_MARKER.search(title):
        return MEDIA_MOVIE
    if _SPECIAL_MARKER.search(title):
        return MEDIA_SPECIAL
    return MEDIA_SERIES


def _detect_season(title: str) -> int:
    for pattern in (_ORDINAL_SEASON, _SEASON_N, _TEMPORADA_N):
        match = pattern.search(title)
        if match:
            return max(1, int(match.group(1)))
    match = _TRAILING_ROMAN.search(title)
    if match:
        return _ROMAN_VALUES[match.group(1).lower()]
    match = _TRAILING_NUMBER.search(title)
    if match:
        number = int(match.group(1))
        if number >= 2:  # un "1" final no suele indicar temporada
            return number
    return 1


def _strip_series_markers(title: str) -> str:
    text = title
    for pattern in (
        _MOVIE_MARKER,
        _SPECIAL_MARKER,
        _ORDINAL_SEASON,
        _SEASON_N,
        _TEMPORADA_N,
        _TRAILING_ROMAN,
        _TRAILING_NUMBER,
    ):
        text = pattern.sub(" ", text)
    # Limpia separadores colgantes (":", "-", guiones) y espacios sobrantes.
    text = re.sub(r"\s{2,}", " ", text).strip()
    text = re.sub(r"[\s:\-–—]+$", "", text).strip()
    return text or title.strip()


def classify_title(title: str, kind_override: str | None = None) -> MediaClassification:
    """Clasifica un titulo de anime en serie/especial/pelicula.

    ``kind_override`` permite a un scraper imponer el tipo cuando conoce un dato
    mas fiable (p. ej. el campo "Tipo" de la ficha) y dejar que aqui solo se
    calcule la temporada y el nombre base.
    """
    title = (title or "").strip()
    kind = kind_override or _detect_media_kind(title)
    if kind == MEDIA_MOVIE:
        # Las peliculas son independientes en Jellyfin: se conserva el titulo
        # completo como nombre de carpeta y de fichero.
        return MediaClassification(kind=kind, season=0, series_name=title or "movie")
    season = 0 if kind == MEDIA_SPECIAL else _detect_season(title)
    return MediaClassification(kind=kind, season=season, series_name=_strip_series_markers(title))


class BaseAnimeSource(ABC):
    """Common contract for every source plugin included in the app.

    Downloads are opt-in. A source must set ``allow_downloads = True`` and return
    direct media streams from ``get_video_streams`` before the queue will write a
    file to disk.
    """

    id: str = ""
    name: str = ""
    lang: str = "es"
    base_url: str = ""
    supports_latest: bool = False
    supports_search: bool = True
    supports_genres: bool = False
    allow_downloads: bool = False
    # Lista de generos disponibles como pares (valor, etiqueta) para la UI.
    genre_options: tuple[tuple[str, str], ...] = ()
    default_headers: dict[str, str] = {
        "User-Agent": "anime-edu-lab/1.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    def __init__(self, timeout: int = 20) -> None:
        if not self.id:
            raise ValueError(f"{self.__class__.__name__} must define id")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(self.default_headers)

    @property
    def enabled(self) -> bool:
        return True

    @property
    def capabilities(self) -> dict[str, bool]:
        return {
            "latest": self.supports_latest,
            "search": self.supports_search,
            "genres": self.supports_genres,
            "downloads": self.allow_downloads,
        }

    def metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name or self.id,
            "lang": self.lang,
            "base_url": self.base_url,
            "capabilities": self.capabilities,
            "genres": [{"value": value, "label": label} for value, label in self.genre_options],
        }

    def absolute_url(self, url: str) -> str:
        return urljoin(self.base_url.rstrip("/") + "/", url)

    def request(self, url: str, **kwargs: Any) -> requests.Response:
        target = self.absolute_url(url)
        timeout = kwargs.pop("timeout", self.timeout)
        last_exc: Exception | None = None
        for attempt in range(_HTML_RETRIES + 1):
            try:
                response = self.session.get(target, timeout=timeout, **kwargs)
                response.raise_for_status()
                return response
            except _RETRYABLE_FETCH_ERRORS as exc:
                # Errores HTTP (404, etc.) los lanza raise_for_status como
                # HTTPError y NO se reintentan; solo reintentamos cortes de red.
                last_exc = exc
                if attempt >= _HTML_RETRIES:
                    break
                if _HTML_RETRY_BACKOFF:
                    time.sleep(_HTML_RETRY_BACKOFF * (attempt + 1))
        raise last_exc  # type: ignore[misc]

    def html(self, url: str, **kwargs: Any) -> BeautifulSoup:
        response = self.request(url, **kwargs)
        document = BeautifulSoup(response.text, "html.parser")
        document._source_url = response.url
        return document

    def owns_url(self, url: str) -> bool:
        if not self.base_url:
            return False
        base = urlparse(self.base_url)
        target = urlparse(self.absolute_url(url))
        return target.netloc == base.netloc

    def normalize_anime(self, item: AnimeItem) -> AnimeItem:
        return AnimeItem(
            source_id=self.id,
            title=item.title.strip(),
            url=self.absolute_url(item.url),
            thumbnail_url=self.absolute_url(item.thumbnail_url) if item.thumbnail_url else "",
            description=item.description.strip(),
            genres=_clean_tuple(item.genres),
            status=item.status.strip(),
            extra=item.extra,
        )

    def normalize_episode(self, item: EpisodeItem) -> EpisodeItem:
        return EpisodeItem(
            source_id=self.id,
            title=item.title.strip(),
            url=self.absolute_url(item.url),
            number=item.number,
            uploaded_at=item.uploaded_at,
            extra=item.extra,
        )

    def popular(self, page: int = 1) -> AnimePage:
        raise SourceError(f"{self.name} does not implement popular listings")

    def latest(self, page: int = 1) -> AnimePage:
        if not self.supports_latest:
            raise SourceError(f"{self.name} does not implement latest updates")
        return self.popular(page)

    def search(self, query: str, page: int = 1) -> AnimePage:
        raise SourceError(f"{self.name} does not implement search")

    def directory(self, page: int = 1) -> AnimePage:
        """Listado del directorio (por defecto, el mismo que popular)."""
        return self.popular(page)

    def by_genre(self, genre: str, page: int = 1) -> AnimePage:
        raise SourceError(f"{self.name} does not implement genre listings")

    def classify(self, anime_title: str, anime_url: str) -> MediaClassification:
        """Determina temporada/especial/pelicula y el nombre base de la serie.

        Por defecto se deduce solo del titulo. Las fuentes con un dato de tipo
        mas fiable (p. ej. el campo "Tipo" de la ficha) deberian sobreescribirlo.
        """
        return classify_title(anime_title)

    @abstractmethod
    def details(self, anime_url: str) -> AnimeItem:
        raise NotImplementedError

    @abstractmethod
    def episodes(self, anime_url: str) -> list[EpisodeItem]:
        raise NotImplementedError

    def episode_page(self, anime_url: str, page: int = 1) -> EpisodePage:
        page = max(1, int(page or 1))
        episodes = self.episodes(anime_url)
        page_size = 24
        start = (page - 1) * page_size
        end = start + page_size
        return EpisodePage(
            items=episodes[start:end],
            page=page,
            has_next=end < len(episodes),
            total=len(episodes),
        )

    def all_episodes(self, anime_url: str) -> list[EpisodeItem]:
        return self.episodes(anime_url)

    def get_video_streams(self, episode_url: str) -> list[VideoStream]:
        raise DownloadsDisabled(f"{self.name} does not expose downloadable streams")

    def get_video_streams_fallback(self, episode_url: str) -> list[VideoStream]:
        """Streams alternativos a probar solo si los primarios fallan al descargar.

        Permite separar un metodo de descarga rapido/preferente (devuelto por
        ``get_video_streams``) de uno mas costoso (ej. resolver extractores), que
        solo se resuelve si el primero no consigue el fichero. Por defecto vacio.
        """
        return []

    def select_stream(
        self,
        streams: list[VideoStream],
        preferred_quality: str | None = None,
    ) -> VideoStream:
        if not streams:
            raise StreamNotFound("No streams returned by source")
        if preferred_quality:
            for stream in streams:
                haystack = f"{stream.label} {stream.quality}".lower()
                if preferred_quality.lower() in haystack:
                    return stream
        return sorted(streams, key=_quality_score, reverse=True)[0]

    def build_filename(
        self,
        anime_title: str,
        episode_title: str,
        stream: VideoStream,
        download_root: Path,
    ) -> Path:
        ext = sanitize_path_segment(stream.extension or "mp4").lstrip(".") or "mp4"
        anime_dir = sanitize_path_segment(anime_title or "anime")
        episode = sanitize_path_segment(episode_title or "episode")
        return download_root / anime_dir / f"{episode}.{ext}"


def _quality_score(stream: VideoStream) -> int:
    text = f"{stream.label} {stream.quality}"
    match = re.search(r"(\d{3,4})\s*p", text, flags=re.IGNORECASE)
    return int(match.group(1)) if match else 0


def sanitize_path_segment(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:120] or "untitled"
