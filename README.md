# MCP Celonis Novedades & Payroll

Servidor MCP custom que conecta Claude Code con los datos de gestión de novedades de Celonis.
Permite a team leaders consultar excepciones, ausentismo, jornadas, horas extra y más en lenguaje natural.

**Contacto:** gabriela.mansilla@mercadolibre.com

## Instalación

Pedile el `install.py` a Gabriela y ejecutá:

```bash
# Windows
python install.py

# Mac / Linux
python3 install.py
```

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
