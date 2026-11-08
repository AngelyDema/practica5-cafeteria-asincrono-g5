import base64
import binascii
import logging
import os
import random
import time
from collections import Counter
from datetime import datetime, timezone
from functools import cache

from flask import Flask, request
from google.cloud import firestore

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cocina")

BASE_DATOS = os.environ.get("BASE_DATOS", "(default)")
TIEMPO_PREPARACION = float(os.environ.get("TIEMPO_PREPARACION", "6"))
PROBABILIDAD_FALLO = float(os.environ.get("PROBABILIDAD_FALLO", "0"))
PLATO_MISTERIOSO = "plato-misterioso"
ESTADOS_FINALES = ("listo", "rechazado")
# En una ráfaga muchos pedidos tocan el mismo producto y las transacciones chocan;
# Firestore las reintenta hasta este número de veces antes de rendirse (500 → Pub/Sub reintenta).
MAX_INTENTOS_TX = 10

app = Flask(__name__)


@cache
def db():
    return firestore.Client(database=BASE_DATOS)


def ahora():
    return datetime.now(timezone.utc)


@firestore.transactional
def empezar(tx, pedido_ref):
    """Devuelve el pedido (o None si no existe) y lo pasa a "preparando" si no está terminado."""
    pedido = pedido_ref.get(transaction=tx)
    if not pedido.exists:
        return None
    if pedido.get("estado") not in ESTADOS_FINALES:
        tx.update(pedido_ref, {"estado": "preparando", "actualizado": ahora()})
    return pedido


@firestore.transactional
def terminar(tx, pedido_ref, cantidades):
    """Revisa estado y existencias y descuenta, todo en una sola transacción.

    Devuelve ("repetido", []), ("rechazado", faltan) o ("listo", []).
    Firestore exige hacer todas las lecturas antes de la primera escritura.
    """
    pedido = pedido_ref.get(transaction=tx)
    if pedido.get("estado") in ESTADOS_FINALES:
        return "repetido", []

    inventario = db().collection("inventario")
    productos = {p: inventario.document(p) for p in cantidades}
    existencias = {}
    for producto, ref in productos.items():
        doc = ref.get(transaction=tx)
        existencias[producto] = (doc.get("existencias") if doc.exists else 0) or 0

    faltan = [p for p, cantidad in cantidades.items() if existencias[p] < cantidad]
    if faltan:
        tx.update(pedido_ref, {
            "estado": "rechazado",
            "motivo": "Se acabó: " + ", ".join(faltan),
            "actualizado": ahora(),
        })
        return "rechazado", faltan

    for producto, cantidad in cantidades.items():
        tx.update(productos[producto], {"existencias": existencias[producto] - cantidad})
    tx.update(pedido_ref, {"estado": "listo", "actualizado": ahora()})
    return "listo", []


@app.post("/")
def recibir():
    # Pub/Sub entrega un sobre JSON: {"message": {"data": <base64>, ...},
    # "subscription": ..., "deliveryAttempt": n}. El último campo solo viene
    # cuando la suscripción tiene política de mensajes muertos.
    sobre = request.get_json(silent=True)
    try:
        pedido_id = base64.b64decode(sobre["message"]["data"], validate=True).decode()
    except (TypeError, KeyError, binascii.Error, UnicodeDecodeError):
        log.error("Llegó un sobre que no es de Pub/Sub o no trae data: %r", sobre)
        return "sobre inválido", 400

    intento = sobre.get("deliveryAttempt")
    log.info("Pedido %s, intento de entrega %s", pedido_id, intento or "desconocido")

    pedido_ref = db().collection("pedidos").document(pedido_id)

    # Primera transacción: marca el pedido como "preparando" solo si todavía no
    # está terminado. Si ya está listo o rechazado, es una entrega repetida.
    pedido = empezar(db().transaction(), pedido_ref)
    if pedido is None:
        # Se confirma igual: reintentar un pedido que no existe no lo va a crear.
        log.warning("El pedido %s no existe; se descarta el mensaje", pedido_id)
        return "", 204
    if pedido.get("estado") in ESTADOS_FINALES:
        log.info("Pedido %s ya estaba %s; entrega repetida, no se descuenta otra vez",
                 pedido_id, pedido.get("estado"))
        return "", 204

    cantidades = Counter()
    for item in pedido.get("items"):
        cantidades[item["producto"]] += item["cantidad"]

    if PLATO_MISTERIOSO in cantidades:
        raise RuntimeError(f"La cocina no tiene receta para el plato misterioso (pedido {pedido_id})")

    time.sleep(TIEMPO_PREPARACION)

    # Segunda transacción: revisar el estado, revisar existencias y descontar
    # van juntos. Si otra instancia termina el mismo pedido o descuenta el mismo
    # producto en medio, Firestore reintenta la transacción con los datos nuevos.
    resultado, faltan = terminar(db().transaction(max_attempts=MAX_INTENTOS_TX), pedido_ref, cantidades)
    if resultado == "repetido":
        log.info("Pedido %s lo terminó otra entrega; no se descuenta otra vez", pedido_id)
        return "", 204
    if resultado == "rechazado":
        log.info("Pedido %s rechazado, no alcanza: %s", pedido_id, faltan)
    else:
        log.info("Pedido %s listo", pedido_id)

    # Simula que la cocina se cae justo después de terminar y antes de confirmar.
    # Pub/Sub no recibe el 2xx y vuelve a entregar el mismo pedido.
    if random.random() < PROBABILIDAD_FALLO:
        log.warning("Falla simulada en el pedido %s: el trabajo quedó hecho pero no se confirma", pedido_id)
        return "falla simulada", 500

    return "", 204
