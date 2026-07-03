from __future__ import annotations

import os

from .base import AnimeItem, AnimePage, BaseAnimeSource, EpisodeItem, VideoStream


class DemoAuthorizedSource(BaseAnimeSource):
    id = "demo"
    name = "Demo autorizado"
    lang = "es"
    base_url = "https://example.test"
    supports_latest = True
    allow_downloads = True

    @property
    def enabled(self) -> bool:
        return os.environ.get("ANIME_ENABLE_DEMO", "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def _media_url(self) -> str:
        return os.environ.get(
            "ANIME_DEMO_MEDIA_URL",
            "https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/360/"
            "Big_Buck_Bunny_360_10s_1MB.mp4",
        )

    def _catalog(self) -> list[AnimeItem]:
        return [
            AnimeItem(
                title="Big Buck Bunny - muestra educativa",
                url="/anime/big-buck-bunny",
                thumbnail_url="https://peach.blender.org/wp-content/uploads/title_anouncement.jpg",
                description="Entrada demo con un clip de prueba configurable por ANIME_DEMO_MEDIA_URL.",
                genres=("Demo", "Creative Commons"),
                status="Completo",
            ),
            AnimeItem(
                title="Sintel - muestra educativa",
                url="/anime/sintel",
                thumbnail_url="https://download.blender.org/durian/trailer/sintel_trailer-480p.jpg",
                description="Entrada demo para validar busqueda, episodios y cola.",
                genres=("Demo", "Animacion"),
                status="Completo",
            ),
        ]

    def popular(self, page: int = 1) -> AnimePage:
        items = [self.normalize_anime(item) for item in self._catalog()]
        return AnimePage(items=items, page=page, has_next=False)

    def latest(self, page: int = 1) -> AnimePage:
        return self.popular(page)

    def search(self, query: str, page: int = 1) -> AnimePage:
        query = query.strip().lower()
        items = [
            self.normalize_anime(item)
            for item in self._catalog()
            if not query or query in item.title.lower() or query in item.description.lower()
        ]
        return AnimePage(items=items, page=page, has_next=False)

    def details(self, anime_url: str) -> AnimeItem:
        for item in self._catalog():
            normalized = self.normalize_anime(item)
            if normalized.url == anime_url or item.url == anime_url:
                return normalized
        return self.normalize_anime(self._catalog()[0])

    def episodes(self, anime_url: str) -> list[EpisodeItem]:
        slug = anime_url.rstrip("/").split("/")[-1] or "demo"
        return [
            self.normalize_episode(
                EpisodeItem(
                    title="Episodio 1 - clip de prueba",
                    number=1,
                    url=f"/watch/{slug}/1",
                ),
            ),
            self.normalize_episode(
                EpisodeItem(
                    title="Episodio 2 - mismo clip",
                    number=2,
                    url=f"/watch/{slug}/2",
                ),
            ),
        ]

    def get_video_streams(self, episode_url: str) -> list[VideoStream]:
        return [
            VideoStream(
                label="Demo MP4 360p",
                quality="360p",
                url=self._media_url(),
                extension="mp4",
                mime_type="video/mp4",
            ),
        ]
