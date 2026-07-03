# Download Tools

Monorepo de herramientas web de descarga. El nombre actual de la carpeta local puede ser `spotube_downloader`, pero el proyecto esta orientado a llamarse `download_tools`.

Las herramientas comparten stack ligero:

- Python + Flask
- SQLite
- HTML/CSS/JS vanilla
- Docker y Docker Compose por herramienta

## Herramientas

### Spotube-Downloader

Descargador de musica con `spotdl`.

```bash
cd spotube_downloader
cp .env.example .env
docker compose up --build -d
```

Interfaz: `http://localhost:8080`

### AnimeDownloader

Descargador/experimento educativo de anime con fuentes extensibles.

```bash
cd anime_web
cp .env.example .env
docker compose up --build -d
```

Interfaz: `http://localhost:8090`

## Convenciones

- Cada herramienta se levanta de forma independiente.
- No hay servicios multimedia auxiliares ni integraciones externas obligatorias.
- Las colas deben ser ligeras y persistentes.
- Las UIs deben compartir una estetica sobria, clara y funcional.
- Las acciones de historial/cola no deben borrar archivos descargados.
