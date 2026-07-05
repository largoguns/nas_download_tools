# Contexto del Proyecto

Este repositorio es un monorepo de herramientas de descarga. El nombre actual de la carpeta local puede seguir siendo `spotube_downloader`, pero la direccion del proyecto es evolucionar hacia algo generico como `download_tools`.

El monorepo contiene aplicaciones web independientes que comparten stack tecnologico, criterios de diseno y reglas operativas. Cada herramienta debe poder levantarse con su propio contenedor Docker sin depender de las demas.

Herramientas actuales:

1. **Spotube-Downloader**: descargador de musica con `spotdl`, ubicado en `spotube_downloader/`.
2. **AnimeDownloader**: descargador/experimento educativo de anime con fuentes extensibles, ubicado en `anime_web/`.
3. **Telegram-Downloader**: interactua con bots de Telegram (menus/busquedas/botones) via MTProto (Pyrogram) y descarga los ficheros que devuelven, ubicado en `telegram_downloader/`.
4. **Launcher**: portal minimo con botones grandes hacia las otras herramientas, ubicado en `launcher/`.

Ademas hay infraestructura compartida: **Proxy** (Traefik) en `proxy/`, punto unico
de entrada que enruta por subruta a todas las apps (ver "Proxy inverso (Traefik)").

Cada aplicacion se levanta, configura y mantiene por separado (contenedor propio).
El monorepo no aloja servidores multimedia (Jellyfin, Navidrome, etc.): esos viven
fuera. Lo que si esta permitido es que cada herramienta **avise a un servidor
multimedia externo para refrescar su biblioteca** al terminar una descarga, via su
API y configurado por entorno (ver "Integracion con servidores multimedia").

---

## Vision de Monorepo

- El repositorio debe alojar multiples herramientas de descarga relacionadas.
- Cada herramienta debe tener backend, frontend, `Dockerfile`, `docker-compose.yml`, `.env.example` y documentacion propia cuando sea necesario.
- El stack compartido por defecto es Python, Flask, SQLite, HTML/CSS/JS sin framework pesado y Docker.
- Las colas deben ser ligeras y persistentes, preferiblemente SQLite.
- Las descargas deben ejecutarse en workers secuenciales salvo que una herramienta justifique otra estrategia.
- No introducir Redis, Celery, RabbitMQ, bases externas o frontends pesados sin una necesidad clara.
- Al documentar comandos, indicar siempre desde que carpeta se ejecutan.

### Estructura Actual

```text
.
|-- spotube_downloader/            # Spotube-Downloader
|   |-- app.py
|   |-- templates/
|   |-- Dockerfile
|   `-- docker-compose.yml
|-- anime_web/                     # AnimeDownloader
|   |-- app.py
|   |-- anime_sources/
|   |-- templates/
|   |-- Dockerfile
|   `-- docker-compose.yml
`-- AGENTS.md
```

### Nombre Futuro del Repo

Cuando se renombre la carpeta del repo a `download_tools`, la estructura interna ya deberia mantenerse asi:

```text
download_tools/
|-- spotube_downloader/
|-- anime_downloader/
`-- AGENTS.md
```

No renombrar la carpeta raiz del repositorio hasta que se pida explicitamente.

---

## Stack Compartido

### Backend

- Python 3.11 o superior.
- Flask para HTTP.
- SQLite para persistencia local y colas.
- Workers con `threading` para procesos en segundo plano.
- Configuracion por variables de entorno y `.env.example`.
- Endpoints JSON simples para estado, acciones y datos de UI.

### Frontend

- HTML, CSS y JavaScript vanilla.
- Sin framework SPA salvo necesidad expresa.
- Una sola pagina funcional por herramienta, evitando landing pages.
- Refresco automatico ligero para estados de cola.
- Controles consistentes: botones compactos, tablas o tarjetas responsivas, barras de progreso finas y estados legibles.

### Docker

- Cada herramienta tiene su propio `Dockerfile`.
- Cada herramienta tiene su propio `docker-compose.yml`.
- No mezclar herramientas distintas en un unico compose salvo que se pida un entorno agregado.
- No fijar `platform` para mantener portabilidad entre amd64 y arm64.
- Los volumenes deben estar nombrados y documentados por herramienta.

---

## Estetica Compartida

Todas las herramientas deben sentirse parte de la misma familia:

