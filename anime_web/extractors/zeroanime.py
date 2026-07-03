from __future__ import annotations

from .base import (
    AnimeItem,
    AnimeList,
    AnimeSource,
    EpisodeItem,
    EpisodePage,
    SourceError,
    VideoContainer,
    VideoStream,
)
from extractors.registry import EXTRACTORS


class ZeroAnime(AnimeSource):
    source_id = "zeroanime"
    name = "ZeroAnime"
    base_url = "https://www4.zeroanime.xyz"
    allow_downloads = True

    def popular(self, page: int = 1) -> AnimeList:
        raise NotImplementedError

    def search(self, query: str, page: int = 1) -> AnimeList:
        raise NotImplementedError

    def details(self, anime_url: str) -> VideoContainer:
        raise NotImplementedError

    def episode_page(self, anime_url: str, page: int = 1) -> EpisodePage:
        raise NotImplementedError

    def get_video_streams(self, episode_url: str) -> list[VideoStream]:
        """Obtiene los streams de video para un episodio.

        Utiliza el registro central de extractores para procesar los enlaces
        de video encontrados en la pagina.
        """
        document = self.html(episode_url)
        streams = []

        # Busca enlaces a servidores en contenedores comunes.
        server_links = document.select(".server-list a, .player-servers a")

        for link in server_links:
            url = link.get("href")
            if not url:
                continue

            for domain, extractor_module in EXTRACTORS.items():
                if domain in url.lower():
                    streams.extend(extractor_module.get_streams(url, self.session))
                    break
        return streams