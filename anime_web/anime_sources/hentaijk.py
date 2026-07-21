from __future__ import annotations

from .jkanime import JkanimeSource


class HentaijkSource(JkanimeSource):
    """hentaijk.com usa el mismo motor y maquetado que jkanime.

    Reutiliza toda la logica de scraping de :class:`JkanimeSource` (directorio,
    busqueda, generos, listado de episodios y descargas); solo cambian id,
    nombre, dominio base y el catalogo de generos, que aqui incluye las
    categorias propias del sitio (extraidas de ``select[name=genero]`` del
    directorio).
    """

    id = "hentaijk"
    name = "Hentaijk"
    base_url = "https://hentaijk.com"
    genre_options = (
        ("accion", "Accion"), ("aventura", "Aventura"), ("autos", "Autos"),
        ("comedia", "Comedia"), ("dementia", "Dementia"), ("demonios", "Demonios"),
        ("misterio", "Misterio"), ("drama", "Drama"), ("ecchi", "Ecchi"),
        ("fantasa", "Fantasia"), ("juegos", "Juegos"), ("hentai", "Hentai"),
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
        ("josei", "Josei"), ("espaol-latino", "Español Latino"), ("escolares", "Escolares"),
        ("orgias", "Orgias"), ("virgenes", "Virgenes"), ("anal", "Anal"),
        ("maduras", "Maduras"), ("ahegao", "Ahegao"), ("tetonas", "Tetonas"),
        ("incesto", "Incesto"), ("ntr", "NTR"), ("vanilla", "Vanilla"),
        ("violacion", "Violacion"), ("hardcore", "Hardcore"), ("trio", "Trio"),
        ("cowgirl", "Cowgirl"), ("bondage", "Bondage"), ("lolis", "Lolis"),
        ("rubias", "Rubias"), ("gay", "Gay"), ("tentaculos", "Tentaculos"),
        ("monstruos", "Monstruos"), ("tsundere", "Tsundere"), ("sin-censura", "Sin Censura"),
        ("softcore", "Softcore"), ("shota", "Shota"), ("ninjas", "Ninjas"),
        ("waifus", "Waifus"), ("gordas", "Gordas"), ("enfermeras", "Enfermeras"),
        ("paizuri", "Paizuri"), ("succubus", "Succubus"), ("juegos-sexuales", "Juegos Sexuales"),
        ("netorare", "Netorare"), ("petit", "Petit"), ("maids", "Maids"),
    )
