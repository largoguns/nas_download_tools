# Telegram-Downloader

Aplicacion web ligera (Python/Flask) para interactuar con **bots de Telegram**
(menus, busquedas, botones) desde una cuenta de usuario y descargar los ficheros
que devuelven a un volumen local. Forma parte del monorepo de herramientas de
descarga y comparte stack y estetica con Spotube-Downloader y AnimeDownloader.

## Como funciona

1. **Anade un bot** por `@usuario` o enlace `t.me/...` (ej. `@videoclubpacobot`).
2. **Abre la consola**: la web refleja la conversacion con el bot. Escribes texto
   (un titulo, `/ayuda`, un comando) y **pulsas sus botones inline**; la app
   reenvia esas acciones a Telegram y muestra la respuesta del bot.
3. Cuando el bot **envia un fichero**, aparece un boton **Descargar**.
4. Un **worker secuencial** descarga uno a uno al volumen `/downloads` via MTProto
   (Pyrogram). Eliminar un registro de la cola nunca borra el fichero del disco.

La interaccion se hace por *polling* del historial del chat con el bot (no hace
falta el modo updates): tras enviar texto o pulsar un boton, la app espera la
reaccion del bot y refresca la conversacion.

## Requisitos previos: sesion de Telegram

MTProto necesita una sesion de usuario. Hazlo una vez en local:

```bash
cd telegram_downloader
pip install -r requirements.txt
TG_API_ID=12345 TG_API_HASH=tu_api_hash python login.py
```

`api_id` y `api_hash` se obtienen en https://my.telegram.org -> *API development
tools*. El script imprime un `TG_SESSION_STRING` que debes pegar en `.env`.

> La cadena de sesion da acceso completo a tu cuenta. Mantenla en privado y nunca
> la subas a git (`.env` ya esta en `.gitignore`).

## Docker

Desde la carpeta de Telegram-Downloader:

```bash
cd telegram_downloader
cp .env.example .env   # y rellena TG_API_ID, TG_API_HASH, TG_SESSION_STRING
docker compose up --build -d
```

La interfaz queda en:

```text
http://localhost:8100
```

## Variables importantes

- `TELEGRAM_WEB_PORT`: puerto host, por defecto `8100`.
- `TELEGRAM_DOWNLOAD_DIR`: carpeta host montada como `/downloads`, por defecto `./downloads`.
- `TG_API_ID`, `TG_API_HASH`: credenciales de https://my.telegram.org.
- `TG_SESSION_STRING`: sesion de usuario generada con `login.py`.
- `BOT_HISTORY_LIMIT`: mensajes recientes mostrados en la consola (def. `15`).
- `WORKER_SLEEP_SECONDS`, `PROGRESS_UPDATE_SECONDS`: ajustes del worker.

## Notas

- Las descargas son **estrictamente secuenciales** (un unico worker), apto para
  Raspberry Pi.
- Manejar un bot desde una cuenta es interaccion normal, pero **evita el spam**:
  acciones a ritmo humano para no toparte con limites de flood.
- Si un bot envia sus ficheros con **contenido protegido** (`noforwards`),
  Telegram bloquea la descarga por API aunque puedas verlos.
- `TgCrypto` acelera el cifrado de MTProto. Si la imagen falla al compilarlo en
  ARM, anade `build-essential` al `Dockerfile` o quitalo de `requirements.txt`
  (funciona igual, mas lento).
