import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib import request as url_request
from urllib.parse import urlencode, urlparse

from flask import Flask, jsonify, render_template, request

import deezer
import navidrome


def parse_int_env(name, default, minimum=None):
    raw_value = os.environ.get(name)
    if raw_value is None:
        value = default
    else:
        try:
            value = int(raw_value)
        except ValueError:
            logging.warning(
                "%s=%r no es un entero valido; usando %s",
                name,
                raw_value,
                default,
            )
            value = default

    if minimum is not None:
        return max(minimum, value)

    return value


def parse_bool_env(name, default):
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default

    return raw_value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_csv_env(name, default):
    raw_value = os.environ.get(name, default)
    return [
        item.strip()
        for item in raw_value.split(",")
        if item.strip()
    ]


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", BASE_DIR / "queue.db"))
MUSIC_DIR = os.environ.get("MUSIC_DIR", "/music")
SPOTDL_OUTPUT_TEMPLATE = os.environ.get(
    "SPOTDL_OUTPUT_TEMPLATE",
    f"{MUSIC_DIR}/{{artist}}/{{album}}/{{title}}.{{output-ext}}",
)
SPOTDL_BITRATE = os.environ.get("SPOTDL_BITRATE", "192k")
SPOTDL_FORMAT = os.environ.get("SPOTDL_FORMAT", "mp3")
SPOTDL_THREADS = os.environ.get("SPOTDL_THREADS", "1")
SPOTDL_AUDIO_PROVIDERS = parse_csv_env("SPOTDL_AUDIO_PROVIDERS", "youtube-music,youtube")
SPOTDL_LOG_LEVEL = os.environ.get("SPOTDL_LOG_LEVEL", "INFO")
SPOTDL_LOG_TRACEBACKS = parse_bool_env("SPOTDL_LOG_TRACEBACKS", False)
SPOTDL_REPROCESS_MISSING_ONLY = parse_bool_env("SPOTDL_REPROCESS_MISSING_ONLY", False)
SPOTDL_MAX_ATTEMPTS = parse_int_env("SPOTDL_MAX_ATTEMPTS", 5, minimum=1)
SPOTDL_RETRY_DELAY_SECONDS = parse_int_env("SPOTDL_RETRY_DELAY_SECONDS", 120, minimum=0)
SPOTDL_RETRY_BACKOFF_FACTOR = parse_int_env("SPOTDL_RETRY_BACKOFF_FACTOR", 2, minimum=1)
SPOTDL_RETRY_MAX_DELAY_SECONDS = parse_int_env(
    "SPOTDL_RETRY_MAX_DELAY_SECONDS",
    900,
    minimum=0,
)
SPOTDL_RETRY_ALL_FAILURES = parse_bool_env("SPOTDL_RETRY_ALL_FAILURES", True)
SPOTDL_SPOTIFY_MAX_RETRIES = parse_int_env("SPOTDL_SPOTIFY_MAX_RETRIES", 5, minimum=1)
SPOTDL_SAVE_TIMEOUT_SECONDS = parse_int_env("SPOTDL_SAVE_TIMEOUT_SECONDS", 60, minimum=30)
SPOTDL_COOKIE_FILE = os.environ.get("SPOTDL_COOKIE_FILE", "").strip()
SPOTDL_YT_DLP_ARGS = os.environ.get("SPOTDL_YT_DLP_ARGS", "").strip()
WORKER_SLEEP_SECONDS = parse_int_env("WORKER_SLEEP_SECONDS", 5, minimum=1)
METADATA_WORKER_SLEEP_SECONDS = parse_int_env(
    "METADATA_WORKER_SLEEP_SECONDS",
    10,
    minimum=1,
)
SPOTIFY_METADATA_TIMEOUT = parse_int_env("SPOTIFY_METADATA_TIMEOUT", 8, minimum=1)
STATUS_VALUES = ("PENDING", "DOWNLOADING", "COMPLETED", "FAILED")
CONTROL_STATUS_VALUES = ("PAUSED", "CANCELED")
METADATA_STATUS_VALUES = ("PENDING", "FETCHING", "FETCHED", "FAILED")
DOWNLOADABLE_SPOTIFY_TYPES = {"track", "playlist", "album"}
SPOTIFY_ID_RE = re.compile(r"^[A-Za-z0-9]{10,}$")
SPOTDL_FOUND_RE = re.compile(r"\bFound\s+(\d+)\s+songs?\b", re.IGNORECASE)
SPOTDL_SUCCESS_RE = re.compile(
    r'^(?:Downloaded|Skipping)\s+"?(?P<title>.+?)"?(?::|\s+\(|$)',
    re.IGNORECASE,
)
SPOTIFY_TITLE_PATTERNS = (
    re.compile(r"^(?P<title>.+?) - (?:Album|Single|EP|Compilation) by (?P<artist>.+)$"),
    re.compile(r"^(?P<title>.+?) - song(?: and lyrics)? by (?P<artist>.+)$"),
    re.compile(r"^(?P<title>.+?) - playlist by (?P<artist>.+)$"),
)
SPOTIFY_METADATA_USER_AGENT = "spotube-downloader/1.0"
MAX_ERROR_LENGTH = 4000
TRANSIENT_SPOTDL_ERRORS = (
    "connection aborted",
    "connectionerror",
    "connection reset",
    "max retries exceeded",
    "read timed out",
    "readtimeout",
    "remotedisconnected",
    "temporarily unavailable",
    "timed out",
    "timeout",
)
NON_RETRYABLE_SPOTDL_ERRORS = (
    "cookies for the authentication",
    "cookies-from-browser",
    "download-deno",
    "install deno",
    "require deno",
    "sign in to confirm",
    "not a bot",
)
SPOTDL_ERROR_PATTERNS = (
    "audioprovidererror",
    "connectionerror",
    "cookies for the authentication",
    "cookies-from-browser",
    "download-deno",
    "httperror",
    "install deno",
    "not a bot",
    "read timed out",
    "readtimeout",
    "readtimeouterror",
    "remotedisconnected",
    "require deno",
    "sign in to confirm",
    "timed out",
    "timeouterror",
    "timeout",
)
SPOTDL_TRACEBACK_PREFIXES = ("╭", "╰", "│")
SPOTDL_TRACEBACK_MARKERS = (
    "traceback",
    "/opt/venv/",
    "/usr/local/lib/python",
    "site-packages",
)

app = Flask(__name__)
_worker_thread = None
_worker_lock = threading.Lock()
_metadata_thread = None
_metadata_lock = threading.Lock()
_active_processes = {}
_active_processes_lock = threading.Lock()


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def utc_after(seconds):
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        + timedelta(seconds=max(0, int(seconds or 0)))
    ).isoformat()


