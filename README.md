# legacy-portal-mcp

MCP server for an old internal web portal with no API (session cookies, ASP-style grids, ~2,500-row user table).

- Read: `menu`, `page`, `usuarios`, `usuario`, `roles`, `permisos_rol`, `comparar_usuarios`, `ejecutar_reporte`, `diagnosticar`.
- Safe writes: `proponer_usuario` (dry run), `crear_usuario` ("same access as user X" = copy the role), `restablecer_clave`.
- **Password handling:** temporary passwords never pass through the chat; they are generated locally into the Credential Manager and copied to the clipboard without being displayed.
- Connections skip the corporate proxy for the internal host (`trust_env=False`).

## Español
Automatiza un portal interno antiguo: altas de usuario con "mismos accesos que...", diagnóstico de pantallas y reportes, y contraseñas temporales que nunca se muestran.

> **Note / Nota:** sanitized portfolio version of production work built for a corporate IT help desk. Company names, people, hosts, IDs and data were replaced with placeholders; secrets never lived in the code (they sit in the Windows Credential Manager). / Versión anonimizada de un trabajo real de mesa de ayuda corporativa: nombres, hosts, IDs y datos fueron reemplazados; los secretos nunca estuvieron en el código.
