# Despliegue en NAS (OMV + Portainer)

Guia de despliegue del monorepo con Portainer, desde este repositorio de GitHub.
Son **5 stacks** (proxy + 4 apps) que comparten una red Docker y un HDD.

- Arquitectura: un **proxy Traefik** escucha en un unico puerto y enruta por subruta.
  Las apps **no publican puertos** (se acabaron los conflictos).

| Ruta                | Stack                | Compose path                          |
|---------------------|----------------------|---------------------------------------|
| `/`                 | Launcher             | `launcher/docker-compose.yml`         |
| `/spotube`          | Spotube-Downloader   | `spotube_downloader/docker-compose.yml` |
| `/anime`            | AnimeDownloader      | `anime_web/docker-compose.yml`        |
| `/telegram`         | Telegram-Downloader  | `telegram_downloader/docker-compose.yml` |
| (infra)             | Proxy (Traefik)      | `proxy/docker-compose.yml`            |

---

## 0. Requisitos previos (una sola vez)

1. **Red Docker compartida** (todas las apps y el proxy la usan):
   ```bash
   docker network create proxy
   ```
   En Portainer: *Networks -> Add network -> Name `proxy`* (driver bridge).

2. **Carpetas en el HDD** para los datos y descargas. Recomendado crear rutas
   dedicadas, p. ej.:
   ```
   /srv/<tu-disco>/media/musica
   /srv/<tu-disco>/media/anime
   /srv/<tu-disco>/media/peliculas
   /srv/<tu-disco>/appdata/anime/data
   ```
   > **Usa rutas ABSOLUTAS del HDD** en las variables `*_DIR`/`*_HOST_DIR`. Las
   > rutas relativas (`./downloads`) en un stack de Portainer-desde-Git acaban en
   > una ubicacion poco predecible; en el NAS pon siempre rutas absolutas.

3. (Opcional) **Jellyfin** y **Navidrome** ya funcionando, con sus bibliotecas
   apuntando a esas mismas carpetas del HDD.

## Orden de despliegue

1. Crear la red `proxy`.
2. Stack **proxy**.
3. Stacks de las apps (en cualquier orden): spotube, anime, telegram, launcher.

## Como se crea cada stack en Portainer

*Stacks -> Add stack -> Repository*:
- **Repository URL**: `https://github.com/largoguns/nas_download_tools.git`
- **Compose path**: el de la tabla de arriba (p. ej. `telegram_downloader/docker-compose.yml`)
- **Environment variables**: las de cada seccion de abajo (aqui van los secretos;
  no estan en git).

---

## 1. Proxy (Traefik)

Punto unico de entrada. Es lo unico que publica un puerto.

| Variable          | Req. | Defecto | Descripcion |
|-------------------|------|---------|-------------|
| `PROXY_HTTP_PORT` | No   | `80`    | Puerto HTTP publico del NAS. **Si OMV ya usa el 80/443, pon otro** (p. ej. `8888`). |

> Acceso: `http://IP_DEL_NAS:<PROXY_HTTP_PORT>/`. Con `80` basta `http://IP_DEL_NAS/`.

---

## 2. Spotube-Downloader (`/spotube`)

Descarga musica con `spotdl`. Refresca **Navidrome** al terminar.

### Rutas / volumenes

| Variable                 | Req. | Defecto           | Descripcion |
|--------------------------|------|-------------------|-------------|
| `MUSIC_HOST_DIR`         | Si*  | `./music`         | Carpeta del HDD para la musica (montada en `/music`). Pon ruta **absoluta**. |
| `SPOTDL_COOKIE_HOST_DIR` | No   | `./cookies_store` | Carpeta con cookies de YouTube si hicieran falta (montada en `/cookies`, solo lectura). |

### Integracion con Navidrome (opcional)

| Variable                          | Req. | Defecto | Descripcion |
|-----------------------------------|------|---------|-------------|
| `NAVIDROME_URL`                   | No   | (vacio) | URL de Navidrome, p. ej. `http://navidrome:4533`. Vacio = no escanea. |
| `NAVIDROME_USER`                  | No   | (vacio) | Usuario de Navidrome. |
| `NAVIDROME_PASSWORD`              | No   | (vacio) | Contrasena de ese usuario (**secreto**). |
| `NAVIDROME_SCAN_DEBOUNCE_SECONDS` | No   | `30`    | Espera tras la ultima descarga antes de un unico escaneo. |
| `NAVIDROME_SCAN_WAIT_SECONDS`     | No   | `180`   | Al descargar una playlist se crea/actualiza en Navidrome; espera max. a que termine el escaneo antes de emparejar las canciones. |

> Al descargar una **playlist** de Spotify, ademas del escaneo se crea (o
> actualiza, si ya existe con ese nombre) una playlist en Navidrome con las
> canciones descargadas. Requiere `NAVIDROME_USER/PASSWORD` con permisos.

### Busqueda en el catalogo (pantalla "Busqueda")

