from __future__ import annotations

import json
import logging
import os

from flask import Flask, jsonify, render_template

app = Flask(__name__)

PORT = int(os.environ.get("PORT", "8000"))
CONFIG_PATH = os.environ.get("CONFIG_PATH", os.path.join(os.path.dirname(__file__), "config.json"))
DEFAULT_TITLE = "Herramientas de descarga"

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")


def load_config() -> dict:
    """Lee la lista de apps desde config.json.

    Se lee en cada peticion (es barato), asi editar el fichero montado surte
    efecto sin reconstruir ni reiniciar. Si falta o es invalido, devuelve vacio.
    """
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        logging.warning("config.json no encontrado en %s", CONFIG_PATH)
        return {"title": DEFAULT_TITLE, "apps": []}
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("config.json invalido (%s)", exc)
        return {"title": DEFAULT_TITLE, "apps": []}

    apps = []
    for entry in data.get("apps", []):
        if not isinstance(entry, dict):
            continue
        apps.append(
            {
                "name": entry.get("name") or entry.get("title") or "App",
                "url": str(entry.get("url", "")).strip(),
                "icon": entry.get("icon") or "\U0001F517",
                "desc": entry.get("desc") or entry.get("description") or "",
                "accent": entry.get("accent") or "#0f766e",
            },
        )
    return {"title": data.get("title", DEFAULT_TITLE), "apps": apps}


@app.get("/")
def index():
    config = load_config()
    return render_template("index.html", title=config["title"], apps=config["apps"])


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
