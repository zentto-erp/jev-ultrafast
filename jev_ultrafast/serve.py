"""La misma recogida, detrás de HTTP, para lo que no corre en esta máquina.

Un agente que corre aquí no necesita esto: llamar a `collect` como biblioteca
—o `python -m jev_ultrafast.collect`— le ahorra la red, la autenticación y un
proceso que mantener vivo. Esta puerta existe para lo otro: CI, un backend, una
máquina distinta, varios agentes que comparten un solo navegador caliente.

    python -m jev_ultrafast.serve --port 5530
    curl -s -X POST localhost:5530/collect -H "Authorization: Bearer $JEV_TOKEN" \\
         -d '{"urls":["https://ejemplo.com"],"match":"Bitcoin"}'

Sin dependencias nuevas: la biblioteca estándar basta para un servicio de una
sola cosa, y una dependencia menos es una superficie menos que parchear.

🚨 **Esto es, por construcción, un servicio que trae URLs que le pidan.** Sin
frenos sería un proxy hacia dentro de la red de quien lo hospeda: bastaría
pedirle `http://127.0.0.1:8200` o una IP interna para que devolviera lo que hay
ahí. Por eso:

- Solo `http` y `https`. Nada de `file:`, `data:` ni esquemas del sistema.
- El nombre se **resuelve** y se comprueba la IP: se rechaza lo privado, el
  bucle local, el enlace local y lo reservado. Comprobar solo el texto de la
  URL no sirve — un nombre público puede resolver a 127.0.0.1, y ese es el
  truco entero.
- Escucha en `127.0.0.1` salvo que se diga otra cosa, y exige un token.

Y sigue sin pulsar nada: lee lo que la página enseña, nada más.
"""

import argparse
import ipaddress
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .collect import DEFAULT_ENDPOINT, collect, inspect_page, open_browser

MAX_BODY = 64 * 1024
MAX_URLS = 10


def reachable(url):
    """Comprueba la dirección, o dice por qué no.

    Devuelve `None` si está bien y un motivo si no. Se resuelve el nombre a
    propósito: la comprobación sobre el texto la esquiva cualquiera con un
    dominio que apunte a una dirección interna.
    """
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        return "only http and https"
    if not parsed.hostname:
        return "no host"
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or
                                   (443 if parsed.scheme == "https" else 80))
    except OSError:
        return "the name does not resolve"
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        # `is_global` es falso para privado, bucle local, enlace local,
        # multicast y reservado — que son justo los que convierten esto en un
        # proxy hacia dentro. Una sola dirección mala basta para rechazar: un
        # nombre puede resolver a varias y la elección no es nuestra.
        if not address.is_global:
            return f"{address} is not a public address"
    return None


class Collector:
    """Un navegador caliente, compartido y de uno en uno.

    Chrome aguanta varias pestañas, pero este motor tiene una sesión CDP por
    navegador y el estado de una observación es de la pestaña. Servir dos
    peticiones a la vez sobre lo mismo mezcla las dos y devuelve a cada una
    parte de la otra — un fallo que aparece solo bajo carga y no se reproduce
    nunca a mano. La cola es deliberada.
    """

    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.lock = threading.Lock()
        self.browser = None

    def gather(self, urls, match, settle, look=False):
        with self.lock:
            # Dos intentos, y el segundo con un navegador nuevo.
            #
            # Chrome se reinicia —se cae, lo reinicia systemd, se actualiza— y
            # la conexión que teníamos muere con él. Al que llama eso le
            # llegaba como un 502, que le dice que su petición estaba mal
            # cuando lo único que pasó es que el navegador se estaba
            # levantando. Medido: un reinicio de Chrome y la siguiente
            # petición fallaba, la de después iba bien.
            #
            # 🚨 Reintentar aquí es seguro porque esto SOLO LEE. Una acción que
            # cambia algo no se reintenta nunca: no hay forma de saber si el
            # primer intento llegó, y repetirlo duplicaría lo que hizo.
            for last in (False, True):
                if self.browser is None:
                    self.browser = open_browser(self.endpoint)
                try:
                    if look:
                        return [inspect_page(self.browser, url, settle) for url in urls]
                    return [collect(self.browser, url, match, settle) for url in urls]
                except Exception:
                    # Un navegador caído deja a este objeto sirviendo errores
                    # para siempre. Se suelta para que el siguiente intento lo
                    # levante otra vez en vez de heredar el cadáver.
                    try:
                        self.browser.close()
                    except Exception:
                        pass
                    self.browser = None
                    if last:
                        raise


