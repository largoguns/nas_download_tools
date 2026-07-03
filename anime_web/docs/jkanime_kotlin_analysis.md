# Analisis del paquete Kotlin `es/jkanime`

Ruta revisada:

`to_be_analyzed/extensions-source-main/src/es/jkanime/src/eu/kanade/tachiyomi/animeextension/es/jkanime`

## Estructura encontrada

- `Jkanime.kt`: fuente principal. Hereda de `AnimeHttpSource` y `ConfigurableAnimeSource`.
- `JkanimeFilters.kt`: define filtros de genero, letra, demografia, categoria, tipo, estado, ano, temporada y orden.
- `models/JkAnimeModel.kt`: DTOs serializables para respuestas paginadas de catalogo, episodios y servidores.
- `extractors/JkanimeExtractor.kt`: resolucion de URLs de algunos reproductores propios.
- `build.gradle`: declara extractores de hosts externos como dependencias: Okru, Mixdrop, StreamWish, Mp4Upload, Filemoon, StreamTape, Voe y Universal.

## Flujo de catalogo

- Populares: `GET /directorio?filtro=popularidad&p={page}`.
- Recientes/en emision: `GET /directorio?estado=emision&p={page}`.
- Busqueda por texto: `GET /buscar/{query}`.
- Busqueda filtrada: `GET /directorio?...`.
- En paginas de directorio, el listado se extrae de un script con `var animes = {...}` y se deserializa como `PopularAnimeModel`.
- En busqueda textual, los resultados salen de nodos `.anime__item`.

Mapeo Python:

- `popular()` equivale a `popularAnimeRequest/Parse`.
- `latest()` equivale a `latestUpdatesRequest/Parse`.
- `search()` equivale a `searchAnimeRequest/Parse`.
- `AnimeItem` reemplaza a `SAnime`.

## Flujo de episodios

- La pagina de detalle contiene `data-anime`, un token CSRF y la URL canonica.
- Los episodios se piden por paginas con `POST /ajax/episodes/{animeId}/{page}`.
- La extension Kotlin preserva cookies y `Referer`.
- El resultado se deserializa como `EpisodeAnimeModel`.
- La extension hace pausas periodicas para reducir respuestas 429.

Mapeo Python:

- `episodes(anime_url)` devuelve `list[EpisodeItem]`.
- Una fuente real deberia usar `BaseAnimeSource.session`, respetar cookies y aplicar rate limiting.

## Flujo de video

- La pagina del episodio contiene un script con `var servers = [...]`.
- Cada servidor trae `remote` en base64, `server`, `slug`, `lang` y metadatos.
- La extension detecta el host por convenciones de dominio y delega en extractores externos.
- La preferencia de idioma/calidad/servidor solo ordena la lista final de `Video`.

Mapeo Python:

- `get_video_streams(episode_url)` devuelve `list[VideoStream]`.
- `select_stream()` en la base elige por calidad preferida o mayor resolucion.
- La cola solo descarga si la fuente declara `allow_downloads = True`.

## Descarga directa (preferente)

La pagina del episodio construye una tabla de enlaces `{remote}/d/{slug}/`
(`remote = https://c1.jkplayers.com`). **Importante:** esos enlaces NO sirven el
fichero: redirigen al servicio que lo aloja (voe.sx, mediafire, mega, ...) y
devuelven HTML, no el video. Por tanto no son utiles para descarga directa.

El mecanismo real de descarga de la web es el boton `#jkdown`, que llama a
`GET /ajax/download_episode/{episodeId}` y obtiene `{url, nombre}` con la URL
real del fichero (la web la descarga por XHR como blob). `get_video_streams` usa
ese endpoint:

1. Obtiene el id del episodio de la pagina (`[data-capitulo]`, o el
   `.list-group-item.current[data-id]`).
2. Llama a `/ajax/download_episode/{id}` y devuelve un stream con la `url` real y
   la extension deducida de `nombre`.