Usa la **API publica de Deezer**: sin credenciales, sin configuracion y sin
Premium. No hay variables que rellenar. Muestra resultados (titulo/artista/
caratula) y, al anadir, spotdl descarga la mejor coincidencia de esa query.

### Ajustes de spotdl (opcionales, tienen buen defecto)

| Variable | Defecto | Descripcion |
|----------|---------|-------------|
| `SPOTDL_AUDIO_PROVIDERS` | `youtube-music,youtube` | Proveedores de audio. |
| `SPOTDL_THREADS` | `1` | Hilos de spotdl (bajo en Raspberry). |
| `SPOTDL_LOG_LEVEL` | `INFO` | Nivel de log. |
| `SPOTDL_MAX_ATTEMPTS` | `5` | Reintentos por cancion. |
| `SPOTDL_RETRY_DELAY_SECONDS` | `120` | Espera base entre reintentos. |
| `SPOTDL_RETRY_BACKOFF_FACTOR` | `2` | Factor de backoff. |
| `SPOTDL_RETRY_MAX_DELAY_SECONDS` | `900` | Tope de espera. |
| `SPOTDL_SPOTIFY_MAX_RETRIES` | `5` | Reintentos contra la API de Spotify. |
| `SPOTDL_SAVE_TIMEOUT_SECONDS` | `60` | Timeout de guardado. |
| `SPOTDL_REPROCESS_MISSING_ONLY` | `false` | Reprocesar solo lo que falta. |
| `SPOTDL_COOKIE_FILE` | (vacio) | Ruta a cookies Netscape bajo `/cookies` si YouTube pide login. |
| `SPOTDL_YT_DLP_ARGS` | (vacio) | Args extra para yt-dlp. |
| `APP_BASE_PATH` | `/spotube` | Prefijo bajo el proxy. Solo cambialo para acceso directo (`""`). |

\* *Funciona sin `MUSIC_HOST_DIR` (usa un volumen), pero para que Navidrome vea la
musica debe apuntar a la carpeta del HDD.*

---

## 3. AnimeDownloader (`/anime`)

Descarga anime de fuentes autorizadas. Refresca **Jellyfin** al terminar.

### Rutas / volumenes

| Variable            | Req. | Defecto       | Descripcion |
|---------------------|------|---------------|-------------|
| `ANIME_DOWNLOAD_DIR`| Si*  | `./downloads` | Carpeta del HDD para los videos (montada en `/downloads`). Ruta **absoluta**. |
| `ANIME_DATA_DIR`    | No   | `./data`      | Carpeta para la BD SQLite (montada en `/data`). |

### Integracion con Jellyfin (opcional)

| Variable                            | Req. | Defecto | Descripcion |
|-------------------------------------|------|---------|-------------|
| `JELLYFIN_URL`                      | No   | (vacio) | URL de Jellyfin, p. ej. `http://jellyfin:8096`. Vacio = no escanea. |
| `JELLYFIN_API_KEY`                  | No   | (vacio) | API key (Panel -> API Keys) (**secreto**). |
| `JELLYFIN_LIBRARY_ID`               | No   | (vacio) | ItemId de la biblioteca de Anime. Vacio = escaneo global. |
| `JELLYFIN_REFRESH_DEBOUNCE_SECONDS` | No   | `30`    | Espera antes de un unico escaneo. |

### Ajustes de descarga (opcionales)

| Variable | Defecto | Descripcion |
|----------|---------|-------------|
| `PREFERRED_QUALITY` | `360p` | Calidad preferida. |
| `DOWNLOAD_CONCURRENCY` | `1` | Descargas simultaneas (1 = secuencial). |
| `DOWNLOAD_MAX_RETRIES` | `5` | Reintentos al reanudar. |
| `DOWNLOAD_START_RETRIES` | `1` | Reintentos cuando el servidor no responde aun. |
| `ANIME_DOWNLOAD_SERVER_PRIORITY` | (vacio) | Orden de servidores a probar. |
| `ANIME_JKANIME_MAX_EPISODE_PAGES` | `100` | Limite de paginas de episodios. |
| `ANIME_JKANIME_DIRECT_DOWNLOAD` | `true` | Descarga directa por el CDN de jkanime. |
| `ANIME_JKANIME_EXTRACTOR_FALLBACK` | `true` | Extractores como fallback. |
| `FFMPEG_BIN` | `ffmpeg` | Ejecutable de ffmpeg (para HLS). |
| `LOG_LEVEL` | `INFO` | Nivel de log. |
| `APP_BASE_PATH` | `/anime` | Prefijo bajo el proxy. |

\* *Debe apuntar a la carpeta del HDD que vigila Jellyfin.*

---

## 4. Telegram-Downloader (`/telegram`)

Interactua con bots de Telegram y descarga sus ficheros. Refresca **Jellyfin**.

### Credenciales MTProto (requeridas para que funcione)

