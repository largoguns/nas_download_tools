"""Fuente AnimeFenix (https://animefenix2.tv).

Port a Python de la extension Kotlin `es/animefenix` (`Animefenix.kt`). Es una
fuente basada en HTML: catalogo, busqueda, detalle y episodios se parsean con
selectores CSS. Los reproductores se obtienen de un script con ``var tabsArray``
que contiene iframes ``redirect.php?id=<url-embed>``; cada URL embebida se
resuelve por convencion de dominio con los extractores ya implementados
(``resolve_streams``), cayendo en el extractor universal cuando no hay match.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote, unquote

from bs4 import BeautifulSoup

from extractors.registry import resolve_streams

from .base import (
    AnimeItem,
    AnimePage,
    BaseAnimeSource,
    EpisodeItem,
    SourceError,
    VideoStream,
)


class AnimefenixSource(BaseAnimeSource):
    id = "animefenix"
    name = "AnimeFenix"
    lang = "es"
    base_url = "https://animefenix2.tv"
    supports_latest = False
    supports_search = True
    allow_downloads = True
    default_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es,en;q=0.8",
    }

    # ------------------------------------------------------------------ catalogo
    def popular(self, page: int = 1) -> AnimePage:
        document = self.html(f"/directorio/anime?p={max(1, page)}&estado=2")
        return self._parse_directory(document, page)

    def search(self, query: str, page: int = 1) -> AnimePage:
        query = query.strip()
        if not query:
            return self.popular(page)
        document = self.html(f"/directorio/anime?q={quote(query)}&p={max(1, page)}")
        return self._parse_directory(document, page)

    def _parse_directory(self, document: BeautifulSoup, page: int) -> AnimePage:
        items: list[AnimeItem] = []
        for element in document.select(".grid-animes li article a"):
            url = attr_or_empty(element, "href")
            title = text_or_empty(element.select_one("p:not(.gray)"))
            thumb = image_url(element.select_one(".main-img img"))
            if not url or not title:
                continue
            items.append(
                self.normalize_anime(
                    AnimeItem(title=title, url=url, thumbnail_url=thumb),
                ),
            )
        has_next = bool(document.select_one(".right:not(.disabledd)"))
        return AnimePage(items=items, page=page, has_next=has_next)

    # -------------------------------------------------------------------- detalle
    def details(self, anime_url: str) -> AnimeItem:
        document = self.html(anime_url)
        status_text = text_or_empty(document.select_one(".relative .rounded"))
        status = (
            "Finalizado"
            if "finalizado" in status_text.lower()
            else ("En emision" if "emision" in status_text.lower() else "")
        )
        genres = tuple(
            text_or_empty(a) for a in document.select(".flex-wrap a") if text_or_empty(a)
        )
        return self.normalize_anime(
            AnimeItem(
                title=own_text(document.select_one("h1.text-4xl")),
                url=anime_url,
                thumbnail_url=image_url(document.select_one("#anime_image")),
                description=text_or_empty(document.select_one(".mb-6 p.text-gray-300")),
                genres=genres,
                status=status,
            ),
        )

    # ------------------------------------------------------------------ episodios
    def episodes(self, anime_url: str) -> list[EpisodeItem]:
        document = self.html(anime_url)

        # El primer lote viene embebido en la pagina de detalle; las series
        # largas se paginan via AJAX (?id={slug}&load=episodes&start=N), con un
        # boton por rango. Recogemos el lote embebido y, si hay mas rangos, los
        # pedimos tambien.
        anchors = self._episode_anchors(document)
        slug = anime_url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
        for start in self._episode_range_starts(document):
            if start == 0:
                continue  # el rango inicial ya viene embebido
            try:
                fragment = self.html(f"/{slug}?id={quote(slug)}&load=episodes&start={start}")
            except Exception:
                continue
            anchors.extend(self._episode_anchors(fragment))

        episodes: list[EpisodeItem] = []
        seen: set[str] = set()
        for link in anchors:
            url = attr_or_empty(link, "href")
            if not url or "/ver/" not in url or url in seen:
                continue
            seen.add(url)
            title = (
                text_or_empty(link.select_one(".ep-title"))
                or text_or_empty(link.select_one(".font-semibold"))
                or text_or_empty(link)
            )
            number = parse_episode_number(title)
            if number is None:
                number = number_from_url(url)
            if not title:
                title = f"Episodio {number:g}" if number is not None else "Episodio"
            episodes.append(
                self.normalize_episode(
                    EpisodeItem(title=title, url=url, number=number),
                ),
            )

        if not episodes:
            logging.warning(
                "AnimeFenix: sin episodios en %s (revisa el selector de la pagina)",
                anime_url,
            )
        episodes.sort(key=lambda e: (e.number if e.number is not None else 10**9, e.title))
        return episodes

    def _episode_anchors(self, node) -> list:
        """Enlaces de episodio del layout actual (card) o antiguo, con fallback."""
        anchors = node.select("a.episode-card")
        if not anchors:
            anchors = node.select(".divide-y li > a")
        if not anchors:
            anchors = [a for a in node.select("a[href]") if "/ver/" in (a.get("href") or "")]
        return anchors

    def _episode_range_starts(self, document) -> list[int]:
        starts: set[int] = set()
        for button in document.select("[onclick*='loadEpisodes']"):
            match = re.search(r"loadEpisodes\((\d+)", button.get("onclick", ""))
            if match:
                starts.add(int(match.group(1)))
        return sorted(starts)

    # --------------------------------------------------------------------- video
    def get_video_streams(self, episode_url: str) -> list[VideoStream]:
        try:
            document = self.html(episode_url)
        except Exception as exc:
            raise SourceError(f"No se pudo obtener la pagina del episodio: {exc}") from exc

        script = None
        for element in document.find_all("script"):
            data = element.string or element.get_text()
            if data and "var tabsArray" in data:
                script = data
                break
        if not script:
            return []

        headers = dict(self.session.headers)
        streams: list[VideoStream] = []
        for embed_url in self._embed_urls(script):
            try:
                streams.extend(resolve_streams(embed_url, self.session, lang="", headers=headers))
            except Exception:
                # Un servidor puede fallar; se continua con el resto.
                continue
        return streams

    def _embed_urls(self, script: str) -> list[str]:
        """Extrae las URLs embebidas de los iframes ``redirect.php?id=...``."""
        if "<iframe" not in script:
            return []
        after_iframe = script.split("<iframe", 1)[1]
        urls: list[str] = []
        for fragment in after_iframe.split("src='")[1:]:
            raw = fragment.split("'", 1)[0]
            candidate = raw.split("redirect.php?id=", 1)[-1].strip()
            candidate = unquote(candidate)
            if candidate.startswith(("http://", "https://")):
                urls.append(candidate)
        return urls


# ---------------------------------------------------------------------- helpers
def text_or_empty(element) -> str:
    return element.get_text(" ", strip=True) if element else ""


def own_text(element) -> str:
    """Texto directo del elemento, sin el de sus hijos (equivale a ownText)."""
    if not element:
        return ""
    direct = "".join(element.find_all(string=True, recursive=False))
    return direct.strip() or element.get_text(" ", strip=True)


def attr_or_empty(element, attr_name: str) -> str:
    if not element:
        return ""
    return (element.get(attr_name) or "").strip()


def image_url(element) -> str:
    if not element:
        return ""
    for attr_name in ("data-src", "data-lazy-src", "srcset", "src"):
        value = attr_or_empty(element, attr_name)
        if value and "data:image/" not in value:
            return value.split(" ", 1)[0]
    return ""


def parse_episode_number(title: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)", title or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def number_from_url(url: str) -> float | None:
    """Deriva el numero de episodio de una URL tipo ``/ver/slug-12``."""
    match = re.search(r"-(\d+(?:\.\d+)?)/?(?:[?#].*)?$", url or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None
