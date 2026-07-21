from __future__ import annotations

import logging
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, render_template, request

import jellyfin
from anime_sources.base import (
    DownloadsDisabled,
    EpisodeItem,
    SourceError,
    StreamNotFound,
    sanitize_path_segment,
)
from anime_sources.registry import load_sources


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", BASE_DIR / "data" / "anime_queue.db"))
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", BASE_DIR / "downloads"))
PORT = int(os.environ.get("PORT", "8090"))
WORKER_SLEEP_SECONDS = max(1, int(os.environ.get("WORKER_SLEEP_SECONDS", "3")))
DOWNLOAD_CONCURRENCY = max(1, int(os.environ.get("DOWNLOAD_CONCURRENCY", "1")))
DOWNLOAD_CHUNK_SIZE = max(8192, int(os.environ.get("DOWNLOAD_CHUNK_SIZE", "1048576")))
PREFERRED_QUALITY = os.environ.get("PREFERRED_QUALITY", "").strip()
REQUEST_TIMEOUT_SECONDS = max(5, int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "30")))
DOWNLOAD_MAX_RETRIES = max(0, int(os.environ.get("DOWNLOAD_MAX_RETRIES", "5")))
# Reintentos cuando aun no se ha recibido ningun byte (el servidor no responde):
# se mantiene bajo para pivotar rapido a otro servidor en vez de insistir.
DOWNLOAD_START_RETRIES = max(0, int(os.environ.get("DOWNLOAD_START_RETRIES", "1")))
DOWNLOAD_RETRY_BACKOFF_SECONDS = max(0.0, float(os.environ.get("DOWNLOAD_RETRY_BACKOFF_SECONDS", "2")))
FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")
JOB_STATUS_VALUES = {"PENDING", "DOWNLOADING", "PAUSED", "CANCELED", "COMPLETED", "FAILED"}
ITEM_STATUS_VALUES = {"PENDING", "DOWNLOADING", "PAUSED", "CANCELED", "COMPLETED", "FAILED"}

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)

app = Flask(__name__)
SOURCES, SOURCE_LOAD_ERRORS = load_sources()
_worker_started = False
_worker_lock = threading.Lock()


class PausedDownload(RuntimeError):
    pass


class CanceledDownload(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_connection() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db() -> None:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with get_connection() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS download_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                anime_title TEXT NOT NULL,
                anime_url TEXT NOT NULL,
                job_type TEXT NOT NULL CHECK(job_type IN ('ANIME', 'EPISODE')),
                status TEXT NOT NULL CHECK (
                    status IN ('PENDING', 'DOWNLOADING', 'PAUSED', 'CANCELED', 'COMPLETED', 'FAILED')
                ),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                error TEXT,
                progress_current INTEGER NOT NULL DEFAULT 0,
                progress_total INTEGER NOT NULL DEFAULT 0
            )
            """,
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS download_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                source_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                anime_title TEXT NOT NULL,
                episode_title TEXT NOT NULL,
                episode_url TEXT NOT NULL,
                episode_number REAL,
                status TEXT NOT NULL CHECK (
                    status IN ('PENDING', 'DOWNLOADING', 'PAUSED', 'CANCELED', 'COMPLETED', 'FAILED')
                ),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                output_path TEXT,
                bytes_downloaded INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                UNIQUE(job_id, position),
                FOREIGN KEY(job_id) REFERENCES download_jobs(id) ON DELETE CASCADE
            )
            """,
        )
        # Migracion idempotente: marca de re-descarga forzada por episodio.
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(download_items)").fetchall()
        }
        if "force_redownload" not in columns:
            connection.execute(
                "ALTER TABLE download_items ADD COLUMN force_redownload INTEGER NOT NULL DEFAULT 0",
            )
        # Servidor/origen desde el que se completo la descarga (informativo).
        if "download_server" not in columns:
            connection.execute(
                "ALTER TABLE download_items ADD COLUMN download_server TEXT",
            )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_download_items_next
                ON download_items(status, job_id, position)
            """,
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_download_items_job_position
                ON download_items(job_id, position)
            """,
        )
        # Seguimiento de animes: guarda la base de episodios al seguir para
        # detectar capitulos nuevos. Incluye la fuente (servidor) para reconsultar.
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tracked_animes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                anime_title TEXT NOT NULL,
                anime_url TEXT NOT NULL,
                thumbnail_url TEXT,
                baseline_number REAL,
                baseline_count INTEGER NOT NULL DEFAULT 0,
                latest_number REAL,
                latest_title TEXT,
                episode_count INTEGER NOT NULL DEFAULT 0,
                new_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_checked_at TEXT,
                error TEXT,
                UNIQUE(source_id, anime_url)
            )
            """,
        )


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def require_source(source_id: str):
    source = SOURCES.get(source_id)
    if source is None:
        raise SourceError(f"Fuente no encontrada: {source_id}")
    return source


def require_download_source(source_id: str):
    source = require_source(source_id)
    if not source.allow_downloads:
        raise DownloadsDisabled(
            f"{source.name} no habilita descargas. Solo se puede listar episodios con esta fuente.",
        )
    return source


def ensure_within_download_dir(path: Path) -> Path:
    root = DOWNLOAD_DIR.resolve()
    target = path.resolve()
    if not target.is_relative_to(root):
        raise SourceError("La fuente intento escribir fuera de DOWNLOAD_DIR")
    return target


def update_item(item_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = utc_now()
    assignments = ", ".join(f"{key}=?" for key in fields)
    values = list(fields.values()) + [item_id]
    with get_connection() as connection:
        connection.execute(
            f"UPDATE download_items SET {assignments} WHERE id=?",
            values,
        )


def update_job(job_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = utc_now()
    assignments = ", ".join(f"{key}=?" for key in fields)
    values = list(fields.values()) + [job_id]
    with get_connection() as connection:
        connection.execute(
            f"UPDATE download_jobs SET {assignments} WHERE id=?",
            values,
        )


def get_job(job_id: int) -> dict[str, Any] | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM download_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    return row_to_dict(row) if row else None


def get_item(item_id: int) -> dict[str, Any] | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM download_items WHERE id=?",
            (item_id,),
        ).fetchone()
    return row_to_dict(row) if row else None


def sorted_episodes(episodes: list[EpisodeItem]) -> list[EpisodeItem]:
    return sorted(
        episodes,
        key=lambda episode: (
            episode.number if episode.number is not None else 10**9,
            episode.title,
        ),
    )


def create_download_job(
    source_id: str,
    anime_title: str,
    anime_url: str,
    episodes: list[EpisodeItem],
    job_type: str,
) -> int:
    if not episodes:
        raise SourceError("No hay episodios para encolar.")

    episodes = sorted_episodes(episodes)
    now = utc_now()
    with get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO download_jobs (
                source_id, anime_title, anime_url, job_type, status,
                created_at, updated_at, progress_total
            )
            VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?)
            """,
            (source_id, anime_title, anime_url, job_type, now, now, len(episodes)),
        )
        job_id = cursor.lastrowid
        for position, episode in enumerate(episodes, start=1):
            connection.execute(
                """
                INSERT INTO download_items (
                    job_id, source_id, position, anime_title, episode_title,
                    episode_url, episode_number, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
                """,
                (
                    job_id,
                    source_id,
                    position,
                    anime_title,
                    episode.title,
                    episode.url,
                    episode.number,
                    now,
                    now,
                ),
            )
    return job_id