def get_connection():
    # Cada peticion/operacion usa su propia conexion: simple y seguro con threads.
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def init_db():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    with get_connection() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('PENDING', 'DOWNLOADING', 'COMPLETED', 'FAILED')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                next_attempt_at TEXT,
                return_code INTEGER,
                error TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                metadata_title TEXT,
                metadata_artist TEXT,
                metadata_type TEXT,
                metadata_image_url TEXT,
                metadata_status TEXT NOT NULL DEFAULT 'PENDING',
                metadata_error TEXT,
                metadata_updated_at TEXT,
                progress_current INTEGER NOT NULL DEFAULT 0,
                progress_total INTEGER NOT NULL DEFAULT 0,
                progress_title TEXT,
                missing_only INTEGER NOT NULL DEFAULT 0,
                control_status TEXT
            )
            """
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(downloads)").fetchall()
        }
        migrations = {
            "attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "next_attempt_at": "TEXT",
            "metadata_title": "TEXT",
            "metadata_artist": "TEXT",
            "metadata_type": "TEXT",
            "metadata_image_url": "TEXT",
            "metadata_status": "TEXT NOT NULL DEFAULT 'PENDING'",
            "metadata_error": "TEXT",
            "metadata_updated_at": "TEXT",
            "progress_current": "INTEGER NOT NULL DEFAULT 0",
            "progress_total": "INTEGER NOT NULL DEFAULT 0",
            "progress_title": "TEXT",
            "missing_only": "INTEGER NOT NULL DEFAULT 0",
            "control_status": "TEXT",
            "group_id": "TEXT",
            "group_title": "TEXT",
        }
        for column, definition in migrations.items():
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE downloads ADD COLUMN {column} {definition}"
                )
        connection.execute(
            """
            UPDATE downloads
               SET metadata_status = 'PENDING'
             WHERE metadata_status IS NULL
                OR metadata_status = 'FETCHING'
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_downloads_status_id ON downloads(status, id)"
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_downloads_metadata_status_id
                ON downloads(metadata_status, id)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS download_tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                download_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                title TEXT NOT NULL,
                artist TEXT,
                url TEXT,
                output_path TEXT,
                status TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(download_id, position),
                FOREIGN KEY(download_id) REFERENCES downloads(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_download_tracks_download_position
                ON download_tracks(download_id, position)
            """
        )


def reset_interrupted_downloads():
    # Si el contenedor se reinicia a media descarga, evita que una fila quede bloqueada para siempre.
    now = utc_now()
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE downloads
               SET status = 'PENDING',
                   updated_at = ?,
                   started_at = NULL,
                   finished_at = NULL,
                   next_attempt_at = NULL,
                   return_code = NULL,
                   error = 'Reencolado tras reinicio del servicio.',
                   progress_current = 0,
                   progress_total = 0,
                   progress_title = 'Pendiente'
             WHERE status = 'DOWNLOADING'
               AND control_status IS NULL
            """,
            (now,),
        )


class SpotifyMetadataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.metadata = {}

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return

        values = dict(attrs)
        key = values.get("property") or values.get("name")
        content = values.get("content")
        if key and content:
            self.metadata[key.lower()] = content


def get_spotify_item_type(value):
    parsed = urlparse(value.strip())

    if parsed.scheme == "spotify":
        parts = parsed.path.split(":")
        if len(parts) == 2:
            return parts[0]
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0].startswith("intl-"):
        parts = parts[1:]

    if parts:
        return parts[0]

    return None


def to_spotify_web_url(value):
    parsed = urlparse(value.strip())

    if parsed.scheme == "spotify":
        parts = parsed.path.split(":")
        if len(parts) == 2:
            return f"https://open.spotify.com/{parts[0]}/{parts[1]}"

    return value.strip()


def parse_spotify_title(raw_title):
    if not raw_title:
        return None, None

    clean_title = re.sub(r"\s*\|\s*Spotify$", "", raw_title).strip()
    for pattern in SPOTIFY_TITLE_PATTERNS:
        match = pattern.match(clean_title)
        if match:
            return match.group("title").strip(), match.group("artist").strip()

    return clean_title, None


def parse_spotify_description_artist(raw_description):
    if not raw_description:
        return None

    parts = [
        part.strip()
        for part in raw_description.replace("\u00b7", "|").replace("\u2022", "|").split("|")
        if part.strip()
    ]
    if not parts:
        return None

    return parts[0]


def fetch_spotify_metadata(url):
    web_url = to_spotify_web_url(url)
    request = url_request.Request(
        web_url,
        headers={
            "Accept-Language": "en",
            "User-Agent": SPOTIFY_METADATA_USER_AGENT,
        },
    )

    with url_request.urlopen(request, timeout=SPOTIFY_METADATA_TIMEOUT) as response:
        html = response.read().decode("utf-8", errors="replace")

    parser = SpotifyMetadataParser()
    parser.feed(html)
    raw_title = parser.metadata.get("og:title") or parser.metadata.get("twitter:title")
    raw_description = (
        parser.metadata.get("og:description")
        or parser.metadata.get("twitter:description")
    )
    title, artist = parse_spotify_title(raw_title)
    artist = artist or parse_spotify_description_artist(raw_description)

    return {
        "metadata_title": title,
        "metadata_artist": artist,
        "metadata_type": get_spotify_item_type(web_url),
        "metadata_image_url": (
            parser.metadata.get("og:image")
            or parser.metadata.get("twitter:image")
        ),
    }


def fetch_spotify_oembed_metadata(url):
    endpoint = "https://open.spotify.com/oembed?" + urlencode({"url": to_spotify_web_url(url)})
    request = url_request.Request(
        endpoint,
        headers={"User-Agent": SPOTIFY_METADATA_USER_AGENT},
    )

    with url_request.urlopen(request, timeout=SPOTIFY_METADATA_TIMEOUT) as response:
        payload = json.load(response)

    return {
        "metadata_title": payload.get("title"),
        "metadata_artist": None,
        "metadata_type": get_spotify_item_type(url),
        "metadata_image_url": payload.get("thumbnail_url"),
    }


def load_spotify_metadata(url):
    try:
        metadata = fetch_spotify_metadata(url)
    except Exception:
        app.logger.debug("No se pudieron leer metadatos OpenGraph de Spotify", exc_info=True)
        metadata = fetch_spotify_oembed_metadata(url)

    return {
        key: value
        for key, value in metadata.items()
        if value
    }


def is_valid_spotify_url(value):
    if not value:
        return False

    parsed = urlparse(value.strip())
    if parsed.scheme == "spotify":
        parts = parsed.path.split(":")
        return (
            len(parts) == 2
            and parts[0] in DOWNLOADABLE_SPOTIFY_TYPES
            and bool(SPOTIFY_ID_RE.match(parts[1]))
        )

    if parsed.scheme not in {"http", "https"}:
        return False

    if parsed.netloc.lower() != "open.spotify.com":
        return False

    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0].startswith("intl-"):
        parts = parts[1:]

    return (
        len(parts) >= 2
        and parts[0] in DOWNLOADABLE_SPOTIFY_TYPES
        and bool(SPOTIFY_ID_RE.match(parts[1]))
    )


