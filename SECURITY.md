# Seguridad

## Cómo informar de una vulnerabilidad

No abras una incidencia pública. Usa el aviso privado de GitHub: pestaña **Security** del repositorio →
**Report a vulnerability**. Tendrá acceso solo el mantenedor.

Incluye la versión (`version` de `pyproject.toml` o la etiqueta `vX.Y.Z`), los pasos para reproducirlo y el impacto
que crees que tiene. No incluyas tokens, IDs de hojas ni credenciales reales.

Es un proyecto personal mantenido por una sola persona: no hay plazos de respuesta garantizados. Se corrige la última
versión publicada; las anteriores no reciben parches.

## Qué cubre

El código de este repositorio: la autenticación por token (`src/gastos_mcp/auth.py`), el acceso a Google Sheets y la
imagen Docker. Quedan fuera tu propio despliegue (proyecto de GCP, secretos, permisos IAM) y los servicios de terceros.