def list_download_jobs(limit: int = 100) -> list[dict[str, Any]]:
    with get_connection() as connection:
        job_rows = connection.execute(
            """
            SELECT *
              FROM download_jobs
             ORDER BY
                CASE status
                    WHEN 'DOWNLOADING' THEN 0
                    WHEN 'PENDING' THEN 1
                    WHEN 'PAUSED' THEN 2
                    WHEN 'FAILED' THEN 3
                    WHEN 'CANCELED' THEN 4
                    ELSE 5
                END,
                CASE
                    WHEN status IN ('DOWNLOADING', 'PENDING', 'PAUSED') THEN id
                    ELSE -id
                END
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
        jobs = [row_to_dict(row) for row in job_rows]
        job_ids = [job["id"] for job in jobs]
        items_by_job = {job_id: [] for job_id in job_ids}

        if job_ids:
            placeholders = ",".join("?" for _ in job_ids)
            item_rows = connection.execute(
                f"""
                SELECT *
                  FROM download_items
                 WHERE job_id IN ({placeholders})
                 ORDER BY job_id, position
                """,
                job_ids,
            ).fetchall()
            for row in item_rows:
                item = row_to_dict(row)
                items_by_job[item["job_id"]].append(item)

    for job in jobs:
        job["items"] = items_by_job.get(job["id"], [])
    return jobs


def refresh_job_status(job_id: int) -> None:
    with get_connection() as connection:
        job = connection.execute(
            "SELECT status FROM download_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if job is None:
            return
        rows = connection.execute(
            """
            SELECT status, COUNT(*) AS count
              FROM download_items
             WHERE job_id=?
             GROUP BY status
            """,
            (job_id,),
        ).fetchall()
        counts = {row["status"]: row["count"] for row in rows}
        total = sum(counts.values())
        completed = counts.get("COMPLETED", 0)
        now = utc_now()

        if job["status"] == "PAUSED":
            next_status = "PAUSED"
        elif job["status"] == "CANCELED":
            next_status = "CANCELED"
        elif counts.get("DOWNLOADING", 0):
            next_status = "DOWNLOADING"
        elif counts.get("PENDING", 0):
            next_status = "PENDING"
        elif counts.get("PAUSED", 0):
            next_status = "PAUSED"
        elif total and completed == total:
            next_status = "COMPLETED"
        elif counts.get("CANCELED", 0) == total:
            next_status = "CANCELED"
        else:
            next_status = "FAILED"

        finished_at = now if next_status in {"COMPLETED", "FAILED", "CANCELED"} else None
        connection.execute(
            """
            UPDATE download_jobs
               SET status=?,
                   updated_at=?,
                   finished_at=COALESCE(?, finished_at),
                   progress_current=?,
                   progress_total=?
             WHERE id=?
            """,
            (next_status, now, finished_at, completed, total, job_id),
        )


def claim_next_item() -> dict[str, Any] | None:
    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT i.*, j.status AS job_status, j.anime_url, j.job_type
              FROM download_items i
              JOIN download_jobs j ON j.id = i.job_id
             WHERE i.status='PENDING'
               AND j.status IN ('PENDING', 'DOWNLOADING')
             ORDER BY j.id ASC, i.position ASC
             LIMIT 1
            """,
        ).fetchone()
        if row is None:
            connection.commit()
            return None

        now = utc_now()
        connection.execute(
            """
            UPDATE download_jobs
               SET status='DOWNLOADING',
                   started_at=COALESCE(started_at, ?),
                   updated_at=?
             WHERE id=?
            """,
            (now, now, row["job_id"]),
        )
        connection.execute(
            """
            UPDATE download_items
               SET status='DOWNLOADING',
                   started_at=COALESCE(started_at, ?),
                   finished_at=NULL,
                   bytes_downloaded=0,
                   total_bytes=0,
                   error=NULL,
                   download_server=NULL,
                   updated_at=?
             WHERE id=?
            """,
            (now, now, row["id"]),
        )
        connection.commit()
        return row_to_dict(row)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def check_control_state(item_id: int) -> None:
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT i.status AS item_status, j.status AS job_status
              FROM download_items i
              JOIN download_jobs j ON j.id = i.job_id
             WHERE i.id=?
            """,
            (item_id,),
        ).fetchone()
    if row is None:
        raise CanceledDownload("Registro eliminado durante la descarga.")
    if row["job_status"] == "PAUSED" or row["item_status"] == "PAUSED":
        raise PausedDownload("Descarga pausada por el usuario.")
    if row["job_status"] == "CANCELED" or row["item_status"] == "CANCELED":
        raise CanceledDownload("Descarga cancelada por el usuario.")


_RESOLUTION_RE = re.compile(r"(\d{3,4})\s*p", re.IGNORECASE)

# Orden de preferencia de servidores (el primero se intenta antes). Mediafire da
# un MP4 directo y fiable; los hosts mas lentos o con HLS quedan despues. Se puede
# sobreescribir con ANIME_DOWNLOAD_SERVER_PRIORITY (lista separada por comas).
DOWNLOAD_SERVER_PRIORITY = [
    name.strip().lower()
    for name in (
        os.environ.get("ANIME_DOWNLOAD_SERVER_PRIORITY", "").strip()
        or "mediafire,streamtape,okru,mp4upload,voe,filemoon,streamwish,mixdrop,doodstream"
    ).split(",")
    if name.strip()
]


def _stream_quality_score(stream) -> int:
    match = _RESOLUTION_RE.search(f"{stream.label} {stream.quality}")
    return int(match.group(1)) if match else 0


def _server_priority(stream) -> int:
    """Mayor valor = se intenta antes. 0 si el servidor no esta en la lista."""
    haystack = f"{stream.label} {stream.quality}".lower()
    total = len(DOWNLOAD_SERVER_PRIORITY)
    for index, name in enumerate(DOWNLOAD_SERVER_PRIORITY):
        if name in haystack:
            return total - index
    return 0


def order_streams(streams: list) -> list:
    """Ordena los streams por: calidad preferida, prioridad de servidor y, por
    ultimo, mayor resolucion. El orden define la secuencia de servidores a probar.
    """
    preferred = (PREFERRED_QUALITY or "").lower()

    def sort_key(stream):
        haystack = f"{stream.label} {stream.quality}".lower()
        is_preferred = 1 if preferred and preferred in haystack else 0
        return (is_preferred, _server_priority(stream), _stream_quality_score(stream))

    return sorted(streams, key=sort_key, reverse=True)


def find_existing_download(item: dict[str, Any]) -> Path | None:
    """Devuelve el fichero ya descargado para este episodio, si existe.

    Busca en ``DOWNLOAD_DIR/{anime}/`` un fichero con el mismo nombre base que el
    episodio (cualquier extension, ignorando ``.part`` y ficheros vacios). Asi un
    capitulo ya bajado no se vuelve a descargar salvo que se fuerce.
    """
    anime_dir = sanitize_path_segment(item.get("anime_title") or "anime")
    episode = sanitize_path_segment(item.get("episode_title") or "episode")
    folder = DOWNLOAD_DIR / anime_dir
    if not folder.is_dir():
        return None
    for candidate in folder.iterdir():
        if (
            candidate.is_file()
            and candidate.suffix.lower() != ".part"
            and candidate.stem == episode
        ):
            try:
                if candidate.stat().st_size > 0:
                    return candidate
            except OSError:
                continue
    return None


def download_item(item: dict[str, Any]) -> None:
    source = require_download_source(item["source_id"])

    # Si el fichero destino ya existe, el capitulo ya esta descargado: se omite
    # (y se evita resolver streams) salvo que se haya pedido forzar la descarga.
    if not item.get("force_redownload"):
        existing = find_existing_download(item)
        if existing is not None:
            size = existing.stat().st_size
            logging.info("Episodio ya descargado, se omite: %s", existing)
            update_item(
                item["id"],
                status="COMPLETED",
                output_path=str(existing),
                bytes_downloaded=size,
                total_bytes=size,
                finished_at=utc_now(),
                error=None,
                force_redownload=0,
                download_server="Ya en disco",
            )
            refresh_job_status(item["job_id"])
            return

    errors: list[str] = []

    # Via primaria (ej. descarga directa). Si todos sus candidatos fallan, se
    # prueba la via de fallback (ej. extractores), que se resuelve solo entonces.
    primary = source.get_video_streams(item["episode_url"])
    if try_download_candidates(item, source, primary, errors):
        return

    fallback = source.get_video_streams_fallback(item["episode_url"])
    if fallback and try_download_candidates(item, source, fallback, errors):
        return

    if not errors:
        raise StreamNotFound("La fuente no devolvio ninguna URL HTTP descargable.")
    raise StreamNotFound("Todos los servidores fallaron. " + " | ".join(errors[:8]))


def try_download_candidates(
    item: dict[str, Any],
    source,
    streams: list,
    errors: list[str],
) -> bool:
    """Intenta descargar la primera fuente que funcione, pivotando entre servidores.

    Devuelve True si alguna descarga se completo. Acumula los fallos en ``errors``.
    """
    candidates = [
        stream
        for stream in order_streams(streams)
        if stream.url.startswith(("http://", "https://"))
    ]

    failed_hosts: set[str] = set()
    for stream in candidates:
        host = urlparse(stream.url).netloc
        # Solo saltamos un host completo si ya cayo por error de conexion; un
        # fallo puntual (404/contenido) de un enlace no descarta a sus vecinos.
        if host and host in failed_hosts:
            continue
        try:
            download_stream_to_disk(item, source, stream)
            logging.info(
                "Descargado '%s' desde %s (%s)",
                item.get("episode_title", item["id"]),
                stream.label or "desconocido",
                host or stream.url,
            )
            return True
        except (PausedDownload, CanceledDownload):
            raise
        except Exception as exc:
            if host and isinstance(exc, RETRYABLE_NETWORK_ERRORS):
                failed_hosts.add(host)
            label = stream.label or host or "servidor"
            errors.append(f"{label}: {exc}")
            logging.warning("Servidor '%s' fallo, probando siguiente: %s", label, exc)
            continue

    return False


def download_stream_to_disk(item: dict[str, Any], source, stream) -> None:
    """Descarga un unico stream (HLS o directo) y marca el item como completado.

    Limpia su fichero parcial ante cualquier error para que el siguiente
    servidor (o un reintento) empiece limpio.
    """
    output_path = ensure_within_download_dir(
        source.build_filename(
            item["anime_title"],
            item["episode_title"],
            stream,
            DOWNLOAD_DIR,
        ),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".part")

    try:
        if is_hls_stream(stream):
            download_hls_stream(item, stream, output_path, temp_path)
            return

        bytes_downloaded = download_direct_stream(item, stream, output_path, temp_path)

        temp_path.replace(output_path)
        update_item(
            item["id"],
            status="COMPLETED",
            bytes_downloaded=bytes_downloaded,
            total_bytes=bytes_downloaded if bytes_downloaded else 0,
            output_path=str(output_path),
            finished_at=utc_now(),
            error=None,
            force_redownload=0,
            download_server=stream.label or "desconocido",
        )
        refresh_job_status(item["job_id"])
    except Exception:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise


RETRYABLE_NETWORK_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def download_direct_stream(
    item: dict[str, Any],
    stream,
    output_path: Path,
    temp_path: Path,
) -> int:
    """Descarga un stream HTTP directo de forma reanudable.

    Los hosts de video (mp4upload, etc.) suelen servir lento y cortar la
    conexion a mitad de fichero. Ante un timeout o corte de red se reintenta
    con `Range: bytes=N-` desde lo ya escrito, evitando volver a empezar. Las
    excepciones de control (pausa/cancelacion) se propagan sin reintento.
    """
    bytes_downloaded = 0
    total_bytes = 0
    last_update = 0.0
    attempt = 0

    while True:
        request_headers = dict(stream.headers)
        resuming = bytes_downloaded > 0
        if resuming:
            request_headers["Range"] = f"bytes={bytes_downloaded}-"

        try:
            with requests.get(
                stream.url,
                headers=request_headers,
                stream=True,
                timeout=(10, REQUEST_TIMEOUT_SECONDS),
            ) as response:
                response.raise_for_status()

                # Si pedimos un rango pero el servidor responde 200, no soporta
                # reanudacion: reiniciamos desde cero.
                if resuming and response.status_code != 206:
                    bytes_downloaded = 0
                    resuming = False

                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type and not (
                    content_type.startswith("video/")
                    or content_type in {"application/octet-stream", "binary/octet-stream"}
                    or content_type == stream.mime_type
                ):
                    raise SourceError(
                        f"Tipo de contenido inesperado para descarga directa: {content_type}",
                    )

                if not resuming:
                    total_bytes = int(response.headers.get("content-length") or stream.size_bytes or 0)
                    update_item(item["id"], total_bytes=total_bytes, output_path=str(output_path))

                mode = "ab" if resuming else "wb"
                with temp_path.open(mode) as file_obj:
                    for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                        if not chunk:
                            continue
                        check_control_state(item["id"])
                        file_obj.write(chunk)
                        bytes_downloaded += len(chunk)
                        now = time.monotonic()
                        if now - last_update > 1:
                            update_item(item["id"], bytes_downloaded=bytes_downloaded)
                            last_update = now

            update_item(item["id"], bytes_downloaded=bytes_downloaded)
            return bytes_downloaded

        except RETRYABLE_NETWORK_ERRORS as exc:
            attempt += 1
            # Con datos ya recibidos reanudamos con el presupuesto completo; si
            # aun no hay bytes, el servidor no responde y fallamos rapido para
            # pivotar a otro servidor (mediafire, etc.) en vez de insistir.
            max_retries = DOWNLOAD_MAX_RETRIES if bytes_downloaded > 0 else DOWNLOAD_START_RETRIES
            if attempt > max_retries:
                raise
            logging.warning(
                "Reintentando descarga (%s/%s) de %s tras %d bytes: %s",
                attempt,
                max_retries,
                item.get("episode_title", item["id"]),
                bytes_downloaded,
                exc,
            )
            if DOWNLOAD_RETRY_BACKOFF_SECONDS:
                # Respeta pausa/cancelacion durante la espera de backoff.
                check_control_state(item["id"])
                time.sleep(DOWNLOAD_RETRY_BACKOFF_SECONDS * attempt)
                check_control_state(item["id"])


def is_hls_stream(stream) -> bool:
    return (
        ".m3u8" in stream.url.lower()
        or "mpegurl" in (stream.mime_type or "").lower()
        or "hls" in (stream.label or "").lower()
    )


def download_hls_stream(
    item: dict[str, Any],
    stream,
    output_path: Path,
    temp_path: Path,
) -> None:
    headers = "".join(f"{key}: {value}\r\n" for key, value in stream.headers.items())
    command = [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
    ]
    if headers:
        command.extend(["-headers", headers])
    command.extend(
        [
            "-i",
            stream.url,
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            str(temp_path),
        ],
    )

    update_item(item["id"], total_bytes=0, output_path=str(output_path))
    stderr_path = None
    with tempfile.NamedTemporaryFile("w+", delete=False) as stderr_file:
        stderr_path = stderr_file.name
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
            text=True,
        )
        try:
            while process.poll() is None:
                check_control_state(item["id"])
                time.sleep(1)
        except (PausedDownload, CanceledDownload):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            raise

    stderr_tail: list[str] = []
    if stderr_path:
        try:
            stderr_tail = Path(stderr_path).read_text(errors="replace").splitlines()[-20:]
        finally:
            Path(stderr_path).unlink(missing_ok=True)

    if process.returncode != 0:
        message = "\n".join(stderr_tail).strip() or f"ffmpeg termino con codigo {process.returncode}."
        raise SourceError(message)

    temp_path.replace(output_path)
    bytes_downloaded = output_path.stat().st_size if output_path.exists() else 0
    update_item(
        item["id"],
        status="COMPLETED",
        bytes_downloaded=bytes_downloaded,
        total_bytes=bytes_downloaded,
        output_path=str(output_path),
        finished_at=utc_now(),
        error=None,
        force_redownload=0,
        download_server=stream.label or "desconocido",
    )
    refresh_job_status(item["job_id"])


def cleanup_part_file(item: dict[str, Any]) -> None:
    stored = get_item(item["id"])
    if not stored or not stored.get("output_path"):
        return
    part_path = Path(stored["output_path"] + ".part")
    if part_path.exists():
        part_path.unlink()


def process_item(item: dict[str, Any]) -> None:
    try:
        download_item(item)
        jellyfin.notify()  # descarga OK: refresca la biblioteca de Jellyfin (con debounce)
    except PausedDownload as exc:
        cleanup_part_file(item)
        update_item(item["id"], status="PAUSED", error=str(exc))
        refresh_job_status(item["job_id"])
    except CanceledDownload as exc:
        cleanup_part_file(item)
        update_item(item["id"], status="CANCELED", finished_at=utc_now(), error=str(exc))
        refresh_job_status(item["job_id"])
    except Exception as exc:
        cleanup_part_file(item)
        logging.exception("Download failed: %s", item["episode_title"])
        update_item(item["id"], status="FAILED", finished_at=utc_now(), error=str(exc)[:1200])
        refresh_job_status(item["job_id"])


def pause_job(job_id: int) -> tuple[bool, str | None]:
    job = get_job(job_id)
    if not job:
        return False, "Trabajo no encontrado."
    if job["status"] not in {"PENDING", "DOWNLOADING"}:
        return False, "Solo se puede pausar un trabajo pendiente o activo."

    with get_connection() as connection:
        connection.execute(
            """
            UPDATE download_jobs
               SET status='PAUSED', updated_at=?
             WHERE id=?
            """,
            (utc_now(), job_id),
        )
        connection.execute(
            """
            UPDATE download_items
               SET status='PAUSED', updated_at=?
             WHERE job_id=? AND status='PENDING'
            """,
            (utc_now(), job_id),
        )
    return True, None


def resume_job(job_id: int) -> tuple[bool, str | None]:
    job = get_job(job_id)
    if not job:
        return False, "Trabajo no encontrado."
    if job["status"] != "PAUSED":
        return False, "Solo se puede reanudar un trabajo pausado."

    with get_connection() as connection:
        now = utc_now()
        connection.execute(
            "UPDATE download_jobs SET status='PENDING', updated_at=? WHERE id=?",
            (now, job_id),
        )
        connection.execute(
            """
            UPDATE download_items
               SET status='PENDING', updated_at=?
             WHERE job_id=? AND status='PAUSED'
            """,
            (now, job_id),
        )
    return True, None


def cancel_job(job_id: int) -> tuple[bool, str | None]:
    job = get_job(job_id)
    if not job:
        return False, "Trabajo no encontrado."
    if job["status"] == "COMPLETED":
        return False, "Un trabajo completado no se cancela; elimina el registro si quieres ocultarlo."

    with get_connection() as connection:
        now = utc_now()
        connection.execute(
            """
            UPDATE download_jobs
               SET status='CANCELED', finished_at=?, updated_at=?
             WHERE id=?
            """,
            (now, now, job_id),
        )
        connection.execute(
            """
            UPDATE download_items
               SET status='CANCELED', finished_at=?, updated_at=?, error='Cancelado por el usuario'
             WHERE job_id=? AND status IN ('PENDING', 'DOWNLOADING', 'PAUSED')
            """,
            (now, now, job_id),
        )
    return True, None


def retry_job(job_id: int) -> tuple[bool, str | None]:
    job = get_job(job_id)
    if not job:
        return False, "Trabajo no encontrado."
    if job["status"] not in {"FAILED", "CANCELED", "COMPLETED"}:
        return False, "Solo se puede reintentar un trabajo fallido, cancelado o completado."

    with get_connection() as connection:
        now = utc_now()
        reset_all = job["status"] == "COMPLETED"
        item_filter = "" if reset_all else "AND status IN ('FAILED', 'CANCELED', 'PAUSED')"
        connection.execute(
            """
            UPDATE download_jobs
               SET status='PENDING',
                   started_at=NULL,
                   finished_at=NULL,
                   error=NULL,
                   progress_current=0,
                   updated_at=?
             WHERE id=?
            """,
            (now, job_id),
        )
        connection.execute(
            f"""
            UPDATE download_items
               SET status='PENDING',
                   started_at=NULL,
                   finished_at=NULL,
                   bytes_downloaded=0,
                   total_bytes=0,
                   error=NULL,
                   updated_at=?
             WHERE job_id=? {item_filter}
            """,
            (now, job_id),
        )
    refresh_job_status(job_id)
    return True, None


def retry_item(item_id: int, force: bool = False) -> tuple[bool, str | None]:
    """Reintenta un episodio concreto dentro de un grupo, de forma independiente.

    Lo deja PENDING y reactiva el trabajo si estaba terminado/pausado para que el
    worker lo recoja. Con ``force`` se vuelve a descargar aunque el fichero exista.
    """
    item = get_item(item_id)
    if not item:
        return False, "Episodio no encontrado."
    if item["status"] == "DOWNLOADING":
        return False, "El episodio ya se esta descargando."

    now = utc_now()
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE download_items
               SET status='PENDING',
                   started_at=NULL,
                   finished_at=NULL,
                   bytes_downloaded=0,
                   total_bytes=0,
                   error=NULL,
                   force_redownload=?,
                   updated_at=?
             WHERE id=?
            """,
            (1 if force else 0, now, item_id),
        )
        # Reactiva el trabajo si estaba en un estado terminal o pausado, para que
        # el worker pueda volver a procesar este episodio.
        connection.execute(
            """
            UPDATE download_jobs
               SET status='PENDING',
                   finished_at=NULL,
                   error=NULL,
                   updated_at=?
             WHERE id=? AND status IN ('FAILED', 'CANCELED', 'COMPLETED', 'PAUSED')
            """,
            (now, item["job_id"]),
        )
    refresh_job_status(item["job_id"])
    return True, None