- Interfaz clara, sobria y utilitaria.
- Fondo claro, paneles blancos, bordes suaves y radios de 6-8px.
- Paleta contenida con acento verde/teal y estados diferenciados:
  - pendiente: ambar
  - descargando: azul
  - completado: verde
  - fallido/cancelado critico: rojo
  - pausado: violeta
- Tipografia del sistema.
- Layout ancho en escritorio y tarjetas en movil.
- Sin decoracion pesada, heroes, gradientes ornamentales ni elementos visuales que distraigan.
- Las acciones de cola deben ser visibles, pequenas y consistentes entre herramientas.

Si se crea una nueva herramienta, copiar primero el lenguaje visual de Spotube-Downloader o AnimeDownloader antes de inventar una estetica nueva.

---

## Proxy inverso (Traefik)

Punto unico de entrada a todas las apps, en `proxy/`. Traefik escucha en el puerto
80 y enruta **por subruta** a cada contenedor; las apps ya **no publican puertos**
al host (solo el proxy), asi dejan de pelearse por puertos.

- Ruteo: `/` -> Launcher, `/spotube`, `/anime`, `/telegram`.
- Cada app declara su ruta con **labels** de Traefik en su propio `docker-compose.yml`,
  con un middleware **StripPrefix** que quita el prefijo antes de llegar a Flask.
- **Convencion `APP_BASE_PATH`**: como el frontend hace `fetch` con rutas absolutas
  desde la raiz, cada app recibe `APP_BASE_PATH` (p. ej. `/telegram`), lo pasa a la
  plantilla como `base_path`, y el JS lo antepone a sus llamadas (`API_BASE`). En
  acceso directo sin proxy, `APP_BASE_PATH=""`.
- Red Docker externa compartida `proxy`, creada una vez: `docker network create proxy`.
- Orden de despliegue: crear red -> stack `proxy` -> stacks de las apps.

Al tocar una app: si se anaden llamadas `fetch` nuevas, deben pasar por el helper
que antepone `API_BASE` (o anteponerlo manualmente), o se rompen bajo el proxy.

---

## Launcher

### Objetivo

Portal minimo Python/Flask con botones grandes hacia las herramientas. Responsive
(una columna en movil, tres en escritorio), misma estetica de la familia. Es la
unica excepcion a la regla de "evitar landing pages": su proposito *es* ser un
punto de entrada. No tiene estado ni base de datos: solo sirve una pagina.

### Restricciones

- Ubicacion: `launcher/`.
- La lista de apps (nombre, url, icono, descripcion, color) se define en
  **`config.json`** (`{"title", "apps": [{name,url,icon,desc,accent}]}`). Se relee
  en cada peticion; montando el fichero como volumen se edita sin reconstruir.
- Se sirve en la raiz `/` a traves del proxy; las `url` son rutas del mismo host
  (`/spotube`, `/anime`, `/telegram`).
- Sin dependencias mas alla de Flask.

### Docker

Es un stack detras del proxy (no publica puerto). Con la red `proxy` creada:

```bash
cd launcher
docker compose up --build -d
```

Accesible en `http://IP/`. `CONFIG_PATH` (opcional) apunta al `config.json`.

---

## Integracion con servidores multimedia

Cada herramienta puede avisar a un servidor multimedia externo para que refresque
su biblioteca al **completar** una descarga. Reglas comunes:

- Es **opcional**: si faltan las variables de entorno de conexion, no se hace nada.
- Solo **stdlib** (`urllib`/`hashlib`), sin dependencias nuevas.
- Con **debounce**: no se escanea en cada fichero, sino un unico escaneo tras
  `*_DEBOUNCE_SECONDS` sin nuevas descargas (agrupa lotes). Implementado con un
  `threading.Timer` que se rearma en cada aviso.
- El aviso **nunca** debe tumbar el worker: cualquier error se registra y se ignora.
- Se dispara en el punto donde una descarga pasa a `COMPLETED`.

Herramientas y destino:

- **Telegram-Downloader** y **AnimeDownloader** -> Jellyfin (`jellyfin.py`). Con
  `JELLYFIN_LIBRARY_ID` escanea esa biblioteca (`POST /Items/{id}/Refresh`); sin
  el, escaneo global (`POST /Library/Refresh`). Cabecera `X-Emby-Token`. Vars:
  `JELLYFIN_URL`, `JELLYFIN_API_KEY`, `JELLYFIN_LIBRARY_ID` (opcional),
  `JELLYFIN_REFRESH_DEBOUNCE_SECONDS`.