| Variable            | Req. | Defecto | Descripcion |
|---------------------|------|---------|-------------|
| `TG_API_ID`         | **Si** | (vacio) | api_id de https://my.telegram.org. |
| `TG_API_HASH`       | **Si** | (vacio) | api_hash de https://my.telegram.org (**secreto**). |
| `TG_SESSION_STRING` | **Si** | (vacio) | Sesion de usuario generada con `python login.py` (**secreto**, da acceso total a tu cuenta). |

### Rutas / volumenes

| Variable                | Req. | Defecto       | Descripcion |
|-------------------------|------|---------------|-------------|
| `TELEGRAM_DOWNLOAD_DIR` | Si*  | `./downloads` | Carpeta del HDD para los ficheros (montada en `/downloads`). Ruta **absoluta**. |

### Integracion con Jellyfin (opcional)

| Variable                            | Req. | Defecto | Descripcion |
|-------------------------------------|------|---------|-------------|
| `JELLYFIN_URL`                      | No   | (vacio) | URL de Jellyfin. Vacio = no escanea. |
| `JELLYFIN_API_KEY`                  | No   | (vacio) | API key (**secreto**). |
| `JELLYFIN_LIBRARY_ID`               | No   | (vacio) | ItemId de la biblioteca de Peliculas/Series. Vacio = global. |
| `JELLYFIN_REFRESH_DEBOUNCE_SECONDS` | No   | `30`    | Espera antes de un unico escaneo. |

### Ajustes (opcionales)

| Variable | Defecto | Descripcion |
|----------|---------|-------------|
| `BOT_HISTORY_LIMIT` | `15` | Mensajes recientes en la consola del bot. |
| `WORKER_SLEEP_SECONDS` | `3` | Cadencia del worker con la cola vacia. |
| `PROGRESS_UPDATE_SECONDS` | `1` | Cada cuanto persiste el progreso. |
| `LOG_LEVEL` | `INFO` | Nivel de log. |
| `APP_BASE_PATH` | `/telegram` | Prefijo bajo el proxy. |

\* *Debe apuntar a la carpeta del HDD que vigila Jellyfin.*

**Como generar `TG_SESSION_STRING`** (una vez, en tu equipo, no en el NAS):
```bash
cd telegram_downloader
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt
TG_API_ID=... TG_API_HASH=... ./.venv/bin/python login.py
# copia la linea TG_SESSION_STRING=... que imprime
```

---

## 5. Launcher (`/`)

Portal con botones hacia las apps. La lista sale de `launcher/config.json`.

| Variable       | Req. | Defecto            | Descripcion |
|----------------|------|--------------------|-------------|
| `CONFIG_PATH`  | No   | `/app/config.json` | Ruta al `config.json`. Monta el tuyo como volumen para editarlo sin reconstruir. |

Los botones (nombre, url, icono, color) se editan en `config.json`. Con el proxy,
las `url` son rutas del mismo host (`/spotube`, `/anime`, `/telegram`).

---

## Checklist de secretos (lo minimo a rellenar)

- [ ] **Telegram**: `TG_API_ID`, `TG_API_HASH`, `TG_SESSION_STRING` (obligatorias).
- [ ] **Jellyfin** (si lo usas, en anime y telegram): `JELLYFIN_URL`, `JELLYFIN_API_KEY` (+ `JELLYFIN_LIBRARY_ID` si quieres por biblioteca).
- [ ] **Navidrome** (si lo usas, en spotube): `NAVIDROME_URL`, `NAVIDROME_USER`, `NAVIDROME_PASSWORD`.
- [ ] **Rutas del HDD**: `MUSIC_HOST_DIR`, `ANIME_DOWNLOAD_DIR` (+`ANIME_DATA_DIR`), `TELEGRAM_DOWNLOAD_DIR` en rutas absolutas.
- [ ] **Proxy**: `PROXY_HTTP_PORT` si el 80 esta ocupado por OMV.

## Avisos importantes

- **URLs internas vs. del navegador**: `JELLYFIN_URL`/`NAVIDROME_URL` las usa el
  *contenedor* -> usa el nombre de servicio en la red Docker o la IP del NAS
  (no `localhost`). Los botones del launcher los abre el *navegador* -> por eso son
  rutas del proxy (`/spotube`...).
- **Puerto 80**: OMV suele ocupar 80/443 con su panel. Si es tu caso, `PROXY_HTTP_PORT=8888`.
- **Permisos**: los contenedores escriben como root; los ficheros descargados
  quedaran con propietario root. Ajusta permisos de la carpeta compartida en OMV
  si necesitas acceso desde otros usuarios/servicios.
- **Acceso directo para depurar**: cada app trae su bloque `ports:` comentado. Si
  lo reactivas, pon tambien `APP_BASE_PATH=""` en esa app.
- **Secretos**: nunca van a git (`.env` esta ignorado). Se configuran en las
  *Environment variables* de cada stack en Portainer.
