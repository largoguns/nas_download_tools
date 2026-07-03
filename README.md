# NAS Download Tools

Monorepo de herramientas web de descarga autoalojadas, pensadas para un NAS
(OMV + Portainer) detras de un proxy inverso. Comparten un stack ligero y una
estetica comun.

**Stack:** Python + Flask · SQLite · HTML/CSS/JS vanilla · Docker · Traefik.

## Herramientas

| App | Que hace | Ruta (tras el proxy) |
|-----|----------|----------------------|
| **Launcher** | Portal con botones hacia las demas (lista en `config.json`). | `/` |
| **Spotube-Downloader** | Descarga musica con `spotdl`. Refresca **Navidrome**. | `/spotube` |
| **AnimeDownloader** | Descarga anime de fuentes autorizadas. Refresca **Jellyfin**. | `/anime` |
| **Telegram-Downloader** | Interactua con bots de Telegram (menus/busquedas/botones) via MTProto y descarga sus ficheros. Refresca **Jellyfin**. | `/telegram` |
| **Proxy (Traefik)** | Punto unico de entrada; enruta por subruta. Las apps no publican puertos. | — |

## Despliegue

La guia completa (orden, stacks y **todas las variables de entorno** por app) esta en
**[DEPLOY.md](DEPLOY.md)**. En resumen:

1. `docker network create proxy` (una vez).
2. Levantar el stack `proxy/`.
3. Levantar cada app como su propio stack en Portainer, apuntando al `docker-compose.yml`
   de su carpeta, con sus variables de entorno (ahi van los secretos, no estan en git).

Acceso: `http://IP_DEL_NAS/` (launcher) y `/spotube`, `/anime`, `/telegram`.

## Desarrollo local

Cada herramienta se puede levantar sola. Para acceso directo sin proxy, descomenta
su bloque `ports:` en el `docker-compose.yml` y pon `APP_BASE_PATH=""`.

```bash
cd <herramienta>
cp .env.example .env      # rellena lo necesario
docker compose up --build -d
```

## Convenciones

- Cada herramienta es independiente: su propio contenedor, compose y documentacion.
- Colas ligeras y persistentes (SQLite); descargas en workers secuenciales.
- UIs sobrias, claras y funcionales, con estetica compartida.
- Las acciones de historial/cola nunca borran los archivos ya descargados.
- Al completar descargas, cada app puede refrescar la biblioteca de su servidor
  multimedia (Jellyfin/Navidrome), configurable por entorno.

Detalles internos y reglas para contribuir: **[AGENTS.md](AGENTS.md)**.
