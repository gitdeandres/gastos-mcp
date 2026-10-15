# syntax=docker/dockerfile:1
#
# Imagen del MCP de Gastos para Cloud Run. Dos etapas:
# - builder: instala las dependencias y el proyecto en /app/.venv con uv.
# - final: solo Python y ese venv (sin uv, sin código fuente, sin tests), usuario no root.
#
# Las dos etapas usan la MISMA imagen de Python: el venv apunta a /usr/local/bin/python3
# y solo funciona si el intérprete de la etapa final es el mismo que lo creó.

# Versión de uv: igual que la local (`uv --version`) para que lea el mismo formato de uv.lock.
ARG UV_VERSION=0.13.1
# Python alineado con .python-version (3.13). Debian trixie: la variante que usa la guía de uv.
ARG PYTHON_IMAGE=python:3.13-slim-trixie

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# --- Etapa 1: construir el venv --------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /bin/uv

# Precompilar a bytecode acelera el arranque en frío (min-instances=0: cada arranque cuenta).
# copy: la caché de uv y el venv están en sistemas de archivos distintos (evita avisos).
# Sin dependencias de desarrollo y con el Python de la imagen (nunca descargar otro).
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Capa 1: solo dependencias. Se reutiliza mientras no cambien pyproject.toml ni uv.lock.
# --locked falla si uv.lock no corresponde a pyproject.toml (nunca resuelve versiones nuevas en el build).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=.python-version,target=.python-version \
    uv sync --locked --no-install-project --no-editable

# Capa 2: el proyecto. --no-editable lo instala dentro del venv, así la etapa final no necesita src/.
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable

# --- Etapa 2: imagen final ---------------------------------------------------------
FROM ${PYTHON_IMAGE}

# Usuario de sistema sin shell ni home. El venv queda propiedad de root: el proceso
# puede leerlo y ejecutarlo, pero no modificarlo.
RUN useradd --system --no-create-home --shell /usr/sbin/nologin app

COPY --from=builder /app/.venv /app/.venv

# PORT=8080 por defecto: es el puerto que usa Cloud Run (que igualmente inyecta PORT) y el que
# declara EXPOSE. Sin esto el servidor escucharía en 8000, su valor por defecto fuera del contenedor.
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PORT=8080

USER app

# El servidor lee PORT de la variable de entorno (8080 en la imagen; Cloud Run también la define).
# 0.0.0.0 es obligatorio en el contenedor; la protección es el token (MCP_AUTH_TOKEN).
EXPOSE 8080
CMD ["gastos-mcp", "--host", "0.0.0.0"]
