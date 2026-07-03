from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request

import jellyfin
import tg_client


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", BASE_DIR / "data" / "telegram_queue.db"))
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", BASE_DIR / "downloads"))
PORT = int(os.environ.get("PORT", "8100"))
WORKER_SLEEP_SECONDS = max(1, int(os.environ.get("WORKER_SLEEP_SECONDS", "3")))
PROGRESS_UPDATE_SECONDS = max(0.5, float(os.environ.get("PROGRESS_UPDATE_SECONDS", "1")))
BOT_HISTORY_LIMIT = max(5, int(os.environ.get("BOT_HISTORY_LIMIT", "15")))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)

app = Flask(__name__)
_worker_started = False
_worker_lock = threading.Lock()


class PausedDownload(RuntimeError):
    pass


class CanceledDownload(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


_UNSAFE_SEGMENT = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_path_segment(value: str, fallback: str = "archivo") -> str:
    cleaned = _UNSAFE_SEGMENT.sub("_", (value or "").strip()).strip(". ")
    return cleaned[:200] or fallback


def _significant_tokens(text: str) -> set[str]:
    """Palabras significativas (sin acentos, minusculas, len>=3) de un texto."""
    normalized = "".join(
        c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c)
    ).lower()
    return {w for w in re.findall(r"[a-z0-9]+", normalized) if len(w) >= 3}


def derive_download_name(title: str | None, original_name: str | None) -> str | None:
    """Elige el nombre con el que guardar el fichero.

    Si el nombre original no comparte ninguna palabra significativa con el titulo
    del post (p. ej. 'video_17354.mp4' frente a 'American Pie: El Reencuentro'),
    se renombra al titulo conservando la extension, para que Jellyfin lo reconozca.
    Si comparten algo (coincidencia total o parcial), se respeta el original.
    """
    original_name = (original_name or "").strip()
    title_line = (title or "").strip().splitlines()[0] if (title or "").strip() else ""
    if not title_line:
        return original_name or None

    if _significant_tokens(title_line) & _significant_tokens(original_name):
        return original_name or None  # se corresponden: no tocar

    ext = os.path.splitext(original_name)[1]
    # ':' -> ' ' para no dejar guiones bajos feos ('Pie: El' -> 'Pie El').
    cleaned_title = re.sub(r"\s+", " ", title_line.replace(":", " ")).strip()
    safe_title = sanitize_path_segment(cleaned_title, "video")
    return f"{safe_title}{ext}" if ext else safe_title


# ----------------------------------------------------------------------- SQLite
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
        # La herramienta se centra en bots: se retiran las tablas del modelo
        # anterior de canales-catalogo si venian de una version previa.
        for legacy in ("posts", "post_links", "channels", "files"):
            connection.execute(f"DROP TABLE IF EXISTS {legacy}")

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                username TEXT,
                type TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error TEXT
            )
            """,
        )

        # `downloads` es por mensaje de Telegram (chat + message_id). Si existe una
        # version vieja sin la columna bot_id, se recrea limpia.
        existing = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(downloads)").fetchall()
        }
        if existing and "bot_id" not in existing:
            connection.execute("DROP TABLE IF EXISTS downloads")

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER,
                chat_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                bot_title TEXT NOT NULL,
                file_name TEXT,
                status TEXT NOT NULL CHECK (
                    status IN ('PENDING', 'DOWNLOADING', 'PAUSED', 'CANCELED', 'COMPLETED', 'FAILED')
                ),
                bytes_downloaded INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER NOT NULL DEFAULT 0,
                output_path TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                UNIQUE(chat_id, message_id)
            )
            """,
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status, id)",
        )


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


# -------------------------------------------------------------------------- bots
def list_bots() -> list[dict[str, Any]]:
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM bots ORDER BY title COLLATE NOCASE ASC",
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def get_bot(bot_id: int) -> dict[str, Any] | None:
    with get_connection() as connection:
        row = connection.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
    return row_to_dict(row) if row else None


def coerce_peer(value: Any) -> int | str:
    """Normaliza un peer para Pyrogram: '@usuario' tal cual, o id numerico a int."""
    text = str(value).strip()
    if text.startswith("@"):
        return text
    try:
        return int(text)
    except ValueError:
        return text


def bot_peer(bot: dict[str, Any]) -> int | str:
    """Peer con el que hablar al bot.

    Se prefiere '@usuario' porque Telegram lo resuelve desde cero en cada sesion.
    La sesion en memoria no persiste la cache de peers entre reinicios, asi que
    usar el id numerico daria PEER_ID_INVALID tras recompilar/reiniciar.
    """
    if bot.get("username"):
        return "@" + bot["username"]
    return coerce_peer(bot["chat_id"])


