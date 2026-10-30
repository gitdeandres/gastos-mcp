# gastos-mcp

![ci](https://github.com/gitdeandres/gastos-mcp/actions/workflows/ci.yml/badge.svg)

Servidor [MCP](https://modelcontextprotocol.io) en Python para registrar y consultar precios de supermercado en una
hoja de Google Sheets. Se usa desde Claude como conector remoto: Claude lee la foto de un ticket, comprueba que no esté
ya registrado y, con tu aprobación, escribe una fila por artículo. El objetivo es comparar el precio de cada artículo
entre comercios, no llevar la contabilidad.

> **Proyecto personal.** Está publicado como referencia y como posible plantilla. No acepta contribuciones ni
> incidencias. Para vulnerabilidades, consulta [SECURITY.md](SECURITY.md). Versión actual: 0.2.0, en producción en
> Cloud Run para uso propio (la URL del servicio no se publica).

## Herramientas

| Herramienta | Qué hace | Escribe |
|---|---|---|
| `check_receipt` | Dice si un ticket (nº de factura + comercio) ya está en la hoja | No |
| `append_receipt_items` | Añade las filas de un ticket; rechaza duplicados en el servidor | Sí |
| `search_prices` | Historial de precios con filtros (`category`, `query`, `merchant`, `limit`, `latest_per_merchant`) | No |
| `list_categories` | Categorías ya usadas, con su número de filas | No |

No hay herramientas para editar ni borrar filas.

## Qué necesitas para usarlo con tu propia hoja

1. Una hoja de Google Sheets con una pestaña llamada exactamente `Gastos - Supermercado` y estas columnas:

   | A | B | C | D | E | F | G | H | I |
   |---|---|---|---|---|---|---|---|---|
   | No. de factura | Fecha | Comercio | Nombre del ítem (como en el ticket) | Clasificación | Pack | Descripción | Precio unitario | Precio total |

   El servidor solo valida que `A1` sea `No. de factura`. Escribe A a E, H e I; F y G se rellenan a mano.

   El precio total (I) se captura del ticket, no se calcula: es el importe de la línea con el descuento ya aplicado
   y es obligatorio en `append_receipt_items`. En productos al peso, el unitario es el precio por kg y el total lo
   cobrado; "2x 2.55" es una sola fila con unitario 2.55 y total 5.10. El servidor no comprueba que cuadren.
   Si tu hoja es distinta, ver "Si cambia la estructura de la hoja".
2. Un proyecto de Google Cloud con la API de Sheets habilitada y una cuenta de servicio con acceso de Editor a la hoja.
   No se usan archivos de clave: en local, impersonación; en Cloud Run, la cuenta asociada al servicio.
3. Python 3.13 y [uv](https://docs.astral.sh/uv/). Para desplegar, Docker y un proyecto de Cloud Run.

## Seguridad del servidor

- Toda petición exige `authorization: Bearer <token>`. El token es un secreto de 256 bits (`MCP_AUTH_TOKEN`; el
  servidor no arranca si falta, es corto o tiene caracteres inválidos). Se compara en tiempo constante y nunca se registra.
  Sin token responde 401 vacío en cualquier ruta.
- La escritura usa el modo `RAW` de la API de Sheets: nada se interpreta como fórmula ni como fecha según el idioma de la hoja.
- Solo se añaden filas. La detección de duplicados y la escritura están serializadas con un candado.
- Los permisos que importan se configuran en el cliente: las lecturas pueden ir sin aprobación; la escritura debe pedirla.

## Estructura

```
src/gastos_mcp/
  models.py      Modelos Pydantic: validan lo que envía el cliente y describen lo que se devuelve
  repository.py  Acceso a Sheets (real y en memoria), esquema de columnas (`Column`) y conversión fila <-> modelo
  service.py     Reglas: duplicados, categorías, búsqueda, candado
  auth.py        Token por cabecera: carga y validación al arrancar, middleware ASGI que responde 401
  server.py      Las 4 herramientas MCP, la app HTTP (`build_app`) y el arranque con uvicorn
tests/           Servicio, repositorio, esquema, servidor (protocolo MCP), autenticación (HTTP) e integración
Dockerfile       Imagen para Cloud Run (dos etapas, usuario no root)
docs/            Guía de despliegue en Cloud Run
```

## Desarrollo local

```bash
uv sync                      # instala dependencias y el comando gastos-mcp (Python 3.13, ver .python-version)
cp .env.example .env         # rellena SPREADSHEET_ID (una COPIA de pruebas) y MCP_AUTH_TOKEN (uno local)
```

`.env` está en `.gitignore` y en `.dockerignore`. En local usa siempre una copia de la hoja, nunca la de producción.

```bash
uv run pytest                                     # unitarios (no tocan Google)
uv run ruff check                                 # lint
uv run --env-file .env pytest -m integration -s   # contra la copia de pruebas real (necesita credenciales ADC)
```

Arrancar el servidor (el token es obligatorio, también con `--fake`):

```bash
uv run --env-file .env gastos-mcp --fake  # datos de ejemplo en memoria, sin Google
uv run --env-file .env gastos-mcp         # contra la hoja de SPREADSHEET_ID
```

Queda en `http://127.0.0.1:8000/mcp` (Streamable HTTP sin estado). Para probarlo con el
[inspector](https://github.com/modelcontextprotocol/inspector):

```bash
source .env   # o exporta MCP_AUTH_TOKEN a mano
npx @modelcontextprotocol/inspector@latest --cli http://127.0.0.1:8000/mcp --transport http \
  --header "authorization: Bearer $MCP_AUTH_TOKEN" --method tools/list
```

Sin `--header`, el inspector interpreta el 401 como petición de OAuth y falla con `auth_required`: es lo esperado.

## Si cambia la estructura de la hoja (insertar o mover una columna)

1. Edita `Column` en `repository.py` (valores 0, 1, 2... sin huecos): de ahí salen el número de columnas, el rango y las letras.
2. Si la columna se lee o se escribe, añade su línea en `parse_sheet_values` y `row_to_cells`.
3. Actualiza a mano los literales de `tests/test_repository.py` y `tests/test_schema.py` (nombre de la pestaña, rango, celdas
   esperadas): son el contrato con la hoja real y no importan las constantes del código a propósito.
4. Ejecuta los tests unitarios y la prueba de integración contra la copia de pruebas.

## Imagen Docker y despliegue

```bash
docker build -t gastos-mcp .
docker run --rm -p 8080:8080 -e MCP_AUTH_TOKEN="$MCP_AUTH_TOKEN" gastos-mcp gastos-mcp --fake --host 0.0.0.0
```

Si el build no resuelve DNS dentro del contenedor (depende de tu red o de tu VPN), prueba con `--network=host`: es un ajuste
del build local y no cambia la imagen.

`.dockerignore` es una lista de permitidos (solo `pyproject.toml`, `uv.lock`, `.python-version` y `src/`): el `.env` nunca
entra en la imagen. `uv.lock` debe estar en el repositorio: el build usa `uv sync --locked`.

El procedimiento completo de despliegue en Cloud Run (revisión sin tráfico, prueba de lecturas, migración, rotación del
token y diagnóstico) está en [docs/despliegue-cloud-run.md](docs/despliegue-cloud-run.md).

## Licencia

[MIT](LICENSE).
