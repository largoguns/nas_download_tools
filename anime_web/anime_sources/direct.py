from __future__ import annotations

from extractors.registry import EXTRACTORS

from .base import AnimeItem, AnimePage, BaseAnimeSource, EpisodeItem, VideoStream


class DirectSource(BaseAnimeSource):
    id = "direct"
    name = "Directo autorizado"
    lang = "es"
    base_url = ""
    supports_latest = False
    supports_search = True
    allow_downloads = True

    def absolute_url(self, url: str) -> str:
        return url

    def popular(self, page: int = 1) -> AnimePage:
        return AnimePage(
            items=[
                AnimeItem(
                    source_id=self.id,
                    title="Descarga directa",
                    url="direct://manual",
                    status="Manual",
                ),
            ],
            page=page,
            has_next=False,
        )

    def search(self, query: str, page: int = 1) -> AnimePage:
        return self.popular(page)

    def details(self, anime_url: str) -> AnimeItem:
        return AnimeItem(source_id=self.id, title="Descarga directa", url=anime_url, status="Manual")

    def episodes(self, anime_url: str) -> list[EpisodeItem]:
        return []

    def get_video_streams(self, episode_url: str) -> list[VideoStream]:
        direct_extractor = EXTRACTORS.get("direct")
        if not direct_extractor:
            return []
        return direct_extractor(episode_url, self.session)
