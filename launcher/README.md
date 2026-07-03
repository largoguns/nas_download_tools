# Launcher

Portal minimo (Python/Flask) con botones grandes hacia las herramientas del
monorepo. Responsive (una columna en movil, tres en escritorio) y con la misma
estetica de la familia. La lista de apps se define en **`config.json`**.

Se sirve en la raiz `/` a traves del proxy (Traefik); no publica puerto propio.

## config.json

```json
{
  "title": "Herramientas de descarga",
  "apps": [
    { "name": "Spotube", "desc": "Musica desde Spotify", "icon": "🎵", "url": "/spotube", "accent": "#16794c" },
    { "name": "AnimeDownloader", "desc": "Series y peliculas de anime", "icon": "🎬", "url": "/anime", "accent": "#1d4ed8" },
    { "name": "Telegram-Downloader", "desc": "Ficheros desde bots de Telegram", "icon": "📥", "url": "/telegram", "accent": "#0f766e" }
  ]
}
```

Campos por app: `name`, `url`, `icon` (emoji), `desc` (opcional), `accent`
(color, opcional). Con el proxy, las `url` son rutas del mismo host (`/spotube`,
`/anime`, `/telegram`). El fichero se relee en cada carga de pagina, asi que si lo
montas como volumen puedes cambiar los botones **sin reconstruir**.

## Docker

Es un stack mas detras del proxy. Con la red `proxy` ya creada:

```bash
cd launcher
docker compose up --build -d
```

Queda accesible en `http://IP_DEL_NAS/`.

Para editar la lista sin reconstruir, descomenta el volumen en `docker-compose.yml`:

```yaml
    volumes:
      - ./config.json:/app/config.json:ro
```

## Nota

No hay estado ni base de datos: solo sirve una pagina a partir de `config.json`.
