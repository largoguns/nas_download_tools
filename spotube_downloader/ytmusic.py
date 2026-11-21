"""Descarga directa de enlaces de YouTube Music con yt-dlp (sin pasar por spotdl).

spotdl empareja cada cancion con Spotify y descarta resultados validos cuando los
metadatos no cuadran. Con un enlace de YouTube Music ya sabemos exactamente que
audio queremos, asi que se baja tal cual con yt-dlp, que ya viene en la imagen
como dependencia de spotdl.

El modulo tiene dos caras:

- Helpers que usa ``app.py``: reconocer/normalizar enlaces y leer metadatos
  ligeros (oEmbed) para la cola.
- Un ejecutable (``python ytmusic.py URL ...``) que corre como subproceso, para
  que el worker pueda cancelarlo matando el grupo de procesos igual que a spotdl.
  Emite eventos ``@@YTM {json}`` por stdout que ``app.py`` interpreta.
"""

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

EVENT_PREFIX = "@@YTM "
YOUTUBE_HOSTS = {
    "music.youtube.com",
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
}
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
# Las playlists de album de YouTube Music tienen ids OLAK5uy_...
ALBUM_PLAYLIST_PREFIX = "OLAK5uy_"
OEMBED_TIMEOUT = 8


# --------------------------------------------------------------------------
# Helpers para app.py
# --------------------------------------------------------------------------


def parse_url(value):
    """Devuelve ``(kind, id)`` con kind ``video`` o ``playlist``, o ``None``."""
    try:
        parsed = urllib.parse.urlparse((value or "").strip())
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    host = parsed.netloc.lower().split(":")[0]
    if host not in YOUTUBE_HOSTS:
        return None

    query = urllib.parse.parse_qs(parsed.query)
    if host == "youtu.be":
        video_id = parsed.path.strip("/")
        return ("video", video_id) if VIDEO_ID_RE.match(video_id) else None

    path = parsed.path.rstrip("/")
    if path == "/watch":
        # Un watch?v=...&list=... se trata como la cancion suelta que se pego.
        video_id = (query.get("v") or [""])[0]
        return ("video", video_id) if VIDEO_ID_RE.match(video_id) else None
    if path == "/playlist":
        playlist_id = (query.get("list") or [""])[0]
        return ("playlist", playlist_id) if PLAYLIST_ID_RE.match(playlist_id) else None
    return None


def is_youtube_url(value):
    return parse_url(value) is not None


def normalize_url(value):
    """URL canonica de YouTube Music, sin parametros de seguimiento (``si=``)."""
    kind, item_id = parse_url(value)
    if kind == "video":
        return f"https://music.youtube.com/watch?v={item_id}"
    return f"https://music.youtube.com/playlist?list={item_id}"


def item_type(value):
    """Tipo para la cola: ``track``, ``album`` o ``playlist``."""
    parsed = parse_url(value)
    if parsed is None:
        return None
    kind, item_id = parsed
    if kind == "video":
        return "track"
    return "album" if item_id.startswith(ALBUM_PLAYLIST_PREFIX) else "playlist"


def fetch_metadata(value):
    """Titulo/artista/caratula para la cola via oEmbed (sin API key)."""
    kind, item_id = parse_url(value)
    if kind == "video":
        web_url = f"https://www.youtube.com/watch?v={item_id}"
    else:
        web_url = f"https://www.youtube.com/playlist?list={item_id}"
    request = urllib.request.Request(
        "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(web_url, safe=""),
        headers={"User-Agent": "spotube-downloader/1.0"},
    )
    with urllib.request.urlopen(request, timeout=OEMBED_TIMEOUT) as response:
        data = json.load(response)

    title = (data.get("title") or "").strip()
    artist = clean_channel_name(data.get("author_name"))
    if kind == "playlist":
        # YouTube Music titula los albumes "Album - Nombre" / "EP - ..." / "Single - ...".
        title = re.sub(r"^(?:Album|EP|Single)\s+-\s+", "", title)
    else:
        split_artist, split_title = split_artist_title(title)
        if split_artist:
            artist, title = split_artist, split_title

    return {
        "metadata_title": title or None,
        "metadata_artist": artist or None,
        "metadata_type": item_type(value),
        "metadata_image_url": data.get("thumbnail_url"),
    }


# --------------------------------------------------------------------------
# Metadatos de cada pista
# --------------------------------------------------------------------------


def clean_channel_name(value):
    value = (value or "").strip()
    value = re.sub(r"\s+-\s+Topic$", "", value)
    value = re.sub(r"VEVO$", "", value).strip()
    return value