def enqueue_download(url):
    now = utc_now()
    metadata_type = get_spotify_item_type(url)
    with get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO downloads (
                url, status, created_at, updated_at, metadata_type, metadata_status
            )
            VALUES (?, 'PENDING', ?, ?, ?, 'PENDING')
            """,
            (url, now, now, metadata_type),
        )
        return cursor.lastrowid


def enqueue_query(
    query,
    title=None,
    artist=None,
    image=None,
    metadata_type="track",
    group_id=None,
    group_title=None,
):
    """Encola una descarga a partir de una query de texto (resultado de catalogo).

    spotdl acepta 'artista - titulo' y busca la mejor coincidencia. Los metadatos
    (titulo/artista/caratula) se guardan ya rellenos desde el catalogo y se marca
    metadata_status='COMPLETED' para que el worker de metadatos no intente raspar
    Spotify (no hay URL que raspar). ``group_id``/``group_title`` agrupan las pistas
    de un mismo album en la cola.
    """
    now = utc_now()
    with get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO downloads (
                url, status, created_at, updated_at, metadata_type, metadata_status,
                metadata_title, metadata_artist, metadata_image_url, metadata_updated_at,
                group_id, group_title
            )
            VALUES (?, 'PENDING', ?, ?, ?, 'COMPLETED', ?, ?, ?, ?, ?, ?)
            """,
            (query, now, now, metadata_type, title, artist, image, now, group_id, group_title),
        )
        return cursor.lastrowid


def list_downloads(limit):
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, url,
                   status AS worker_status,
                   COALESCE(control_status, status) AS status,
                   control_status,
                   created_at, updated_at, started_at, finished_at,
                   next_attempt_at, return_code, error, attempt_count,
                   metadata_title, metadata_artist, metadata_type,
                   metadata_image_url, metadata_status, metadata_error,
                   metadata_updated_at,
                   progress_current, progress_total, progress_title,
                   group_id, group_title
             FROM downloads
             ORDER BY
                CASE COALESCE(control_status, status)
                    WHEN 'DOWNLOADING' THEN 0
                    WHEN 'PENDING' THEN 1
                    WHEN 'PAUSED' THEN 2
                    WHEN 'CANCELED' THEN 3
                    ELSE 4
                END,
                CASE
                    WHEN COALESCE(control_status, status) IN ('DOWNLOADING', 'PENDING') THEN id
                    ELSE -id
                END
             LIMIT ?
            """,
            (limit,),
        ).fetchall()

        items = [dict(row) for row in rows]
        download_ids = [item["id"] for item in items]
        tracks_by_download = {download_id: [] for download_id in download_ids}

        if download_ids:
            placeholders = ",".join("?" for _ in download_ids)
            track_rows = connection.execute(
                f"""
                SELECT download_id, position, title, artist, url,
                       status, error, updated_at
                  FROM download_tracks
                 WHERE download_id IN ({placeholders})
                 ORDER BY download_id, position
                """,
                download_ids,
            ).fetchall()
            for row in track_rows:
                track = dict(row)
                tracks_by_download[row["download_id"]].append(track)

    for item in items:
        item["tracks"] = tracks_by_download.get(item["id"], [])

    return items


def claim_next_job():
    # BEGIN IMMEDIATE evita que dos workers reclamen el mismo registro si cambia el despliegue.
    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT id, url, attempt_count, missing_only, error
             FROM downloads
             WHERE status = 'PENDING'
               AND control_status IS NULL
               AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
             ORDER BY id
             LIMIT 1
            """
            ,
            (utc_now(),),
        ).fetchone()

        if row is None:
            connection.commit()
            return None

        now = utc_now()
        next_attempt = row["attempt_count"] + 1
        connection.execute(
            """
            UPDATE downloads
               SET status = 'DOWNLOADING',
                   started_at = COALESCE(started_at, ?),
                   updated_at = ?,
                   error = NULL,
                   return_code = NULL,
                   next_attempt_at = NULL,
                   attempt_count = ?,
                   progress_current = 0,
                   progress_total = 0,
                   progress_title = 'Preparando descarga'
             WHERE id = ?
            """,
            (now, now, next_attempt, row["id"]),
        )
        connection.commit()
        job = dict(row)
        job["attempt_count"] = next_attempt
        return job
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def finish_job(job_id, status, return_code=None, error=None):
    if status not in STATUS_VALUES:
        raise ValueError(f"Estado no valido: {status}")

    now = utc_now()
    clean_error = error[-MAX_ERROR_LENGTH:] if error else None
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE downloads
               SET status = ?,
                   updated_at = ?,
                   finished_at = ?,
                   next_attempt_at = NULL,
                   return_code = ?,
                   error = ?,
                   control_status = NULL,
                   progress_current = CASE
                       WHEN ? = 'COMPLETED' AND progress_total > 0 THEN progress_total
                       ELSE progress_current
                   END,
                   progress_title = CASE
                       WHEN ? = 'COMPLETED' THEN NULL
                       ELSE progress_title
                   END
             WHERE id = ?
            """,
            (status, now, now, return_code, clean_error, status, status, job_id),
        )

    if status == "COMPLETED":
        on_job_completed(job_id)


def on_job_completed(job_id):
    """Al completar: si es una playlist, crea/actualiza la playlist en Navidrome
    (en segundo plano, esperando al escaneo); si no, solo dispara el escaneo."""
    with get_connection() as connection:
        row = connection.execute(
            "SELECT metadata_type, metadata_title FROM downloads WHERE id = ?",
            (job_id,),
        ).fetchone()

    if row and row["metadata_type"] == "playlist" and navidrome.enabled():
        threading.Thread(
            target=sync_navidrome_playlist,
            args=(job_id, row["metadata_title"]),
            daemon=True,
        ).start()
    else:
        navidrome.notify()  # nueva musica: escanea Navidrome (con debounce)


def sync_navidrome_playlist(job_id, name):
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT title, artist FROM download_tracks
             WHERE download_id = ? AND status IN ('COMPLETED', 'EXISTS')
             ORDER BY position
            """,
            (job_id,),
        ).fetchall()
    tracks = [{"title": row["title"], "artist": row["artist"]} for row in rows]
    if not tracks:
        logging.info("Playlist #%s sin pistas completadas; no se crea en Navidrome.", job_id)
        return
    navidrome.sync_playlist(name or "Playlist", tracks)