def add_bot(identifier: str) -> dict[str, Any]:
    info = tg_client.manager.resolve_channel(identifier)
    now = utc_now()
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO bots (chat_id, title, username, type, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title=excluded.title,
                username=excluded.username,
                type=excluded.type,
                updated_at=excluded.updated_at,
                error=NULL
            """,
            (str(info["chat_id"]), info["title"], info["username"], info["type"], now, now),
        )
        row = connection.execute(
            "SELECT * FROM bots WHERE chat_id=?", (str(info["chat_id"]),),
        ).fetchone()
    return row_to_dict(row)


def delete_bot(bot_id: int) -> bool:
    with get_connection() as connection:
        cursor = connection.execute("DELETE FROM bots WHERE id=?", (bot_id,))
    return cursor.rowcount > 0


# ------------------------------------------------------------------ conversacion
def bot_messages(bot_id: int, limit: int = BOT_HISTORY_LIMIT) -> list[dict[str, Any]]:
    bot = get_bot(bot_id)
    if bot is None:
        raise ValueError("Bot no encontrado.")
    return tg_client.manager.bot_history(bot_peer(bot), limit=limit)


def bot_send(bot_id: int, text: str) -> list[dict[str, Any]]:
    bot = get_bot(bot_id)
    if bot is None:
        raise ValueError("Bot no encontrado.")
    return tg_client.manager.bot_send(bot_peer(bot), text, limit=BOT_HISTORY_LIMIT)


def bot_click(bot_id: int, message_id: int, row: int, col: int) -> dict[str, Any]:
    bot = get_bot(bot_id)
    if bot is None:
        raise ValueError("Bot no encontrado.")
    return tg_client.manager.bot_click(bot_peer(bot), message_id, row, col, limit=BOT_HISTORY_LIMIT)


# -------------------------------------------------------------------- descargas
def enqueue_bot_message(
    bot_id: int,
    message_id: int,
    file_name: str | None,
    title: str | None = None,
) -> dict[str, Any]:
    bot = get_bot(bot_id)
    if bot is None:
        raise ValueError("Bot no encontrado.")
    # Renombra al titulo del post si el nombre del fichero no se corresponde.
    file_name = derive_download_name(title, file_name)
    # Guarda un peer resoluble ('@usuario') para que la descarga funcione tras
    # reiniciar (la sesion en memoria no recuerda peers por id numerico).
    peer = str(bot_peer(bot))
    now = utc_now()
    with get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO downloads (
                bot_id, chat_id, message_id, bot_title, file_name,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?)
            ON CONFLICT(chat_id, message_id) DO UPDATE SET
                status=CASE
                    WHEN downloads.status IN ('FAILED', 'CANCELED') THEN 'PENDING'
                    ELSE downloads.status
                END,
                updated_at=excluded.updated_at
            """,
            (bot_id, peer, message_id, bot["title"], file_name, now, now),
        )
    return {"queued": 1 if cursor.rowcount > 0 else 0}


def list_downloads(limit: int = 200) -> list[dict[str, Any]]:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT *
              FROM downloads
             ORDER BY
                CASE status
                    WHEN 'DOWNLOADING' THEN 0
                    WHEN 'PENDING' THEN 1
                    WHEN 'PAUSED' THEN 2
                    WHEN 'FAILED' THEN 3
                    WHEN 'CANCELED' THEN 4
                    ELSE 5
                END,
                CASE WHEN status IN ('DOWNLOADING', 'PENDING', 'PAUSED') THEN id ELSE -id END
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def get_download(download_id: int) -> dict[str, Any] | None:
    with get_connection() as connection:
        row = connection.execute("SELECT * FROM downloads WHERE id=?", (download_id,)).fetchone()
    return row_to_dict(row) if row else None