def split_artist_title(title):
    """``"Artista - Titulo"`` -> ``("Artista", "Titulo")``; si no, ``(None, title)``."""
    match = re.match(r"^\s*(?P<artist>.+?)\s+[-–]\s+(?P<title>.+?)\s*$", title or "")
    if not match:
        return None, title
    clean_title = re.sub(
        r"\s*[\(\[](?:official\s+)?(?:music\s+|lyric\s+)?(?:video|audio|visualizer)[\)\]]\s*$",
        "",
        match.group("title"),
        flags=re.IGNORECASE,
    )
    return match.group("artist").strip(), clean_title.strip()


def first_artist(artists_text):
    return re.split(r",|\s&\s|\sfeat\.?\s|\sft\.?\s", artists_text, flags=re.IGNORECASE)[0].strip()


def path_part(value, fallback):
    """Nombre de carpeta/fichero seguro, con el mismo criterio que spotdl (':' -> '-')."""
    value = (value or "").strip() or fallback
    value = value.replace(":", "-")
    value = re.sub(r'[\\/*?"<>|]', "", value)
    value = value.strip(" .")
    return value[:180] or fallback


class AlbumLookup:
    """Datos reales del album desde YouTube Music (via ytmusicapi).

    YouTube no da el artista del album en los videos, y adivinarlo pista a pista
    hace que Navidrome parta un disco en varios. La pagina del album de YouTube
    Music si lo tiene (p. ej. "Various Artists", "Descendants 3 – Cast"), junto al
    titulo completo, el ano y el numero de pista. Se usa igual para un album
    entero que para una cancion suelta, asi ambas acaban en el mismo disco.
    Cualquier fallo devuelve ``None`` y se cae a las heuristicas.
    """

    def __init__(self):
        self._ytm = None
        self._albums = {}  # browse_id -> info del album
        self._video_album = {}  # video_id -> browse_id

    def _client(self):
        if self._ytm is None:
            from ytmusicapi import YTMusic

            self._ytm = YTMusic()
        return self._ytm

    def _album(self, browse_id):
        if browse_id not in self._albums:
            data = self._client().get_album(browse_id)
            artists = [a.get("name") for a in data.get("artists") or [] if a.get("name")]
            numbers = {}
            for index, track in enumerate(data.get("tracks") or [], start=1):
                if track.get("videoId"):
                    numbers[track["videoId"]] = track.get("trackNumber") or index
            self._albums[browse_id] = {
                "title": data.get("title"),
                "artist": artists[0] if artists else None,
                "year": data.get("year"),
                "track_numbers": numbers,
            }
        return self._albums[browse_id]

    def preload_playlist(self, playlist_id):
        """Album de una playlist OLAK5uy_ (una sola consulta para todo el disco)."""
        try:
            browse_id = self._client().get_album_browse_id(playlist_id)
            album = self._album(browse_id) if browse_id else None
        except Exception as exc:
            print(f"WARNING: no se pudo leer el album de YouTube Music: {exc}", flush=True)
            return None
        if album:
            for video_id in album["track_numbers"]:
                self._video_album[video_id] = browse_id
        return album

    def for_video(self, video_id):
        try:
            if video_id not in self._video_album:
                tracks = self._client().get_watch_playlist(video_id, limit=1).get("tracks") or []
                album_ref = (tracks[0].get("album") or {}) if tracks else {}
                self._video_album[video_id] = album_ref.get("id")
            browse_id = self._video_album[video_id]
            return self._album(browse_id) if browse_id else None
        except Exception as exc:
            print(f"WARNING: no se pudo leer el album de {video_id}: {exc}", flush=True)
            self._video_album[video_id] = None
            return None


def fill_track_metadata(info, album=None):
    """Rellena track/artist/album/... en el info dict de yt-dlp.

    Las pistas "Topic" de YouTube Music traen metadatos de musica; los videos
    musicales (VEVO, etc.) no, y entonces se deducen del titulo del video.
    ``album`` son los datos reales del album (``AlbumLookup``); con ellos el
    artista de album, el titulo y el ano son los mismos para todas las pistas.
    """
    title = info.get("title") or ""
    track = info.get("track")
    artist = info.get("artist") or ", ".join(info.get("artists") or []) or None
    if not track or not artist:
        split_artist, split_title = split_artist_title(title)
        track = track or split_title or title
        artist = artist or split_artist or clean_channel_name(info.get("uploader") or info.get("channel"))
    artist = artist or "Desconocido"
    main_artist = first_artist(artist)
    album = album or {}

    playlist_id = info.get("playlist_id") or ""
    is_album_playlist = playlist_id.startswith(ALBUM_PLAYLIST_PREFIX)
    album_title = album.get("title") or info.get("album")
    if not album_title and is_album_playlist:
        album_title = re.sub(r"^(?:Album|EP|Single)\s+-\s+", "", info.get("playlist_title") or "")
    album_title = album_title or track  # sencillo suelto: album = titulo, como hace spotdl

    album_artist = album.get("artist") or info.get("album_artist") or main_artist
    track_number = (album.get("track_numbers") or {}).get(info.get("id")) or info.get("track_number")
    if not track_number and is_album_playlist:
        track_number = info.get("playlist_index")

    info["description"] = None  # la descripcion del video no es una etiqueta util
    info["track"] = track
    info["artist"] = artist
    info["album"] = album_title
    info["album_artist"] = album_artist
    if track_number:
        info["track_number"] = track_number
    year = album.get("year") or info.get("release_year")
    if year:
        info["meta_date"] = str(year)  # ano de publicacion, no el de subida a YouTube
    # Campos propios para la plantilla de salida: {artista album}/{album}/{titulo}.mp3
    info["ytm_dir_artist"] = path_part(album_artist, "Desconocido")
    info["ytm_dir_album"] = path_part(album_title, "Singles")
    info["ytm_file_title"] = path_part(track, info.get("id") or "cancion")
    return info