def update_job_progress(job_id, current=0, total=0, title=None):
    clean_title = title[:500] if title else None
    current = max(0, int(current or 0))
    total = max(0, int(total or 0))

    with get_connection() as connection:
        connection.execute(
            """
            UPDATE downloads
               SET updated_at = ?,
                   progress_current = ?,
                   progress_total = ?,
                   progress_title = ?
             WHERE id = ?
            """,
            (utc_now(), current, total, clean_title, job_id),
        )


def requeue_job(job_id, return_code, error, delay_seconds=0, missing_only=None):
    clean_error = error[-MAX_ERROR_LENGTH:] if error else None
    next_attempt_at = utc_after(delay_seconds) if delay_seconds else None
    progress_title = f"Reintento en {delay_seconds}s" if delay_seconds else "Esperando reintento"
    missing_only_sql = ""
    params = [
        utc_now(),
        return_code,
        clean_error,
        next_attempt_at,
        progress_title,
    ]
    if missing_only is not None:
        missing_only_sql = ", missing_only = ?"
        params.append(1 if missing_only else 0)
    params.append(job_id)

    with get_connection() as connection:
        connection.execute(
            """
            UPDATE downloads
               SET status = 'PENDING',
                   updated_at = ?,
                   finished_at = NULL,
                   return_code = ?,
                   error = ?,
                   next_attempt_at = ?,
                   control_status = NULL,
                   progress_current = 0,
                   progress_total = 0,
                   progress_title = ?
                   {missing_only_sql}
             WHERE id = ?
            """.format(missing_only_sql=missing_only_sql),
            params,
        )


def requeue_existing_job(job_id):
    now = utc_now()
    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE downloads
               SET status = 'PENDING',
                   updated_at = ?,
                   started_at = NULL,
                   finished_at = NULL,
                   next_attempt_at = NULL,
                   return_code = NULL,
                   error = NULL,
                   control_status = NULL,
                   attempt_count = 0,
                   missing_only = CASE
                       WHEN metadata_type = 'track' THEN 0
                       ELSE 1
                   END,
                   progress_current = 0,
                   progress_total = 0,
                   progress_title = 'Pendiente'
             WHERE id = ?
               AND (status IN ('FAILED', 'COMPLETED') OR control_status = 'CANCELED')
            """,
            (now, job_id),
        )
        return cursor.rowcount == 1


def get_effective_status(row):
    return row["control_status"] or row["status"]


def get_download_control_status(job_id):
    with get_connection() as connection:
        row = connection.execute(
            "SELECT control_status FROM downloads WHERE id = ?",
            (job_id,),
        ).fetchone()

    if row is None:
        return None

    return row["control_status"]


def register_active_process(job_id, process):
    with _active_processes_lock:
        _active_processes[job_id] = process


def unregister_active_process(job_id, process):
    with _active_processes_lock:
        if _active_processes.get(job_id) is process:
            _active_processes.pop(job_id, None)


def stop_process(process, grace_seconds=8):
    if process.poll() is not None:
        return False

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    except OSError:
        process.terminate()

    try:
        process.wait(timeout=grace_seconds)
        return True
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except OSError:
            process.kill()
        process.wait(timeout=5)
        return True


def stop_active_process(job_id):
    with _active_processes_lock:
        process = _active_processes.get(job_id)

    if process is None:
        return False

    return stop_process(process)


def set_control_status(job_id, control_status, allowed_statuses, progress_title):
    if control_status not in CONTROL_STATUS_VALUES:
        raise ValueError(f"Estado de control no valido: {control_status}")

    now = utc_now()
    with get_connection() as connection:
        row = connection.execute(
            "SELECT id, status, control_status FROM downloads WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return False, "No existe esa descarga.", False

        effective_status = get_effective_status(row)
        if effective_status not in allowed_statuses:
            return (
                False,
                f"No se puede aplicar esta accion a una descarga en estado {effective_status}.",
                False,
            )

        connection.execute(
            """
            UPDATE downloads
               SET control_status = ?,
                   updated_at = ?,
                   next_attempt_at = NULL,
                   progress_title = ?
             WHERE id = ?
            """,
            (control_status, now, progress_title, job_id),
        )
        return True, None, row["status"] == "DOWNLOADING"


def pause_download_job(job_id):
    ok, error, was_active = set_control_status(
        job_id,
        "PAUSED",
        {"PENDING", "DOWNLOADING"},
        "Pausado",
    )
    if ok and was_active:
        stop_active_process(job_id)
    return ok, error


def cancel_download_job(job_id):
    ok, error, was_active = set_control_status(
        job_id,
        "CANCELED",
        {"PENDING", "DOWNLOADING", "PAUSED"},
        "Cancelado",
    )
    if ok and was_active:
        stop_active_process(job_id)
    return ok, error


def resume_paused_job(job_id):
    now = utc_now()
    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE downloads
               SET status = 'PENDING',
                   control_status = NULL,
                   updated_at = ?,
                   started_at = NULL,
                   finished_at = NULL,
                   next_attempt_at = NULL,
                   return_code = NULL,
                   error = NULL,
                   missing_only = 1,
                   progress_title = 'Pendiente'
             WHERE id = ?
               AND control_status = 'PAUSED'
            """,
            (now, job_id),
        )
        return cursor.rowcount == 1


def delete_download_job(job_id):
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT id, status, control_status FROM downloads WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False, "No existe esa descarga."

            if row["status"] == "DOWNLOADING" and row["control_status"] is None:
                connection.rollback()
                return False, "Cancela o pausa la descarga antes de eliminarla."

            connection.execute(
                "DELETE FROM download_tracks WHERE download_id = ?",
                (job_id,),
            )
            connection.execute("DELETE FROM downloads WHERE id = ?", (job_id,))
            connection.commit()
            return True, None
        except Exception:
            connection.rollback()
            raise


def claim_next_metadata_job():
    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT id, url
              FROM downloads
             WHERE metadata_status = 'PENDING'
             ORDER BY id DESC
             LIMIT 1
            """
        ).fetchone()

        if row is None:
            connection.commit()
            return None

        connection.execute(
            """
            UPDATE downloads
               SET metadata_status = 'FETCHING',
                   metadata_error = NULL
             WHERE id = ?
            """,
            (row["id"],),
        )
        connection.commit()
        return dict(row)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def finish_metadata_job(job_id, metadata, error=None):
    now = utc_now()
    status = "FAILED" if error else "FETCHED"
    clean_error = error[-MAX_ERROR_LENGTH:] if error else None
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE downloads
               SET metadata_title = COALESCE(?, metadata_title),
                   metadata_artist = COALESCE(?, metadata_artist),
                   metadata_type = COALESCE(?, metadata_type),
                   metadata_image_url = COALESCE(?, metadata_image_url),
                   metadata_status = ?,
                   metadata_error = ?,
                   metadata_updated_at = ?
             WHERE id = ?
            """,
            (
                metadata.get("metadata_title") if metadata else None,
                metadata.get("metadata_artist") if metadata else None,
                metadata.get("metadata_type") if metadata else None,
                metadata.get("metadata_image_url") if metadata else None,
                status,
                clean_error,
                now,
                job_id,
            ),
        )


