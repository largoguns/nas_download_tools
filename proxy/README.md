# Proxy (Traefik)

Punto unico de entrada a todas las herramientas. Traefik escucha en el puerto 80
y enruta por **subruta** a cada app, que ya no necesita publicar su propio puerto:

| Ruta        | App                 |
|-------------|---------------------|
| `/`         | Launcher            |
| `/spotube`  | Spotube-Downloader  |
| `/anime`    | AnimeDownloader     |
| `/telegram` | Telegram-Downloader |

Cada app declara su ruta con **labels** de Traefik en su propio `docker-compose.yml`
(incluye un middleware *StripPrefix* que quita el prefijo antes de llegar a Flask).
Su frontend usa un `API_BASE` inyectado por entorno para que las llamadas lleven el
prefijo correcto.

## Requisito: red compartida

Todas las apps y el proxy comparten una red Docker externa llamada `proxy`. Se crea
**una sola vez** en el NAS (o desde Portainer):

```bash
docker network create proxy
```

## Levantar

```bash
cd proxy
cp .env.example .env
docker compose up -d
```

Luego levanta cada app (cada una es su propio stack). Accede a todo por
`http://IP_DEL_NAS/` (launcher) y `http://IP_DEL_NAS/spotube`, `/anime`, `/telegram`.

## Notas

- Solo el proxy publica el puerto 80. Las apps ya no exponen puertos al host, asi
  que dejan de pelearse entre si ni con otros servicios del NAS.
- El panel de Traefik viene desactivado. Para activarlo (inseguro, solo LAN),
  descomenta las lineas indicadas en `docker-compose.yml`.
- Orden de despliegue: crea la red `proxy` -> levanta este stack -> levanta las apps.
