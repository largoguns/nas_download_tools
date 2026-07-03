"""Cliente MTProto (Pyrogram) compartido para Telegram-Downloader.

Pyrogram es asincrono y exige que un mismo cliente se use siempre desde el mismo
event loop. El stack del monorepo, en cambio, trabaja con workers de `threading`
sincronos. Para conciliar ambos mundos se arranca un unico event loop en un hilo
dedicado (daemon) que es el duenno del `Client`, y el resto del proceso habla con
el cliente mediante `run_coroutine_threadsafe`, exponiendo funciones sincronas:

    resolve_channel(identifier) -> dict
    list_channel_posts(chat_id, limit, offset_id) -> list[dict]
    download_message(chat_id, message_id, dest_dir, on_progress, control_check) -> dict

Asi el worker de descargas sigue siendo secuencial y sincrono como en las otras
herramientas, mientras Pyrogram vive en su propio loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from typing import Any, Callable

from pyrogram import Client
from pyrogram.errors import RPCError
from pyrogram.types import Message

try:  # StopTransmission permite abortar una descarga desde el callback de progreso.
    from pyrogram.errors import StopTransmission
except ImportError:  # Pyrogram lo expone en la raiz en algunas versiones.
    from pyrogram import StopTransmission  # type: ignore


API_ID = os.environ.get("TG_API_ID", "").strip()
API_HASH = os.environ.get("TG_API_HASH", "").strip()
SESSION_STRING = os.environ.get("TG_SESSION_STRING", "").strip()
SESSION_NAME = os.environ.get("TG_SESSION_NAME", "telegram_downloader")


class TelegramConfigError(RuntimeError):
    """Falta configuracion (api_id/api_hash/session) o no se pudo iniciar sesion."""


class TelegramClientManager:
    """Posee el event loop y el `Client` de Pyrogram en un hilo dedicado."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Client | None = None
        self._thread: threading.Thread | None = None
        self._loop_ready = threading.Event()
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()

    # ------------------------------------------------------------------ arranque
    def _build_client(self) -> Client:
        if not API_ID or not API_HASH:
            raise TelegramConfigError(
                "Faltan TG_API_ID / TG_API_HASH. Obtenlos en https://my.telegram.org.",
            )
        if not SESSION_STRING:
            raise TelegramConfigError(
                "Falta TG_SESSION_STRING. Genera una sesion con: python login.py",
            )
        try:
            api_id = int(API_ID)
        except ValueError as exc:
            raise TelegramConfigError("TG_API_ID debe ser numerico.") from exc

        return Client(
            name=SESSION_NAME,
            api_id=api_id,
            api_hash=API_HASH,
            session_string=SESSION_STRING,
            in_memory=True,  # no escribe fichero de sesion: la sesion vive en el env.
            no_updates=True,  # solo descargamos; no necesitamos recibir updates.
        )

    def _run_loop(self) -> None:
        """Arranca un event loop y lo mantiene vivo con run_forever().

        El loop NO se detiene nunca entre llamadas: las tareas de fondo de
        Pyrogram (NetworkTask, PingTask) siguen corriendo. El cliente se inicia
        encima de este loop ya en marcha, via run_coroutine_threadsafe.
        """
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._loop_ready.set()
        loop.run_forever()

    def _ensure_loop(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._loop_ready.clear()
                self._thread = threading.Thread(
                    target=self._run_loop,
                    name="telegram-client-loop",
                    daemon=True,
                )
                self._thread.start()
        if not self._loop_ready.wait(timeout=15):
            raise TelegramConfigError("No se pudo iniciar el event loop de Telegram.")

    def ensure_started(self) -> None:
        self._ensure_loop()
        if self._client is not None:
            return
        with self._start_lock:
            if self._client is not None:
                return

            async def _start() -> Client:
                client = self._build_client()
                await client.start()
                return client

            try:
                assert self._loop is not None
                future = asyncio.run_coroutine_threadsafe(_start(), self._loop)
                self._client = future.result(timeout=60)
                logging.info("Cliente de Telegram iniciado.")
            except TelegramConfigError:
                raise
            except Exception as exc:  # noqa: BLE001
                logging.exception("Fallo al iniciar el cliente de Telegram")
                raise TelegramConfigError(f"No se pudo iniciar Telegram: {exc}") from exc

    def _submit(self, make_coro: Callable[[], Any]) -> Any:
        """Ejecuta una corrutina en el loop de Telegram y devuelve su resultado.

        Recibe una *factoria* (no la corrutina) para crearla solo despues de que
        el cliente este listo; asi, si el arranque falla, no queda ninguna
        corrutina sin await (evita el RuntimeWarning).
        """
        self.ensure_started()
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(make_coro(), self._loop)
        return future.result()

    # ------------------------------------------------------------------ acciones
    def resolve_channel(self, identifier: str) -> dict[str, Any]:
        return self._submit(lambda: self._resolve_channel(identifier))

    def bot_history(self, chat_id: int | str, limit: int = 15) -> list[dict[str, Any]]:
        return self._submit(lambda: self._bot_history(chat_id, limit))

    def bot_send(self, chat_id: int | str, text: str, limit: int = 15) -> list[dict[str, Any]]:
        return self._submit(lambda: self._bot_send(chat_id, text, limit))

    def bot_click(
        self,
        chat_id: int | str,
        message_id: int,
        row: int,
        col: int,
        limit: int = 15,
    ) -> dict[str, Any]:
        return self._submit(lambda: self._bot_click(chat_id, message_id, row, col, limit))

    def download_message(
        self,
        chat_id: int | str,
        message_id: int,
        dest_dir: str,
        on_progress: Callable[[int, int], None] | None = None,
        control_check: Callable[[], str] | None = None,
        preferred_name: str | None = None,
    ) -> dict[str, Any] | None:
        """Descarga el fichero de un mensaje concreto a ``dest_dir``.

        Si se da ``preferred_name`` se usa como nombre de fichero (conservando la
        extension original si el preferido no la trae). Devuelve {path, file_name,
        size}, o None si se aborto (pausa/cancelacion).
        """
        return self._submit(
            lambda: self._download_message(
                chat_id, message_id, dest_dir, on_progress, control_check, preferred_name,
            ),
        )

    # ------------------------------------------------------- coroutines internas
    async def _resolve_channel(self, identifier: str) -> dict[str, Any]:
        assert self._client is not None
        chat = await self._client.get_chat(normalize_channel_identifier(identifier))
        return {
            "chat_id": chat.id,
            "title": chat.title or chat.first_name or str(chat.id),
            "username": chat.username or "",
            "type": str(getattr(chat.type, "value", chat.type)),
        }

    async def _bot_history(self, chat_id: int | str, limit: int) -> list[dict[str, Any]]:
        """Devuelve la conversacion reciente con el bot, del mas nuevo al mas viejo.

        get_chat_history ya llega de mas nuevo a mas viejo, asi que la lista sale
        con los mensajes recientes primero (arriba en la consola).
        """
        assert self._client is not None
        messages: list[dict[str, Any]] = []
        async for message in self._client.get_chat_history(chat_id, limit=limit):
            messages.append(_render_message(message))
        return messages

    async def _current_max_id(self, chat_id: int | str) -> int:
        assert self._client is not None
        async for message in self._client.get_chat_history(chat_id, limit=1):
            return message.id
        return 0

    async def _wait_for_reply(
        self,
        chat_id: int | str,
        after_id: int,
        edited_message_id: int | None = None,
        edited_baseline: Any = None,
        timeout: float = 12.0,
    ) -> None:
        """Espera a que el bot reaccione: mensaje entrante nuevo o edicion del menu.

        Sondea el historial (no requiere modo updates). Sale al detectar un mensaje
        entrante con id > after_id, o que ``edited_message_id`` cambie su edit_date.
        Si expira, vuelve igualmente (el frontend mostrara el ultimo estado).
        """
        assert self._client is not None
        steps = max(1, int(timeout / 0.6))
        for _ in range(steps):
            await asyncio.sleep(0.6)
            newest_incoming = None
            async for message in self._client.get_chat_history(chat_id, limit=5):
                if not message.outgoing and message.id > after_id:
                    newest_incoming = message
                    break
            if newest_incoming is not None:
                return
            if edited_message_id is not None:
                edited = await self._client.get_messages(chat_id, edited_message_id)
                if edited and getattr(edited, "edit_date", None) != edited_baseline:
                    return

    async def _bot_send(self, chat_id: int | str, text: str, limit: int) -> list[dict[str, Any]]:
        assert self._client is not None
        sent = await self._client.send_message(chat_id, text)
        await self._wait_for_reply(chat_id, after_id=sent.id)
        return await self._bot_history(chat_id, limit)

    async def _bot_click(
        self,
        chat_id: int | str,
        message_id: int,
        row: int,
        col: int,
        limit: int,
    ) -> dict[str, Any]:
        assert self._client is not None
        message = await self._client.get_messages(chat_id, message_id)
        markup = getattr(message, "reply_markup", None)
        rows = getattr(markup, "inline_keyboard", None) or []
        if row >= len(rows) or col >= len(rows[row]):
            raise RPCError("Ese boton ya no existe (el mensaje ha cambiado).")
        button = rows[row][col]

        # Boton de URL: no hay callback; el frontend lo abre directamente.
        url = getattr(button, "url", None)
        if url:
            return {"opened_url": url, "messages": await self._bot_history(chat_id, limit)}

        data = getattr(button, "callback_data", None)
        if data is None:
            raise RPCError("El boton no es pulsable (sin callback).")

        baseline_max = await self._current_max_id(chat_id)
        baseline_edit = getattr(message, "edit_date", None)
        try:
            await self._client.request_callback_answer(chat_id, message_id, data)
        except Exception as exc:  # noqa: BLE001 - algunas respuestas son alertas/timeout
            logging.info("request_callback_answer: %s", exc)
        await self._wait_for_reply(
            chat_id,
            after_id=baseline_max,
            edited_message_id=message_id,
            edited_baseline=baseline_edit,
        )
        return {"opened_url": None, "messages": await self._bot_history(chat_id, limit)}

    async def _download_message(
        self,
        chat_id: int | str,
        message_id: int,
        dest_dir: str,
        on_progress: Callable[[int, int], None] | None,
        control_check: Callable[[], str] | None,
        preferred_name: str | None = None,
    ) -> dict[str, Any] | None:
        assert self._client is not None
        message = await self._client.get_messages(chat_id, message_id)
        if message is None or getattr(message, "empty", False) or not message.media:
            raise RPCError(f"El mensaje {message_id} no tiene fichero descargable.")

        info = _extract_file(message)
        if info is None:
            raise RPCError(f"El mensaje {message_id} no contiene un fichero soportado.")

        name = info["file_name"]
        if preferred_name:
            name = preferred_name
            # Si el nombre preferido no trae extension, conserva la del original.
            if not os.path.splitext(name)[1] and os.path.splitext(info["file_name"])[1]:
                name += os.path.splitext(info["file_name"])[1]

        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, _safe_name(name))

        def progress(current: int, total: int) -> None:
            if on_progress is not None:
                on_progress(current, total)
            if control_check is not None and control_check() != "ok":
                # Aborta la descarga; el worker decide si fue pausa o cancelacion.
                raise StopTransmission

        result = await self._client.download_media(
            message,
            file_name=dest_path,
            progress=progress,
        )
        if result is None:
            return None
        size = os.path.getsize(result) if os.path.exists(result) else 0
        return {"path": result, "file_name": os.path.basename(result), "size": size}