def is_transient_spotdl_error(output):
    text = (output or "").lower()
    return any(pattern in text for pattern in TRANSIENT_SPOTDL_ERRORS)


def is_non_retryable_spotdl_error(output):
    text = (output or "").lower()
    return any(pattern in text for pattern in NON_RETRYABLE_SPOTDL_ERRORS)


def should_retry_spotdl_failure(output):
    if is_non_retryable_spotdl_error(output):
        return False

    if SPOTDL_RETRY_ALL_FAILURES:
        return True

    return is_transient_spotdl_error(output)


def get_retry_delay(attempt):
    if SPOTDL_RETRY_DELAY_SECONDS <= 0:
        return 0

    delay = SPOTDL_RETRY_DELAY_SECONDS * (SPOTDL_RETRY_BACKOFF_FACTOR ** max(attempt - 1, 0))
    if SPOTDL_RETRY_MAX_DELAY_SECONDS > 0:
        return min(delay, SPOTDL_RETRY_MAX_DELAY_SECONDS)

    return delay


def build_retry_message(attempt, output):
    reason = "error transitorio" if is_transient_spotdl_error(output) else "fallo de descarga"
    delay = get_retry_delay(attempt)
    return (
        f"Intento {attempt}/{SPOTDL_MAX_ATTEMPTS} fallido por {reason}. "
        f"Reintentando en {delay}s.\n\n{output}"
    )


def new_spotdl_stats():
    return {
        "expected_count": None,
        "success_count": 0,
        "had_transient_error": False,
        "progress_items": [],
        "progress_item_index": 0,
    }


def update_spotdl_stats(stats, line):
    event = {}
    found_match = SPOTDL_FOUND_RE.search(line)
    if found_match:
        stats["expected_count"] = int(found_match.group(1))
        event["total"] = stats["expected_count"]

    if line.startswith("Downloaded ") or line.startswith("Skipping "):
        stats["success_count"] += 1
        event["completed"] = True
        title_match = SPOTDL_SUCCESS_RE.search(line)
        if title_match:
            event["title"] = title_match.group("title").strip().strip('"')

    if is_transient_spotdl_error(line):
        stats["had_transient_error"] = True

    return event or None


def get_song_progress_title(song):
    title = song.get("name") or song.get("title") or "Cancion sin titulo"
    artists = song.get("artists") or song.get("artist")

    if isinstance(artists, list):
        artist = ", ".join(str(item) for item in artists if item)
    else:
        artist = artists

    if artist:
        return f"{artist} - {title}"

    return title


def sync_spotdl_progress(job_id, stats, event):
    total = stats.get("expected_count") or 0
    progress_items = stats.get("progress_items") or []

    if event.get("total") and not progress_items:
        update_job_progress(
            job_id,
            current=stats.get("success_count", 0),
            total=event["total"],
            title="Preparando descarga",
        )

    if not event.get("completed"):
        return

    if progress_items:
        current_index = stats.get("progress_item_index", 0)
        if current_index < len(progress_items):
            current_item = progress_items[current_index]
            update_track_status(job_id, current_item["position"], "COMPLETED")

        next_index = current_index + 1
        stats["progress_item_index"] = next_index
        if next_index < len(progress_items):
            next_item = progress_items[next_index]
            update_track_status(job_id, next_item["position"], "DOWNLOADING")
            update_job_progress(
                job_id,
                current=next_item["position"],
                total=total,
                title=next_item["title"],
            )
            return

        update_job_progress(job_id, current=total, total=total, title=None)
        return

    update_job_progress(
        job_id,
        current=stats.get("success_count", 0),
        total=total,
        title=event.get("title"),
    )
    upsert_observed_track(
        job_id,
        stats.get("success_count", 0),
        event.get("title"),
        "COMPLETED",
    )


def is_spotdl_traceback_noise(line):
    stripped = line.strip()
    lowered = stripped.lower()
    return (
        stripped.startswith(SPOTDL_TRACEBACK_PREFIXES)
        or any(marker in lowered for marker in SPOTDL_TRACEBACK_MARKERS)
    )


def is_spotdl_error_line(line):
    lowered = line.lower()
    return any(pattern in lowered for pattern in SPOTDL_ERROR_PATTERNS)


def compact_spotdl_line(line):
    return (
        line.replace("│", "")
        .replace("╭", "")
        .replace("╰", "")
        .replace("─", "")
        .replace("❱", "")
        .strip()
    )


def build_spotdl_output(output_tail, error_tail):
    compact_errors = [
        compact_spotdl_line(line)
        for line in error_tail
        if compact_spotdl_line(line)
    ]
    if compact_errors:
        return "\n".join(compact_errors[-12:])

    return "\n".join(output_tail)


def prefer_audio_provider(providers, provider):
    if provider not in providers:
        return providers

    return [provider, *[item for item in providers if item != provider]]


def choose_audio_providers(previous_error=None):
    providers = list(SPOTDL_AUDIO_PROVIDERS)
    if len(providers) < 2 or not previous_error:
        return providers

    text = previous_error.lower()
    if "music.youtube.com" in text and is_transient_spotdl_error(text):
        return prefer_audio_provider(providers, "youtube")

    if "no results found for song" in text:
        return prefer_audio_provider(providers, "youtube-music")

    if "www.youtube.com" in text and is_transient_spotdl_error(text):
        return prefer_audio_provider(providers, "youtube-music")

    return providers


def build_spotdl_base_command(include_lyrics=True, audio_providers=None):
    audio_providers = audio_providers or SPOTDL_AUDIO_PROVIDERS
    command = [
        "spotdl",
        "--audio",
        *audio_providers,
    ]
    if not include_lyrics:
        # `spotdl save` busca letras por defecto; para calcular pendientes no hace falta.
        command.append("--lyrics")

    command.extend(
        [
            "--log-level",
            SPOTDL_LOG_LEVEL,
            "--max-retries",
            str(SPOTDL_SPOTIFY_MAX_RETRIES),
            "--threads",
            SPOTDL_THREADS,
        ]
    )
    if SPOTDL_COOKIE_FILE:
        if Path(SPOTDL_COOKIE_FILE).is_file():
            command.extend(["--cookie-file", SPOTDL_COOKIE_FILE])
        else:
            app.logger.warning(
                "SPOTDL_COOKIE_FILE=%s no existe; spotdl seguira sin cookies de YouTube",
                SPOTDL_COOKIE_FILE,
            )
    return command


