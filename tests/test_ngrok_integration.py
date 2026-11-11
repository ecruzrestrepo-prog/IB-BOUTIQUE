"""Prueba remota opt-in, sin abrir túneles ni modificar registros.

    .venv/bin/python tests/test_ngrok_integration.py --url https://example.ngrok-free.dev

Las credenciales se leen exclusivamente de .env y nunca se imprimen. Ejecutar
este archivo explícitamente; unittest discover solo lo importa y no usa la red.
websockets se importa dentro de la prueba para no imponerlo al resto de tests.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from http.cookiejar import CookieJar
import json
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]


class ProbeFailure(RuntimeError):
    """Fallo controlado cuyo texto no contiene datos de la conexión."""


def require(condition: bool, message: str):
    if not condition:
        raise ProbeFailure(message)


def request_status(url, authorization=None, *, opener=None):
    headers = {"ngrok-skip-browser-warning": "true"}
    if authorization:
        headers["Authorization"] = authorization
    request = Request(url, headers=headers)
    try:
        with (opener or build_opener()).open(request, timeout=20) as response:
            return response.status, response.read(200000)
    except HTTPError as exc:
        exc.close()
        return exc.code, b""


def inspect_run(websocket, public_url):
    from streamlit.proto.BackMsg_pb2 import BackMsg
    from streamlit.proto.ClientState_pb2 import ClientState, ContextInfo
    from streamlit.proto.ForwardMsg_pb2 import ForwardMsg

    init = BackMsg(rerun_script=ClientState(
        query_string="",
        context_info=ContextInfo(timezone="America/Bogota", timezone_offset=300,
                                 locale="es-CO", url=public_url + "/",
                                 is_embedded=False, color_scheme="light")))
    websocket.send(init.SerializeToString())
    deadline = time.monotonic() + 60
    types, elements = Counter(), Counter()
    session_id = ""
    while time.monotonic() < deadline:
        raw = websocket.recv(timeout=max(0.1, deadline - time.monotonic()))
        require(isinstance(raw, bytes), "Streamlit devolvió un frame no binario.")
        message = ForwardMsg()
        message.ParseFromString(raw)
        kind = message.WhichOneof("type")
        types[kind] += 1
        if kind == "new_session":
            session_id = message.new_session.initialize.session_id
        elif kind == "delta" and message.delta.WhichOneof("type") == "new_element":
            element = message.delta.new_element.WhichOneof("type")
            elements[element] += 1
            require(element != "exception", "El dashboard envió una excepción de ejecución.")
        elif kind == "script_finished":
            require(message.script_finished == ForwardMsg.FINISHED_SUCCESSFULLY,
                    "El script no finalizó correctamente.")
            require(bool(session_id), "No llegó una sesión Streamlit identificable.")
            require(elements["metric"] >= 4, "No se recibieron los indicadores esperados del dashboard.")
            return {"session_id": session_id, "messages": dict(types),
                    "elements": dict(elements), "script_finished": "success"}
    raise ProbeFailure("No se completó el dashboard en 60 segundos.")


def probe(public_url: str):
    from dotenv import dotenv_values
    try:
        from websockets.exceptions import InvalidStatus
        from websockets.sync.client import connect
    except ImportError as exc:
        raise ProbeFailure("Esta prueba opcional necesita websockets disponible en el entorno.") from exc

    parsed = urlsplit(public_url)
    require(parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username
            and not parsed.password and not parsed.query and not parsed.fragment
            and parsed.path in {"", "/"}, "Usa la URL HTTPS pública sin credenciales ni parámetros.")
    public_url = public_url.rstrip("/")
    values = dotenv_values(ROOT / ".env", interpolate=False)
    user = values.get("NGROK_BASIC_USER") or ""
    password = values.get("NGROK_BASIC_PASSWORD") or ""
    require(bool(user and password), "Faltan credenciales Basic Auth en .env.")
    authorization = "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    incorrect = "Basic " + base64.b64encode(b"incorrect-user:incorrect-password").decode("ascii")
    absent_status, _ = request_status(public_url + "/")
    incorrect_status, _ = request_status(public_url + "/", incorrect)
    require(absent_status == incorrect_status == 401, "La puerta HTTP no rechazó credenciales ausentes o incorrectas.")

    contexts = []
    for _ in range(2):
        jar = CookieJar()
        opener = build_opener(HTTPCookieProcessor(jar))
        status, html = request_status(public_url + "/", authorization, opener=opener)
        require(status == 200 and b"streamlit" in html.lower(), "No se recibió el HTML de Streamlit autenticado.")
        status, body = request_status(public_url + "/_stcore/health", authorization, opener=opener)
        require(status == 200 and body.strip() == b"ok", "El servidor remoto no está listo.")
        cookie_values = {cookie.name: cookie.value for cookie in jar}
        token = cookie_values.get("_streamlit_xsrf")
        require(bool(token), "No se recibió la cookie XSRF; no se omitirán las protecciones.")
        contexts.append({"token": token, "headers": {
            "Authorization": authorization,
            "Cookie": "; ".join(f"{key}={value}" for key, value in cookie_values.items()),
            "ngrok-skip-browser-warning": "true"}})

    ws_url = "wss://" + parsed.netloc + "/_stcore/stream"
    try:
        with connect(ws_url, origin=public_url, subprotocols=["streamlit"],
                     open_timeout=20, close_timeout=3):
            raise ProbeFailure("WebSocket permitió acceso sin Basic Auth.")
    except InvalidStatus as exc:
        require(exc.response.status_code == 401, "WebSocket sin credenciales no devolvió 401.")

    with ExitStack() as stack:
        connections = [stack.enter_context(connect(
            ws_url, origin=public_url,
            subprotocols=["streamlit", context["token"]],
            additional_headers=context["headers"], open_timeout=20,
            close_timeout=3, max_size=16 * 1024 * 1024)) for context in contexts]
        require(all(conn.subprotocol == "streamlit" for conn in connections),
                "No se negoció el protocolo Streamlit.")
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(inspect_run, conn, public_url) for conn in connections]
            reports = [future.result() for future in futures]
        require(reports[0]["session_id"] != reports[1]["session_id"],
                "Las dos conexiones no obtuvieron sesiones independientes.")
        for report in reports:
            del report["session_id"]  # Ni tokens ni identificadores de sesión se muestran.

    return {"http_without_credentials": absent_status,
            "http_wrong_credentials": incorrect_status, "http_authenticated": 200,
            "websocket_without_credentials": 401, "websocket_authenticated": 101,
            "xsrf_cookie_supplied": True, "https_origin_supplied": True,
            "simultaneous_distinct_sessions": 2, "same_public_endpoint": True,
            "runs": reports}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="URL HTTPS de un launcher ya ejecutándose.")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(probe(args.url), ensure_ascii=False, indent=2))
        return 0
    except ProbeFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        # Las excepciones de red pueden contener cabeceras o tokens: solo el tipo.
        print(f"La prueba no terminó: {type(exc).__name__}. No se muestran detalles sensibles.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