def _extract_file(message: Message) -> dict[str, Any] | None:
    """Normaliza un mensaje con media a un dict de fichero, o None si no aplica."""
    media_type = None
    file_name = None
    mime_type = None
    file_size = 0
    file_unique_id = None

    if message.document:
        media = message.document
        media_type = "document"
        file_name = media.file_name
    elif message.video:
        media = message.video
        media_type = "video"
        file_name = media.file_name
    elif message.audio:
        media = message.audio
        media_type = "audio"
        file_name = media.file_name
    elif message.voice:
        media = message.voice
        media_type = "voice"
    elif message.animation:
        media = message.animation
        media_type = "animation"
        file_name = media.file_name
    elif message.photo:
        media = message.photo
        media_type = "photo"
    else:
        return None

    mime_type = getattr(media, "mime_type", None)
    file_size = getattr(media, "file_size", 0) or 0
    file_unique_id = getattr(media, "file_unique_id", None)

    if not file_name:
        ext = _extension_for(media_type, mime_type)
        file_name = f"{media_type}_{message.id}{ext}"

    caption = message.caption or ""
    date = message.date.replace(microsecond=0).isoformat() if message.date else None

    return {
        "message_id": message.id,
        "file_unique_id": file_unique_id,
        "file_name": file_name,
        "mime_type": mime_type or "",
        "file_size": int(file_size),
        "media_type": media_type,
        "caption": caption,
        "date": date,
    }