def compact_spotdl_output(output):
    output_tail = deque(maxlen=24)
    error_tail = deque(maxlen=12)

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        if is_spotdl_error_line(line):
            error_tail.append(line)
        if SPOTDL_LOG_TRACEBACKS or not is_spotdl_traceback_noise(line):
            output_tail.append(line)

    return build_spotdl_output(output_tail, error_tail)


def run_spotdl_save(job_id, url, audio_providers=None):
    descriptor, save_path = tempfile.mkstemp(prefix=f"spotdl-{job_id}-", suffix=".spotdl")
    os.close(descriptor)
    path = Path(save_path)

    command = build_spotdl_base_command(
        include_lyrics=False,
        audio_providers=audio_providers,
    )
    command.extend(["save", url, "--save-file", str(path)])

    app.logger.info("Listando canciones spotdl #%s: %s", job_id, url)
    process = None
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        register_active_process(job_id, process)
        stdout, _ = process.communicate(timeout=SPOTDL_SAVE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        if process is not None:
            stop_process(process)
        path.unlink(missing_ok=True)
        return (
            124,
            None,
            f"Tiempo de espera agotado listando canciones con spotdl "
            f"({SPOTDL_SAVE_TIMEOUT_SECONDS}s).",
        )
    finally:
        if process is not None:
            unregister_active_process(job_id, process)

    output = compact_spotdl_output(stdout or "")
    if process.returncode != 0:
        path.unlink(missing_ok=True)
        return process.returncode, None, output or "No se pudo listar la descarga."

    try:
        songs = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        path.unlink(missing_ok=True)
        return 1, None, f"No se pudo leer la lista de canciones de spotdl: {exc}"

    path.unlink(missing_ok=True)
    if not isinstance(songs, list):
        return 1, None, "spotdl devolvio una lista de canciones no valida."

    return 0, [song for song in songs if isinstance(song, dict)], output


def get_song_output_path(song_data):
    from spotdl.types.song import Song
    from spotdl.utils.formatter import create_file_name

    return create_file_name(
        song=Song.from_dict(song_data),
        template=SPOTDL_OUTPUT_TEMPLATE,
        file_extension=SPOTDL_FORMAT,
    )


def audio_file_exists(path):
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def get_song_artist(song):
    artists = song.get("artists") or song.get("artist")

    if isinstance(artists, list):
        return ", ".join(str(item) for item in artists if item)

    return artists


def replace_download_tracks(job_id, track_rows):
    now = utc_now()
    with get_connection() as connection:
        connection.execute("DELETE FROM download_tracks WHERE download_id = ?", (job_id,))
        connection.executemany(
            """
            INSERT INTO download_tracks (
                download_id, position, title, artist, url, output_path,
                status, error, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            [
                (
                    job_id,
                    row["position"],
                    row["title"],
                    row.get("artist"),
                    row.get("url"),
                    row.get("output_path"),
                    row["status"],
                    now,
                    now,
                )
                for row in track_rows
            ],
        )


def update_track_status(job_id, position, status, error=None):
    clean_error = error[-MAX_ERROR_LENGTH:] if error else None
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE download_tracks
               SET status = ?,
                   error = ?,
                   updated_at = ?
             WHERE download_id = ?
               AND position = ?
            """,
            (status, clean_error, utc_now(), job_id, position),
        )


def upsert_observed_track(job_id, position, title, status):
    now = utc_now()
    clean_title = (title or f"Cancion {position}")[:500]
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO download_tracks (
                download_id, position, title, artist, url, output_path,
                status, error, created_at, updated_at
            )
            VALUES (?, ?, ?, NULL, NULL, NULL, ?, NULL, ?, ?)
            ON CONFLICT(download_id, position) DO UPDATE SET
                title = excluded.title,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (job_id, position, clean_title, status, now, now),
        )


def refresh_track_statuses_from_disk(job_id, stats, mark_missing_failed=False):
    expected_paths = stats.get("expected_paths") or []
    progress_items = stats.get("progress_items") or []
    path_by_position = {
        item["position"]: item["path"]
        for item in progress_items
        if item.get("path")
    }

    for position, output_path in path_by_position.items():
        if audio_file_exists(output_path):
            update_track_status(job_id, position, "COMPLETED")
        elif mark_missing_failed:
            update_track_status(job_id, position, "FAILED", "No se encontro el archivo descargado.")

    if expected_paths:
        return sum(1 for output_path in expected_paths if audio_file_exists(output_path))

    return None


