"""Repetir una decision ya tomada, en vez de volver a pagarla.

La primera vez que un recorrido llega a una pantalla, decidir es un juicio:
alguien —o algo— mira las opciones y elige. La segunda vez no lo es. Si la
pantalla ofrece lo mismo y el objetivo es el mismo, la respuesta es la misma, y
volver a preguntarla cuesta una peticion y una espera por cada paso.

Esto guarda lo decidido y lo reproduce. Tres decisiones de diseno que importan
mas que el codigo:

**Se guardan etiquetas, no identificadores.** Un id de nodo muere en cuanto la
pagina vuelve a renderizar, asi que cada paso se resuelve contra una observacion
fresca, igual que lo haria una persona leyendo la pantalla. Es lo mismo que hace
`replay.py`, y por lo mismo: un recorrido guardado sobrevive a un cambio de
maquetacion y falla cuando de verdad cambio lo que el recorrido hacia.

**La clave es el objetivo y el numero de paso, no la pantalla.** Un recorrido es
una secuencia de decisiones —el paso 3 de "crear una factura"— y no un mapa de
pantallas. La primera version metia en la clave las opciones ofrecidas, lo cual
parecia mas robusto y rompia lo que importa: si cambia una etiqueta, la clave
cambia, no se encuentra nada, y volver a preguntar es un acierto de cache fallido
en vez de una divergencia. Con eso el modo estricto NUNCA dispararia y toda
deriva de la aplicacion saldria en verde, que es el fallo exacto que estos dos
modos existen para evitar.

Tampoco la huella de la observacion, que ya existe y seria comodo: incluye el
texto, asi que un reloj o un contador de notificaciones invalidarian la entrada
en cada corrida. Lo que la pantalla ofrece no es la clave — es lo que se
comprueba al resolver.

**Dos modos, y el estricto es el de por defecto.** Son dos usos con exigencias
opuestas y mezclarlos es lo que hace que un fallo salga en verde:

- En pruebas, que el guion deje de encajar ES el resultado. Caer al modelo en
  silencio convertiria una deriva de la aplicacion en una corrida correcta, y
  nadie se enteraria de que lo guardado ya no la describe.
- En automatizacion, un rediseno menor no deberia parar el trabajo: se vuelve a
  preguntar, se reescribe lo guardado y se dice que se hizo.

Nunca entra una contrasena: la observacion no ofrece campos de tipo `password`,
asi que no hay forma de que una acabe aqui.
"""

import hashlib
import json
from pathlib import Path


def key(goal, step):
    """Lo que identifica una decision: el objetivo y el paso dentro de el.

    Ni la pantalla ni la direccion entran aqui. Si entraran, cualquier cambio en
    ellas produciria una clave nueva —un fallo de cache— cuando lo que de verdad
    ocurrio es que la aplicacion dejo de encajar con lo guardado, y eso hay que
    poder distinguirlo. La direccion se guarda DENTRO de la entrada y se
    comprueba al resolver, que es donde una discrepancia significa algo.
    """
    material = json.dumps({"goal": goal, "step": int(step)}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def worth_remembering(decision, action, text=None, url=None):
    """La forma en que se guarda un paso: que operacion, sobre que etiqueta, donde.

    `DONE` y `BLOCKED` tambien se guardan. Que un recorrido acabe ahi es una
    decision como cualquier otra, y la mas repetida de todas.
    """
    remembered = {"operation": decision["operation"], "url": url}
    if action is not None:
        remembered["kind"] = action["kind"]
        remembered["label"] = action.get("label")
        if action["kind"] == "tab":
            # Una pestana no tiene etiqueta estable —su titulo cambia con lo que
            # carga— asi que lo que se guarda es la intencion, no el nombre.
            remembered["label"] = None
    if text is not None:
        remembered["text"] = text
    return remembered


def resolve(page, remembered):
    """Encontrar en la observacion de AHORA lo que se decidio antes.

    Devuelve la accion, o `None` si lo guardado ya no esta. Se compara por
    etiqueta y tipo, que es lo que una persona leeria; por id seria inutil.

    La direccion se comprueba primero: si el paso 3 ocurria en la pantalla de
    facturas y ahora estamos en otra, lo guardado no describe esto y da igual que
    exista un boton con el mismo nombre.
    """
    where = remembered.get("url")
    if where and page.get("url") and where != page["url"]:
        return None
    if remembered.get("operation") in {"DONE", "BLOCKED"}:
        return remembered["operation"]
    label, kind = remembered.get("label"), remembered.get("kind")
    if kind == "tab":
        # Cualquier pestana que este recorrido haya provocado sirve: lo que se
        # guardo es "ir a la que se acaba de abrir".
        return next((a for a in page.get("actions", []) if a["kind"] == "tab"), None)
    if not label:
        return None
    for action in page.get("actions", []):
        if action["kind"] == kind and action.get("label") == label:
            return action
    return None


class Decisions:
    """Las decisiones guardadas de un recorrido, en un fichero.

    Un fichero por directorio y no una base de datos porque tiene que poder
    viajar: entrar en el repositorio junto al caso que describe, moverse entre
    maquinas, y leerse cuando algo no cuadra. Una cache que no se puede abrir con
    un editor es una cache en la que no se puede confiar.
    """

    def __init__(self, where, heal=False):
        self.path = Path(where) / "decisions.json"
        self.heal = heal
        self.hits = 0
        self.misses = 0
        self.healed = 0
        try:
            # utf-8-sig porque en Windows casi todo lo que escribe un fichero a
            # mano le pone un BOM delante, y `json.loads` lo rechaza con un error
            # que habla de bytes y no de lo que pasa.
            self.entries = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            self.entries = {}
        if not isinstance(self.entries, dict):
            self.entries = {}

    def look_up(self, goal, step):
        """Lo decidido antes en este paso de este objetivo, si lo hay."""
        return self.entries.get(key(goal, step))

    def remember(self, goal, step, decision, action, text=None, url=None):
        self.entries[key(goal, step)] = worth_remembering(decision, action, text, url)
        self.flush()

    def forget(self, goal, step):
        """Quitar una entrada que dejo de describir la aplicacion.

        Solo en modo self-heal. En estricto la entrada se queda, porque lo que se
        quiere es que la proxima corrida vuelva a fallar en el mismo sitio hasta
        que alguien mire.
        """
        self.entries.pop(key(goal, step), None)
        self.flush()

    def flush(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.entries, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def report(self):
        return {"hits": self.hits, "misses": self.misses, "healed": self.healed,
                "entries": len(self.entries), "mode": "heal" if self.heal else "strict",
                "file": str(self.path)}


class Diverged(Exception):
    """Lo guardado ya no describe la aplicacion, y el modo estricto no lo cura.

    Lleva dentro lo que se buscaba y lo que habia en su lugar, porque "no
    encontrado" sin la lista de lo que si estaba obliga a repetir la corrida a
    mano para averiguarlo.
    """

    def __init__(self, remembered, page):
        self.remembered = remembered
        self.offered = [
            {"kind": a["kind"], "label": a.get("label")} for a in page.get("actions", [])[:40]
        ]
        super().__init__(
            "lo guardado ya no encaja: se esperaba {} sobre {!r} y no esta en la pantalla. "
            "Esto no cae al modelo a proposito; una deriva del caso no debe salir en verde.".format(
                remembered.get("operation"), remembered.get("label"))
        )
