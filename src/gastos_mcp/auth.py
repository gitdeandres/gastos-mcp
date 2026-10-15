"""Autenticación del servidor con un token estático por cabecera.

Claude envía en cada petición la cabecera `authorization: Bearer <token>`, configurada
a mano en el conector (modo "Sin inicio de sesión"). Aquí se comprueba con un
middleware ASGI puro que envuelve toda la app HTTP del SDK:

- Mira solo las cabeceras: rechaza antes de leer el cuerpo y antes de que el SDK
  procese nada (ni JSON-RPC ni sesión).
- Responde 401 sin cuerpo ni `WWW-Authenticate`: no da pistas y no invita al cliente a
  iniciar un flujo OAuth que este servidor no tiene.
- Compara con `hmac.compare_digest` (tiempo constante) y nunca registra el token.

No se usa el `token_verifier` del SDK porque está pensado para un servidor de recursos
OAuth: exige `AuthSettings.issuer_url` y responde con `WWW-Authenticate: Bearer error=...`.
"""

from __future__ import annotations

import hmac
import logging
import os
import string
from collections.abc import Mapping

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

TOKEN_ENV_VAR = "MCP_AUTH_TOKEN"
# `secrets.token_urlsafe(32)` (256 bits) produce 43 caracteres. Un token más corto es
# casi seguro un error de configuración (valor vacío, recortado o de prueba).
MIN_TOKEN_LENGTH = 43
# Caracteres válidos en un valor de cabecera sin ambigüedades (base64url y similares).
_ALLOWED_TOKEN_CHARS = frozenset(string.ascii_letters + string.digits + "-_.~+/=")


class AuthConfigError(Exception):
    """El token no está configurado o no es válido. El mensaje nunca incluye el token."""


def load_token(environ: Mapping[str, str] | None = None) -> str:
    """Lee el token de `MCP_AUTH_TOKEN` y lo valida; falla al arrancar si falta o es débil.

    En Cloud Run la variable la rellena Secret Manager (`--set-secrets`) al iniciar la
    instancia. Se recortan espacios y saltos de línea finales: un secreto creado con
    `echo` sin `-n` acaba en "\\n" y, sin recortar, ninguna petición pasaría.
    """
    source = os.environ if environ is None else environ
    token = source.get(TOKEN_ENV_VAR, "").strip()
    if not token:
        raise AuthConfigError(f"Falta {TOKEN_ENV_VAR}: el servidor no arranca sin token.")
    if len(token) < MIN_TOKEN_LENGTH:
        raise AuthConfigError(
            f"{TOKEN_ENV_VAR} es demasiado corto ({len(token)} caracteres, mínimo {MIN_TOKEN_LENGTH}). "
            'Genera uno con: python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    if not set(token) <= _ALLOWED_TOKEN_CHARS:
        raise AuthConfigError(f"{TOKEN_ENV_VAR} contiene caracteres no válidos para una cabecera HTTP.")
    return token


class BearerTokenMiddleware:
    """Middleware ASGI que exige `Authorization: Bearer <token>` en toda petición HTTP.

    Es ASGI puro (no `BaseHTTPMiddleware` de Starlette) para decidir con las cabeceras
    del `scope` sin tocar `receive`, es decir, sin leer el cuerpo de la petición.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        # Se compara en bytes: las cabeceras ASGI llegan como bytes.
        self._expected = token.encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # `lifespan` (arranque y parada del gestor de sesiones del SDK) no es una petición.
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope["type"] == "http" and self._is_authorized(scope):
            await self._app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            # El servidor no usa websockets; se cierra sin aceptar la conexión.
            await send({"type": "websocket.close", "code": 1008})
            return
        # Se registra el motivo genérico, la ruta y el método; nunca el valor de la cabecera.
        logger.warning("petición rechazada (401): %s %s", scope.get("method", "?"), scope.get("path", "?"))
        await send({"type": "http.response.start", "status": 401, "headers": [(b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})

    def _is_authorized(self, scope: Scope) -> bool:
        """True si hay exactamente una cabecera `authorization` con esquema Bearer y el token correcto."""
        values = [value for name, value in scope.get("headers", []) if name.lower() == b"authorization"]
        # Varias cabeceras `authorization` son ambiguas (cada capa podría leer una distinta).
        if len(values) != 1:
            return False
        scheme, _, credentials = values[0].strip().partition(b" ")
        # El esquema no distingue mayúsculas (RFC 9110, 11.1); el token sí.
        if scheme.lower() != b"bearer":
            return False
        return hmac.compare_digest(credentials.strip(), self._expected)
