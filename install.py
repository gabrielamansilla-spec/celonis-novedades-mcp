#!/usr/bin/env python3
"""
Instalador -- MCP Celonis Novedades & Payroll
Ejecutar una sola vez: python install.py
"""
import subprocess, sys, pathlib, shutil, platform, os, urllib.request, json

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


def get_config_paths():
    """Devuelve los archivos de config de Claude a actualizar."""
    paths = []
    # Claude Code (scope user)
    paths.append(("Claude Code", pathlib.Path.home() / ".claude" / "settings.json"))
    # Claude Desktop app
    system = platform.system()
    if system == "Windows":
        desktop = pathlib.Path(os.environ.get("APPDATA", "")) / "Claude" / "claude_desktop_config.json"
    elif system == "Darwin":
        desktop = pathlib.Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    else:
        desktop = pathlib.Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
    if desktop.exists():
        paths.append(("Claude Desktop", desktop))
    return paths


def register_mcp_direct(dest):
    """Registra el servidor editando directamente los JSON de configuración de Claude."""
    server_config = {
        "command": PYTHON_CMD,
        "args": [str(dest)],
        "env": {
            "CELONIS_CLIENT_ID": CLIENT_ID,
            "CELONIS_CLIENT_SECRET": CLIENT_SECRET,
        }
    }
    registered = False
    for label, config_path in get_config_paths():
        try:
            config = {}
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
            config.setdefault("mcpServers", {})[SERVER_NAME] = server_config
            config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
            print(f"      Registrado en: {config_path}")
            registered = True
        except Exception as e:
            print(f"      [!] No se pudo actualizar {label}: {e}")
    return registered


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
    if claude_bin:
        print(f"\n[3/3] Registrando servidor (claude CLI)...")
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
        print("      Servidor registrado OK")

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
    else:
        print("\n[3/3] CLI de Claude no detectado — registrando en config directamente...")
        if not register_mcp_direct(dest):
            print("\n[ERROR] No se pudo registrar el servidor.")
            print("        Asegurate de tener Claude Code o Claude Desktop instalado")
            print("        e intentá de nuevo.")
            sys.exit(1)
        print("      Servidor registrado OK (modo directo)")
        print("\n  Nota: ejecutá 'claude mcp list' en terminal para verificar,")
        print("  o abrí Claude Desktop y revisá Settings > Developer > MCP Servers.")

    print("\n" + "=" * 55)
    print("  Instalación completada.")
    print("  IMPORTANTE: abrí una NUEVA sesión de Claude Code.")
    print("  Las sesiones ya abiertas no cargan los tools nuevos.")
    print("=" * 55)


if __name__ == "__main__":
    main()
