# Spotube-Downloader

Herramienta web ligera para encolar URLs de Spotify y descargarlas con `spotdl` en MP3 a 192 kbps.

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
- `SPOTDL_COOKIE_FILE`: ruta interna, por ejemplo `/cookies/youtube.txt`.

