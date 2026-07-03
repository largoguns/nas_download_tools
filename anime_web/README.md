# AnimeDownloader

Pequena app Flask con arquitectura de fuentes extensibles inspirada en las extensiones Kotlin revisadas. La fuente activa por defecto es JKAnime para catalogo, busqueda y episodios.

## Ejecutar en local

```bash
cd anime_web
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Abre `http://localhost:8090`.

## Docker

```bash
cd anime_web
cp .env.example .env
docker compose up --build
```

## Crear una extension

1. Crea un modulo en `anime_sources/`.
2. Define una clase que herede de `BaseAnimeSource`.
3. Implementa `popular`, `search`, `details` y `episodes` segun tu fuente.
4. Implementa `get_video_streams` solo para contenido que tengas permiso de descargar.
5. Declara `allow_downloads = True` solo si la fuente devuelve streams directos autorizados.

La cola procesa descargas una a una y no borra archivos fisicos al eliminar registros.

## Fuentes incluidas

- `jkanime`: activo por defecto. Permite catalogo, busqueda y episodios paginados; las descargas no estan habilitadas.
- `direct`: activo por defecto. Permite encolar URLs directas MP4/HLS con permiso.
- `demo`: desactivado por defecto. Puede activarse con `ANIME_ENABLE_DEMO=true` para probar descargas directas autorizadas.

## Cola

Las descargas se agrupan por anime. Un capitulo suelto crea un trabajo con un item; un anime completo crea un trabajo plegable con todos sus capitulos en orden ascendente.

Las fuentes deben declarar `allow_downloads = True` y devolver streams directos autorizados para que los botones de descarga se activen.

Las URLs HLS (`.m3u8`) se descargan mediante `ffmpeg` dentro del contenedor.

## Analisis Kotlin

El resumen del paquete `es/jkanime` esta en `docs/jkanime_kotlin_analysis.md`.

Para inventariar las extensiones espanolas:

```bash
python tools/analyze_spanish_extensions.py ../to_be_analyzed/extensions-source-main/src/es
```
