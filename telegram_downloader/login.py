"""Genera el TG_SESSION_STRING para Telegram-Downloader (login interactivo, una vez).

MTProto requiere iniciar sesion como usuario al menos una vez (telefono + codigo,
y 2FA si la tienes activada). Este script hace ese login en local y te imprime la
cadena de sesion para que la pegues en `.env` como TG_SESSION_STRING. A partir de
ahi el contenedor arranca sin login interactivo.

Uso:

    cd telegram_downloader
    pip install -r requirements.txt
    TG_API_ID=12345 TG_API_HASH=abcdef... python login.py

api_id / api_hash se obtienen en https://my.telegram.org -> API development tools.
"""
from __future__ import annotations

import os

from pyrogram import Client


def main() -> None:
    api_id = os.environ.get("TG_API_ID") or input("TG_API_ID: ").strip()
    api_hash = os.environ.get("TG_API_HASH") or input("TG_API_HASH: ").strip()

    with Client(
        name="login_tmp",
        api_id=int(api_id),
        api_hash=api_hash,
        in_memory=True,
    ) as app:
        session_string = app.export_session_string()
        me = app.get_me()

    print("\n================ SESION GENERADA ================")
    print(f"Conectado como: {me.first_name} (@{me.username or 'sin_username'})")
    print("\nPega esto en tu .env:\n")
    print(f"TG_SESSION_STRING={session_string}")
    print("\n=================================================")
    print("Guarda la cadena en privado: da acceso completo a tu cuenta.")


if __name__ == "__main__":
    main()
