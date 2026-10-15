# gastos-mcp

MCP de Gastos: registra y consulta precios de supermercado en la pestaña "Gastos - Supermercado" de Google Sheets.
Herramientas: `check_receipt`, `append_receipt_items`, `search_prices`, `list_categories`.

Estado: chat 3, bloque A. Autenticación por token (`authorization: Bearer <token>`), `search_prices` con
`latest_per_merchant`, Python fijado a 3.13 y Dockerfile. Aún sin desplegar.

## Estructura

```
src/gastos_mcp/
  models.py      Modelos Pydantic: validan lo que envía Claude y describen lo que se devuelve
  repository.py  Acceso a Sheets (real y en memoria), esquema de columnas (`Column`) y conversión fila <-> modelo
  service.py     Reglas: duplicados, categorías, búsqueda, candado
  auth.py        Token por cabecera: carga y validación al arrancar, middleware ASGI que responde 401
  server.py      Las 4 herramientas MCP, la app HTTP (`build_app`) y el arranque con uvicorn
tests/           Servicio, repositorio, esquema, servidor (protocolo MCP), autenticación (HTTP), integración
Dockerfile       Imagen para Cloud Run (dos etapas, usuario no root)
```

## Si cambia la estructura de la hoja (insertar o mover una columna)

1. Edita `Column` en `repository.py` (valores 0, 1, 2... sin huecos): de ahí salen el número de columnas, el rango y las letras.
2. Si la columna se lee o se escribe, añade su línea en `parse_sheet_values` y `row_to_cells`.
3. Actualiza a mano los literales de `tests/test_repository.py` y `tests/test_schema.py` (nombre de la pestaña, rango, celdas
   esperadas): son el contrato con la hoja real y NO importan las constantes del código a propósito.
4. Ejecuta los tests unitarios y la prueba de integración contra la copia de pruebas.

## Preparar el entorno

```bash
cd ~/Dev/gastos-mcp
uv sync                      # instala dependencias y el comando gastos-mcp (Python 3.13, ver .python-version)
cp .env.example .env         # rellena SPREADSHEET_ID (COPIA de pruebas) y MCP_AUTH_TOKEN (uno local)
```

## Tests

```bash
uv run pytest                                     # unitarios (no tocan Google)
uv run --env-file .env pytest -m integration -s   # contra la copia de pruebas real (necesita ADC)
uvx ruff check                                    # lint (ruff no está en el grupo dev)
```

## Ejecutar el servidor

El servidor no arranca sin `MCP_AUTH_TOKEN` (también con `--fake`).

```bash
uv run --env-file .env gastos-mcp --fake  # datos de ejemplo en memoria, sin Google
uv run --env-file .env gastos-mcp         # contra la hoja de SPREADSHEET_ID (ADC con impersonación)
```

Queda en `http://127.0.0.1:8000/mcp` (Streamable HTTP sin estado). Toda petición necesita la cabecera
`authorization: Bearer <MCP_AUTH_TOKEN>`; sin ella responde 401 vacío.

## Probar con el inspector

```bash
npx @modelcontextprotocol/inspector@latest     # la v1.0.2 está obsoleta
```

Interfaz v2: edita o clona la tarjeta `example-server-default` (Streamable HTTP), URL `http://127.0.0.1:8000/mcp`
y añade la cabecera `authorization` con valor `Bearer <token>` en la configuración del servidor.

Modo línea de comandos (verificado):

```bash
source .env   # o exporta MCP_AUTH_TOKEN a mano
npx @modelcontextprotocol/inspector@latest --cli http://127.0.0.1:8000/mcp --transport http \
  --header "authorization: Bearer $MCP_AUTH_TOKEN" --method tools/list
npx @modelcontextprotocol/inspector@latest --cli http://127.0.0.1:8000/mcp --transport http \
  --header "authorization: Bearer $MCP_AUTH_TOKEN" \
  --method tools/call --tool-name search_prices --tool-arg category=Mantequilla --tool-arg latest_per_merchant=true
```

Sin `--header`, el inspector interpreta el 401 como petición de OAuth y falla con `auth_required`: es lo esperado.

## Medir la latencia de lectura de la hoja

Separa el coste del primer acceso (obtener el token de la cuenta suplantada) del de una lectura en caliente:

```bash
uv run --env-file .env python - <<'EOF'
import os, time
from gastos_mcp.repository import GoogleSheetsRepository
start = time.perf_counter()
repository = GoogleSheetsRepository(os.environ["SPREADSHEET_ID"])
print(f"crear cliente: {time.perf_counter() - start:.2f} s")
for attempt in range(1, 4):
    start = time.perf_counter()
    rows = repository.read_rows()
    print(f"lectura {attempt}: {time.perf_counter() - start:.2f} s ({len(rows)} filas)")
EOF
```

## Imagen Docker

En este equipo, Docker no resuelve DNS dentro de los contenedores (el DNS del sistema es el stub 127.0.0.53 de
systemd-resolved y el respaldo 8.8.8.8 no responde), así que el build necesita `--network=host`. No cambia la imagen.

```bash
docker build --network=host -t gastos-mcp .
docker run --rm -p 8080:8080 -e MCP_AUTH_TOKEN="$MCP_AUTH_TOKEN" gastos-mcp gastos-mcp --fake --host 0.0.0.0
```

`.dockerignore` es una lista de permitidos (solo `pyproject.toml`, `uv.lock`, `.python-version` y `src/`): el `.env`
nunca entra en la imagen. `uv.lock` debe estar en el repositorio: el build usa `uv sync --locked`.
