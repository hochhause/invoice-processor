"""
settings_store.py — desktop-mode settings file (KEY=VALUE, .env syntax).

Lives at <app-data>/settings.env so the packaged app needs no terminal and no
shell environment. The launcher calls load_into_environ() BEFORE importing
main/llm, so existing os.environ-based config (ANTHROPIC_API_KEY, bank
accounts, LLM_MODEL, …) keeps working unchanged.

Precedence: real environment variables win over the file — a value already in
os.environ is never overwritten. set_value() persists to the file AND updates
os.environ so changes (e.g. API key pasted in the web UI) apply immediately
without restart.

The file is plain text on the user's machine; this is deliberate — same trust
level as a .env file, no fake security from obfuscation.
"""
import os
import re
from pathlib import Path

import paths

_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def settings_path() -> Path:
    return paths.app_data_dir() / "settings.env"


def _parse(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[key] = val
    return out


def load() -> dict:
    p = settings_path()
    if not p.exists():
        return {}
    return _parse(p.read_text(encoding="utf-8"))


def load_into_environ():
    """Apply file values as defaults — never overrides a set env var."""
    for k, v in load().items():
        os.environ.setdefault(k, v)


def ensure_template(template: Path):
    """First run: seed settings.env from the bundled template."""
    p = settings_path()
    if not p.exists() and template.exists():
        p.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")


def set_value(key: str, value: str):
    """Persist one key (replace in place, comments preserved) + apply to env."""
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
        raise ValueError(f"invalid settings key: {key!r}")
    p = settings_path()
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    new_line = f"{key}={value}"
    replaced = False
    for i, line in enumerate(lines):
        m = _LINE_RE.match(line)
        if m and m.group(1) == key and not line.lstrip().startswith("#"):
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = value
