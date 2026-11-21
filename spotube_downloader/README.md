# Spotube-Downloader

Herramienta web ligera para encolar URLs de Spotify y descargarlas con `spotdl` en MP3 a 192 kbps.

Tambien acepta enlaces de **YouTube Music** (cancion `watch?v=...` o playlist/album
`playlist?list=...`). Esos no pasan por spotdl: se descargan directamente con
yt-dlp (`ytmusic.py`), sin emparejar con Spotify. Los metadatos salen de YouTube
Music; en videos musicales sin metadatos se deducen del titulo ("Artista - Titulo").
Las playlists de album (`OLAK5uy_...`) se guardan como un unico album.

## Docker

Desde esta carpeta:

```bash
cp .env.example .env
docker compose up --build -d
```

Interfaz: `http://localhost:8080`

## Carpetas

- `music/`: salida de musica montada como `/music`.
- `cookies_store/`: cookies opcionales montadas como `/cookies`.
- `templates/`: interfaz web.

## Variables principales

- `SPOTUBE_WEB_PORT`: puerto host, por defecto `8080`.
- `MUSIC_HOST_DIR`: carpeta host de musica, por defecto `./music`.
- `SPOTDL_COOKIE_HOST_DIR`: carpeta host de cookies, por defecto `./cookies_store`.
- `SPOTDL_COOKIE_FILE`: ruta interna, por ejemplo `/cookies/youtube.txt` (tambien la usan los enlaces de YouTube Music).
- `SPOTDL_THREADS`: canciones en paralelo **dentro** de un trabajo de spotdl (playlist/album de Spotify).
- `DOWNLOAD_WORKERS`: trabajos de la cola descargandose a la vez, por defecto `5`. Es el valor inicial: se cambia en caliente desde la interfaz (Descargas > Simultaneas) y se guarda en SQLite, con prioridad sobre la variable.
- `DOWNLOAD_WORKERS_MAX`: tope del selector, por defecto `10`.
- `YTMUSIC_DENO_PATH`: runtime JS para yt-dlp; por defecto el que instala `spotdl --download-deno`.