def delete_job(job_id: int) -> tuple[bool, str | None]:
    job = get_job(job_id)
    if not job:
        return False, "Trabajo no encontrado."
    if job["status"] == "DOWNLOADING":
        return False, "Pausa o cancela el trabajo antes de eliminarlo."

    with get_connection() as connection:
        connection.execute("DELETE FROM download_jobs WHERE id=?", (job_id,))
    return True, None


# ============================ Seguimiento de animes ============================
def _episode_stats(episodes: list[EpisodeItem]) -> tuple[int, float | None]:
    """Devuelve (numero de episodios, numero del ultimo episodio)."""
    numbers = [ep.number for ep in episodes if ep.number is not None]
    return len(episodes), (max(numbers) if numbers else None)


def _count_new_episodes(episodes: list[EpisodeItem], baseline_number, baseline_count) -> int:
    if baseline_number is not None:
        return sum(
            1 for ep in episodes if ep.number is not None and ep.number > baseline_number
        )
    return max(0, len(episodes) - (baseline_count or 0))


def get_tracking(track_id: int) -> dict[str, Any] | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM tracked_animes WHERE id=?",
            (track_id,),
        ).fetchone()
    return row_to_dict(row) if row else None


def list_tracking() -> list[dict[str, Any]]:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT * FROM tracked_animes
             ORDER BY (new_count > 0) DESC, new_count DESC, anime_title ASC
            """,
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def add_tracking(source_id: str, anime_url: str, anime_title: str) -> dict[str, Any]:
    source = require_source(source_id)
    anime_url = source.absolute_url(anime_url)

    title = anime_title.strip()
    thumbnail = ""
    try:
        info = source.details(anime_url)
        title = title or info.title
        thumbnail = info.thumbnail_url or ""
    except Exception as exc:  # noqa: BLE001 - los detalles son opcionales
        logging.info("Seguimiento: no se pudieron leer detalles de %s: %s", anime_url, exc)

    episodes = source.episodes(anime_url)
    count, last = _episode_stats(episodes)
    if not title:
        title = anime_url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").strip() or anime_url

    now = utc_now()
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO tracked_animes (
                source_id, anime_title, anime_url, thumbnail_url,
                baseline_number, baseline_count, latest_number, latest_title,
                episode_count, new_count, created_at, updated_at, last_checked_at, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, NULL)
            ON CONFLICT(source_id, anime_url) DO UPDATE SET
                anime_title=excluded.anime_title,
                thumbnail_url=excluded.thumbnail_url,
                baseline_number=excluded.baseline_number,
                baseline_count=excluded.baseline_count,
                latest_number=excluded.latest_number,
                latest_title=excluded.latest_title,
                episode_count=excluded.episode_count,
                new_count=0,
                updated_at=excluded.updated_at,
                last_checked_at=excluded.last_checked_at,
                error=NULL
            """,
            (
                source_id, title, anime_url, thumbnail,
                last, count, last, title,
                count, now, now, now,
            ),
        )
    return {"ok": True}


