from __future__ import annotations

import base64
import binascii
import json
import os
import re
import time
from datetime import datetime
from urllib.parse import quote

from bs4 import BeautifulSoup

from extractors.registry import LANGUAGES, resolve_streams
from .base import (
    AnimeItem,
    AnimePage,
    BaseAnimeSource,
    EpisodeItem,
    EpisodePage,
    SourceError,
    VideoStream,
)


class JkanimeSource(BaseAnimeSource):
    id = "jkanime"
    name = "Jkanime"
    lang = "es"
    base_url = "https://jkanime.net"
    supports_latest = True
    supports_search = True
    supports_genres = True
    allow_downloads = True
    genre_options = (
        ("accion", "Accion"), ("aventura", "Aventura"), ("autos", "Autos"),
        ("comedia", "Comedia"), ("dementia", "Dementia"), ("demonios", "Demonios"),
        ("misterio", "Misterio"), ("drama", "Drama"), ("ecchi", "Ecchi"),
        ("fantasia", "Fantasia"), ("juegos", "Juegos"), ("hentai", "Hentai"),
        ("historico", "Historico"), ("terror", "Terror"), ("nios", "Niños"),
        ("magia", "Magia"), ("artes-marciales", "Artes Marciales"), ("mecha", "Mecha"),
        ("musica", "Musica"), ("parodia", "Parodia"), ("samurai", "Samurai"),
        ("romance", "Romance"), ("colegial", "Colegial"), ("sci-fi", "Sci-Fi"),
        ("shoujo", "Shoujo"), ("shoujo-ai", "Shoujo Ai"), ("shounen", "Shounen"),
        ("shounen-ai", "Shounen Ai"), ("space", "Space"), ("deportes", "Deportes"),
        ("super-poderes", "Super Poderes"), ("vampiros", "Vampiros"), ("yaoi", "Yaoi"),
        ("yuri", "Yuri"), ("harem", "Harem"), ("cosas-de-la-vida", "Cosas de la vida"),
        ("sobrenatural", "Sobrenatural"), ("militar", "Militar"), ("policial", "Policial"),
        ("psicologico", "Psicologico"), ("thriller", "Thriller"), ("seinen", "Seinen"),
        ("josei", "Josei"), ("latino", "Español Latino"), ("isekai", "Isekai"),
    )
    default_headers = {
        "User-Agent": "anime-downloader/1.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es,en;q=0.8",
    }

    def __init__(self, timeout: int = 20) -> None:
        super().__init__(timeout=timeout)
        self.max_episode_pages = max(1, int_env("ANIME_JKANIME_MAX_EPISODE_PAGES", 100))
        self.episode_pause_seconds = max(
            0.0,
            float_env("ANIME_JKANIME_EPISODE_PAUSE_SECONDS", 0.25),
        )
        # Descargas directas via el CDN propio de jkanime (c1.jkplayers.com/d/{slug}/).
        # Es mas simple y fiable que resolver cada host con extractores; estos
        # quedan como fallback cuando no hay enlaces directos disponibles.
        self.direct_download_enabled = bool_env("ANIME_JKANIME_DIRECT_DOWNLOAD", True)
        self.extractor_fallback_enabled = bool_env("ANIME_JKANIME_EXTRACTOR_FALLBACK", True)
        # jkanime corta conexiones de forma transitoria (RemoteDisconnected/429).
        # Reintentamos la carga de la pagina del episodio antes de rendirnos.
        self.page_fetch_retries = max(0, int_env("ANIME_JKANIME_PAGE_RETRIES", 2))
        self.page_retry_backoff = max(0.0, float_env("ANIME_JKANIME_PAGE_RETRY_BACKOFF", 1.0))

    def popular(self, page: int = 1) -> AnimePage:
        document = self.html(f"/directorio?filtro=popularidad&p={max(1, page)}")
        return self._parse_directory_page(document, page)

    def latest(self, page: int = 1) -> AnimePage:
        document = self.html(f"/directorio?estado=emision&p={max(1, page)}")
        return self._parse_directory_page(document, page)

    def search(self, query: str, page: int = 1) -> AnimePage:
        query = query.strip()
        if not query:
            return self.popular(page)

        document = self.html(f"/buscar/{quote(query)}")
        if "directorio" in document_url(document):
            return self._parse_directory_page(document, page)
        return AnimePage(
            items=self._parse_search_items(document),
            page=page,
            has_next=False,
        )

    def by_genre(self, genre: str, page: int = 1) -> AnimePage:
        slug = (genre or "").strip().strip("/")
        if not slug:
            return self.popular(page)
        # El directorio filtra por genero con el mismo JSON `var animes` que
        # popular/recientes y pagina con &p=N.
        document = self.html(f"/directorio?genero={quote(slug)}&p={max(1, page)}")
        return self._parse_directory_page(document, page)

    def details(self, anime_url: str) -> AnimeItem:
        document = self.html(anime_url)
        # Enlaces a /genero/<slug>/: sacamos etiqueta + slug para tags clicables.
        genre_pairs: list[dict[str, str]] = []
        seen_slugs: set[str] = set()
        for link in document.select('a[href*="/genero/"]'):
            label = link.get_text(strip=True)
            match = re.search(r"/genero/([^/?#]+)", link.get("href", ""))
            if not label or not match:
                continue
            slug = match.group(1)
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            genre_pairs.append({"label": label, "value": slug})

        status = "Finalizado" if document.select(".finished") else "En emision"
        return self.normalize_anime(
            AnimeItem(
                title=text_or_empty(document.select_one(".anime_info h3")),
                url=anime_url,
                thumbnail_url=image_url(document.select_one(".anime_info img")),
                description=text_or_empty(document.select_one(".scroll")),
                genres=tuple(pair["label"] for pair in genre_pairs),
                status=status,
                extra={"genres": genre_pairs},
            ),
        )

    def episodes(self, anime_url: str) -> list[EpisodeItem]:
        return self.all_episodes(anime_url)

    def episode_page(self, anime_url: str, page: int = 1) -> EpisodePage:
        context = self._episode_context(anime_url)
        payload = self._fetch_episode_page(
            anime_id=context["anime_id"],
            token=context["token"],
            page=max(1, page),
            referer=anime_url,
        )
        episodes = self._episodes_from_payload(payload, context["episode_base_url"])
        return EpisodePage(
            items=episodes,
            page=max(1, page),
            has_next=bool(payload.get("next_page_url")),
            total=payload.get("total"),
        )

    def all_episodes(self, anime_url: str) -> list[EpisodeItem]:
        context = self._episode_context(anime_url)
        episodes: list[EpisodeItem] = []
        current_page = 1
        while current_page <= self.max_episode_pages:
            payload = self._fetch_episode_page(
                anime_id=context["anime_id"],
                token=context["token"],
                page=current_page,
                referer=anime_url,
            )
            episodes.extend(self._episodes_from_payload(payload, context["episode_base_url"]))

            if not payload.get("next_page_url"):
                break

            current_page += 1
            if self.episode_pause_seconds:
                time.sleep(self.episode_pause_seconds)

        return sorted(episodes, key=episode_sort_key)

    def get_video_streams(self, episode_url: str) -> list[VideoStream]:
        """Streams primarios para descargar un episodio de JKAnime.

        Por defecto usa el endpoint de descarga oficial de la web
        (``GET /ajax/download_episode/{id}``), que devuelve la URL real del
        fichero. Es el mismo mecanismo que el boton "Descargar" del sitio.

        Si la descarga directa esta desactivada se usan directamente los
        extractores. Los extractores tambien actuan como fallback (ver
        ``get_video_streams_fallback``) cuando la descarga directa falla.
        """
        if self.direct_download_enabled:
            direct = self._ajax_download_streams(episode_url)
            if direct:
                return direct
            if self.extractor_fallback_enabled:
                # El fallback a nivel de descarga (app) resolvera extractores.
                return []

        return self._extractor_streams(self._fetch_servers(episode_url))

    def get_video_streams_fallback(self, episode_url: str) -> list[VideoStream]:
        # Solo tiene sentido si la directa fue la via primaria; si la directa
        # estaba desactivada, los extractores ya fueron los primarios.
        if not (self.direct_download_enabled and self.extractor_fallback_enabled):
            return []
        return self._extractor_streams(self._fetch_servers(episode_url))

    def _episode_document(self, episode_url: str) -> BeautifulSoup:
        """Carga la pagina del episodio reintentando ante cortes transitorios."""
        last_exc: Exception | None = None
        for attempt in range(self.page_fetch_retries + 1):
            try:
                return self.html(episode_url)
            except Exception as exc:
                last_exc = exc
                if attempt >= self.page_fetch_retries:
                    break
                if self.page_retry_backoff:
                    time.sleep(self.page_retry_backoff * (attempt + 1))
        raise SourceError(f"No se pudo obtener la pagina del episodio: {last_exc}") from last_exc

    def _ajax_download_streams(self, episode_url: str) -> list[VideoStream]:
        """Resuelve la URL real del fichero via ``/ajax/download_episode/{id}``."""
        try:
            document = self._episode_document(episode_url)
        except SourceError:
            # No abortamos: dejamos que la via de fallback (extractores) lo intente.
            return []

        episode_id = self._parse_episode_id(document)
        if not episode_id:
            return []

        try:
            response = self.session.get(
                self.absolute_url(f"/ajax/download_episode/{episode_id}"),
                headers={
                    "Referer": self.absolute_url(episode_url),
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return []

        url = (payload.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return []

        nombre = payload.get("nombre") or ""
        extension = nombre.rsplit(".", 1)[-1].lower() if "." in nombre else "mp4"
        return [
            VideoStream(
                url=url,
                label="Descarga directa",
                quality="Descarga directa",
                extension=extension,
                headers={"Referer": self.absolute_url(episode_url)},
            ),
        ]

    def _parse_episode_id(self, document: BeautifulSoup) -> str:
        # El id del episodio aparece en varios sitios; preferimos data-capitulo.
        node = document.select_one("[data-capitulo]")
        if node and node.get("data-capitulo"):
            return node.get("data-capitulo").strip()
        current = document.select_one(".list-group-item.current[data-id]")
        if current and current.get("data-id"):
            return current.get("data-id").strip()
        return ""

    def _fetch_servers(self, episode_url: str) -> list[dict]:
        return self._parse_servers(self._episode_document(episode_url))

    def _parse_servers(self, document: BeautifulSoup) -> list[dict]:
        servers_script = None
        for script in document.find_all("script"):
            if script.string and "var servers" in script.string:
                servers_script = script.string
                break
        if not servers_script:
            return []

        match = re.search(r"var\s+servers\s*=\s*(\[.*?\]);", servers_script, re.S)
        if not match:
            return []
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    def _extractor_streams(self, servers: list[dict]) -> list[VideoStream]:
        all_streams: list[VideoStream] = []
        for server in servers:
            remote_b64 = server.get("remote")
            if not remote_b64:
                continue
            try:
                server_url = base64.b64decode(remote_b64).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError):
                continue

            lang = LANGUAGES.get(server.get("lang"), "")
            try:
                all_streams.extend(
                    resolve_streams(server_url, self.session, lang=lang, headers=dict(self.session.headers)),
                )
            except Exception:
                # Un host puede fallar (caido, cambio de formato); se ignora y se
                # continua con el resto de servidores, como hace el original.
                continue
        return all_streams

    def _episode_context(self, anime_url: str) -> dict[str, str]:
        document = self.html(anime_url)
        anime_id = attr_or_empty(document.select_one("[data-anime]"), "data-anime")
        token = attr_or_empty(document.select_one("meta[name=csrf-token]"), "content")
        episode_base_url = attr_or_empty(document.select_one('meta[property="og:url"]'), "content")

        if not anime_id or not token or not episode_base_url:
            raise SourceError("No se pudo localizar el listado de episodios de Jkanime.")

        return {
            "anime_id": anime_id,
            "token": token,
            "episode_base_url": episode_base_url,
        }

    def _fetch_episode_page(
        self,
        anime_id: str,
        token: str,
        page: int,
        referer: str,
    ) -> dict:
        response = self.session.post(
            self.absolute_url(f"/ajax/episodes/{anime_id}/{page}"),
            data={"_token": token},
            headers={
                "Referer": self.absolute_url(referer),
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def _episodes_from_payload(self, payload: dict, episode_base_url: str) -> list[EpisodeItem]:
        episodes: list[EpisodeItem] = []
        for item in payload.get("data") or []:
            number = parse_episode_number(item.get("number"))
            title = item.get("title") or f"Episodio {item.get('number')}"
            episodes.append(
                self.normalize_episode(
                    EpisodeItem(
                        title=title,
                        number=number,
                        uploaded_at=item.get("timestamp") or "",
                        url=f"{episode_base_url.rstrip('/')}/{item.get('number')}/",
                        extra={
                            "raw_status": item.get("estado") or item.get("status") or "",
                            "date_upload": parse_timestamp(item.get("timestamp")),
                        },
                    ),
                ),
            )
        return episodes

    def _parse_directory_page(self, document: BeautifulSoup, page: int) -> AnimePage:
        payload = self._extract_animes_json(document)
        items = [
            self.normalize_anime(
                AnimeItem(
                    title=item.get("title") or "",
                    url=item.get("url") or "",
                    thumbnail_url=item.get("image") or "",
                    description=item.get("synopsis") or "",
                    status=item.get("status") or item.get("estado") or "",
                    extra={
                        "studios": item.get("studios") or "",
                        "type": item.get("type") or item.get("tipo") or "",
                        "slug": item.get("slug") or "",
                    },
                ),
            )
            for item in payload.get("data") or []
            if item.get("title") and item.get("url")
        ]
        return AnimePage(
            items=items,
            page=page,
            has_next=bool(payload.get("next_page_url")),
        )

    def _parse_search_items(self, document: BeautifulSoup) -> list[AnimeItem]:
        items: list[AnimeItem] = []
        for element in document.select(".anime__item"):
            link = element.select_one("h5 a") or element.select_one("a")
            if not link:
                continue
            title = text_or_empty(link)
            url = attr_or_empty(link, "href")
            if not title or not url:
                continue
            items.append(
                self.normalize_anime(
                    AnimeItem(
                        title=title,
                        url=url,
                        thumbnail_url=attr_or_empty(element.select_one(".set-bg"), "data-setbg"),
                    ),
                ),
            )
        return items

    def _extract_animes_json(self, document: BeautifulSoup) -> dict:
        for script in document.find_all("script"):
            data = script.string or script.get_text()
            if "var animes" not in data:
                continue
            match = re.search(r"var\s+animes\s*=\s*(\{.*?\});", data, flags=re.S)
            if match:
                return json.loads(match.group(1))
        return {"data": [], "next_page_url": None}


def document_url(document: BeautifulSoup) -> str:
    url = getattr(document, "_source_url", "")
    return str(url or "")


def int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "si"}


def text_or_empty(element) -> str:
    return element.get_text(" ", strip=True) if element else ""


def attr_or_empty(element, attr_name: str) -> str:
    if not element:
        return ""
    return (element.get(attr_name) or "").strip()


def image_url(element) -> str:
    if not element:
        return ""
    for attr_name in ("data-src", "data-lazy-src", "srcset", "src"):
        value = attr_or_empty(element, attr_name)
        if value and "anime.png" not in value:
            return value.split(" ", 1)[0]
    return ""


def parse_episode_number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def episode_sort_key(episode: EpisodeItem) -> tuple[float, str]:
    number = episode.number if episode.number is not None else 10**9
    return number, episode.title


def parse_timestamp(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S").isoformat()
    except ValueError:
        return value.strip()