- **Spotube-Downloader** -> Navidrome (`navidrome.py`, Subsonic `startScan` con
  auth por token salt+md5). Al descargar una **playlist** de Spotify, ademas crea
  o actualiza una playlist homonima en Navidrome (`getScanStatus` + `search3` +
  `createPlaylist`), emparejando por artista+titulo. Vars: `NAVIDROME_URL`,
  `NAVIDROME_USER`, `NAVIDROME_PASSWORD`, `NAVIDROME_SCAN_DEBOUNCE_SECONDS`,
  `NAVIDROME_SCAN_WAIT_SECONDS`.

---

## Spotube-Downloader

### Objetivo

Aplicacion web ligera en Python/Flask para pegar una URL de Spotify, encolarla en SQLite y ejecutar `spotdl` en segundo plano para descargar audio MP3 a 192 kbps.

### Restricciones

- Debe seguir siendo apta para Raspberry Pi 3B.
- No usar Celery, Redis, RabbitMQ ni colas pesadas.
- Procesar descargas de forma estrictamente secuencial en un unico worker de fondo.
- Los archivos descargados se guardan en el volumen `/music`.
- Eliminar registros desde la interfaz solo borra filas de SQLite, nunca archivos fisicos.

### Archivos principales

- `spotube_downloader/app.py`: backend Flask, SQLite, workers y ejecucion de `spotdl`.
- `spotube_downloader/templates/index.html`: interfaz web.
- `spotube_downloader/requirements.txt`: dependencias Python.
- `spotube_downloader/Dockerfile`: imagen del descargador de musica.
- `spotube_downloader/docker-compose.yml`: levanta solo `spotube-downloader`.
- `spotube_downloader/.env.example`: variables de entorno de ejemplo para musica.

### Docker

Desde la carpeta de Spotube-Downloader:

```bash
cd spotube_downloader
cp .env.example .env
docker compose up --build -d
```

La interfaz queda en:

```text
http://localhost:8080
```

Variables importantes:

- `SPOTUBE_WEB_PORT`: puerto host, por defecto `8080`.
- `MUSIC_HOST_DIR`: carpeta host montada como `/music`, por defecto `./music`.
- `SPOTDL_COOKIE_HOST_DIR`: carpeta host montada como `/cookies`, por defecto `./cookies_store`.
- `SPOTDL_AUDIO_PROVIDERS`: por defecto `youtube-music,youtube`.
- `SPOTDL_THREADS`: mantener bajo en Raspberry Pi.
- `SPOTDL_REPROCESS_MISSING_ONLY`: por defecto recomendado `false` en Raspberry Pi.
- `SPOTDL_COOKIE_FILE`: archivo Netscape de cookies montado bajo `/cookies` si YouTube lo requiere.

---

## AnimeDownloader

### Objetivo

Aplicacion web Python/Flask para analizar fuentes de anime, listar series/capitulos y descargar videos desde fuentes autorizadas. La arquitectura debe ser extensible: toda fuente incluida debe heredar de una libreria base comun.

### Restricciones

- Ubicacion actual: `anime_web/`.
- La fuente base esta en `anime_web/anime_sources/base.py`.
- Las fuentes se descubren con `anime_web/anime_sources/registry.py`.
- Los extractores de video autorizados se centralizan en `anime_web/extractors/registry.py`.
- Una fuente solo puede descargar si declara explicitamente `allow_downloads = True`.
- La lista de episodios debe poder consumirse paginada.
- La cola procesa descargas una a una y no borra archivos fisicos al eliminar registros.
- La cola de AnimeDownloader se agrupa por anime: un anime completo es un trabajo plegable con capitulos hijos, procesados en orden ascendente.

### Archivos principales

- `anime_web/app.py`: backend Flask, SQLite y worker de descarga.
- `anime_web/templates/index.html`: interfaz web.
- `anime_web/anime_sources/base.py`: contrato base para fuentes.
- `anime_web/anime_sources/jkanime.py`: fuente JKAnime para catalogo, busqueda y episodios.
- `anime_web/anime_sources/direct.py`: fuente de descarga directa autorizada MP4/HLS.
- `anime_web/extractors/registry.py`: registro de extractores autorizados para convertir URLs en streams.
- `anime_web/extractors/direct.py`: extractor para URLs directas autorizadas MP4/WebM/MKV/HLS.
- `anime_web/anime_sources/demo.py`: fuente demo autorizada, desactivada por defecto.
- `anime_web/docs/jkanime_kotlin_analysis.md`: analisis del paquete Kotlin revisado.
- `anime_web/Dockerfile`: imagen de AnimeDownloader.
- `anime_web/docker-compose.yml`: levanta solo `anime-downloader`.