def refresh_tracking() -> list[dict[str, Any]]:
    now = utc_now()
    for row in list_tracking():
        source = SOURCES.get(row["source_id"])
        if source is None:
            update_tracking(row["id"], error="Fuente no disponible", last_checked_at=now)
            continue
        try:
            episodes = source.episodes(row["anime_url"])
        except Exception as exc:  # noqa: BLE001 - error por anime, no global
            update_tracking(row["id"], error=str(exc)[:300], last_checked_at=now)
            continue

        count, last = _episode_stats(episodes)
        new_count = _count_new_episodes(episodes, row["baseline_number"], row["baseline_count"])
        update_tracking(
            row["id"],
            latest_number=last,
            episode_count=count,
            new_count=new_count,
            last_checked_at=now,
            error=None,
        )
    return list_tracking()


def update_tracking(track_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = utc_now()
    assignments = ", ".join(f"{key}=?" for key in fields)
    with get_connection() as connection:
        connection.execute(
            f"UPDATE tracked_animes SET {assignments} WHERE id=?",
            list(fields.values()) + [track_id],
        )


def mark_tracking_seen(track_id: int) -> tuple[bool, str | None]:
    row = get_tracking(track_id)
    if not row:
        return False, "Seguimiento no encontrado."
    update_tracking(
        track_id,
        baseline_number=row["latest_number"],
        baseline_count=row["episode_count"],
        new_count=0,
    )
    return True, None


def delete_tracking(track_id: int) -> tuple[bool, str | None]:
    with get_connection() as connection:
        connection.execute("DELETE FROM tracked_animes WHERE id=?", (track_id,))
    return True, None


def download_new_tracked(track_id: int) -> tuple[bool, str | None]:
    row = get_tracking(track_id)
    if not row:
        return False, "Seguimiento no encontrado."
    try:
        source = require_download_source(row["source_id"])
    except DownloadsDisabled as exc:
        return False, str(exc)

    episodes = source.episodes(row["anime_url"])
    baseline = row["baseline_number"]
    new_episodes = [
        ep
        for ep in episodes
        if baseline is None or (ep.number is not None and ep.number > baseline)
    ]
    if not new_episodes:
        return False, "No hay episodios nuevos para descargar."

    create_download_job(
        source_id=row["source_id"],
        anime_title=row["anime_title"],
        anime_url=row["anime_url"],
        episodes=new_episodes,
        job_type="ANIME" if len(new_episodes) > 1 else "EPISODE",
    )
    mark_tracking_seen(track_id)
    return True, None


def worker_loop() -> None:
    while True:
        item = claim_next_item()
        if item is None:
            time.sleep(WORKER_SLEEP_SECONDS)
            continue
        process_item(item)


def start_worker() -> None:
    """Arranca los hilos de descarga.

    El numero de descargas simultaneas se controla con ``DOWNLOAD_CONCURRENCY``
    (por defecto 1, secuencial). Cada hilo reclama items de forma atomica con
    ``claim_next_item`` (``BEGIN IMMEDIATE``), por lo que dos workers nunca
    procesan el mismo item.
    """
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        for index in range(DOWNLOAD_CONCURRENCY):
            threading.Thread(
                target=worker_loop,
                name=f"anime-download-worker-{index + 1}",
                daemon=True,
            ).start()
        _worker_started = True
        logging.info("Workers de descarga iniciados: %d", DOWNLOAD_CONCURRENCY)


def json_error(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


@app.get("/")
def index():
    # base_path = prefijo publico bajo el proxy (p. ej. "/anime"). El front lo
    # antepone a sus llamadas fetch; Traefik lo quita antes de llegar aqui.
    return render_template("index.html", base_path=os.environ.get("APP_BASE_PATH", "").rstrip("/"))


@app.get("/api/sources")
def api_sources():
    return jsonify(
        {
            "sources": [source.metadata() for source in SOURCES.values()],
            "load_errors": [error.__dict__ for error in SOURCE_LOAD_ERRORS],
        },
    )


@app.get("/api/popular")
def api_popular():
    try:
        source = require_source(request.args.get("source", ""))
        page = max(1, int(request.args.get("page", "1")))
        return jsonify(source.popular(page).to_dict())
    except Exception as exc:
        return json_error(str(exc))


@app.get("/api/latest")
def api_latest():
    try:
        source = require_source(request.args.get("source", ""))
        page = max(1, int(request.args.get("page", "1")))
        return jsonify(source.latest(page).to_dict())
    except Exception as exc:
        return json_error(str(exc))


@app.get("/api/search")
def api_search():
    try:
        source = require_source(request.args.get("source", ""))
        query = request.args.get("q", "")
        page = max(1, int(request.args.get("page", "1")))
        return jsonify(source.search(query, page).to_dict())
    except Exception as exc:
        return json_error(str(exc))


@app.get("/api/directory")
def api_directory():
    try:
        source = require_source(request.args.get("source", ""))
        page = max(1, int(request.args.get("page", "1")))
        return jsonify(source.directory(page).to_dict())
    except Exception as exc:
        return json_error(str(exc))


@app.get("/api/genre")
def api_genre():
    try:
        source = require_source(request.args.get("source", ""))
        genre = request.args.get("genre", "")
        page = max(1, int(request.args.get("page", "1")))
        if not genre:
            return json_error("Falta genre")
        return jsonify(source.by_genre(genre, page).to_dict())
    except Exception as exc:
        return json_error(str(exc))


@app.get("/api/details")
def api_details():
    try:
        source = require_source(request.args.get("source", ""))
        anime_url = request.args.get("url", "")
        if not anime_url:
            return json_error("Falta url")
        return jsonify(source.details(anime_url).to_dict())
    except Exception as exc:
        return json_error(str(exc))


@app.get("/api/episodes")
def api_episodes():
    try:
        source = require_source(request.args.get("source", ""))
        anime_url = request.args.get("url", "")
        page = max(1, int(request.args.get("page", "1")))
        if not anime_url:
            return json_error("Falta url")
        return jsonify(source.episode_page(anime_url, page).to_dict())
    except Exception as exc:
        return json_error(str(exc))


@app.get("/api/downloads")
def api_downloads():
    try:
        limit = min(200, max(1, int(request.args.get("limit", "100"))))
    except ValueError:
        limit = 100
    return jsonify({"downloads": list_download_jobs(limit)})


@app.post("/api/downloads")
def api_enqueue_episode():
    payload = request.get_json(silent=True) or {}
    source_id = str(payload.get("source_id", "")).strip()
    anime_title = str(payload.get("anime_title", "")).strip()
    anime_url = str(payload.get("anime_url", "")).strip()
    episode_title = str(payload.get("episode_title", "")).strip()
    episode_url = str(payload.get("episode_url", "")).strip()
    episode_number = payload.get("episode_number")

    if not source_id or not anime_title or not episode_title or not episode_url:
        return json_error("Faltan source_id, anime_title, episode_title o episode_url")

    try:
        require_download_source(source_id)
        job_id = create_download_job(
            source_id=source_id,
            anime_title=anime_title,
            anime_url=anime_url,
            episodes=[
                EpisodeItem(
                    title=episode_title,
                    url=episode_url,
                    source_id=source_id,
                    number=episode_number,
                ),
            ],
            job_type="EPISODE",
        )
        return jsonify({"ok": True, "id": job_id})
    except DownloadsDisabled as exc:
        return json_error(str(exc), 403)
    except Exception as exc:
        return json_error(str(exc))


@app.post("/api/downloads/anime")
def api_enqueue_anime():
    payload = request.get_json(silent=True) or {}
    source_id = str(payload.get("source_id", "")).strip()
    anime_title = str(payload.get("anime_title", "")).strip()
    anime_url = str(payload.get("anime_url", "")).strip()

    if not source_id or not anime_title or not anime_url:
        return json_error("Faltan source_id, anime_title o anime_url")

    try:
        source = require_download_source(source_id)
        episodes = source.all_episodes(anime_url)
        job_id = create_download_job(
            source_id=source_id,
            anime_title=anime_title,
            anime_url=anime_url,
            episodes=episodes,
            job_type="ANIME",
        )
        return jsonify({"ok": True, "id": job_id, "episodes": len(episodes)})
    except DownloadsDisabled as exc:
        return json_error(str(exc), 403)
    except Exception as exc:
        return json_error(str(exc))


@app.post("/api/downloads/direct")
def api_enqueue_direct():
    payload = request.get_json(silent=True) or {}
    anime_title = str(payload.get("anime_title", "")).strip()
    episodes_payload = payload.get("episodes") or []

    if not anime_title:
        return json_error("Falta anime_title")
    if not isinstance(episodes_payload, list) or not episodes_payload:
        return json_error("Falta la lista de episodios directos")

    try:
        require_download_source("direct")
        episodes: list[EpisodeItem] = []
        for index, item in enumerate(episodes_payload, start=1):
            if not isinstance(item, dict):
                return json_error("Cada episodio debe ser un objeto")
            title = str(item.get("title") or f"Episodio {index}").strip()
            url = str(item.get("url") or "").strip()
            number = item.get("number")
            if not title or not url:
                return json_error("Cada episodio directo necesita title y url")
            episodes.append(
                EpisodeItem(
                    source_id="direct",
                    title=title,
                    url=url,
                    number=number,
                ),
            )

        job_id = create_download_job(
            source_id="direct",
            anime_title=anime_title,
            anime_url="direct://manual",
            episodes=episodes,
            job_type="ANIME" if len(episodes) > 1 else "EPISODE",
        )
        return jsonify({"ok": True, "id": job_id, "episodes": len(episodes)})
    except Exception as exc:
        return json_error(str(exc))


@app.post("/api/downloads/<int:job_id>/<action>")
def api_download_action(job_id: int, action: str):
    handlers = {
        "pause": pause_job,
        "resume": resume_job,
        "cancel": cancel_job,
        "retry": retry_job,
    }
    handler = handlers.get(action)
    if handler is None:
        return json_error("Accion no soportada", 404)

    ok, error = handler(job_id)
    if not ok:
        return json_error(error or "No se pudo aplicar la accion", 409)

    return jsonify({"ok": True, "download": get_job(job_id)})


@app.post("/api/downloads/items/<int:item_id>/<action>")
def api_item_action(item_id: int, action: str):
    if action != "retry":
        return json_error("Accion no soportada", 404)
    payload = request.get_json(silent=True) or {}
    force = bool(payload.get("force")) or request.args.get("force", "").lower() in {"1", "true", "yes"}
    ok, error = retry_item(item_id, force=force)
    if not ok:
        return json_error(error or "No se pudo reintentar el episodio", 409)
    return jsonify({"ok": True})


@app.delete("/api/downloads/<int:job_id>")
def api_delete_download(job_id: int):
    ok, error = delete_job(job_id)
    if not ok:
        return json_error(error or "No se pudo eliminar el registro", 409)
    return jsonify({"ok": True})


# -------------------------------- Seguimiento --------------------------------
@app.get("/api/tracking")
def api_tracking_list():
    return jsonify({"tracking": list_tracking()})


@app.post("/api/tracking")
def api_tracking_add():
    payload = request.get_json(silent=True) or {}
    source_id = str(payload.get("source_id", "")).strip()
    anime_url = str(payload.get("anime_url", "")).strip()
    anime_title = str(payload.get("anime_title", "")).strip()
    if not source_id or not anime_url:
        return json_error("Faltan source_id o anime_url")
    try:
        add_tracking(source_id, anime_url, anime_title)
        return jsonify({"ok": True, "tracking": list_tracking()})
    except Exception as exc:  # noqa: BLE001
        return json_error(str(exc))


@app.post("/api/tracking/refresh")
def api_tracking_refresh():
    return jsonify({"ok": True, "tracking": refresh_tracking()})


@app.post("/api/tracking/<int:track_id>/<action>")
def api_tracking_action(track_id: int, action: str):
    if action == "seen":
        ok, error = mark_tracking_seen(track_id)
    elif action == "download":
        ok, error = download_new_tracked(track_id)
    else:
        return json_error("Accion no soportada", 404)
    if not ok:
        return json_error(error or "No se pudo aplicar la accion", 409)
    return jsonify({"ok": True})


@app.delete("/api/tracking/<int:track_id>")
def api_tracking_delete(track_id: int):
    ok, error = delete_tracking(track_id)
    if not ok:
        return json_error(error or "No se pudo eliminar el seguimiento", 409)
    return jsonify({"ok": True})


init_db()
start_worker()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