Si la directa no resuelve o falla al descargar, se usan los extractores como
fallback real a nivel de descarga (`get_video_streams_fallback`), que se resuelven
solo entonces (evitando coste y 429 innecesarios). El flujo de `download_item`:
intenta los candidatos primarios y, si todos fallan, los de fallback.

Variables: `ANIME_JKANIME_DIRECT_DOWNLOAD` (def. `true`),
`ANIME_JKANIME_EXTRACTOR_FALLBACK` (def. `true`).

### Paginas destino de cada servidor

El `remote` base64 de cada servidor decodifica a la pagina del host (embed `/e/`
o `/file/`). Como se obtiene el fichero final de cada una:

| Host | URL decodificada | Como se obtiene el fichero |
| --- | --- | --- |
| Mediafire | `/file/{id}/` | `a#downloadButton[href]` -> MP4 directo (`mediafire.py`) |
| Mixdrop | `/e/{id}` | script empaquetado -> `MDCore.wurl` (`mixdrop.py`) |
| Streamwish | `/e/{id}` | script -> `master.m3u8` (HLS, `streamwish.py`) |
| Mega | `/file/{id}#key` | **No soportado**: descarga cifrada en cliente (AES-CTR); se omite y se pivota (`mega.py`) |
| Doodstream | `/e/{id}` | `pass_md5` (`doodstream.py`, best-effort por dominios rotatorios) |

Notas:

- Mediafire y Mega se anadieron a `CONVENTIONS`; Mega devuelve `[]` a proposito
  para que el descargador pivote a otro servidor sin perder tiempo.
- `c1.jkplayers.com/d/{slug}/` NO sirve el fichero: redirige a estas mismas
  paginas de host, por eso la via fiable es decodificar el `remote` y extraer.

## Extractores portados

Los extractores que declara `build.gradle` de jkanime se portaron desde
`to_be_analyzed/extensions-source-main/lib` a `anime_web/extractors/`:

| Host | Kotlin | Python |
| --- | --- | --- |
| Voe | `voe-extractor` | `voe.py` (cadena `decryptF7` + HLS) |
| Okru | `okru-extractor` | `okru.py` (`data-options`: HLS/DASH/MP4) |
| Filemoon | `filemoon-extractor` | `filemoon.py` (API embed/playback + AES-GCM) |
| StreamTape | `streamtape-extractor` | `streamtape.py` (`robotlink` + `xcd`) |
| Mp4Upload | `mp4upload-extractor` | `mp4upload.py` (Unpacker / `player.src`) |
| MixDrop | `mixdrop-extractor` | `mixdrop.py` (Unpacker + `MDCore.wurl`) |
| StreamWish | `streamwish-extractor` | `streamwish.py` (Unpacker + regex m3u8 + HLS) |
| Universal | `universal-extractor` | `universal.py` (mejor esfuerzo; ver nota) |
| Desuka/Nozomi/Desu | `extractors/JkanimeExtractor.kt` | `jkanime_internal.py` |

Dependencias comunes portadas en `extractors/_common.py`:

- `Unpacker` (`lib/unpacker`): desempaquetado del *packer* de Dean Edwards.
- `PlaylistUtils.extract_from_hls` (`lib/playlist-utils`): variantes de un
  master playlist HLS.
- Helpers de cadenas con semantica de Kotlin (`after`, `before`, ...).

Deteccion de host y despacho en `extractors/registry.py`:

- `CONVENTIONS`: mismos alias de dominio y orden que `Jkanime.kt`.
- `resolve_streams(url, session, lang, headers)`: replica el bloque
  `when (matched)` de `videoListParse`, aplica el prefijo de idioma
  (`LANGUAGES`: 1=`[JAP]`, 3=`[LAT]`, 4=`[CHIN]`) y cae en el extractor
  universal cuando ningun host coincide.

Notas:

- `filemoon.py` usa `cryptography` (anadido a `requirements.txt`) para el
  AES-256-GCM; si la libreria no esta presente, omite el ramo cifrado y usa las
  `sources` en claro.
- `universal.py` no puede replicar el `WebView` de Android; hace un mejor
  esfuerzo buscando una URL de medios por regex en la pagina.
