#!/usr/bin/env python3
"""ig-stats · configurar.py: deja listo discovery.json, el fichero con el que el servidor lee tus estadísticas.

Qué hace
    1. Coge el token corto que sacas del Explorador de la API Graph de Meta (dura una hora).
    2. Lo cambia por uno largo con el ID y la clave secreta de tu app de Meta.
    3. Pide tus páginas de Facebook y la cuenta de Instagram profesional unida a cada una.
       El token de una página sacado de un token largo no caduca.
    4. Escribe discovery.json en esta carpeta, con permisos solo para ti.

Uso
    python3 configurar.py
    (te pregunta el ID de la app, la clave secreta y el token; no se guardan más que en discovery.json)

    O sin preguntas:
    IG_APP_ID=... IG_APP_SECRET=... IG_TOKEN=... python3 configurar.py

Solo biblioteca estándar de Python. No envía nada a ningún sitio salvo a graph.facebook.com.
"""
import getpass
import json
import os
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://graph.facebook.com/v22.0"
AQUI = os.path.dirname(os.path.abspath(__file__))
DESTINO = os.path.join(AQUI, "discovery.json")


def pedir(url, params):
    try:
        with urllib.request.urlopen(url + "?" + urllib.parse.urlencode(params), timeout=30) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8")).get("error", {})
            return None, "%s (código %s)" % (err.get("message", e.reason), err.get("code", e.code))
        except ValueError:
            return None, "HTTP %s" % e.code
    except (urllib.error.URLError, OSError) as e:
        return None, str(e)


def dato(nombre, variable, secreto=False):
    v = os.environ.get(variable, "").strip()
    if v:
        return v
    v = (getpass.getpass if secreto else input)("%s: " % nombre).strip()
    if not v:
        sys.exit("Falta %s." % nombre)
    return v


def main():
    print("Configurar ig-stats. Necesitas el ID y la clave secreta de tu app de Meta y un token del Explorador "
          "(mira el README, paso 2).\n")
    app_id = dato("ID de la app", "IG_APP_ID")
    secreto = dato("Clave secreta de la app", "IG_APP_SECRET", secreto=True)
    corto = dato("Token del Explorador", "IG_TOKEN", secreto=True)

    largo, err = pedir(API + "/oauth/access_token", {"grant_type": "fb_exchange_token", "client_id": app_id,
                                                      "client_secret": secreto, "fb_exchange_token": corto})
    if err:
        sys.exit("No he podido alargar el token: %s\nSuele ser que el token ya caducó (dura una hora) o que el ID y la "
                 "clave no son de la misma app." % err)
    token_largo = largo["access_token"]

    campos = "id,name,access_token,instagram_business_account{id,username,name,followers_count,media_count}"
    paginas, err = pedir(API + "/me/accounts", {"fields": campos, "access_token": token_largo, "limit": 100})
    if err:
        sys.exit("No he podido leer tus páginas: %s\nRevisa que el token tenga el permiso pages_show_list." % err)

    cuentas = []
    for p in paginas.get("data", []):
        ig = p.get("instagram_business_account")
        if not ig:
            continue
        cuentas.append({"page_id": p["id"], "page_name": p.get("name", ""), "page_access_token": p["access_token"],
                        "ig_id": ig["id"], "ig_username": ig.get("username", ""), "ig_name": ig.get("name", ""),
                        "followers": ig.get("followers_count"), "media_count": ig.get("media_count")})
    if not cuentas:
        sys.exit("Ninguna de tus páginas de Facebook tiene una cuenta de Instagram profesional unida. Únela en la app "
                 "de Instagram: Configuración > Centro de cuentas, o desde la página de Facebook.")

    if len(cuentas) > 1:
        print("Tienes varias cuentas de Instagram:")
        for i, c in enumerate(cuentas, 1):
            print("  %d. @%s (página %s)" % (i, c["ig_username"], c["page_name"]))
        n = input("¿Cuál uso? [1]: ").strip() or "1"
        try:
            elegida = cuentas.pop(int(n) - 1)
        except (ValueError, IndexError):
            sys.exit("Número no válido.")
        cuentas.insert(0, elegida)

    with open(DESTINO, "w", encoding="utf-8") as fh:
        json.dump({"ig_accounts": cuentas}, fh, ensure_ascii=False, indent=2)
    os.chmod(DESTINO, stat.S_IRUSR | stat.S_IWUSR)
    c = cuentas[0]
    print("\nListo: @%s, %s seguidores, %s publicaciones. Guardado en %s (solo lo puedes leer tú; no lo subas a "
          "ningún sitio)." % (c["ig_username"], c["followers"], c["media_count"], DESTINO))


if __name__ == "__main__":
    main()
