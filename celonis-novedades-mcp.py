#!/usr/bin/env python3
"""
Celonis Novedades & Payroll — Custom MCP Server
Tools focused on team leader use cases: exceptions without paycode + absenteeism KPIs.
"""
import sys
import json
import os
import time
import urllib.request
import urllib.parse
from datetime import date, timedelta, datetime

CLIENT_ID = os.environ["CELONIS_CLIENT_ID"]
CLIENT_SECRET = os.environ["CELONIS_CLIENT_SECRET"]
TOKEN_URL = "https://mercadolibre.us-1.celonis.cloud/oauth2/token"
MCP_URL = "https://mercadolibre.us-1.celonis.cloud/studio-copilot/api/v1/mcp-servers/mcp/5c772110-cfc3-455d-9963-bcf6e40590ca?draft=false"
SCOPE = "mcp-asset.tools:execute"

_token_cache = {"token": None, "expires_at": 0}

_SUPERVISOR_SAMPLE_SIZE = 500  # rows fetched in one call to extract unique supervisors


def get_token():
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": SCOPE,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded"
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read())
    _token_cache["token"] = body["access_token"]
    _token_cache["expires_at"] = now + body.get("expires_in", 3600)
    return _token_cache["token"]


def call_celonis_tool(tool_name, arguments):
    token = get_token()
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }).encode("utf-8")
    req = urllib.request.Request(MCP_URL, data=payload, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
    for line in raw.splitlines():
        if line.startswith("data: "):
            rpc = json.loads(line[6:])
            if "error" in rpc:
                raise RuntimeError(rpc["error"].get("message", "Celonis error"))
            content = rpc.get("result", {}).get("content", [])
            if content:
                return json.loads(content[0]["text"])
            return {}
    raise RuntimeError("No data line in SSE response")


def extract_rows(raw):
    """Handle the multiple response formats Celonis may return."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        if "value" in raw:
            return raw["value"]
        if "data_frame_content" in raw:
            return raw["data_frame_content"].get("data", [])
        if "data" in raw:
            return raw["data"]
    return []


_PERIODS = {
    "current_month":  lambda t: (t.replace(day=1).isoformat(), t.isoformat()),
    "last_month":     lambda t: ((t.replace(day=1) - timedelta(days=1)).replace(day=1).isoformat(),
                                 (t.replace(day=1) - timedelta(days=1)).isoformat()),
    "last_7_days":    lambda t: ((t - timedelta(days=7)).isoformat(), t.isoformat()),
    "last_30_days":   lambda t: ((t - timedelta(days=30)).isoformat(), t.isoformat()),
    "last_90_days":   lambda t: ((t - timedelta(days=90)).isoformat(), t.isoformat()),
    "last_6_months":  lambda t: ((t - timedelta(days=180)).isoformat(), t.isoformat()),
    "last_12_months": lambda t: ((t - timedelta(days=365)).isoformat(), t.isoformat()),
}


def resolve_dates(args):
    """
    Returns (start_date, end_date, period_label).
    Priority: explicit start_date/end_date > period shorthand > default last_30_days.
    """
    start = args.get("start_date", "").strip()
    end = args.get("end_date", "").strip()
    period = args.get("period", "").strip().lower().replace(" ", "_")

    if start and end:
        return start, end, f"{start} → {end}"

    today = date.today()
    if period and period in _PERIODS:
        s, e = _PERIODS[period](today)
        return s, e, period

    # Default: last 30 days so results aren't the full historical dataset
    s, e = _PERIODS["last_30_days"](today)
    return s, e, "last_30_days (default)"


def build_date_filters(start_date, end_date, column_id):
    if start_date and end_date:
        return [{"column_id": column_id, "start_date": start_date, "end_date": end_date}]
    return []


def build_site_filter(site):
    """Returns a string_filter dict for site/location, or None if not specified."""
    if not site or not site.strip():
        return None
    return {
        "column_id": "O_CUSTOM_EMPLOYEE.LOCACIONDESCRIPTION",
        "values": [site.strip()],
        "add_wildcard_before": True,
        "add_wildcard_after": True,
        "case_sensitive": False,
    }


def clarification_for_status():
    """Returns a clarification response when employee_status is not provided."""
    return {
        "clarification_needed": True,
        "question": (
            "¿Querés ver los datos para qué empleados?\n"
            "- 'Activos' (employee_status: Active)\n"
            "- 'Cesantes' (employee_status: Terminated)\n"
            "- 'Todos' (employee_status: all)"
        ),
        "instruction_for_claude": (
            "Ask the user: '¿Querés ver los datos de Activos, Cesantes o Todos?' "
            "Then call the same tool again with the corresponding employee_status: "
            "Active, Terminated, or all."
        ),
    }


ANALYSIS_GUIDANCE = (
    "IMPORTANT: No organizational targets are defined for these metrics. "
    "Present all values descriptively only — state what the numbers are, do NOT interpret them as good, bad, high, low, alarming, or excellent. "
    "Example: say 'there were 112 unjustified absences' not '112 unjustified absences is a high number'. "
    "Only flag something as noteworthy if the user explicitly asks for an evaluation or provides a target."
)


def parse_dt(val):
    """Parse datetime from ISO string or millisecond epoch. Returns datetime or None."""
    if not val:
        return None
    if isinstance(val, (int, float)):
        try:
            return datetime.utcfromtimestamp(val / 1000)
        except Exception:
            return None
    if isinstance(val, str):
        s = val.strip().rstrip("Zz")
        for fmt, length in [
            ("%Y-%m-%dT%H:%M:%S", 19),
            ("%Y-%m-%d %H:%M:%S", 19),
            ("%Y-%m-%dT%H:%M", 16),
            ("%Y-%m-%d %H:%M", 16),
            ("%Y-%m-%d", 10),
        ]:
            try:
                return datetime.strptime(s[:length], fmt)
            except ValueError:
                continue
    return None


def get_iso_week(date_val):
    """Returns ISO year-week string (e.g. '2025-W03') from a date string, date, or datetime."""
    try:
        if isinstance(date_val, str):
            d = date.fromisoformat(str(date_val)[:10])
        elif isinstance(date_val, datetime):
            d = date_val.date()
        elif isinstance(date_val, date):
            d = date_val
        else:
            return None
        iso_year, iso_week, _ = d.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    except Exception:
        return None


def build_site_type_filter(site_type):
    """Returns a string_filter for site type prefix (e.g., 'SC', 'FBM', 'XD')."""
    if not site_type or not site_type.strip():
        return None
    prefix = site_type.strip().upper()
    return {
        "column_id": "O_CUSTOM_EMPLOYEE.LOCACIONDESCRIPTION",
        "values": [f"{prefix} - "],
        "add_wildcard_before": False,
        "add_wildcard_after": True,
        "case_sensitive": False,
    }


def build_status_filter(status):
    """Returns a string_filter for employment status, or None if 'all'."""
    if not status or status.strip().lower() == "all":
        return None
    return {
        "column_id": "O_CUSTOM_EMPLOYEE.EMPLOYMENTSTATUS",
        "values": [status.strip()],
        "add_wildcard_before": False,
        "add_wildcard_after": False,
        "case_sensitive": False,
    }


_SITE_DESCRIPTION = (
    "Optional site/location filter (partial match on location description, "
    "e.g. 'Tultitlan', 'MXCD14'). Use list_sites to see valid values."
)

_SITE_TYPE_DESCRIPTION = (
    "Optional site type filter by prefix: 'SC' (Service Centers), 'FBM' (Fulfillment), "
    "'XD' (Cross-Docking), etc. Matches sites whose description starts with 'TYPE - '. "
    "Use list_sites to discover valid types."
)

_STATUS_DESCRIPTION = (
    "Employment status filter. Default: 'Active' (only active employees). "
    "Use 'Terminated' for former employees, or 'all' to include everyone."
)


# ── Tool definitions ──────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "start_consultation",
        "description": (
            "Entry point for any Celonis consultation — call this first. "
            "Fetches available site types and returns a guided workflow so Claude asks the user: "
            "(1) which site type, specific site, or all; "
            "(2) a specific supervisor or pending exceptions first; "
            "(3) employee status (Active/Terminated/all). "
            "Filtering by site_type or site in every subsequent call significantly reduces Celonis query time."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "compare_supervisors",
        "description": (
            "Compares absenteeism KPIs across multiple team leaders side by side and returns a ranking. "
            "Pass a list of supervisor names to compare. Always ask for employee_status first. "
            "Defaults to last 30 days if no date range specified."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "supervisor_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of supervisor full names to compare (2-10).",
                    "minItems": 2,
                    "maxItems": 10,
                },
                "employee_status": {"type": "string", "description": "Ask the user first: 'Active' (Activos), 'Terminated' (Cesantes), or 'all' (Todos). Do not default — always confirm."},
                "period": {"type": "string", "description": "Quick date preset: current_month, last_month, last_7_days, last_30_days, last_90_days, last_6_months, last_12_months."},
                "start_date": {"type": "string", "description": "Custom start date YYYY-MM-DD."},
                "end_date": {"type": "string", "description": "Custom end date YYYY-MM-DD."},
                "site": {"type": "string", "description": _SITE_DESCRIPTION},
                "site_type": {"type": "string", "description": _SITE_TYPE_DESCRIPTION},
                "sort_by": {
                    "type": "string",
                    "default": "total_absences",
                    "description": "Metric to rank by: total_absences, unjustified_absences, employees_critical_10pct. Default: total_absences.",
                },
            },
            "required": ["supervisor_names"],
        },
    },
    {
        "name": "get_employee_absences",
        "description": (
            "Returns detailed absence records for a specific employee: "
            "list of absence events with date, type (justified/unjustified), status, and validity. "
            "Search by employee full name or ID. Always ask for employee_status first. "
            "Defaults to last 30 days if no date range specified."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "employee": {
                    "type": "string",
                    "description": "Employee full name (partial match works) or employee ID.",
                },
                "employee_status": {"type": "string", "description": "Ask the user first: 'Active' (Activos), 'Terminated' (Cesantes), or 'all' (Todos). Do not default — always confirm."},
                "period": {"type": "string", "description": "Quick date preset: current_month, last_month, last_7_days, last_30_days, last_90_days, last_6_months, last_12_months."},
                "start_date": {"type": "string", "description": "Custom start date YYYY-MM-DD."},
                "end_date": {"type": "string", "description": "Custom end date YYYY-MM-DD."},
                "page": {"type": "integer", "default": 0},
                "page_size": {"type": "integer", "default": 50, "description": "Max 100."},
            },
            "required": ["employee"],
        },
    },
    {
        "name": "get_supervisor_dashboard",
        "description": (
            "Returns a complete dashboard for a team leader in a single call: "
            "absenteeism KPIs, incomplete shifts, punch edits, paycode corrections, "
            "and pending exceptions count. Use this instead of calling individual KPI tools separately. "
            "Accepts the same period/date/site filters as the individual tools."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "supervisor_name": {
                    "type": "string",
                    "description": "Full name of the supervisor/team leader.",
                },
                "period": {
                    "type": "string",
                    "description": "Quick date preset: current_month, last_month, last_7_days, last_30_days, last_90_days, last_6_months, last_12_months.",
                },
                "start_date": {"type": "string", "description": "Custom start date YYYY-MM-DD."},
                "end_date": {"type": "string", "description": "Custom end date YYYY-MM-DD."},
                "site": {"type": "string", "description": _SITE_DESCRIPTION},
                "site_type": {"type": "string", "description": _SITE_TYPE_DESCRIPTION},
                "employee_status": {"type": "string", "description": "Ask the user before calling: 'Active' (Activos), 'Terminated' (Cesantes), or 'all' (Todos). Do not default — always confirm."},
            },
            "required": ["supervisor_name"],
        },
    },
    {
        "name": "get_exceptions_without_paycode",
        "description": (
            "Returns exceptions that have no associated payment code for the direct reports "
            "of a given team leader (supervisor). Covers unexcused absences, missed punches, "
            "late arrivals, etc. without a paycode assigned. "
            "Always filters by a date range — defaults to last 30 days if not specified. "
            "Use 'period' for quick presets or 'start_date'+'end_date' for a custom range."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "supervisor_name": {
                    "type": "string",
                    "description": "Full name of the supervisor/team leader (e.g. 'GARCIA LOPEZ, JUAN').",
                },
                "period": {
                    "type": "string",
                    "description": (
                        "Quick date preset. Options: current_month, last_month, "
                        "last_7_days, last_30_days, last_90_days, last_6_months, last_12_months. "
                        "Ignored if start_date+end_date are provided."
                    ),
                },
                "start_date": {
                    "type": "string",
                    "description": "Custom start date YYYY-MM-DD (inclusive). Use with end_date.",
                },
                "end_date": {
                    "type": "string",
                    "description": "Custom end date YYYY-MM-DD (inclusive). Use with start_date.",
                },
                "site": {"type": "string", "description": _SITE_DESCRIPTION},
                "site_type": {"type": "string", "description": _SITE_TYPE_DESCRIPTION},
                "employee_status": {"type": "string", "description": "Ask the user before calling: 'Active' (Activos), 'Terminated' (Cesantes), or 'all' (Todos). Do not default — always confirm."},
                "page": {"type": "integer", "default": 0, "description": "Page number (0-based)."},
                "page_size": {"type": "integer", "default": 50, "description": "Rows per page (max 100)."},
            },
            "required": ["supervisor_name"],
        },
    },
    {
        "name": "get_absenteeism_kpis",
        "description": (
            "Returns aggregated absenteeism KPIs for the team of a given supervisor. "
            "IMPORTANT: employee_status is required — always ask the user whether they want "
            "Activos (Active), Cesantes (Terminated), or Todos (all) before calling this tool. "
            "If employee_status is not provided, the tool will return a clarification request. "
            "Defaults to last 30 days if no date range is specified."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "supervisor_name": {
                    "type": "string",
                    "description": "Full name of the supervisor/team leader.",
                },
                "employee_status": {
                    "type": "string",
                    "description": (
                        "REQUIRED before executing. Ask the user first: "
                        "'Active' = Activos, 'Terminated' = Cesantes, 'all' = Todos. "
                        "Do not default — always confirm with the user."
                    ),
                },
                "period": {
                    "type": "string",
                    "description": (
                        "Quick date preset: current_month, last_month, "
                        "last_7_days, last_30_days, last_90_days, last_6_months, last_12_months."
                    ),
                },
                "start_date": {
                    "type": "string",
                    "description": "Custom start date YYYY-MM-DD. Use with end_date.",
                },
                "end_date": {
                    "type": "string",
                    "description": "Custom end date YYYY-MM-DD. Use with start_date.",
                },
                "site": {"type": "string", "description": _SITE_DESCRIPTION},
                "site_type": {"type": "string", "description": _SITE_TYPE_DESCRIPTION},
            },
            "required": ["supervisor_name"],
        },
    },
    {
        "name": "get_punch_edits",
        "description": (
            "Returns KPIs about manually edited punches (marcajes editados manualmente) for a team leader's team: "
            "number of edits, timecards affected, % of timecards with manual edits. "
            "Optionally returns a detail list of each edited punch (employee, date, edit source). "
            "Defaults to last 30 days if no date range specified."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "supervisor_name": {
                    "type": "string",
                    "description": "Full name of the supervisor/team leader.",
                },
                "period": {
                    "type": "string",
                    "description": (
                        "Quick date preset: current_month, last_month, "
                        "last_7_days, last_30_days, last_90_days, last_6_months, last_12_months."
                    ),
                },
                "start_date": {"type": "string", "description": "Custom start date YYYY-MM-DD."},
                "end_date": {"type": "string", "description": "Custom end date YYYY-MM-DD."},
                "site": {"type": "string", "description": _SITE_DESCRIPTION},
                "site_type": {"type": "string", "description": _SITE_TYPE_DESCRIPTION},
                "employee_status": {"type": "string", "description": "Ask the user before calling: 'Active' (Activos), 'Terminated' (Cesantes), or 'all' (Todos). Do not default — always confirm."},
                "page": {"type": "integer", "default": 0},
                "page_size": {"type": "integer", "default": 50},
            },
            "required": ["supervisor_name"],
        },
    },
    {
        "name": "get_paycode_corrections",
        "description": (
            "Returns KPIs about paycode corrections for a team leader's team: "
            "number of corrections, timecards affected, % of timecards with corrections. "
            "Also returns a detail list of corrected paycodes (employee, paycode name, date, duration). "
            "Defaults to last 30 days if no date range specified."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "supervisor_name": {
                    "type": "string",
                    "description": "Full name of the supervisor/team leader.",
                },
                "period": {
                    "type": "string",
                    "description": (
                        "Quick date preset: current_month, last_month, "
                        "last_7_days, last_30_days, last_90_days, last_6_months, last_12_months."
                    ),
                },
                "start_date": {"type": "string", "description": "Custom start date YYYY-MM-DD."},
                "end_date": {"type": "string", "description": "Custom end date YYYY-MM-DD."},
                "site": {"type": "string", "description": _SITE_DESCRIPTION},
                "site_type": {"type": "string", "description": _SITE_TYPE_DESCRIPTION},
                "employee_status": {"type": "string", "description": "Ask the user before calling: 'Active' (Activos), 'Terminated' (Cesantes), or 'all' (Todos). Do not default — always confirm."},
            },
            "required": ["supervisor_name"],
        },
    },
    {
        "name": "get_employee_detail",
        "description": (
            "Returns full HR profile of an employee: role, department, supervisor, location, "
            "LDAP, SAP ID, employment status, team, BU, society, country, and more. "
            "Search by full name (partial match works) or by employee ID."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "employee": {
                    "type": "string",
                    "description": "Employee full name (e.g. 'GARCIA LOPEZ, JUAN') or employee ID. Partial name match works.",
                },
                "employee_status": {
                    "type": "string",
                    "default": "all",
                    "description": "Filter by employment status: 'Active', 'Terminated', or 'all' (default). For employee lookups, 'all' is recommended.",
                },
                "page": {"type": "integer", "default": 0},
                "page_size": {"type": "integer", "default": 20, "description": "Max 50."},
            },
            "required": ["employee"],
        },
    },
    {
        "name": "get_overtime_summary",
        "description": (
            "Returns overtime (horas extra) summary for a team leader's team: "
            "list of overtime approval records grouped by action (approved/rejected/pending), "
            "total hours requested and approved. "
            "Defaults to last 30 days if no date range specified."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "supervisor_name": {
                    "type": "string",
                    "description": "Full name of the supervisor/team leader.",
                },
                "period": {
                    "type": "string",
                    "description": (
                        "Quick date preset: current_month, last_month, "
                        "last_7_days, last_30_days, last_90_days, last_6_months, last_12_months."
                    ),
                },
                "start_date": {"type": "string", "description": "Custom start date YYYY-MM-DD."},
                "end_date": {"type": "string", "description": "Custom end date YYYY-MM-DD."},
                "site": {"type": "string", "description": _SITE_DESCRIPTION},
                "site_type": {"type": "string", "description": _SITE_TYPE_DESCRIPTION},
                "page": {"type": "integer", "default": 0},
                "page_size": {"type": "integer", "default": 50, "description": "Max 100."},
            },
            "required": ["supervisor_name"],
        },
    },
    {
        "name": "get_incomplete_shifts_kpis",
        "description": (
            "Returns KPIs about incomplete shifts (jornadas incompletas) for a team leader's team: "
            "total pending, justified (approved / pending approval), closed without justification, "
            "and shifts requiring no action. Defaults to last 30 days if no date range specified. "
            "Use 'period' for quick presets or 'start_date'+'end_date' for a custom range."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "supervisor_name": {
                    "type": "string",
                    "description": "Full name of the supervisor/team leader.",
                },
                "period": {
                    "type": "string",
                    "description": (
                        "Quick date preset: current_month, last_month, "
                        "last_7_days, last_30_days, last_90_days, last_6_months, last_12_months."
                    ),
                },
                "start_date": {"type": "string", "description": "Custom start date YYYY-MM-DD."},
                "end_date": {"type": "string", "description": "Custom end date YYYY-MM-DD."},
                "site": {"type": "string", "description": _SITE_DESCRIPTION},
                "site_type": {"type": "string", "description": _SITE_TYPE_DESCRIPTION},
                "employee_status": {"type": "string", "description": "Ask the user before calling: 'Active' (Activos), 'Terminated' (Cesantes), or 'all' (Todos). Do not default — always confirm."},
            },
            "required": ["supervisor_name"],
        },
    },
    {
        "name": "get_pending_exceptions",
        "description": (
            "Returns exceptions with status 'Pendiente' (requiring supervisor action) for a team leader's direct reports. "
            "Includes a summary grouped by exception type so the TL can prioritize what to resolve first. "
            "Defaults to last 30 days if no date range is specified. "
            "Use 'period' for quick presets or 'start_date'+'end_date' for a custom range."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "supervisor_name": {
                    "type": "string",
                    "description": "Full name of the supervisor/team leader.",
                },
                "period": {
                    "type": "string",
                    "description": (
                        "Quick date preset: current_month, last_month, "
                        "last_7_days, last_30_days, last_90_days, last_6_months, last_12_months."
                    ),
                },
                "start_date": {"type": "string", "description": "Custom start date YYYY-MM-DD."},
                "end_date": {"type": "string", "description": "Custom end date YYYY-MM-DD."},
                "site": {"type": "string", "description": _SITE_DESCRIPTION},
                "site_type": {"type": "string", "description": _SITE_TYPE_DESCRIPTION},
                "page": {"type": "integer", "default": 0, "description": "Page number (0-based)."},
                "page_size": {"type": "integer", "default": 50, "description": "Rows per page (max 100)."},
            },
            "required": ["supervisor_name"],
        },
    },
    {
        "name": "list_supervisors",
        "description": (
            "Returns all supervisors/team leaders present in the system, scanning all employees. "
            "Use this to discover valid supervisor_name values before calling other tools. "
            "Optionally filter by partial name with the 'search' parameter or by site."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": "Optional partial name to filter supervisors (case-insensitive).",
                },
                "site": {"type": "string", "description": _SITE_DESCRIPTION},
                "site_type": {"type": "string", "description": _SITE_TYPE_DESCRIPTION},
                "employee_status": {"type": "string", "description": "Ask the user before calling: 'Active' (Activos), 'Terminated' (Cesantes), or 'all' (Todos). Do not default — always confirm."},
            },
        },
    },
    {
        "name": "list_sites",
        "description": (
            "Returns all unique sites/locations present in the system, including their type prefix (SC, FBM, XD, etc.). "
            "Use this to discover valid 'site' or 'site_type' values before filtering other tools by location."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": "Optional partial name to filter sites (case-insensitive).",
                },
                "site_type": {
                    "type": "string",
                    "description": "Optional filter by site type prefix (e.g., 'SC', 'FBM', 'XD').",
                },
            },
        },
    },
    {
        "name": "get_extended_shifts_analysis",
        "description": (
            "Identifies employees with extended daily shifts (>12h30m) and weekly overtime accumulation (>13h). "
            "Uses early-entry and late-out exceptions — regardless of their approval status — to determine actual "
            "shift start and end times. Also detects employees with >2 violations in the same week, and flags "
            "overtime records associated with double or triple overtime paycodes. "
            "Always ask for employee_status first. Defaults to last 30 days if no date range is specified."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "supervisor_name": {
                    "type": "string",
                    "description": "Full name of the supervisor/team leader. Optional — can filter by site instead.",
                },
                "employee_status": {
                    "type": "string",
                    "description": (
                        "REQUIRED before executing. Ask the user first: "
                        "'Active' = Activos, 'Terminated' = Cesantes, 'all' = Todos. "
                        "Do not default — always confirm with the user."
                    ),
                },
                "period": {
                    "type": "string",
                    "description": "Quick date preset: current_month, last_month, last_7_days, last_30_days, last_90_days, last_6_months, last_12_months.",
                },
                "start_date": {"type": "string", "description": "Custom start date YYYY-MM-DD."},
                "end_date": {"type": "string", "description": "Custom end date YYYY-MM-DD."},
                "site": {"type": "string", "description": _SITE_DESCRIPTION},
                "site_type": {"type": "string", "description": _SITE_TYPE_DESCRIPTION},
                "page": {"type": "integer", "default": 0, "description": "Page number (0-based) for exception rows."},
                "page_size": {"type": "integer", "default": 100, "description": "Rows per page for exception fetch (max 500)."},
            },
            "required": [],
        },
    },
]


# ── Tool handlers ─────────────────────────────────────────────────────────────

def handle_start_consultation(args):
    try:
        raw = call_celonis_tool("load_data", {
            "columns": ["O_CUSTOM_EMPLOYEE.LOCACIONDESCRIPTION"],
            "applied_filters": {
                "null_filters": [{"column_id": "O_CUSTOM_EMPLOYEE.LOCACIONDESCRIPTION", "is_null": False}]
            },
            "page": 0,
            "page_size": 500,
        })
        rows = extract_rows(raw)
        seen = {}
        for r in rows:
            desc = r.get("O_CUSTOM_EMPLOYEE.LOCACIONDESCRIPTION")
            if desc and desc not in seen:
                seen[desc] = _extract_site_type(desc)
        type_counts = {}
        for stype in seen.values():
            t = stype or "OTHER"
            type_counts[t] = type_counts.get(t, 0) + 1
        site_types = sorted(
            [{"site_type": k, "count": v} for k, v in type_counts.items()],
            key=lambda x: -x["count"],
        )
        total_sites = len(seen)
    except Exception:
        site_types = []
        total_sites = 0

    return {
        "instruction_for_claude": (
            "Guide the consultation in order:\n"
            "PASO 1 — SCOPE: Ask '¿Querés filtrar por tipo de sitio, un sitio específico, o ver todos?' "
            "Show available_site_types. Use answer as site_type (e.g. 'SC') or site in all subsequent calls.\n"
            "PASO 2 — FOCO: Ask '¿Buscás un supervisor en particular, o preferís ver primero las excepciones pendientes?' "
            "If supervisor → call list_supervisors with the site filter to pick one, then get_supervisor_dashboard. "
            "If pending first → call get_pending_exceptions with the site filter.\n"
            "PASO 3 — ESTADO: Ask '¿Activos, Cesantes o Todos?' "
            "Use as employee_status in all subsequent calls.\n"
            "Performance tip: always pass site_type or site to every tool call — it filters at source."
        ),
        "available_site_types": site_types,
        "total_unique_sites": total_sites,
        "suggested_workflow": [
            "start_consultation → elegir scope (site_type / site / todos)",
            "list_supervisors (con el site_type elegido) → elegir supervisor",
            "get_supervisor_dashboard (supervisor + site_type + employee_status)",
            "get_pending_exceptions → acciones inmediatas",
            "get_absenteeism_kpis / get_punch_edits / etc. según necesidad",
        ],
    }


def base_string_filters(supervisor, site=None, status="Active", site_type=None):
    """Returns string_filters list with supervisor + optional site/site_type + employment status."""
    filters = [{
        "column_id": "O_CUSTOM_EMPLOYEE.SUPERVISORNAME",
        "values": [supervisor],
        "add_wildcard_before": False,
        "add_wildcard_after": False,
        "case_sensitive": False,
    }]
    sf = build_site_filter(site)
    if sf:
        filters.append(sf)
    stf = build_site_type_filter(site_type)
    if stf:
        filters.append(stf)
    status_f = build_status_filter(status)
    if status_f:
        filters.append(status_f)
    return filters


def handle_compare_supervisors(args):
    supervisors = args.get("supervisor_names", [])
    status = args.get("employee_status")
    if status is None:
        return clarification_for_status()
    if not supervisors or len(supervisors) < 2:
        return {"error": "At least 2 supervisor names are required."}

    site = args.get("site")
    site_type = args.get("site_type")
    start_date, end_date, period_label = resolve_dates(args)
    sort_by = args.get("sort_by", "total_absences")
    date_f = build_date_filters(start_date, end_date, "O_CUSTOM_EXCEPTION.EVENTDATE")

    results = []
    errors = []
    for supervisor in supervisors[:10]:
        try:
            raw = call_celonis_tool("load_data", {
                "columns": [
                    "total_de_ausencias",
                    "total_de_ausencias_injustificadas",
                    "total_de_ausencias_justitificas",
                    "de_ausencias_justificadas",
                    "empleados_con_3_ausencias_injustificadas",
                    "empleados_con_ausentismo_cr_tico_10_",
                    "de_empleados_con_ausentismo_cr_tico",
                    "total_empleados_con_al_menos_una_ausencia",
                ],
                "applied_filters": {
                    "string_filters": base_string_filters(supervisor, site, status, site_type),
                    "date_filters": date_f,
                },
            })
            k = (extract_rows(raw) or [{}])[0]
            results.append({
                "supervisor": supervisor,
                "total_absences": k.get("total_de_ausencias") or 0,
                "unjustified_absences": k.get("total_de_ausencias_injustificadas") or 0,
                "justified_absences": k.get("total_de_ausencias_justitificas") or 0,
                "pct_justified_raw": k.get("de_ausencias_justificadas"),
                "employees_3plus_unjustified": k.get("empleados_con_3_ausencias_injustificadas") or 0,
                "employees_critical_10pct": k.get("empleados_con_ausentismo_cr_tico_10_") or 0,
                "pct_critical_raw": k.get("de_empleados_con_ausentismo_cr_tico"),
                "employees_with_absence": k.get("total_empleados_con_al_menos_una_ausencia") or 0,
            })
        except Exception as e:
            errors.append({"supervisor": supervisor, "error": str(e)})

    # Sort by requested metric
    sort_key = {
        "total_absences": "total_absences",
        "unjustified_absences": "unjustified_absences",
        "employees_critical_10pct": "employees_critical_10pct",
    }.get(sort_by, "total_absences")
    ranked = sorted(results, key=lambda x: -(x.get(sort_key) or 0))
    for i, r in enumerate(ranked):
        r["rank"] = i + 1

    return {
        "_guidance": ANALYSIS_GUIDANCE,
        "employee_status_filter": status,
        "site_filter": site or None,
        "site_type_filter": site_type or None,
        "period": period_label,
        "date_range": {"start": start_date, "end": end_date},
        "sorted_by": sort_by,
        "ranking": ranked,
        "errors": errors if errors else None,
    }


def handle_get_employee_absences(args):
    employee = args["employee"]
    status = args.get("employee_status")
    if status is None:
        return clarification_for_status()
    page = args.get("page", 0)
    page_size = min(int(args.get("page_size", 50)), 100)
    start_date, end_date, period_label = resolve_dates(args)

    filters = {
        "string_filters": [
            {
                "column_id": "O_CUSTOM_EMPLOYEE.FULLNAME",
                "values": [employee],
                "add_wildcard_before": True,
                "add_wildcard_after": True,
                "case_sensitive": False,
            },
            {
                "column_id": "O_CUSTOM_EXCEPTION.EXCEPTIONTYPENAME",
                "values": ["ABSENCE", "UNEXCUSED_ABSENCE", "EXCUSED_ABSENCE"],
                "add_wildcard_before": False,
                "add_wildcard_after": False,
                "case_sensitive": False,
            },
        ],
        "date_filters": build_date_filters(start_date, end_date, "O_CUSTOM_EXCEPTION.EVENTDATE"),
    }
    status_f = build_status_filter(status)
    if status_f:
        filters["string_filters"].append(status_f)

    raw = call_celonis_tool("load_data", {
        "columns": [
            "O_CUSTOM_EMPLOYEE.FULLNAME",
            "O_CUSTOM_EMPLOYEE.LDAP",
            "O_CUSTOM_EMPLOYEE.SUPERVISORNAME",
            "O_CUSTOM_EMPLOYEE.EMPLOYMENTSTATUS",
            "O_CUSTOM_EXCEPTION.EXCEPTIONTYPENAME",
            "O_CUSTOM_EXCEPTION.exception_status",
            "O_CUSTOM_EXCEPTION.EVENTDATE",
            "O_CUSTOM_EXCEPTION.STARTDATETIME",
            "O_CUSTOM_EXCEPTION.ENDDATETIME",
            "O_CUSTOM_EXCEPTION.REVIEWED",
            "O_CUSTOM_EXCEPTION.validez_de_excepcion",
            "O_CUSTOM_EXCEPTION.PAYCODEQUALIFIER",
        ],
        "applied_filters": filters,
        "order_by": "O_CUSTOM_EXCEPTION.EVENTDATE",
        "ascending": False,
        "page": page,
        "page_size": page_size,
    })
    rows = extract_rows(raw)
    total = raw.get("Count", len(rows)) if isinstance(raw, dict) else len(rows)

    # Summary counts — check exact type to avoid UNEXCUSED matching EXCUSED
    def exc_type(r):
        return (r.get("O_CUSTOM_EXCEPTION.EXCEPTIONTYPENAME") or "").upper()

    justified   = sum(1 for r in rows if exc_type(r) == "EXCUSED_ABSENCE")
    unjustified = sum(1 for r in rows if exc_type(r) == "UNEXCUSED_ABSENCE")

    employee_info = {}
    if rows:
        r0 = rows[0]
        employee_info = {
            "full_name": r0.get("O_CUSTOM_EMPLOYEE.FULLNAME"),
            "ldap": r0.get("O_CUSTOM_EMPLOYEE.LDAP"),
            "supervisor": r0.get("O_CUSTOM_EMPLOYEE.SUPERVISORNAME"),
            "employment_status": r0.get("O_CUSTOM_EMPLOYEE.EMPLOYMENTSTATUS"),
        }

    return {
        "_guidance": ANALYSIS_GUIDANCE,
        "search": employee,
        "employee_status_filter": status,
        "employee": employee_info,
        "period": period_label,
        "date_range": {"start": start_date, "end": end_date},
        "total_absences": total,
        "summary": {
            "justified": justified,
            "unjustified": unjustified,
            "other": total - justified - unjustified,
        },
        "page": page,
        "page_size": page_size,
        "absences": [
            {
                "event_date": r.get("O_CUSTOM_EXCEPTION.EVENTDATE"),
                "start_datetime": r.get("O_CUSTOM_EXCEPTION.STARTDATETIME"),
                "end_datetime": r.get("O_CUSTOM_EXCEPTION.ENDDATETIME"),
                "type": r.get("O_CUSTOM_EXCEPTION.EXCEPTIONTYPENAME"),
                "status": r.get("O_CUSTOM_EXCEPTION.exception_status"),
                "reviewed": bool(r.get("O_CUSTOM_EXCEPTION.REVIEWED")),
                "validity": r.get("O_CUSTOM_EXCEPTION.validez_de_excepcion"),
                "has_paycode": r.get("O_CUSTOM_EXCEPTION.PAYCODEQUALIFIER") is not None,
            }
            for r in rows
        ],
    }


def handle_get_supervisor_dashboard(args):
    supervisor = args["supervisor_name"]
    site = args.get("site")
    site_type = args.get("site_type")
    status = args.get("employee_status")
    if status is None:
        return clarification_for_status()
    start_date, end_date, period_label = resolve_dates(args)

    sf = base_string_filters(supervisor, site, status, site_type)
    date_f = build_date_filters(start_date, end_date, "O_CUSTOM_EXCEPTION.EVENTDATE")

    def safe(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            return {"error": str(e)}

    # ── Absenteeism ───────────────────────────────────────────────────────────
    def get_absenteeism():
        raw = call_celonis_tool("load_data", {
            "columns": ["total_de_ausencias", "total_de_ausencias_injustificadas", "total_de_ausencias_justitificas", "de_ausencias_justificadas", "empleados_con_3_ausencias_injustificadas", "empleados_con_ausentismo_cr_tico_10_", "de_empleados_con_ausentismo_cr_tico"],
            "applied_filters": {"string_filters": sf, "date_filters": date_f},
        })
        k = (extract_rows(raw) or [{}])[0]
        return {
            "total_absences": k.get("total_de_ausencias"),
            "unjustified": k.get("total_de_ausencias_injustificadas"),
            "justified": k.get("total_de_ausencias_justitificas"),
            "pct_justified_raw": k.get("de_ausencias_justificadas"),
            "employees_3plus_unjustified": k.get("empleados_con_3_ausencias_injustificadas"),
            "employees_critical_10pct": k.get("empleados_con_ausentismo_cr_tico_10_"),
            "pct_critical_raw": k.get("de_empleados_con_ausentismo_cr_tico"),
        }

    # ── Incomplete shifts ─────────────────────────────────────────────────────
    def get_shifts():
        raw = call_celonis_tool("load_data", {
            "columns": ["jornadas_pendientes", "justificadas_y_aprobadas", "cerradas_sin_justificar", "sin_gesti_n_requerida"],
            "applied_filters": {"string_filters": sf, "date_filters": date_f},
        })
        k = (extract_rows(raw) or [{}])[0]
        pct = lambda v: round((v or 0) * 100, 1)
        return {
            "pct_pending": pct(k.get("jornadas_pendientes")),
            "pct_justified_approved": pct(k.get("justificadas_y_aprobadas")),
            "pct_closed_no_justif": pct(k.get("cerradas_sin_justificar")),
            "pct_no_action_required": pct(k.get("sin_gesti_n_requerida")),
        }

    # ── Punch edits ───────────────────────────────────────────────────────────
    def get_punches():
        raw = call_celonis_tool("load_data", {
            "columns": ["ediciones_manuales_de_marcaje", "marcajes_editados_manualmente", "timecards_con_marcaje_editado_manualmente"],
            "applied_filters": {"string_filters": sf},
        })
        k = (extract_rows(raw) or [{}])[0]
        pct = lambda v: round((v or 0) * 100, 1)
        return {
            "manual_edits": k.get("ediciones_manuales_de_marcaje"),
            "pct_edited_punches": pct(k.get("marcajes_editados_manualmente")),
            "timecards_affected": k.get("timecards_con_marcaje_editado_manualmente"),
        }

    # ── Paycode corrections ───────────────────────────────────────────────────
    def get_paycodes():
        raw = call_celonis_tool("load_data", {
            "columns": ["correcciones_de_paycode", "timecards_con_correcciones_de_paycode", "percentage_timecards_con_correcciones_de_paycode"],
            "applied_filters": {"string_filters": sf},
        })
        k = (extract_rows(raw) or [{}])[0]
        pct = lambda v: round((v or 0) * 100, 1)
        return {
            "corrections": k.get("correcciones_de_paycode"),
            "timecards_affected": k.get("timecards_con_correcciones_de_paycode"),
            "pct_timecards_with_corrections": pct(k.get("percentage_timecards_con_correcciones_de_paycode")),
        }

    # ── Pending exceptions count ──────────────────────────────────────────────
    def get_pending_count():
        raw = call_celonis_tool("load_data", {
            "columns": ["O_CUSTOM_EMPLOYEE.FULLNAME", "O_CUSTOM_EXCEPTION.exception_status"],
            "applied_filters": {
                "string_filters": sf + [{"column_id": "O_CUSTOM_EXCEPTION.exception_status", "values": ["Pendiente"], "add_wildcard_before": False, "add_wildcard_after": False, "case_sensitive": False}],
                "date_filters": date_f,
            },
            "page": 0,
            "page_size": 1,
        })
        total = raw.get("Count", len(extract_rows(raw))) if isinstance(raw, dict) else len(extract_rows(raw))
        return {"pending_exceptions": total}

    # ── Run all in sequence (Celonis doesn't support parallel calls) ──────────
    absenteeism    = safe(get_absenteeism)
    shifts         = safe(get_shifts)
    punch_edits    = safe(get_punches)
    paycode_corr   = safe(get_paycodes)
    pending        = safe(get_pending_count)

    return {
        "supervisor": supervisor,
        "_guidance": ANALYSIS_GUIDANCE,
        "site_filter": site or None,
        "site_type_filter": site_type or None,
        "employee_status_filter": status if status.lower() != "all" else "all",
        "period": period_label,
        "date_range": {"start": start_date, "end": end_date},
        "absenteeism": absenteeism,
        "incomplete_shifts": shifts,
        "punch_edits": punch_edits,
        "paycode_corrections": paycode_corr,
        "pending_exceptions": pending,
    }


def handle_get_exceptions_without_paycode(args):
    supervisor = args["supervisor_name"]
    site = args.get("site")
    site_type = args.get("site_type")
    status = args.get("employee_status")
    if status is None:
        return clarification_for_status()
    page = args.get("page", 0)
    page_size = min(int(args.get("page_size", 50)), 100)
    start_date, end_date, period_label = resolve_dates(args)

    filters = {
        "string_filters": base_string_filters(supervisor, site, status, site_type),
        "null_filters": [
            {"column_id": "O_CUSTOM_EXCEPTION.PAYCODEQUALIFIER", "is_null": True}
        ],
        "date_filters": build_date_filters(start_date, end_date, "O_CUSTOM_EXCEPTION.EVENTDATE"),
    }

    celonis_args = {
        "columns": [
            "O_CUSTOM_EMPLOYEE.SUPERVISORNAME",
            "O_CUSTOM_EMPLOYEE.FULLNAME",
            "O_CUSTOM_EMPLOYEE.LDAP",
            "O_CUSTOM_EXCEPTION.EXCEPTIONTYPENAME",
            "O_CUSTOM_EXCEPTION.exception_status",
            "O_CUSTOM_EXCEPTION.EVENTDATE",
            "O_CUSTOM_EXCEPTION.REVIEWED",
            "O_CUSTOM_EXCEPTION.validez_de_excepcion",
        ],
        "applied_filters": filters,
        "order_by": "O_CUSTOM_EXCEPTION.EVENTDATE",
        "ascending": False,
        "page": page,
        "page_size": page_size,
    }

    raw = call_celonis_tool("load_data", celonis_args)
    rows = extract_rows(raw)
    total = raw.get("Count", len(rows)) if isinstance(raw, dict) else len(rows)

    return {
        "supervisor": supervisor,
        "period": period_label,
        "date_range": {"start": start_date, "end": end_date},
        "site_filter": site or None,
        "site_type_filter": site_type or None,
        "total_exceptions_without_paycode": total,
        "page": page,
        "page_size": page_size,
        "exceptions": [
            {
                "employee": r.get("O_CUSTOM_EMPLOYEE.FULLNAME"),
                "ldap": r.get("O_CUSTOM_EMPLOYEE.LDAP"),
                "exception_type": r.get("O_CUSTOM_EXCEPTION.EXCEPTIONTYPENAME"),
                "status": r.get("O_CUSTOM_EXCEPTION.exception_status"),
                "event_date": r.get("O_CUSTOM_EXCEPTION.EVENTDATE"),
                "reviewed": bool(r.get("O_CUSTOM_EXCEPTION.REVIEWED")),
                "validity": r.get("O_CUSTOM_EXCEPTION.validez_de_excepcion"),
            }
            for r in rows
        ],
    }


def handle_get_absenteeism_kpis(args):
    supervisor = args["supervisor_name"]
    site = args.get("site")
    site_type = args.get("site_type")
    status = args.get("employee_status")
    if status is None:
        return clarification_for_status()

    if status is None:
        return {
            "clarification_needed": True,
            "question": (
                "¿Querés ver los KPIs de ausentismo para qué empleados?\n"
                "- 'Activos' (employee_status: Active)\n"
                "- 'Cesantes' (employee_status: Terminated)\n"
                "- 'Todos' (employee_status: all)"
            ),
            "instruction_for_claude": (
                "Before calling this tool again, ask the user: "
                "'¿Querés ver los datos de Activos, Cesantes o Todos?' "
                "Then call get_absenteeism_kpis again with the corresponding employee_status value: "
                "Active, Terminated, or all."
            ),
        }

    start_date, end_date, period_label = resolve_dates(args)

    filters = {
        "string_filters": base_string_filters(supervisor, site, status, site_type),
        "date_filters": build_date_filters(start_date, end_date, "O_CUSTOM_EXCEPTION.EVENTDATE"),
    }

    celonis_args = {
        "columns": [
            "total_de_ausencias",
            "total_de_ausencias_injustificadas",
            "total_de_ausencias_justitificas",
            "de_ausencias_justificadas",
            "empleados_con_3_ausencias_injustificadas",
            "percentage_empleados_con_3_ausencias_injustificadas",
            "de_empleados_con_al_menos_1_ausencia_injustificada",
            "total_empleados_con_al_menos_una_ausencia",
            "empleados_con_ausentismo_cr_tico_10_",
            "de_empleados_con_ausentismo_cr_tico",
        ],
        "applied_filters": filters,
    }

    raw = call_celonis_tool("load_data", celonis_args)
    rows = extract_rows(raw)
    kpis = rows[0] if rows else {}

    def pct(val):
        return round((val or 0) * 100, 1)

    return {
        "supervisor": supervisor,
        "period": period_label,
        "date_range": {"start": start_date, "end": end_date},
        "employee_status": status if status.lower() != "all" else "all",
        "site_filter": site or None,
        "site_type_filter": site_type or None,
        "total_absences": kpis.get("total_de_ausencias"),
        "unjustified": kpis.get("total_de_ausencias_injustificadas"),
        "justified": kpis.get("total_de_ausencias_justitificas"),
        "pct_justified": pct(kpis.get("de_ausencias_justificadas")),
        "employees_with_3plus_unjustified": kpis.get("empleados_con_3_ausencias_injustificadas"),
        "pct_employees_3plus_unjustified": pct(kpis.get("percentage_empleados_con_3_ausencias_injustificadas")),
        "pct_employees_at_least_1_unjustified": pct(kpis.get("de_empleados_con_al_menos_1_ausencia_injustificada")),
        "employees_with_any_absence": kpis.get("total_empleados_con_al_menos_una_ausencia"),
        "employees_critical_10pct": kpis.get("empleados_con_ausentismo_cr_tico_10_"),
        "pct_employees_critical": pct(kpis.get("de_empleados_con_ausentismo_cr_tico")),
    }


def handle_get_punch_edits(args):
    supervisor = args["supervisor_name"]
    site = args.get("site")
    site_type = args.get("site_type")
    status = args.get("employee_status")
    if status is None:
        return clarification_for_status()
    page = args.get("page", 0)
    page_size = min(int(args.get("page_size", 50)), 100)
    start_date, end_date, period_label = resolve_dates(args)

    kpi_args = {
        "columns": [
            "ediciones_manuales_de_marcaje",
            "timecards_con_marcaje_editado_manualmente",
            "percentage_timecards_con_marcaje_editado_manualmente",
            "marcajes_editados_manualmente",
        ],
        "applied_filters": {"string_filters": base_string_filters(supervisor, site, status, site_type)},
    }
    raw_kpi = call_celonis_tool("load_data", kpi_args)
    kpi_rows = extract_rows(raw_kpi)
    kpis = kpi_rows[0] if kpi_rows else {}

    def pct(val):
        return round((val or 0) * 100, 1)

    result = {
        "supervisor": supervisor,
        "period": period_label,
        "date_range": {"start": start_date, "end": end_date},
        "site_filter": site or None,
        "site_type_filter": site_type or None,
        "note": "KPIs cover the full history for this supervisor. Date range applies to detail rows only.",
        "kpis": {
            "manual_punch_edits": kpis.get("ediciones_manuales_de_marcaje"),
            "pct_manually_edited_punches": pct(kpis.get("marcajes_editados_manualmente")),
            "timecards_with_edited_punches": kpis.get("timecards_con_marcaje_editado_manualmente"),
            "pct_timecards_with_edited_punches": pct(
                kpis.get("percentage_timecards_con_marcaje_editado_manualmente")
            ),
        },
    }

    return result


def handle_get_paycode_corrections(args):
    supervisor = args["supervisor_name"]
    site = args.get("site")
    site_type = args.get("site_type")
    status = args.get("employee_status")
    if status is None:
        return clarification_for_status()
    page = args.get("page", 0)
    page_size = min(int(args.get("page_size", 50)), 100)
    start_date, end_date, period_label = resolve_dates(args)

    def pct(val):
        return round((val or 0) * 100, 1)

    raw_kpi = call_celonis_tool("load_data", {
        "columns": [
            "correcciones_de_paycode",
            "timecards_con_correcciones_de_paycode",
            "percentage_timecards_con_correcciones_de_paycode",
            "percentage_correcciones_de_paycode",
        ],
        "applied_filters": {"string_filters": base_string_filters(supervisor, site, status, site_type)},
    })
    kpis = (extract_rows(raw_kpi) or [{}])[0]

    return {
        "supervisor": supervisor,
        "_guidance": ANALYSIS_GUIDANCE,
        "site_filter": site or None,
        "site_type_filter": site_type or None,
        "employee_status_filter": status if status.lower() != "all" else "all",
        "period": period_label,
        "date_range": {"start": start_date, "end": end_date},
        "kpis": {
            "paycode_corrections": kpis.get("correcciones_de_paycode"),
            "timecards_with_corrections": kpis.get("timecards_con_correcciones_de_paycode"),
            "pct_timecards_with_corrections": pct(kpis.get("percentage_timecards_con_correcciones_de_paycode")),
            "pct_corrections": pct(kpis.get("percentage_correcciones_de_paycode")),
        },
    }


def handle_get_employee_detail(args):
    employee = args["employee"]
    status = args.get("employee_status")
    if status is None:
        return clarification_for_status()
    page = args.get("page", 0)
    page_size = min(int(args.get("page_size", 20)), 50)

    raw = call_celonis_tool("load_data_employee", {
        "employee": employee,
        "page": page,
        "page_size": page_size,
    })
    rows = extract_rows(raw)
    total = raw.get("Count", len(rows)) if isinstance(raw, dict) else len(rows)

    def clean(r):
        return {
            "employee_id":        r.get("O_CUSTOM_EMPLOYEE.ID"),
            "role":               r.get("O_CUSTOM_EMPLOYEE.PUESTO"),
            "employee_type":      r.get("O_CUSTOM_EMPLOYEE.TIPOEMPLEADO"),
            "employment_status":  r.get("O_CUSTOM_EMPLOYEE.EMPLOYMENTSTATUS"),
            "employment_state":   r.get("O_CUSTOM_EMPLOYEE.estado_laboral"),
            "supervisor_name":    r.get("O_CUSTOM_EMPLOYEE.SUPERVISORNAME"),
            "society":            r.get("O_CUSTOM_EMPLOYEE.SOCIEDAD"),
            "location":           r.get("O_CUSTOM_EMPLOYEE.LOCACIONDESCRIPTION"),
            "country":            r.get("O_CUSTOM_EMPLOYEE.pais"),
            "team":               r.get("O_CUSTOM_EMPLOYEE.EQUIPO"),
            "effective_date":     r.get("O_CUSTOM_EMPLOYEE.EFFECTIVEDATE"),
            "kpis": {
                "total_absences":              r.get("total_de_ausencias"),
                "unjustified_absences":        r.get("total_de_ausencias_injustificadas"),
                "justified_absences":          r.get("total_de_ausencias_justitificas"),
                "pct_justified_shifts_raw":    r.get("jornadas_incompletas_justificadas"),
                "pct_closed_no_justif_raw":    r.get("cerradas_sin_justificar"),
                "pct_paycode_corrections_raw": r.get("percentage_timecards_con_correcciones_de_paycode"),
            },
        }

    employees = [c for c in (clean(r) for r in rows) if c.get("employee_id")]

    # Post-filter by status (load_data_employee doesn't support it natively)
    if status.lower() != "all":
        employees = [e for e in employees if (e.get("employment_status") or "").lower() == status.lower()]

    return {
        "_guidance": ANALYSIS_GUIDANCE,
        "search": employee,
        "employee_status_filter": status,
        "total": len(employees),
        "page": page,
        "page_size": page_size,
        "employees": employees,
    }


def handle_get_overtime_summary(args):
    supervisor = args["supervisor_name"]
    site = args.get("site")
    site_type = args.get("site_type")
    status = args.get("employee_status")
    if status is None:
        return clarification_for_status()
    page = args.get("page", 0)
    page_size = min(int(args.get("page_size", 50)), 100)
    start_date, end_date, period_label = resolve_dates(args)

    # ── Step 1: get employee IDs + names for this supervisor (+ site + status) ─
    emp_raw = call_celonis_tool("load_data", {
        "columns": [
            "O_CUSTOM_EMPLOYEE.FULLNAME",
            "O_CUSTOM_EMPLOYEE.LDAP",
            "O_CUSTOM_EXCEPTION.EMPLOYEEID",
        ],
        "applied_filters": {"string_filters": base_string_filters(supervisor, site, status, site_type)},
        "page": 0,
        "page_size": 500,
    })
    emp_rows = extract_rows(emp_raw)

    # Build id → {name, ldap} lookup (dedup)
    emp_lookup = {}
    for r in emp_rows:
        eid = r.get("O_CUSTOM_EXCEPTION.EMPLOYEEID")
        if eid and eid not in emp_lookup:
            emp_lookup[eid] = {
                "name": r.get("O_CUSTOM_EMPLOYEE.FULLNAME"),
                "ldap": r.get("O_CUSTOM_EMPLOYEE.LDAP"),
            }

    if not emp_lookup:
        return {
            "supervisor": supervisor,
            "site_filter": site or None,
            "period": period_label,
            "date_range": {"start": start_date, "end": end_date},
            "error": "No employees found for this supervisor.",
        }

    # ── Step 2: overtime records filtered by employee IDs + date ─────────────
    ot_raw = call_celonis_tool("load_data", {
        "columns": [
            "O_CUSTOM_OVERTIMEAPPROVAL.EMPLOYEEID",
            "O_CUSTOM_OVERTIMEAPPROVAL.ACTION",
            "O_CUSTOM_OVERTIMEAPPROVAL.AMOUNT",
            "O_CUSTOM_OVERTIMEAPPROVAL.APPLYDATE",
            "O_CUSTOM_OVERTIMEAPPROVAL.REVIEWEDDATE",
            "O_CUSTOM_OVERTIMEAPPROVAL.REVIEWERNAME",
        ],
        "applied_filters": {
            "string_filters": [
                {
                    "column_id": "O_CUSTOM_OVERTIMEAPPROVAL.EMPLOYEEID",
                    "values": list(emp_lookup.keys()),
                    "add_wildcard_before": False,
                    "add_wildcard_after": False,
                    "case_sensitive": False,
                }
            ],
            "date_filters": build_date_filters(
                start_date, end_date, "O_CUSTOM_OVERTIMEAPPROVAL.APPLYDATE"
            ),
        },
        "order_by": "O_CUSTOM_OVERTIMEAPPROVAL.APPLYDATE",
        "ascending": False,
        "page": page,
        "page_size": page_size,
    })
    rows = extract_rows(ot_raw)
    total = ot_raw.get("Count", len(rows)) if isinstance(ot_raw, dict) else len(rows)

    # ── Aggregate ─────────────────────────────────────────────────────────────
    by_action = {}
    total_secs_requested = 0.0
    total_secs_approved = 0.0
    for r in rows:
        action = r.get("O_CUSTOM_OVERTIMEAPPROVAL.ACTION") or "UNKNOWN"
        secs = r.get("O_CUSTOM_OVERTIMEAPPROVAL.AMOUNT") or 0
        by_action.setdefault(action, {"count": 0, "total_hours": 0.0})
        by_action[action]["count"] += 1
        by_action[action]["total_hours"] = round(by_action[action]["total_hours"] + secs / 3600, 2)
        total_secs_requested += secs
        if "APPROV" in action.upper():
            total_secs_approved += secs

    return {
        "supervisor": supervisor,
        "period": period_label,
        "date_range": {"start": start_date, "end": end_date},
        "site_filter": site or None,
        "site_type_filter": site_type or None,
        "total_records": total,
        "page": page,
        "page_size": page_size,
        "summary": {
            "total_hours_requested": round(total_secs_requested / 3600, 2),
            "total_hours_approved": round(total_secs_approved / 3600, 2),
            "by_action": sorted(
                [{"action": k, **v} for k, v in by_action.items()],
                key=lambda x: -x["count"],
            ),
        },
        "records": [
            {
                "employee": emp_lookup.get(r.get("O_CUSTOM_OVERTIMEAPPROVAL.EMPLOYEEID"), {}).get("name"),
                "ldap": emp_lookup.get(r.get("O_CUSTOM_OVERTIMEAPPROVAL.EMPLOYEEID"), {}).get("ldap"),
                "action": r.get("O_CUSTOM_OVERTIMEAPPROVAL.ACTION"),
                "hours": round((r.get("O_CUSTOM_OVERTIMEAPPROVAL.AMOUNT") or 0) / 3600, 2),
                "apply_date": r.get("O_CUSTOM_OVERTIMEAPPROVAL.APPLYDATE"),
                "reviewed_date": r.get("O_CUSTOM_OVERTIMEAPPROVAL.REVIEWEDDATE"),
                "reviewer": r.get("O_CUSTOM_OVERTIMEAPPROVAL.REVIEWERNAME"),
            }
            for r in rows
        ],
    }


def handle_get_incomplete_shifts_kpis(args):
    supervisor = args["supervisor_name"]
    site = args.get("site")
    site_type = args.get("site_type")
    status = args.get("employee_status")
    start_date, end_date, period_label = resolve_dates(args)

    filters = {
        "string_filters": base_string_filters(supervisor, site, status or "Active", site_type),
        "date_filters": build_date_filters(start_date, end_date, "O_CUSTOM_EXCEPTION.EVENTDATE"),
    }

    celonis_args = {
        "columns": [
            "jornadas_pendientes",
            "justificadas_y_aprobadas",
            "justificadas_pendientes_de_aprobaci_n",
            "cerradas_sin_justificar",
            "sin_gesti_n_requerida",
            "jornadas_incompletas_justificadas",
            "jornadas_incompletas_sin_justificar",
            "jornadas_incompletas_sin_gesti_n_requerida",
        ],
        "applied_filters": filters,
    }

    raw = call_celonis_tool("load_data", celonis_args)
    rows = extract_rows(raw)
    kpis = rows[0] if rows else {}

    def pct(val):
        return round((val or 0) * 100, 1)

    return {
        "supervisor": supervisor,
        "period": period_label,
        "date_range": {"start": start_date, "end": end_date},
        "site_filter": site or None,
        "site_type_filter": site_type or None,
        "note": "All values are percentages of total incomplete shifts in the period.",
        "kpis": {
            "pct_pending":                    pct(kpis.get("jornadas_pendientes")),
            "pct_justified_approved":         pct(kpis.get("justificadas_y_aprobadas")),
            "pct_justified_pending_approval": pct(kpis.get("justificadas_pendientes_de_aprobaci_n")),
            "pct_closed_without_justification": pct(kpis.get("cerradas_sin_justificar")),
            "pct_no_action_required":         pct(kpis.get("sin_gesti_n_requerida")),
            "pct_justified_total":            pct(kpis.get("jornadas_incompletas_justificadas")),
            "pct_unjustified_total":          pct(kpis.get("jornadas_incompletas_sin_justificar")),
            "pct_no_mgmt_required":           pct(kpis.get("jornadas_incompletas_sin_gesti_n_requerida")),
        },
    }


def handle_get_pending_exceptions(args):
    supervisor = args["supervisor_name"]
    site = args.get("site")
    site_type = args.get("site_type")
    status = args.get("employee_status")
    if status is None:
        return clarification_for_status()
    page = args.get("page", 0)
    page_size = min(int(args.get("page_size", 50)), 100)
    start_date, end_date, period_label = resolve_dates(args)

    filters = {
        "string_filters": base_string_filters(supervisor, site, status, site_type) + [
            {
                "column_id": "O_CUSTOM_EXCEPTION.exception_status",
                "values": ["Pendiente"],
                "add_wildcard_before": False,
                "add_wildcard_after": False,
                "case_sensitive": False,
            },
        ],
        "date_filters": build_date_filters(start_date, end_date, "O_CUSTOM_EXCEPTION.EVENTDATE"),
    }

    celonis_args = {
        "columns": [
            "O_CUSTOM_EMPLOYEE.FULLNAME",
            "O_CUSTOM_EMPLOYEE.LDAP",
            "O_CUSTOM_EXCEPTION.EXCEPTIONTYPENAME",
            "O_CUSTOM_EXCEPTION.exception_type_mapping",
            "O_CUSTOM_EXCEPTION.exception_status",
            "O_CUSTOM_EXCEPTION.EVENTDATE",
            "O_CUSTOM_EXCEPTION.STARTDATETIME",
            "O_CUSTOM_EXCEPTION.REVIEWED",
            "O_CUSTOM_EXCEPTION.validez_de_excepcion",
        ],
        "applied_filters": filters,
        "order_by": "O_CUSTOM_EXCEPTION.EVENTDATE",
        "ascending": False,
        "page": page,
        "page_size": page_size,
    }

    raw = call_celonis_tool("load_data", celonis_args)
    rows = extract_rows(raw)
    total = raw.get("Count", len(rows)) if isinstance(raw, dict) else len(rows)

    by_type = {}
    for r in rows:
        exc_type = r.get("O_CUSTOM_EXCEPTION.EXCEPTIONTYPENAME") or "UNKNOWN"
        by_type[exc_type] = by_type.get(exc_type, 0) + 1

    return {
        "supervisor": supervisor,
        "period": period_label,
        "date_range": {"start": start_date, "end": end_date},
        "site_filter": site or None,
        "site_type_filter": site_type or None,
        "total_pending": total,
        "page": page,
        "page_size": page_size,
        "summary_by_type": sorted(
            [{"exception_type": k, "count": v} for k, v in by_type.items()],
            key=lambda x: -x["count"],
        ),
        "exceptions": [
            {
                "employee": r.get("O_CUSTOM_EMPLOYEE.FULLNAME"),
                "ldap": r.get("O_CUSTOM_EMPLOYEE.LDAP"),
                "exception_type": r.get("O_CUSTOM_EXCEPTION.EXCEPTIONTYPENAME"),
                "category": r.get("O_CUSTOM_EXCEPTION.exception_type_mapping"),
                "event_date": r.get("O_CUSTOM_EXCEPTION.EVENTDATE"),
                "start_datetime": r.get("O_CUSTOM_EXCEPTION.STARTDATETIME"),
                "reviewed": bool(r.get("O_CUSTOM_EXCEPTION.REVIEWED")),
                "validity": r.get("O_CUSTOM_EXCEPTION.validez_de_excepcion"),
            }
            for r in rows
        ],
    }


def handle_list_supervisors(args):
    search = args.get("search", "").strip()
    site = args.get("site")
    site_type = args.get("site_type")
    status = args.get("employee_status")
    if status is None:
        return clarification_for_status()

    filters = {
        "null_filters": [
            {"column_id": "O_CUSTOM_EMPLOYEE.SUPERVISORNAME", "is_null": False}
        ]
    }
    string_filters = []
    if search:
        string_filters.append({
            "column_id": "O_CUSTOM_EMPLOYEE.SUPERVISORNAME",
            "values": [search],
            "add_wildcard_before": True,
            "add_wildcard_after": True,
            "case_sensitive": False,
        })
    sf = build_site_filter(site)
    if sf:
        string_filters.append(sf)
    stf = build_site_type_filter(site_type)
    if stf:
        string_filters.append(stf)
    status_f = build_status_filter(status)
    if status_f:
        string_filters.append(status_f)
    if string_filters:
        filters["string_filters"] = string_filters

    page_size = 100 if (search or site) else _SUPERVISOR_SAMPLE_SIZE
    celonis_args = {
        "columns": [
            "O_CUSTOM_EMPLOYEE.SUPERVISORNAME",
            "O_CUSTOM_EMPLOYEE.SUPERVISORLDAP",
            "O_CUSTOM_EXCEPTION.ID",
        ],
        "applied_filters": filters,
        "page": 0,
        "page_size": page_size,
    }

    raw = call_celonis_tool("load_data", celonis_args)
    rows = extract_rows(raw)

    seen = {}
    for r in rows:
        name = r.get("O_CUSTOM_EMPLOYEE.SUPERVISORNAME")
        if name and name not in seen:
            seen[name] = r.get("O_CUSTOM_EMPLOYEE.SUPERVISORLDAP")

    supervisors = sorted(
        [{"name": n, "ldap": l} for n, l in seen.items()],
        key=lambda x: x["name"],
    )
    return {
        "total": len(supervisors),
        "search_filter": search or None,
        "site_filter": site or None,
        "site_type_filter": site_type or None,
        "employee_status_filter": status if status.lower() != "all" else "all",
        "note": "Results based on a sample of recent exception records. Use 'search' to find a specific supervisor." if not (search or site) else None,
        "supervisors": supervisors,
    }


def _extract_site_type(description):
    """Extracts type prefix from 'SC - Guadalajara SGD1' → 'SC'. Returns None if no ' - ' pattern."""
    if description and " - " in description:
        return description.split(" - ")[0].strip()
    return None


def handle_list_sites(args):
    search = args.get("search", "").strip()
    site_type = args.get("site_type", "").strip()

    filters = {
        "null_filters": [
            {"column_id": "O_CUSTOM_EMPLOYEE.LOCACIONDESCRIPTION", "is_null": False}
        ]
    }
    string_filters = []
    if search:
        string_filters.append({
            "column_id": "O_CUSTOM_EMPLOYEE.LOCACIONDESCRIPTION",
            "values": [search],
            "add_wildcard_before": True,
            "add_wildcard_after": True,
            "case_sensitive": False,
        })
    stf = build_site_type_filter(site_type)
    if stf:
        string_filters.append(stf)
    if string_filters:
        filters["string_filters"] = string_filters

    raw = call_celonis_tool("load_data", {
        "columns": [
            "O_CUSTOM_EMPLOYEE.LOCACIONDESCRIPTION",
            "O_CUSTOM_EMPLOYEE.LOCACION",
            "O_CUSTOM_EMPLOYEE.SOCIEDAD",
            "O_CUSTOM_EMPLOYEE.pais",
            "O_CUSTOM_EXCEPTION.ID",
        ],
        "applied_filters": filters,
        "page": 0,
        "page_size": 500,
    })
    rows = extract_rows(raw)

    seen = {}
    for r in rows:
        desc = r.get("O_CUSTOM_EMPLOYEE.LOCACIONDESCRIPTION")
        if desc and desc not in seen:
            seen[desc] = {
                "site_type": _extract_site_type(desc),
                "location_code": r.get("O_CUSTOM_EMPLOYEE.LOCACION"),
                "society": r.get("O_CUSTOM_EMPLOYEE.SOCIEDAD"),
                "country": r.get("O_CUSTOM_EMPLOYEE.pais"),
            }

    sites = sorted(
        [{"description": d, **v} for d, v in seen.items()],
        key=lambda x: (x["site_type"] or "", x["description"]),
    )

    # Summary count by type
    type_counts = {}
    for s in sites:
        t = s["site_type"] or "OTHER"
        type_counts[t] = type_counts.get(t, 0) + 1

    return {
        "total": len(sites),
        "search_filter": search or None,
        "site_type_filter": site_type or None,
        "summary_by_type": sorted(
            [{"site_type": k, "count": v} for k, v in type_counts.items()],
            key=lambda x: -x["count"],
        ),
        "sites": sites,
    }


def handle_get_extended_shifts_analysis(args):
    supervisor = (args.get("supervisor_name") or "").strip() or None
    site = args.get("site")
    site_type = args.get("site_type")
    status = args.get("employee_status")
    if status is None:
        return clarification_for_status()

    start_date, end_date, period_label = resolve_dates(args)
    page = args.get("page", 0)
    page_size = min(int(args.get("page_size", 100)), 500)

    DAILY_THRESHOLD_HOURS = 12.5   # 12h work + 30min site transit tolerance
    WEEKLY_OT_THRESHOLD_HOURS = 13.0

    # ── Step 1: Resolve employees under this supervisor/site/status ───────────
    base_sf = []
    if supervisor:
        base_sf.append({
            "column_id": "O_CUSTOM_EMPLOYEE.SUPERVISORNAME",
            "values": [supervisor],
            "add_wildcard_before": False,
            "add_wildcard_after": False,
            "case_sensitive": False,
        })
    sf = build_site_filter(site)
    if sf:
        base_sf.append(sf)
    stf = build_site_type_filter(site_type)
    if stf:
        base_sf.append(stf)
    status_f = build_status_filter(status)
    if status_f:
        base_sf.append(status_f)

    emp_raw = call_celonis_tool("load_data", {
        "columns": [
            "O_CUSTOM_EMPLOYEE.FULLNAME",
            "O_CUSTOM_EMPLOYEE.LDAP",
            "O_CUSTOM_EXCEPTION.EMPLOYEEID",
        ],
        "applied_filters": {"string_filters": base_sf} if base_sf else {},
        "page": 0,
        "page_size": 500,
    })
    emp_rows = extract_rows(emp_raw)

    emp_lookup = {}
    for r in emp_rows:
        eid = r.get("O_CUSTOM_EXCEPTION.EMPLOYEEID")
        if eid and eid not in emp_lookup:
            emp_lookup[eid] = {
                "name": r.get("O_CUSTOM_EMPLOYEE.FULLNAME"),
                "ldap": r.get("O_CUSTOM_EMPLOYEE.LDAP"),
            }

    if not emp_lookup:
        return {
            "supervisor": supervisor,
            "site_filter": site or None,
            "period": period_label,
            "date_range": {"start": start_date, "end": end_date},
            "error": "No employees found for the given filters.",
        }

    emp_ids = list(emp_lookup.keys())
    date_f_exc = build_date_filters(start_date, end_date, "O_CUSTOM_EXCEPTION.EVENTDATE")

    # ── Step 2: Fetch early-entry + late-out exceptions (all statuses) ────────
    EARLY_KW = {"early", "temprana", "anticipada"}
    LATE_KW = {"late", "tarde", "tardia", "tardía", "salida tard"}

    def exc_is_early(type_name):
        lower = (type_name or "").lower()
        return any(k in lower for k in EARLY_KW)

    def exc_is_late(type_name):
        lower = (type_name or "").lower()
        return any(k in lower for k in LATE_KW)

    exc_raw = call_celonis_tool("load_data", {
        "columns": [
            "O_CUSTOM_EXCEPTION.EMPLOYEEID",
            "O_CUSTOM_EXCEPTION.EXCEPTIONTYPENAME",
            "O_CUSTOM_EXCEPTION.exception_status",
            "O_CUSTOM_EXCEPTION.EVENTDATE",
            "O_CUSTOM_EXCEPTION.STARTDATETIME",
            "O_CUSTOM_EXCEPTION.ENDDATETIME",
            "O_CUSTOM_EXCEPTION.PAYCODEQUALIFIER",
        ],
        "applied_filters": {
            "string_filters": [
                {
                    "column_id": "O_CUSTOM_EXCEPTION.EMPLOYEEID",
                    "values": emp_ids,
                    "add_wildcard_before": False,
                    "add_wildcard_after": False,
                    "case_sensitive": False,
                },
            ],
            "date_filters": date_f_exc,
        },
        "page": page,
        "page_size": page_size,
    })
    exc_rows = extract_rows(exc_raw)

    # Group by (emp_id, eventdate) to reconstruct shift window per day
    shift_map = {}  # (emp_id, date_str) -> {earliest_start, latest_end, early_excs, late_excs}

    for r in exc_rows:
        type_name = r.get("O_CUSTOM_EXCEPTION.EXCEPTIONTYPENAME") or ""
        is_early = exc_is_early(type_name)
        is_late = exc_is_late(type_name)
        if not is_early and not is_late:
            continue

        emp_id = r.get("O_CUSTOM_EXCEPTION.EMPLOYEEID")
        event_date = r.get("O_CUSTOM_EXCEPTION.EVENTDATE")
        if not emp_id or not event_date:
            continue

        start_dt = parse_dt(r.get("O_CUSTOM_EXCEPTION.STARTDATETIME"))
        end_dt = parse_dt(r.get("O_CUSTOM_EXCEPTION.ENDDATETIME"))
        date_str = str(event_date)[:10]
        key = (emp_id, date_str)

        if key not in shift_map:
            shift_map[key] = {
                "earliest_start": None,
                "latest_end": None,
                "early_excs": [],
                "late_excs": [],
            }

        entry = shift_map[key]

        if is_early and start_dt:
            if entry["earliest_start"] is None or start_dt < entry["earliest_start"]:
                entry["earliest_start"] = start_dt
            entry["early_excs"].append({
                "type": type_name,
                "status": r.get("O_CUSTOM_EXCEPTION.exception_status"),
                "start": r.get("O_CUSTOM_EXCEPTION.STARTDATETIME"),
                "paycode": r.get("O_CUSTOM_EXCEPTION.PAYCODEQUALIFIER"),
            })

        if is_late and end_dt:
            if entry["latest_end"] is None or end_dt > entry["latest_end"]:
                entry["latest_end"] = end_dt
            entry["late_excs"].append({
                "type": type_name,
                "status": r.get("O_CUSTOM_EXCEPTION.exception_status"),
                "end": r.get("O_CUSTOM_EXCEPTION.ENDDATETIME"),
                "paycode": r.get("O_CUSTOM_EXCEPTION.PAYCODEQUALIFIER"),
            })

    # Identify daily violations: shift window > 12h30m
    daily_violations = []
    for (emp_id, date_str), entry in shift_map.items():
        s = entry["earliest_start"]
        e = entry["latest_end"]
        if not s or not e:
            continue
        duration_secs = (e - s).total_seconds()
        if duration_secs <= 0:
            continue
        duration_hours = round(duration_secs / 3600, 2)
        if duration_hours > DAILY_THRESHOLD_HOURS:
            emp_info = emp_lookup.get(emp_id, {})
            daily_violations.append({
                "employee_id": emp_id,
                "employee": emp_info.get("name"),
                "ldap": emp_info.get("ldap"),
                "date": date_str,
                "shift_start": s.strftime("%Y-%m-%dT%H:%M:%S"),
                "shift_end": e.strftime("%Y-%m-%dT%H:%M:%S"),
                "duration_hours": duration_hours,
                "early_entry_exceptions": entry["early_excs"],
                "late_out_exceptions": entry["late_excs"],
            })

    # ── Step 3: Weekly overtime from O_CUSTOM_OVERTIMEAPPROVAL ───────────────
    ot_raw = call_celonis_tool("load_data", {
        "columns": [
            "O_CUSTOM_OVERTIMEAPPROVAL.EMPLOYEEID",
            "O_CUSTOM_OVERTIMEAPPROVAL.ACTION",
            "O_CUSTOM_OVERTIMEAPPROVAL.AMOUNT",
            "O_CUSTOM_OVERTIMEAPPROVAL.APPLYDATE",
            "O_CUSTOM_OVERTIMEAPPROVAL.PAYCODEQUALIFIER",
            "O_CUSTOM_OVERTIMEAPPROVAL.REVIEWERNAME",
        ],
        "applied_filters": {
            "string_filters": [
                {
                    "column_id": "O_CUSTOM_OVERTIMEAPPROVAL.EMPLOYEEID",
                    "values": emp_ids,
                    "add_wildcard_before": False,
                    "add_wildcard_after": False,
                    "case_sensitive": False,
                }
            ],
            "date_filters": build_date_filters(start_date, end_date, "O_CUSTOM_OVERTIMEAPPROVAL.APPLYDATE"),
        },
        "page": 0,
        "page_size": page_size,
    })
    ot_rows = extract_rows(ot_raw)

    def classify_paycode(action, paycode):
        combined = ((action or "") + " " + (paycode or "")).lower()
        if any(k in combined for k in ["triple", "3x", "3 x"]):
            return "triple_overtime"
        if any(k in combined for k in ["double", "doble", "2x", "2 x"]):
            return "double_overtime"
        return None

    weekly_ot = {}  # (emp_id, week_str) -> {total_hours, records}
    for r in ot_rows:
        emp_id = r.get("O_CUSTOM_OVERTIMEAPPROVAL.EMPLOYEEID")
        apply_date = r.get("O_CUSTOM_OVERTIMEAPPROVAL.APPLYDATE")
        amount_secs = r.get("O_CUSTOM_OVERTIMEAPPROVAL.AMOUNT") or 0
        week = get_iso_week(apply_date)
        if not emp_id or not week:
            continue

        key = (emp_id, week)
        if key not in weekly_ot:
            weekly_ot[key] = {"total_hours": 0.0, "records": []}

        hours = round(amount_secs / 3600, 2)
        weekly_ot[key]["total_hours"] = round(weekly_ot[key]["total_hours"] + hours, 2)

        paycode = r.get("O_CUSTOM_OVERTIMEAPPROVAL.PAYCODEQUALIFIER")
        action = r.get("O_CUSTOM_OVERTIMEAPPROVAL.ACTION")
        weekly_ot[key]["records"].append({
            "date": str(apply_date)[:10],
            "hours": hours,
            "action": action,
            "paycode": paycode or None,
            "overtime_category": classify_paycode(action, paycode),
            "reviewer": r.get("O_CUSTOM_OVERTIMEAPPROVAL.REVIEWERNAME"),
        })

    weekly_violations = []
    for (emp_id, week), data in weekly_ot.items():
        if data["total_hours"] > WEEKLY_OT_THRESHOLD_HOURS:
            emp_info = emp_lookup.get(emp_id, {})
            special = sorted({r["overtime_category"] for r in data["records"] if r["overtime_category"]})
            weekly_violations.append({
                "employee_id": emp_id,
                "employee": emp_info.get("name"),
                "ldap": emp_info.get("ldap"),
                "week": week,
                "total_overtime_hours": data["total_hours"],
                "overtime_categories": special if special else None,
                "records": data["records"],
            })

    # ── Step 4: Repeat offenders (> 2 daily violations in the same week) ─────
    viol_by_emp_week = {}
    for v in daily_violations:
        week = get_iso_week(v["date"])
        if not week:
            continue
        key = (v["employee_id"], week)
        viol_by_emp_week.setdefault(key, []).append(v["date"])

    repeat_offenders = [
        {
            "employee_id": emp_id,
            "employee": emp_lookup.get(emp_id, {}).get("name"),
            "ldap": emp_lookup.get(emp_id, {}).get("ldap"),
            "week": week,
            "violation_days_count": len(dates),
            "dates": sorted(dates),
        }
        for (emp_id, week), dates in viol_by_emp_week.items()
        if len(dates) > 2
    ]

    return {
        "_guidance": ANALYSIS_GUIDANCE,
        "supervisor": supervisor,
        "site_filter": site or None,
        "site_type_filter": site_type or None,
        "employee_status_filter": status,
        "period": period_label,
        "date_range": {"start": start_date, "end": end_date},
        "thresholds": {
            "daily_max_hours": DAILY_THRESHOLD_HOURS,
            "daily_tolerance_note": "30min tolerance for site entry/exit included (flags shifts > 12h actual work)",
            "weekly_overtime_max_hours": WEEKLY_OT_THRESHOLD_HOURS,
        },
        "summary": {
            "employees_scanned": len(emp_lookup),
            "total_daily_violations": len(daily_violations),
            "affected_employees_daily": len({v["employee_id"] for v in daily_violations}),
            "weeks_over_13h_overtime": len(weekly_violations),
            "employees_with_3plus_days_per_week": len(repeat_offenders),
            "exception_rows_analyzed": len(exc_rows),
        },
        "daily_violations": sorted(
            daily_violations, key=lambda x: (x.get("employee") or "", x["date"])
        ),
        "weekly_overtime_violations": sorted(
            weekly_violations, key=lambda x: -x["total_overtime_hours"]
        ),
        "repeat_offenders_by_week": sorted(
            repeat_offenders, key=lambda x: -x["violation_days_count"]
        ),
        "methodology": (
            "daily_violations: earliest early-entry STARTDATETIME to latest late-out ENDDATETIME "
            "per employee per day (all exception statuses included). "
            "weekly_overtime_violations: sum of O_CUSTOM_OVERTIMEAPPROVAL.AMOUNT per employee "
            "per ISO calendar week. "
            "repeat_offenders_by_week: employees with >2 daily violation days in the same week."
        ),
    }


# ── MCP protocol ──────────────────────────────────────────────────────────────

HANDLERS = {
    "start_consultation":        handle_start_consultation,
    "compare_supervisors":      handle_compare_supervisors,
    "get_employee_absences":    handle_get_employee_absences,
    "get_supervisor_dashboard": handle_get_supervisor_dashboard,
    "get_employee_detail": handle_get_employee_detail,
    "list_sites": handle_list_sites,
    "get_exceptions_without_paycode": handle_get_exceptions_without_paycode,
    "get_absenteeism_kpis": handle_get_absenteeism_kpis,
    "get_incomplete_shifts_kpis": handle_get_incomplete_shifts_kpis,
    "get_overtime_summary": handle_get_overtime_summary,
    "get_paycode_corrections": handle_get_paycode_corrections,
    "get_pending_exceptions": handle_get_pending_exceptions,
    "get_punch_edits": handle_get_punch_edits,
    "list_supervisors": handle_list_supervisors,
    "get_extended_shifts_analysis": handle_get_extended_shifts_analysis,
}


def respond(req_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}) + "\n")
    sys.stdout.flush()


def respond_error(req_id, code, message):
    sys.stdout.write(json.dumps({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }) + "\n")
    sys.stdout.flush()


def handle_request(req):
    method = req.get("method", "")
    req_id = req.get("id")

    if method == "initialize":
        respond(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "celonis-novedades", "version": "1.2.0"},
        })
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        respond(req_id, {"tools": TOOLS})
    elif method == "tools/call":
        tool_name = req.get("params", {}).get("name")
        tool_args = req.get("params", {}).get("arguments", {})
        handler = HANDLERS.get(tool_name)
        if not handler:
            respond_error(req_id, -32601, f"Unknown tool: {tool_name}")
            return
        try:
            result = handler(tool_args)
            respond(req_id, {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]
            })
        except Exception as e:
            respond_error(req_id, -32603, str(e))
    elif req_id is not None:
        respond_error(req_id, -32601, f"Method not found: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": f"Parse error: {e}"},
            }) + "\n")
            sys.stdout.flush()
            continue
        try:
            handle_request(req)
        except Exception as e:
            req_id = req.get("id")
            if req_id is not None:
                respond_error(req_id, -32603, f"Internal error: {e}")


if __name__ == "__main__":
    main()