def write_spotdl_save_file(songs):
    descriptor, save_path = tempfile.mkstemp(prefix="spotdl-missing-", suffix=".spotdl")
    os.close(descriptor)
    path = Path(save_path)
    path.write_text(
        json.dumps(songs, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )
    return path


def build_missing_only_plan(job_id, url, stats, audio_providers=None):
    return_code, songs, output = run_spotdl_save(
        job_id,
        url,
        audio_providers=audio_providers,
    )
    if return_code != 0:
        return {
            "return_code": return_code,
            "output": output,
        }

    songs = [song for song in songs if song.get("url")]
    if not songs:
        return {
            "return_code": 1,
            "output": "spotdl no encontro canciones descargables en la URL.",
        }

    expected_paths = []
    missing_songs = []
    progress_items = []
    track_rows = []
    for position, song in enumerate(songs, start=1):
        output_path = get_song_output_path(song)
        expected_paths.append(output_path)
        title = song.get("name") or song.get("title") or "Cancion sin titulo"
        exists = audio_file_exists(output_path)

        track_rows.append(
            {
                "position": position,
                "title": title,
                "artist": get_song_artist(song),
                "url": song.get("url"),
                "output_path": str(output_path),
                "status": "EXISTS" if exists else "PENDING",
            }
        )

        if not exists:
            missing_songs.append(song)
            progress_items.append(
                {
                    "position": position,
                    "title": get_song_progress_title(song),
                    "path": output_path,
                }
            )

    existing_count = len(expected_paths) - len(missing_songs)
    stats["expected_count"] = len(expected_paths)
    stats["success_count"] = existing_count
    stats["expected_paths"] = expected_paths
    stats["progress_items"] = progress_items
    stats["progress_item_index"] = 0
    replace_download_tracks(job_id, track_rows)

    return {
        "return_code": None,
        "missing_songs": missing_songs,
        "progress_items": progress_items,
        "existing_count": existing_count,
        "expected_count": len(expected_paths),
        "output": output,
    }


def get_spotdl_incomplete_reason(stats):
    expected_count = stats.get("expected_count")
    success_count = stats.get("success_count", 0)

    if expected_count is not None and success_count < expected_count:
        return f"spotdl solo completo {success_count}/{expected_count} canciones."

    if stats.get("had_transient_error"):
        return "spotdl devolvio codigo 0, pero registro un error de conexion."

    return None


def run_spotdl(job_id, url, missing_only=False, previous_error=None):
    stats = new_spotdl_stats()
    query = url
    missing_file = None
    plan_summary = None
    audio_providers = choose_audio_providers(previous_error)
    is_track_url = get_spotify_item_type(url) == "track"

    if SPOTDL_REPROCESS_MISSING_ONLY and missing_only and not is_track_url:
        update_job_progress(job_id, title="Leyendo canciones")
        plan = build_missing_only_plan(
            job_id,
            url,
            stats,
            audio_providers=audio_providers,
        )
        if plan["return_code"] is not None:
            return plan["return_code"], plan["output"], stats

        missing_songs = plan["missing_songs"]
        progress_items = plan["progress_items"]
        plan_summary = (
            f"Pendientes detectadas: {len(missing_songs)}/"
            f"{plan['expected_count']} canciones."
        )
        app.logger.info(
            "spotdl #%s: %s existentes=%s",
            job_id,
            plan_summary,
            plan["existing_count"],
        )

        if not missing_songs:
            stats["success_count"] = plan["expected_count"]
            update_job_progress(
                job_id,
                current=plan["expected_count"],
                total=plan["expected_count"],
                title=None,
            )
            return (
                0,
                f"No hay canciones pendientes: {plan['expected_count']}/"
                f"{plan['expected_count']} archivos ya existen.",
                stats,
            )

        first_item = progress_items[0]
        update_track_status(job_id, first_item["position"], "DOWNLOADING")
        update_job_progress(
            job_id,
            current=first_item["position"],
            total=plan["expected_count"],
            title=first_item["title"],
        )
        missing_file = write_spotdl_save_file(missing_songs)
        query = str(missing_file)
    else:
        update_job_progress(job_id, title="Preparando descarga")

    command = build_spotdl_base_command(audio_providers=audio_providers)
    command.extend(
        [
            "download",
            query,
            "--output",
            SPOTDL_OUTPUT_TEMPLATE,
            "--format",
            SPOTDL_FORMAT,
            "--bitrate",
            SPOTDL_BITRATE,
        ]
    )
    if SPOTDL_YT_DLP_ARGS:
        command.extend(["--yt-dlp-args", SPOTDL_YT_DLP_ARGS])

    output_tail = deque(maxlen=80)
    error_tail = deque(maxlen=24)

    app.logger.info(
        "Iniciando spotdl #%s proveedores=%s query=%s",
        job_id,
        ",".join(audio_providers),
        query,
    )
    try:
        # Guardamos solo las ultimas lineas para no consumir RAM con playlists grandes.
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        register_active_process(job_id, process)

        if process.stdout is not None:
            for line in process.stdout:
                line = line.rstrip()
                if line:
                    progress_event = update_spotdl_stats(stats, line)
                    if progress_event:
                        sync_spotdl_progress(job_id, stats, progress_event)

                    if is_spotdl_error_line(line):
                        error_tail.append(line)

                    if SPOTDL_LOG_TRACEBACKS or not is_spotdl_traceback_noise(line):
                        output_tail.append(line)
                        app.logger.info("spotdl #%s: %s", job_id, line)
                    elif is_spotdl_error_line(line):
                        app.logger.warning(
                            "spotdl #%s: %s",
                            job_id,
                            compact_spotdl_line(line),
                        )

        return_code = process.wait()

        expected_paths = stats.get("expected_paths")
        if expected_paths:
            completed_count = refresh_track_statuses_from_disk(
                job_id,
                stats,
                mark_missing_failed=return_code != 0,
            )
            if completed_count is not None:
                stats["success_count"] = completed_count

        output = build_spotdl_output(output_tail, error_tail)
        if plan_summary:
            output = "\n".join(part for part in (plan_summary, output) if part)

        return return_code, output, stats
    finally:
        if "process" in locals():
            unregister_active_process(job_id, process)
        if missing_file:
            missing_file.unlink(missing_ok=True)


def process_job(job):
    try:
        return_code, output, stats = run_spotdl(
            job["id"],
            job["url"],
            missing_only=bool(job.get("missing_only")),
            previous_error=job.get("error"),
        )
        control_status = get_download_control_status(job["id"])
        if control_status in CONTROL_STATUS_VALUES:
            app.logger.info(
                "spotdl #%s detenido por accion de usuario: %s",
                job["id"],
                control_status,
            )
            return

        attempt = job.get("attempt_count", 1)
        if return_code == 0:
            incomplete_reason = get_spotdl_incomplete_reason(stats)
            if incomplete_reason:
                message = f"{incomplete_reason}\n\n{output}" if output else incomplete_reason
                if attempt < SPOTDL_MAX_ATTEMPTS and should_retry_spotdl_failure(message):
                    retry_delay = get_retry_delay(attempt)
                    retry_message = build_retry_message(attempt, message)
                    requeue_job(
                        job["id"],
                        1,
                        retry_message,
                        retry_delay,
                        missing_only=get_spotify_item_type(job["url"]) != "track",
                    )
                    app.logger.warning(
                        "spotdl #%s: salida incompleta; reencolando intento=%s/%s espera=%ss",
                        job["id"],
                        attempt,
                        SPOTDL_MAX_ATTEMPTS,
                        retry_delay,
                    )
                    return

                refresh_track_statuses_from_disk(job["id"], stats, mark_missing_failed=True)
                finish_job(job["id"], "FAILED", return_code=1, error=message)
                app.logger.warning(
                    "spotdl #%s: salida incompleta tras agotar intentos",
                    job["id"],
                )
                return

            finish_job(job["id"], "COMPLETED", return_code=return_code)
            app.logger.info("Descarga completada: #%s", job["id"])
        else:
            message = output or f"spotdl termino con codigo {return_code}."
            if attempt < SPOTDL_MAX_ATTEMPTS and should_retry_spotdl_failure(message):
                retry_delay = get_retry_delay(attempt)
                retry_message = build_retry_message(attempt, message)
                requeue_job(job["id"], return_code, retry_message, retry_delay)
                app.logger.warning(
                    "spotdl #%s: reencolado tras fallo intento=%s/%s espera=%ss",
                    job["id"],
                    attempt,
                    SPOTDL_MAX_ATTEMPTS,
                    retry_delay,
                )
                return

            refresh_track_statuses_from_disk(job["id"], stats, mark_missing_failed=True)
            finish_job(job["id"], "FAILED", return_code=return_code, error=message)
            app.logger.warning("spotdl #%s: descarga fallida codigo=%s", job["id"], return_code)
    except FileNotFoundError:
        finish_job(
            job["id"],
            "FAILED",
            return_code=127,
            error="No se encontro el ejecutable spotdl en el contenedor o entorno local.",
        )
        app.logger.exception("spotdl no esta instalado o no esta en PATH")
    except Exception as exc:
        finish_job(job["id"], "FAILED", error=str(exc))
        app.logger.exception("Error procesando la descarga #%s", job["id"])


def worker_loop():
    # Un unico hilo procesa la cola secuencialmente para no saturar la Raspberry Pi.
    app.logger.info("Worker de descargas iniciado")
    while True:
        try:
            job = claim_next_job()
            if job is None:
                time.sleep(WORKER_SLEEP_SECONDS)
                continue

            process_job(job)
        except Exception:
            app.logger.exception("Error inesperado en el worker de descargas")
            time.sleep(WORKER_SLEEP_SECONDS)


def metadata_worker_loop():
    app.logger.info("Worker de metadatos iniciado")
    while True:
        try:
            job = claim_next_metadata_job()
            if job is None:
                time.sleep(METADATA_WORKER_SLEEP_SECONDS)
                continue

            try:
                metadata = load_spotify_metadata(job["url"])
                finish_metadata_job(job["id"], metadata)
                app.logger.info("Metadatos Spotify actualizados: #%s", job["id"])
            except Exception as exc:
                finish_metadata_job(job["id"], {}, error=str(exc))
                app.logger.warning(
                    "No se pudieron obtener metadatos Spotify para #%s: %s",
                    job["id"],
                    exc,
                )
        except Exception:
            app.logger.exception("Error inesperado en el worker de metadatos")
            time.sleep(METADATA_WORKER_SLEEP_SECONDS)


def start_worker_once():
    global _worker_thread

    with _worker_lock:
        if _worker_thread and _worker_thread.is_alive():
            return

        _worker_thread = threading.Thread(
            target=worker_loop,
            name="spotdl-worker",
            daemon=True,
        )
        _worker_thread.start()


def start_metadata_worker_once():
    global _metadata_thread

    with _metadata_lock:
        if _metadata_thread and _metadata_thread.is_alive():
            return

        _metadata_thread = threading.Thread(
            target=metadata_worker_loop,
            name="spotify-metadata-worker",
            daemon=True,
        )
        _metadata_thread.start()


@app.get("/")
def index():
    # base_path = prefijo publico bajo el proxy (p. ej. "/spotube"). El front lo
    # antepone a sus llamadas fetch; Traefik lo quita antes de llegar aqui.
    return render_template("index.html", base_path=os.environ.get("APP_BASE_PATH", "").rstrip("/"))


@app.post("/add")
def add_download():
    payload = request.get_json(silent=True) or request.form
    url = (payload.get("url") or "").strip()

    if not is_valid_spotify_url(url):
        return jsonify({"ok": False, "error": "Introduce un enlace valido de Spotify."}), 400

    download_id = enqueue_download(url)
    return jsonify({"ok": True, "id": download_id, "status": "PENDING"}), 201


@app.get("/search")
def search():
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"ok": False, "error": "Escribe algo que buscar."}), 400
    try:
        return jsonify({"ok": True, "results": deezer.search(query)})
    except deezer.CatalogError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/catalog/add")