# --------------------------------------------------------------------------
# Ejecutable (subproceso del worker)
# --------------------------------------------------------------------------


def emit(event, **data):
    print(EVENT_PREFIX + json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def run(args):
    import yt_dlp
    from yt_dlp.postprocessor.common import PostProcessor

    albums = AlbumLookup()
    kind, item_id = parse_url(args.url)
    playlist_album = None
    if kind == "playlist" and item_id.startswith(ALBUM_PLAYLIST_PREFIX):
        playlist_album = albums.preload_playlist(item_id)

    class MusicMetadataPP(PostProcessor):
        def run(self, info):
            album = playlist_album or albums.for_video(info.get("id"))
            fill_track_metadata(info, album)
            emit(
                "item",
                index=info.get("playlist_index") or 1,
                total=info.get("n_entries") or info.get("playlist_count") or 1,
                title=info["track"],
                artist=info["artist"],
                url=info.get("webpage_url"),
            )
            return [], info

    class EventLogger:
        def debug(self, msg):
            if not msg.startswith("[debug] "):
                print(msg, flush=True)

        def info(self, msg):
            print(msg, flush=True)

        def warning(self, msg):
            print(f"WARNING: {msg}", flush=True)

        def error(self, msg):
            emit("error", message=re.sub(r"^ERROR:\s*", "", msg))

    def on_postprocessor(status):
        if status.get("status") == "finished" and status.get("postprocessor") == "MoveFiles":
            info = status.get("info_dict") or {}
            emit(
                "done",
                path=info.get("filepath"),
                index=info.get("playlist_index") or 1,
                title=info.get("track") or info.get("title"),
                artist=info.get("artist"),
            )

    bitrate = re.sub(r"[^0-9]", "", args.bitrate) or "192"
    output = str(
        Path(args.output_dir)
        / "%(ytm_dir_artist)s"
        / "%(ytm_dir_album)s"
        / "%(ytm_file_title)s.%(ext)s"
    )
    options = {
        "format": "bestaudio/best",
        "outtmpl": {"default": output, "pl_thumbnail": ""},  # sin miniatura de playlist
        "final_ext": args.format,  # si el .mp3 final ya existe, no se vuelve a bajar
        "overwrites": False,
        "noplaylist": True,  # un watch?v=...&list=... baja solo la cancion
        "ignoreerrors": "only_download",  # en playlists, seguir con el resto
        "windowsfilenames": False,
        "writethumbnail": True,
        "noprogress": True,
        "logger": EventLogger(),
        "postprocessor_hooks": [on_postprocessor],
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": args.format, "preferredquality": bitrate},
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
            {"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": False},
            {"key": "EmbedThumbnail", "already_have_thumbnail": False},
        ],
        # Caratula cuadrada: los videos traen miniaturas 16:9.
        "postprocessor_args": {
            "thumbnailsconvertor+ffmpeg_o": [
                "-c:v", "mjpeg", "-qmin", "1", "-qscale:v", "2",
                "-vf", "crop='if(gt(ih,iw),iw,ih)':'if(gt(iw,ih),ih,iw)'",
            ],
        },
    }
    if args.cookies:
        options["cookiefile"] = args.cookies
    if args.deno:
        options["js_runtimes"] = {"deno": {"path": args.deno}}

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.add_post_processor(MusicMetadataPP(ydl), when="pre_process")
        return ydl.download([args.url])


def main(argv=None):
    parser = argparse.ArgumentParser(description="Descarga directa de YouTube Music")
    parser.add_argument("url")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--format", default="mp3")
    parser.add_argument("--bitrate", default="192k")
    parser.add_argument("--cookies")
    parser.add_argument("--deno")
    args = parser.parse_args(argv)
    if not is_youtube_url(args.url):
        emit("error", message="Enlace de YouTube Music no valido.")
        return 2
    try:
        return run(args)
    except Exception as exc:  # cualquier fallo se reporta como evento
        emit("error", message=str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