_EXTENSION_BY_TYPE = {
    "photo": ".jpg",
    "voice": ".ogg",
}


def _extension_for(media_type: str, mime_type: str | None) -> str:
    if media_type in _EXTENSION_BY_TYPE:
        return _EXTENSION_BY_TYPE[media_type]
    if mime_type and "/" in mime_type:
        subtype = mime_type.split("/", 1)[1]
        return "." + subtype.split(";", 1)[0].strip()
    return ".bin"


def _render_message(message: Message) -> dict[str, Any]:
    """Normaliza un mensaje para pintarlo en la consola de bot.

    Incluye texto, si lleva fichero, y los botones inline con sus coordenadas
    (row/col) para poder "pulsarlos" luego sin exponer el callback_data al front.
    """
    file_info = _extract_file(message)
    buttons: list[dict[str, Any]] = []
    markup = getattr(message, "reply_markup", None)
    rows = getattr(markup, "inline_keyboard", None) or []
    for r, row in enumerate(rows):
        for c, button in enumerate(row):
            url = getattr(button, "url", None)
            buttons.append(
                {
                    "text": button.text,
                    "row": r,
                    "col": c,
                    "kind": "url" if url else "callback",
                    "url": url,
                }
            )
    return {
        "id": message.id,
        "outgoing": bool(message.outgoing),
        "from_bot": not bool(message.outgoing),
        "text": message.text or message.caption or "",
        "date": message.date.replace(microsecond=0).isoformat() if message.date else None,
        "has_file": file_info is not None,
        "file_name": file_info["file_name"] if file_info else None,
        "file_size": file_info["file_size"] if file_info else 0,
        "media_type": file_info["media_type"] if file_info else None,
        "buttons": buttons,
    }


