"""Portable secret storage.

Lookup order: environment variable, Windows DPAPI file, local secrets.env file, then
the optional `keyring` package. Secrets never live in config.toml or the vault.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

NAMES = {
    "openrouter": ("OPENROUTER_API_KEY", "openrouter.key.dpapi"),
    "telegram": ("GTD_TELEGRAM_TOKEN", "telegram.key.dpapi"),
    "calendar": ("GTD_CALENDAR_ICS_URL", "calendar.url.dpapi"),
    "radicale": ("GTD_RADICALE_PASSWORD", "radicale.key.dpapi"),
}
SERVICE = "gtd-agent"
SECRET_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{1,31}")


def secret_names(name: str) -> tuple[str, str]:
    """(environment variable, DPAPI file) for a secret. Names beyond the built-in three serve extra model
    endpoints ([endpoint_keys] in config.toml): 'router' reads GTD_SECRET_ROUTER or router.key.dpapi."""
    if name in NAMES:
        return NAMES[name]
    if not SECRET_NAME_RE.fullmatch(name):
        raise ValueError("A secret name uses 2-32 lowercase letters, digits, - or _, starting with a letter")
    return f"GTD_SECRET_{name.upper().replace('-', '_')}", f"{name}.key.dpapi"


def _credentials(state_dir: Path) -> Path:
    return state_dir / "credentials"


def _dpapi(data: bytes, protect: bool) -> bytes:
    """Windows user-scoped DPAPI via ctypes (same scope as PowerShell ConvertFrom-SecureString)."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data, len(data))
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    result = Blob()
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = function(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result))
    if not ok:
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)  # type: ignore[attr-defined]


def _read_dpapi(path: Path) -> str | None:
    if os.name != "nt" or not path.is_file():
        return None
    encoded = path.read_text(encoding="utf-8-sig").strip()
    try:
        return _dpapi(bytes.fromhex(encoded), protect=False).decode("utf-16-le")
    except (OSError, ValueError, AttributeError):
        pass
    script = ("$s = ConvertTo-SecureString -String ([IO.File]::ReadAllText($env:GTD_SECRET_FILE).Trim()); "
              "[Console]::Out.Write([Net.NetworkCredential]::new('', $s).Password)")
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True,
                                text=True, timeout=20, env={**os.environ, "GTD_SECRET_FILE": str(path)},
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))  # no flash when hidden
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 and result.stdout else None


def _write_dpapi(path: Path, value: str) -> None:
    blob = _dpapi(value.encode("utf-16-le"), protect=True)
    _atomic_text(path, blob.hex())


def _env_file(state_dir: Path) -> Path:
    return _credentials(state_dir) / "secrets.env"


def _read_env_file(state_dir: Path) -> dict[str, str]:
    path = _env_file(state_dir)
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    if os.name != "nt":
        os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def get_secret(name: str, state_dir: Path) -> str | None:
    env_name, dpapi_file = secret_names(name)
    value = os.environ.get(env_name)
    if value:
        return value
    value = _read_dpapi(_credentials(state_dir) / dpapi_file)
    if value:
        return value
    value = _read_env_file(state_dir).get(env_name)
    if value:
        return value
    try:
        import keyring  # type: ignore
        return keyring.get_password(SERVICE, name)
    except Exception:  # keyring is optional and may be missing or misconfigured
        return None


def set_secret(name: str, value: str, state_dir: Path) -> str:
    env_name, dpapi_file = secret_names(name)
    value = value.strip()
    if not value:
        raise ValueError("Nothing entered; the saved value is unchanged")
    if os.name == "nt":
        _write_dpapi(_credentials(state_dir) / dpapi_file, value)
        return "Encrypted for this Windows account (DPAPI)"
    values = _read_env_file(state_dir)
    values[env_name] = value
    _atomic_text(_env_file(state_dir), "".join(f"{k}={v}\n" for k, v in sorted(values.items())))
    return f"Saved to {_env_file(state_dir)} (readable only by you)"


def has_secret(name: str, state_dir: Path) -> bool:
    return bool(get_secret(name, state_dir))