def update_download(download_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = utc_now()
    assignments = ", ".join(f"{key}=?" for key in fields)
    with get_connection() as connection:
        connection.execute(
            f"UPDATE downloads SET {assignments} WHERE id=?",
            list(fields.values()) + [download_id],
        )


def claim_next_download() -> dict[str, Any] | None:
    """Reclama atomicamente la siguiente descarga PENDING (worker secuencial)."""
    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM downloads WHERE status='PENDING' ORDER BY id ASC LIMIT 1",
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        now = utc_now()
        connection.execute(
            """
            UPDATE downloads
               SET status='DOWNLOADING',
                   started_at=COALESCE(started_at, ?),
                   finished_at=NULL,
                   bytes_downloaded=0,
                   error=NULL,
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


def control_state(download_id: int) -> str:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT status FROM downloads WHERE id=?", (download_id,),
        ).fetchone()
    if row is None:
        return "gone"
    if row["status"] == "PAUSED":
        return "paused"
    if row["status"] == "CANCELED":
        return "canceled"
    return "ok"


def process_download(download: dict[str, Any]) -> None:
    download_id = download["id"]
    folder = DOWNLOAD_DIR / sanitize_path_segment(download["bot_title"], "bot")
    folder.mkdir(parents=True, exist_ok=True)

    last_update = 0.0

    def on_progress(current: int, total: int) -> None:
        nonlocal last_update
        now = time.monotonic()
        if now - last_update >= PROGRESS_UPDATE_SECONDS:
            update_download(download_id, bytes_downloaded=current, total_bytes=total)
            last_update = now

    def check() -> str:
        state = control_state(download_id)
        return "ok" if state == "ok" else "stop"

    try:
        result = tg_client.manager.download_message(
            chat_id=coerce_peer(download["chat_id"]),
            message_id=download["message_id"],
            dest_dir=str(folder),
            on_progress=on_progress,
            control_check=check,
            preferred_name=download["file_name"],
        )
        if result is None:
            state = control_state(download_id)
            if state == "paused":
                raise PausedDownload("Descarga pausada por el usuario.")
            raise CanceledDownload("Descarga cancelada por el usuario.")

        update_download(
            download_id,
            status="COMPLETED",
            file_name=result["file_name"],
            output_path=result["path"],
            bytes_downloaded=result["size"],
            total_bytes=result["size"],
            finished_at=utc_now(),
            error=None,
        )
        logging.info("Descargado: %s", result["path"])
        jellyfin.notify()  # refresca la biblioteca de Jellyfin (con debounce)
    except PausedDownload as exc:
        update_download(download_id, status="PAUSED", error=str(exc))
    except CanceledDownload as exc:
        update_download(download_id, status="CANCELED", finished_at=utc_now(), error=str(exc))
    except Exception as exc:
        logging.exception("Fallo la descarga %s", download_id)
        update_download(download_id, status="FAILED", finished_at=utc_now(), error=str(exc)[:1000])


# ------------------------------------------------------------- acciones de cola
def pause_download(download_id: int) -> tuple[bool, str | None]:
    item = get_download(download_id)
    if not item:
        return False, "Descarga no encontrada."
    if item["status"] not in {"PENDING", "DOWNLOADING"}:
        return False, "Solo se puede pausar una descarga pendiente o activa."
    update_download(download_id, status="PAUSED")
    return True, None


def resume_download(download_id: int) -> tuple[bool, str | None]:
    item = get_download(download_id)
    if not item:
        return False, "Descarga no encontrada."
    if item["status"] != "PAUSED":
        return False, "Solo se puede reanudar una descarga pausada."
    update_download(download_id, status="PENDING")
    return True, None


def cancel_download(download_id: int) -> tuple[bool, str | None]:
    item = get_download(download_id)
    if not item:
        return False, "Descarga no encontrada."
    if item["status"] == "COMPLETED":
        return False, "Una descarga completada no se cancela; elimina el registro."
    update_download(download_id, status="CANCELED", finished_at=utc_now())
    return True, None


def retry_download(download_id: int) -> tuple[bool, str | None]:
    item = get_download(download_id)
    if not item:
        return False, "Descarga no encontrada."
    if item["status"] == "DOWNLOADING":
        return False, "La descarga ya esta en curso."
    update_download(
        download_id,
        status="PENDING",
        started_at=None,
        finished_at=None,
        bytes_downloaded=0,
        error=None,
    )
    return True, None


def delete_download(download_id: int) -> tuple[bool, str | None]:
    item = get_download(download_id)
    if not item:
        return False, "Descarga no encontrada."
    if item["status"] == "DOWNLOADING":
        return False, "Pausa o cancela la descarga antes de eliminarla."
    with get_connection() as connection:
        connection.execute("DELETE FROM downloads WHERE id=?", (download_id,))
    return True, None


# ------------------------------------------------------------------------ worker
def worker_loop() -> None:
    while True:
        try:
            item = claim_next_download()
        except Exception:
            logging.exception("Error reclamando descarga")
            time.sleep(WORKER_SLEEP_SECONDS)
            continue
        if item is None:
            time.sleep(WORKER_SLEEP_SECONDS)
            continue
        process_download(item)


def start_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        threading.Thread(target=worker_loop, name="telegram-download-worker", daemon=True).start()
        _worker_started = True
        logging.info("Worker de descargas iniciado (secuencial).")


# ------------------------------------------------------------------------ rutas
def json_error(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


@app.get("/")
def index():
    # base_path = prefijo publico bajo el proxy (p. ej. "/telegram"). El front lo
    # antepone a sus llamadas fetch; Traefik lo quita antes de llegar aqui.
    return render_template("index.html", base_path=os.environ.get("APP_BASE_PATH", "").rstrip("/"))


@app.get("/api/bots")
def api_bots():
    return jsonify({"bots": list_bots()})


@app.post("/api/bots")
def api_add_bot():
    payload = request.get_json(silent=True) or {}
    identifier = str(payload.get("identifier", "")).strip()
    if not identifier:
        return json_error("Falta el identificador del bot (@usuario o enlace).")
    try:
        bot = add_bot(identifier)
        return jsonify({"ok": True, "bot": bot})
    except Exception as exc:
        logging.exception("No se pudo resolver el bot '%s'", identifier)
        return json_error(f"{type(exc).__name__}: {exc}")


@app.delete("/api/bots/<int:bot_id>")
def api_delete_bot(bot_id: int):
    if not delete_bot(bot_id):
        return json_error("Bot no encontrado.", 404)
    return jsonify({"ok": True})


@app.get("/api/bots/<int:bot_id>/messages")
def api_bot_messages(bot_id: int):
    try:
        limit = min(50, max(5, int(request.args.get("limit", str(BOT_HISTORY_LIMIT)))))
    except ValueError:
        limit = BOT_HISTORY_LIMIT
    try:
        return jsonify({"ok": True, "messages": bot_messages(bot_id, limit)})
    except Exception as exc:
        logging.exception("Error leyendo conversacion del bot %s", bot_id)
        return json_error(f"{type(exc).__name__}: {exc}")


@app.post("/api/bots/<int:bot_id>/send")
def api_bot_send(bot_id: int):
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text", "")).strip()
    if not text:
        return json_error("Falta el texto a enviar.")
    try:
        return jsonify({"ok": True, "messages": bot_send(bot_id, text)})
    except Exception as exc:
        logging.exception("Error enviando texto al bot %s", bot_id)
        return json_error(f"{type(exc).__name__}: {exc}")


@app.post("/api/bots/<int:bot_id>/click")
def api_bot_click(bot_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        message_id = int(payload.get("message_id"))
        row = int(payload.get("row"))
        col = int(payload.get("col"))
    except (TypeError, ValueError):
        return json_error("Faltan message_id, row o col.")
    try:
        return jsonify({"ok": True, **bot_click(bot_id, message_id, row, col)})
    except Exception as exc:
        logging.exception("Error pulsando boton del bot %s", bot_id)
        return json_error(f"{type(exc).__name__}: {exc}")


@app.post("/api/bots/<int:bot_id>/download")
def api_bot_download(bot_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        message_id = int(payload.get("message_id"))
    except (TypeError, ValueError):
        return json_error("Falta message_id.")
    file_name = payload.get("file_name")
    title = payload.get("title")
    try:
        result = enqueue_bot_message(bot_id, message_id, file_name, title)
        return jsonify({"ok": True, **result})
    except Exception as exc:
        return json_error(str(exc))


@app.get("/api/downloads")
def api_downloads():
    try:
        limit = min(500, max(1, int(request.args.get("limit", "200"))))
    except ValueError:
        limit = 200
    return jsonify({"downloads": list_downloads(limit)})


@app.post("/api/downloads/<int:download_id>/<action>")
def api_download_action(download_id: int, action: str):
    handlers = {
        "pause": pause_download,
        "resume": resume_download,
        "cancel": cancel_download,
        "retry": retry_download,
    }
    handler = handlers.get(action)
    if handler is None:
        return json_error("Accion no soportada", 404)
    ok, error = handler(download_id)
    if not ok:
        return json_error(error or "No se pudo aplicar la accion", 409)
    return jsonify({"ok": True, "download": get_download(download_id)})


@app.delete("/api/downloads/<int:download_id>")
def api_delete_download(download_id: int):
    ok, error = delete_download(download_id)
    if not ok:
        return json_error(error or "No se pudo eliminar el registro", 409)
    return jsonify({"ok": True})


init_db()
start_worker()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