def _render_message(message: Message) -> dict[str, Any]:
    """Normaliza un mensaje para pintarlo en la consola de bot.

    Incluye texto, si lleva fichero, y los botones inline con sus coordenadas
    (row/col) para poder "pulsarlos" luego sin exponer el callback_data al front.
    """
    file_info = _extract_file(message)
    buttons: list[dict[str, Any]] = []
    markup = getattr(message, "reply_markup", None)
    rows = getattr(markup, "inline_keyboard", None) or []
    for r, row in enumerate(rows):
        for c, button in enumerate(row):
            url = getattr(button, "url", None)
            buttons.append(
                {
                    "text": button.text,
                    "row": r,
                    "col": c,
                    "kind": "url" if url else "callback",
                    "url": url,
                }
            )
    return {
        "id": message.id,
        "outgoing": bool(message.outgoing),
        "from_bot": not bool(message.outgoing),
        "text": message.text or message.caption or "",
        "date": message.date.replace(microsecond=0).isoformat() if message.date else None,
        "has_file": file_info is not None,
        "file_name": file_info["file_name"] if file_info else None,
        "file_size": file_info["file_size"] if file_info else 0,
        "media_type": file_info["media_type"] if file_info else None,
        "buttons": buttons,
    }


_URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_name(value: str, fallback: str = "archivo") -> str:
    cleaned = _UNSAFE_NAME.sub("_", (value or "").strip()).strip(". ")
    return cleaned[:200] or fallback


def _extract_links(message: Message) -> list[str]:
    """Reune los enlaces de un post: texto plano, hipervinculos y botones inline.

    Evita slicing por offset de entidades (que va en unidades UTF-16 y es propenso
    a errores): para URLs en el texto usa regex, y para hipervinculos usa el campo
    ``url`` de la entidad. Devuelve la lista deduplicada conservando el orden.
    """
    found: list[str] = []

    text = message.text or message.caption or ""
    if text:
        found.extend(_URL_RE.findall(text))

    # Hipervinculos (text_link) en entidades de texto o de caption.
    for entity in (message.entities or []) + (message.caption_entities or []):
        url = getattr(entity, "url", None)
        if url:
            found.append(url)

    # Botones inline con URL.
    markup = getattr(message, "reply_markup", None)
    rows = getattr(markup, "inline_keyboard", None) or []
    for row in rows:
        for button in row:
            url = getattr(button, "url", None)
            if url:
                found.append(url)

    # Dedup conservando orden.
    seen: set[str] = set()
    unique: list[str] = []
    for url in found:
        url = url.strip().rstrip(".,)")
        if url and url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


