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

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "page": self.page,
            "has_next": self.has_next,
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

    def by_genre(self, genre: str, page: int = 1) -> AnimePage:
        raise SourceError(f"{self.name} does not implement genre listings")

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
