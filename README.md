# MCP Celonis Novedades & Payroll

Servidor MCP custom que conecta Claude Code con los datos de gestión de novedades de Celonis.
Permite a team leaders consultar excepciones, ausentismo, jornadas, horas extra y más en lenguaje natural.

**Contacto:** gabriela.mansilla@mercadolibre.com

## Instalación

Hay dos instaladores según tu situación:

### Opción A — `install.py` (requiere internet)

Descarga el servidor directamente desde este repositorio al momento de instalar.

```bash
# Windows
python install.py

# Mac / Linux
python3 install.py
```

### Opción B — `install-standalone.py` (sin conexión a internet)

Todo embebido: no necesita descargar nada durante la instalación.
Ideal si tenés restricciones de red o preferís un instalador autónomo.

```bash
# Windows
python install-standalone.py

# Mac / Linux
python3 install-standalone.py
```

### Requisitos

- Python 3.7 o superior
- Claude Code **o** Claude Desktop instalado
- Acceso a la red/VPN de MELI (para que el MCP pueda conectarse a Celonis en runtime)

> **¿Sin `claude` en la terminal?** Si solo tenés Claude Desktop (sin el CLI), el instalador
> configura el servidor editando directamente los archivos de configuración de Claude.
> No es necesario tener el comando `claude` disponible en la terminal.

Después de instalar, **abrí una nueva sesión de Claude Code** — las sesiones ya abiertas no cargan los tools nuevos.

## Tools disponibles

| Tool | Qué devuelve |
|------|-------------|
| `list_supervisors` | Lista de team leaders |
| `list_sites` | Sitios/locaciones disponibles |
| `compare_supervisors` | Ranking comparativo de N supervisores |
| `get_supervisor_dashboard` | Dashboard completo del equipo |
| `get_employee_detail` | Perfil completo de un empleado |
| `get_employee_absences` | Detalle de ausencias de un empleado |
| `get_absenteeism_kpis` | KPIs de ausentismo del equipo |
| `get_incomplete_shifts_kpis` | % de jornadas incompletas por estado |
| `get_punch_edits` | KPIs de marcajes editados manualmente |
| `get_paycode_corrections` | KPIs de correcciones de paycode |
| `get_pending_exceptions` | Excepciones pendientes de acción |
| `get_exceptions_without_paycode` | Excepciones sin código de pago |
| `get_overtime_summary` | Horas extra solicitadas/aprobadas/rechazadas |