def catalog_add():
    payload = request.get_json(silent=True) or {}
    kind = str(payload.get("kind", "track"))
    title = (payload.get("title") or "").strip() or None
    artist = (payload.get("artist") or "").strip() or None
    image = (payload.get("image") or "").strip() or None

    try:
        if kind == "album":
            album_id = payload.get("album_id")
            if album_id in (None, ""):
                return jsonify({"ok": False, "error": "Falta album_id."}), 400
            tracks = deezer.album_tracks(album_id)
            if not tracks:
                return jsonify({"ok": False, "error": "El album no tiene pistas."}), 400
            group_id = f"album-{album_id}-{utc_now()}"
            group_title = " - ".join(p for p in [artist, title] if p) or "Álbum"
            for track in tracks:
                enqueue_query(
                    track["query"], track["title"], track["artist"], image,
                    group_id=group_id, group_title=group_title,
                )
            return jsonify({"ok": True, "queued": len(tracks)}), 201

        query = (payload.get("query") or "").strip()
        if not query:
            return jsonify({"ok": False, "error": "Falta query."}), 400
        download_id = enqueue_query(query, title, artist, image)
        return jsonify({"ok": True, "queued": 1, "id": download_id}), 201
    except deezer.CatalogError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.get("/status")
def status():
    try:
        limit = int(request.args.get("limit", "100"))
    except ValueError:
        limit = 100

    limit = min(max(limit, 1), 200)
    return jsonify(
        {
            "items": list_downloads(limit),
            "config": {
                "spotdl_audio_providers": SPOTDL_AUDIO_PROVIDERS,
                "spotdl_reprocess_missing_only": SPOTDL_REPROCESS_MISSING_ONLY,
                "spotdl_max_attempts": SPOTDL_MAX_ATTEMPTS,
                "spotdl_retry_delay_seconds": SPOTDL_RETRY_DELAY_SECONDS,
                "spotdl_retry_backoff_factor": SPOTDL_RETRY_BACKOFF_FACTOR,
                "spotdl_retry_max_delay_seconds": SPOTDL_RETRY_MAX_DELAY_SECONDS,
                "spotdl_retry_all_failures": SPOTDL_RETRY_ALL_FAILURES,
                "spotdl_save_timeout_seconds": SPOTDL_SAVE_TIMEOUT_SECONDS,
            },
        }
    )


@app.post("/retry/<int:download_id>")
def retry_download(download_id):
    if requeue_existing_job(download_id):
        return jsonify({"ok": True, "id": download_id, "status": "PENDING"})

    return jsonify(
        {
            "ok": False,
            "error": "Solo se pueden reencolar descargas fallidas o completadas.",
        }
    ), 409


@app.post("/pause/<int:download_id>")
def pause_download(download_id):
    ok, error = pause_download_job(download_id)
    if ok:
        return jsonify({"ok": True, "id": download_id, "status": "PAUSED"})

    return jsonify({"ok": False, "error": error or "No se pudo pausar."}), 409


@app.post("/resume/<int:download_id>")
def resume_download(download_id):
    if resume_paused_job(download_id):
        return jsonify({"ok": True, "id": download_id, "status": "PENDING"})

    return jsonify(
        {
            "ok": False,
            "error": "Solo se pueden reanudar descargas pausadas.",
        }
    ), 409


@app.post("/cancel/<int:download_id>")
def cancel_download(download_id):
    ok, error = cancel_download_job(download_id)
    if ok:
        return jsonify({"ok": True, "id": download_id, "status": "CANCELED"})

    return jsonify({"ok": False, "error": error or "No se pudo cancelar."}), 409


@app.delete("/download/<int:download_id>")
def delete_download(download_id):
    ok, error = delete_download_job(download_id)
    if ok:
        return jsonify({"ok": True, "id": download_id, "status": "DELETED"})

    return jsonify({"ok": False, "error": error or "No se pudo eliminar."}), 409


init_db()
reset_interrupted_downloads()
start_worker_once()
start_metadata_worker_once()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
