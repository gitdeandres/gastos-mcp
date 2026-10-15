"""Tests de la autenticación por token (D12).

Dos niveles: el middleware aislado (con una app ASGI mínima, para comprobar que rechaza
sin leer el cuerpo) y la app real por HTTP (`build_app`), para comprobar que el SDK queda
detrás del middleware y que el token no aparece en los logs.
"""

import logging
import secrets
from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from gastos_mcp.auth import AuthConfigError, BearerTokenMiddleware, load_token
from gastos_mcp.repository import InMemoryRepository, demo_rows
from gastos_mcp.server import build_app
from gastos_mcp.service import GastosService

TOKEN = secrets.token_urlsafe(32)

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
}
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


# --- load_token -------------------------------------------------------------------


def test_load_token_returns_a_valid_token() -> None:
    assert load_token({"MCP_AUTH_TOKEN": TOKEN}) == TOKEN


def test_load_token_strips_the_trailing_newline_of_a_secret_created_with_echo() -> None:
    assert load_token({"MCP_AUTH_TOKEN": TOKEN + "\n"}) == TOKEN


@pytest.mark.parametrize("environ", [{}, {"MCP_AUTH_TOKEN": ""}, {"MCP_AUTH_TOKEN": "   \n"}])
def test_load_token_fails_when_missing(environ: dict[str, str]) -> None:
    with pytest.raises(AuthConfigError, match="Falta MCP_AUTH_TOKEN"):
        load_token(environ)


def test_load_token_rejects_a_short_token_without_echoing_it() -> None:
    with pytest.raises(AuthConfigError, match="demasiado corto") as error:
        load_token({"MCP_AUTH_TOKEN": "secreto-de-prueba"})
    assert "secreto-de-prueba" not in str(error.value)


def test_load_token_rejects_characters_invalid_in_a_header() -> None:
    with pytest.raises(AuthConfigError, match="no válidos"):
        load_token({"MCP_AUTH_TOKEN": TOKEN[:-1] + " é"})


# --- middleware aislado -------------------------------------------------------------


class RecordingApp:
    """App ASGI mínima que registra si fue llamada."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _call(middleware: BearerTokenMiddleware, headers: list[tuple[bytes, bytes]]) -> tuple[int, bool]:
    """Ejecuta una petición HTTP; devuelve el estado y si se llegó a leer el cuerpo."""
    body_read = False
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal body_read
        body_read = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": headers}
    await middleware(scope, receive, send)
    return sent[0]["status"], body_read


@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param([], id="ausente"),
        pytest.param([(b"authorization", b"Bearer " + b"x" * 43)], id="token-invalido"),
        pytest.param([(b"authorization", TOKEN.encode())], id="sin-esquema"),
        pytest.param([(b"authorization", b"Basic " + TOKEN.encode())], id="esquema-basic"),
        pytest.param([(b"authorization", b"Token " + TOKEN.encode())], id="esquema-token"),
        pytest.param([(b"authorization", b"Bearer")], id="bearer-vacio"),
        pytest.param([(b"authorization", b"Bearer " + TOKEN[:-1].encode())], id="token-recortado"),
        pytest.param([(b"authorization", b"Bearer " + TOKEN.encode() + b"x")], id="token-alargado"),
        pytest.param([(b"authorization", b"Bearer " + TOKEN.upper().encode())], id="token-otras-mayusculas"),
        pytest.param(
            [(b"authorization", b"Bearer bad"), (b"authorization", b"Bearer " + TOKEN.encode())], id="duplicada"
        ),
        pytest.param(
            [(b"authorization", b"Bearer " + TOKEN.encode()), (b"authorization", b"Bearer bad")],
            id="duplicada-valida-primero",
        ),
    ],
)
async def test_rejects_with_401_without_reading_the_body(headers: list[tuple[bytes, bytes]]) -> None:
    app = RecordingApp()
    status, body_read = await _call(BearerTokenMiddleware(app, TOKEN), headers)
    assert (status, body_read, app.calls) == (401, False, 0)


@pytest.mark.anyio
@pytest.mark.parametrize("scheme", [b"Bearer", b"bearer", b"BEARER"])
async def test_accepts_the_valid_token_with_any_scheme_case(scheme: bytes) -> None:
    app = RecordingApp()
    status, _ = await _call(BearerTokenMiddleware(app, TOKEN), [(b"Authorization", scheme + b" " + TOKEN.encode())])
    assert (status, app.calls) == (200, 1)


@pytest.mark.anyio
async def test_lifespan_passes_through_without_a_token() -> None:
    app = RecordingApp()

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        pass

    await BearerTokenMiddleware(app, TOKEN)({"type": "lifespan"}, receive, send)
    assert app.calls == 1


# --- app completa por HTTP ----------------------------------------------------------


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Cliente HTTP contra la app real; arranca su lifespan (gestor de sesiones del SDK)."""
    app = build_app(GastosService(InMemoryRepository(demo_rows())), TOKEN, host="0.0.0.0")
    with TestClient(app) as test_client:
        yield test_client


def test_initialize_with_the_token_succeeds(client: TestClient) -> None:
    response = client.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "gastos"


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", f"Basic {TOKEN}"])
def test_requests_without_a_valid_token_get_an_empty_401(client: TestClient, authorization: str | None) -> None:
    headers = dict(MCP_HEADERS)
    if authorization:
        headers["authorization"] = authorization
    response = client.post("/mcp", json=INITIALIZE, headers=headers)
    assert response.status_code == 401
    assert response.content == b""
    assert "www-authenticate" not in response.headers


def test_unknown_paths_also_require_the_token(client: TestClient) -> None:
    # Sin token no se distingue una ruta existente de una inexistente.
    assert client.get("/.well-known/oauth-protected-resource").status_code == 401
    assert client.get("/nada", headers={"authorization": f"Bearer {TOKEN}"}).status_code == 404


def test_the_token_never_appears_in_the_logs(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    client.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "authorization": f"Bearer {TOKEN}"})
    client.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "authorization": f"Bearer {TOKEN}x"})
    assert "401" in caplog.text
    assert TOKEN not in caplog.text


def test_dns_rebinding_protection_stays_active_on_localhost() -> None:
    # En local (host 127.0.0.1) el SDK sigue rechazando un Host ajeno, ya autenticado.
    app = build_app(GastosService(InMemoryRepository()), TOKEN, host="127.0.0.1")
    headers = {**MCP_HEADERS, "authorization": f"Bearer {TOKEN}"}
    with TestClient(app, base_url="http://127.0.0.1:8000") as local:
        assert local.post("/mcp", json=INITIALIZE, headers={**headers, "host": "evil.example"}).status_code == 421
        assert local.post("/mcp", json=INITIALIZE, headers=headers).status_code == 200
