#!/usr/bin/env python3
"""
Instalador -- MCP Celonis Novedades & Payroll
Ejecutar una sola vez: python install.py
"""
import subprocess, sys, pathlib, shutil, platform, os, urllib.request

CLIENT_ID     = "3315db6f-ff30-48ca-8c25-046294546d48"
CLIENT_SECRET = "wujrJSa6eYlLpHRtdvcAKMCm01QMEW9bGthLPNLby7GXJfsiTiSnUkpj2uUPRI9G"
SERVER_NAME   = "celonis-novedades"
FILENAME      = "celonis-novedades-mcp.py"
SCRIPT_URL    = "https://raw.githubusercontent.com/gabrielamansilla-spec/celonis-novedades-mcp/main/celonis-novedades-mcp.py"

# Windows: "python" es suficiente; Mac/Linux: path absoluto evita problemas de PATH en Claude Code
PYTHON_CMD = "python" if platform.system() == "Windows" else sys.executable


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def find_claude():
    found = shutil.which("claude")
    if found:
        return found
    candidates = [
        os.path.expanduser("~/.claude/local/claude"),
        os.path.expanduser("~/Library/Application Support/Claude/claude"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def main():
    print("=" * 55)
    print("  Instalador MCP Celonis Novedades & Payroll")
    print("=" * 55)

    print(f"\n[1/3] Python {sys.version.split()[0]} OK")
    print(f"      Usando: {PYTHON_CMD}")

    claude_dir = pathlib.Path.home() / ".claude"
    claude_dir.mkdir(exist_ok=True)
    dest = claude_dir / FILENAME

    print(f"\n[2/3] Descargando servidor desde GitHub...")
    try:
        with urllib.request.urlopen(SCRIPT_URL, timeout=30) as resp:
            dest.write_bytes(resp.read())
        print(f"      Guardado en: {dest}")
    except Exception as e:
        print(f"\n[ERROR] No se pudo descargar el servidor:")
        print(f"        {e}")
        print(f"        Verificá tu conexión a internet y volvé a intentar.")
        sys.exit(1)

    claude_bin = find_claude()
    if not claude_bin:
        print("\n[ERROR] No se encontró el comando 'claude'.")
        print("        Instalá Claude Code, abrilo al menos una vez")
        print("        desde la terminal, y volvé a correr este script.")
        sys.exit(1)

    run([claude_bin, "mcp", "remove", SERVER_NAME, "--scope", "user"])

    cmd = [
        claude_bin, "mcp", "add", SERVER_NAME,
        "--scope", "user",
        "--env", f"CELONIS_CLIENT_ID={CLIENT_ID}",
        "--env", f"CELONIS_CLIENT_SECRET={CLIENT_SECRET}",
        "--",
        PYTHON_CMD, str(dest),
    ]
    result = run(cmd)
    if result.returncode != 0:
        print(f"\n[ERROR] No se pudo registrar el servidor:")
        print(result.stderr or result.stdout)
        sys.exit(1)

    print("[3/3] Servidor registrado OK")

    print("\nVerificando conexión...")
    check = run([claude_bin, "mcp", "list"])
    server_line = next((l for l in check.stdout.splitlines() if f"{SERVER_NAME}:" in l), None)
    if server_line:
        ok = "Connected" in server_line
        print(f"  {'[OK]' if ok else '[!] '} {server_line.strip()}")
        if not ok:
            print("\n  Si dice 'Failed to connect':")
            print("  - Verificá que tenés acceso a la red o VPN de MELI")
            print("  - Corré: claude mcp remove celonis-novedades")
            print("    y volvé a ejecutar install.py")
    else:
        print("  Ejecutá 'claude mcp list' para verificar manualmente")

    print("\n" + "=" * 55)
    print("  Instalación completada.")
    print("  IMPORTANTE: abrí una NUEVA sesión de Claude Code.")
    print("  Las sesiones ya abiertas no cargan los tools nuevos.")
    print("=" * 55)


if __name__ == "__main__":
    main()
