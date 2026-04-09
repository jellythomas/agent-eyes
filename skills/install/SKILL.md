---
name: install
description: >
  Install agent-eyes platform dependencies and verify system permissions.
  Detects OS, installs platform-specific packages (pyobjc on macOS,
  python-xlib on Linux, comtypes on Windows), checks accessibility
  permissions, verifies Chrome debug port, and runs self-test.
  Use when: user types /agent-eyes:install, user asks to install agent-eyes
  dependencies, first time setup, or after upgrading agent-eyes.
  Also triggers on: "install agent-eyes", "setup dependencies",
  "agent-eyes permissions", "accessibility permissions".
user_invocable: true
---

# agent-eyes:install — Platform Setup

Install platform-specific dependencies and verify system access.

## Instructions

### Step 1: Detect Platform

```bash
uv run python -c "import sys, platform; print(f'{sys.platform} {platform.machine()}')"
```

Map result:
- `darwin arm64` → macOS Apple Silicon
- `darwin x86_64` → macOS Intel
- `linux x86_64` → Linux x86
- `linux aarch64` → Linux ARM
- `win32 AMD64` → Windows x64

### Step 2: Check Dependencies

Try importing each platform-specific package:

**macOS:**
```bash
uv run python -c "
import importlib
deps = [
    'ApplicationServices',
    'Quartz',
    'Cocoa',
]
for dep in deps:
    try:
        importlib.import_module(dep)
        print(f'✓ {dep}')
    except ImportError:
        print(f'✗ {dep} — missing')
"
```

**Linux:**
```bash
uv run python -c "
try:
    from Xlib import display; print('✓ python-xlib')
except: print('✗ python-xlib — missing')
try:
    import Atspi; print('✓ pyatspi2')
except: print('✗ pyatspi2 — install at-spi2-core')
"
```

**Windows:**
```bash
uv run python -c "
try:
    import comtypes; print('✓ comtypes')
except: print('✗ comtypes — missing')
"
```

### Step 3: Install Missing

```bash
# macOS
pip install "agent-eyes[macos]"

# Linux
pip install "agent-eyes[linux]"
# Also need system package:
sudo apt install at-spi2-core    # Ask user first!

# Windows
pip install "agent-eyes[windows]"
```

### Step 4: Check System Permissions

**macOS — Accessibility:**
```bash
uv run python -c "
try:
    from ApplicationServices import AXIsProcessTrusted
    print('✓ Accessibility granted' if AXIsProcessTrusted() else '✗ Accessibility not granted')
except Exception as e:
    print(f'✗ Cannot check: {e}')
"
```

If not granted, tell the user:
> Open **System Settings → Privacy & Security → Accessibility** and add your terminal app.

**All platforms — Chrome debug port:**
```bash
uv run python -c "
import socket
s = socket.socket()
try:
    s.settimeout(1)
    s.connect(('localhost', 9222))
    print('✓ Chrome debug port 9222 available')
except:
    print('✗ Chrome not on port 9222')
    print('  Start with: open -a \"Google Chrome\" --args --remote-debugging-port=9222')
finally:
    s.close()
"
```

### Step 5: Self-Test

```bash
# Test native adapter
uv run python -c "
from agent_eyes.adapters.macos import MacOSAdapter  # or linux/windows
adapter = MacOSAdapter()
if adapter.is_available():
    apps = adapter.list_apps()
    print(f'✓ Native adapter: {len(apps)} apps found')
else:
    print('✗ Native adapter not available')
"
```

### Step 6: Save State

Write to `~/.agent-eyes/install.json`:

```bash
uv run python -c "
import json, sys, platform, datetime
from pathlib import Path

state = {
    'installed': True,
    'platform': sys.platform,
    'arch': platform.machine(),
    'version': '0.5.0',
    'installed_at': datetime.datetime.now().isoformat(),
}

path = Path.home() / '.agent-eyes' / 'install.json'
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(state, indent=2))
print(f'✓ State saved to {path}')
"
```

### Step 7: Report

Print summary:
```
✓ agent-eyes installed on macOS (arm64)
  Dependencies: all installed
  Permissions: accessibility ✓, CDP ✓
  Self-test: native ✓, browser ✓
  State: saved to ~/.agent-eyes/install.json

Next: run /agent-eyes:init to configure your AI tools
```