def handler_for(collector, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "jev-collect"

        def log_message(self, fmt, *args):
            # El log por defecto escribe la URL pedida en stderr. Aquí eso es
            # el contenido de la petición, y no tiene por qué acabar en el
            # journal de nadie.
            pass

        def reply(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/healthz":
                # Sin token: es para el orquestador, y no dice nada de nadie.
                return self.reply(200, {"ok": True, "service": "jev-collect"})
            return self.reply(404, {"error": "not_found"})

        def do_POST(self):
            # Dos puertas al mismo navegador: `/collect` responde que DICE la
            # pantalla, `/inspect` responde si esta sana. Un agente de QA
            # necesita las dos, y separarlas evita que una corrida que solo
            # queria datos cargue con un informe de errores, y al reves.
            if self.path not in ("/collect", "/inspect"):
                return self.reply(404, {"error": "not_found"})
            given = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            # Comparación en tiempo constante: con `==` el tiempo de respuesta
            # filtra cuántos caracteres del token son correctos.
            import hmac
            if not token or not hmac.compare_digest(given, token):
                return self.reply(401, {"error": "unauthorized"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self.reply(400, {"error": "bad_length"})
            if length <= 0 or length > MAX_BODY:
                return self.reply(413, {"error": "body_too_large", "max_bytes": MAX_BODY})
            try:
                ask = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return self.reply(400, {"error": "bad_json"})
            urls = ask.get("urls") or ([ask["url"]] if ask.get("url") else [])
            if not isinstance(urls, list) or not urls:
                return self.reply(400, {"error": "no_urls"})
            if len(urls) > MAX_URLS:
                return self.reply(400, {"error": "too_many_urls", "max": MAX_URLS})
            for url in urls:
                why = reachable(url)
                if why:
                    return self.reply(400, {"error": "refused", "url": url, "because": why})
            match = ask.get("match")
            try:
                settle = min(float(ask.get("settle", 8.0)), 30.0)
            except (TypeError, ValueError):
                return self.reply(400, {"error": "bad_settle"})
            try:
                pages = collector.gather(urls, match, settle, look=self.path == "/inspect")
            except Exception as failed:
                return self.reply(502, {"error": "collect_failed", "because": str(failed)[:300]})
            return self.reply(200, pages[0] if len(pages) == 1 else {"pages": pages})

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(prog="jev-serve", description=__doc__)
    parser.add_argument("--port", type=int, default=5530)
    parser.add_argument("--host", default="127.0.0.1",
                        help="127.0.0.1 a propósito: exponerlo pide decidirlo")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT,
                        help="el Chrome sin ventana al que hablar")
    args = parser.parse_args(argv)

    token = os.environ.get("JEV_COLLECT_TOKEN")
    if not token:
        raise SystemExit("JEV_COLLECT_TOKEN no está en el entorno: sin token no se arranca, "
                         "porque un recolector de URLs sin autenticar es un proxy abierto")
    server = ThreadingHTTPServer((args.host, args.port),
                                 handler_for(Collector(args.endpoint), token))
    print(json.dumps({"listening": f"http://{args.host}:{args.port}",
                      "endpoints": ["GET /healthz", "POST /collect", "POST /inspect"]}))
    server.serve_forever()


if __name__ == "__main__":
    main()