### Docker

Desde la carpeta de AnimeDownloader:

```bash
cd anime_web
cp .env.example .env
docker compose up --build -d
```

La interfaz queda en:

```text
http://localhost:8090
```

Variables importantes:

- `ANIME_WEB_PORT`: puerto host, por defecto `8090`.
- `ANIME_DATA_DIR`: carpeta host para SQLite, por defecto `./data`.
- `ANIME_DOWNLOAD_DIR`: carpeta host para descargas, por defecto `./downloads`.
- `ANIME_JKANIME_MAX_EPISODE_PAGES`: limite de paginas de episodios a consultar, por defecto `100`.
- `FFMPEG_BIN`: ejecutable usado para HLS directo, por defecto `ffmpeg`.
- `ANIME_ENABLE_DEMO`: activa la fuente demo, por defecto `false`.
- `ANIME_DEMO_MEDIA_URL`: URL MP4 directa opcional para la fuente demo.

---

## Telegram-Downloader

### Objetivo

Aplicacion web Python/Flask para interactuar con **bots de Telegram** (menus,
busquedas, botones inline) desde una cuenta de usuario y descargar los ficheros
que devuelven a un volumen local. La web refleja la conversacion con el bot como
una consola: escribes texto y pulsas sus botones, y los ficheros salen con boton
de descarga. (El modelo anterior de canales-catalogo se retiro.)

### Restricciones

- Ubicacion actual: `telegram_downloader/`.
- Interaccion y descarga via MTProto con sesion de usuario (Pyrogram), no Bot API.
- El cliente Pyrogram vive en su propio event loop dentro de un hilo dedicado
  (`tg_client.py`); el resto del proceso lo usa con funciones sincronas.
- La interaccion con el bot es por *polling* del historial (no modo updates):
  enviar texto = `send_message`; pulsar boton = `request_callback_answer`.
- El worker de descargas es estrictamente secuencial (un unico hilo).
- Los ficheros se guardan en el volumen `/downloads`, agrupados por bot.
- Eliminar un registro de la cola solo borra filas de SQLite, nunca el fichero.

### Archivos principales

- `telegram_downloader/app.py`: backend Flask, SQLite (bots, downloads) y worker.
- `telegram_downloader/tg_client.py`: cliente Pyrogram (conversacion con bot: `bot_history`/`bot_send`/`bot_click`; `download_message`).
- `telegram_downloader/login.py`: genera el `TG_SESSION_STRING` (login interactivo, una vez).
- `telegram_downloader/templates/index.html`: interfaz web (consola de bot + cola).
- `telegram_downloader/Dockerfile` y `docker-compose.yml`: imagen y arranque propios.

### Docker

Desde la carpeta de Telegram-Downloader:

```bash
cd telegram_downloader
cp .env.example .env   # rellena TG_API_ID, TG_API_HASH, TG_SESSION_STRING
docker compose up --build -d
```

La interfaz queda en:

```text
http://localhost:8100
```

Variables importantes:

- `TELEGRAM_WEB_PORT`: puerto host, por defecto `8100`.
- `TELEGRAM_DOWNLOAD_DIR`: carpeta host montada como `/downloads`.
- `TG_API_ID`, `TG_API_HASH`: credenciales de https://my.telegram.org.
- `TG_SESSION_STRING`: sesion de usuario generada con `login.py` (mantener en privado).
- `BOT_HISTORY_LIMIT`: mensajes recientes mostrados en la consola, por defecto `15`.

---

## Reglas de Mantenimiento

- Mantener las herramientas independientes: no mezclar servicios en un unico compose.
- Mantener stack y estetica comunes para todas las herramientas nuevas.
- No anadir dependencias pesadas sin necesidad.
- No borrar archivos descargados desde las acciones de historial/cola.
- Antes de cambiar una fuente de anime, comprobar que hereda de `BaseAnimeSource`.
- Antes de cambiar Spotube-Downloader, comprobar que el worker sigue siendo secuencial.
- Antes de cambiar Telegram-Downloader, comprobar que el worker sigue siendo secuencial, que el cliente Pyrogram vive en su propio event loop (`tg_client.py`) y que la interaccion con bots sigue siendo por polling del historial (no modo updates).
- Si se introduce una nueva herramienta, crear su carpeta propia y documentar sus comandos Docker.