_TME_HOSTS = ("t.me", "telegram.me", "telegram.dog")


def parse_message_link(url: str) -> tuple[int | str, int] | None:
    """Si ``url`` es un enlace a un mensaje de Telegram, devuelve (canal, message_id).

    Soporta:
      - https://t.me/<usuario>/<id>
      - https://t.me/c/<id_interno>/<id>   (canal privado -> -100<id_interno>)
    Devuelve None si no es un enlace a un mensaje concreto.
    """
    value = (url or "").strip()
    lowered = value.lower()
    for prefix in ("https://", "http://"):
        if lowered.startswith(prefix):
            value = value[len(prefix):]
            lowered = value.lower()
            break
    host = value.split("/", 1)[0].lower()
    if host not in _TME_HOSTS:
        return None

    path = value[len(host):].strip("/")
    parts = [p for p in path.split("/") if p]

    if not parts:
        return None

    if parts[0].lower() == "c":
        # c/<id_interno>/<message_id>
        if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
            return int(f"-100{parts[1]}"), int(parts[2])
        return None

    # <usuario>/<message_id>  (ignora el thread id de los grupos: <user>/<thread>/<id>)
    if len(parts) >= 2 and parts[-1].isdigit():
        username = parts[0].lstrip("@")
        if username and not username.startswith("+") and username.lower() != "joinchat":
            return username, int(parts[-1])
    return None


def classify_link(url: str) -> str:
    """Devuelve 'telegram_msg', 'telegram_chan' o 'external'."""
    if parse_message_link(url) is not None:
        return "telegram_msg"
    lowered = (url or "").strip().lower()
    for prefix in ("https://", "http://"):
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix):]
            break
    host = lowered.split("/", 1)[0]
    if host in _TME_HOSTS:
        return "telegram_chan"
    return "external"


def normalize_channel_identifier(identifier: str) -> int | str:
    """Convierte lo que pegue el usuario en algo que `get_chat` entienda.

    Acepta:
      - "@usuario" o "usuario"                       -> "usuario"
      - "https://t.me/usuario"                       -> "usuario"
      - "https://t.me/usuario/2877" (link a mensaje) -> "usuario" (se ignora el msg)
      - "https://t.me/c/1234567890/12" (canal priv.) -> -1001234567890
      - "-100123..." o "123..."                      -> int(...)
    Para enlaces de invitacion (t.me/+hash, /joinchat/) lanza un error claro:
    hay que unirse al canal antes (get_chat no resuelve invitaciones).
    """
    value = (identifier or "").strip()
    if not value:
        raise TelegramConfigError("Identificador de canal vacio.")

    # Quita esquema y dominio para quedarnos con la ruta de t.me.
    lowered = value.lower()
    for prefix in ("https://", "http://"):
        if lowered.startswith(prefix):
            value = value[len(prefix):]
            lowered = value.lower()
            break
    for host in ("t.me/", "telegram.me/", "telegram.dog/"):
        if lowered.startswith(host):
            value = value[len(host):]
            break

    value = value.strip("/")

    # Enlaces de invitacion: no resolubles con get_chat.
    if value.startswith("+") or value.lower().startswith("joinchat/"):
        raise TelegramConfigError(
            "Es un enlace de invitacion privado. Unete al canal desde tu app de "
            "Telegram y luego anadelo por @usuario o por su id.",
        )

    # Canal privado por id interno: t.me/c/<id>/<msg>
    if value.lower().startswith("c/"):
        rest = value[2:].split("/", 1)[0]
        if rest.isdigit():
            return int(f"-100{rest}")
        raise TelegramConfigError(f"No se entiende el enlace privado: {identifier}")

    # Primer segmento de la ruta = usuario del canal (ignora /<message_id>).
    first = value.split("/", 1)[0].lstrip("@")

    # Id numerico directo (-100... o 123...).
    if first.lstrip("-").isdigit():
        return int(first)

    if not first:
        raise TelegramConfigError(f"No se pudo extraer el canal de: {identifier}")
    return first


# Instancia compartida por toda la app.
manager = TelegramClientManager()
