"""Settings: a JSON file next to the program, password encrypted by Windows.

Encryption uses the built-in DPAPI (CryptProtectData). The key is tied to the
Windows account: a settings file carried to another machine or user simply fails
to decrypt, and the password is asked for again.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import platform
import sys

IS_WINDOWS = platform.system() == "Windows"

DEFAULTS = {
    "camera": 0,
    "backend": "auto",
    "width": 1280,
    "height": 720,
    "fps": 15,
    "quality": 70,
    "port": 8080,
    "user": "admin",
    "tunnel": True,
    "autostart": False,
}


if IS_WINDOWS:
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
else:
    _Blob = None


def _to_blob(data: bytes):
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _dpapi(func_name: str, data: bytes) -> bytes | None:
    if not IS_WINDOWS:
        return None
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        func = getattr(crypt32, func_name)

        blob_in, _keepalive = _to_blob(data)
        blob_out = _Blob()
        description = ctypes.c_wchar_p("webcam stream")

        if func_name == "CryptProtectData":
            ok = func(ctypes.byref(blob_in), description, None, None, None, 0,
                      ctypes.byref(blob_out))
        else:
            ok = func(ctypes.byref(blob_in), None, None, None, None, 0,
                      ctypes.byref(blob_out))
        if not ok:
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return None


def encrypt_password(password: str) -> dict:
    """Returns the password part of the settings, encrypted when Windows allows it."""
    if not password:
        return {"password": "", "password_enc": ""}
    blob = _dpapi("CryptProtectData", password.encode("utf-8"))
    if blob:
        return {"password": "", "password_enc": base64.b64encode(blob).decode("ascii")}
    return {"password": password, "password_enc": ""}


def decrypt_password(data: dict) -> str:
    blob64 = data.get("password_enc") or ""
    if blob64:
        try:
            plain = _dpapi("CryptUnprotectData", base64.b64decode(blob64))
        except Exception:
            plain = None
        if plain:
            return plain.decode("utf-8", "replace")
        return ""
    return data.get("password") or ""


def password_is_encrypted() -> bool:
    """Whether this system can encrypt the password (used for the UI hint)."""
    if not IS_WINDOWS:
        return False
    return _dpapi("CryptProtectData", b"test") is not None


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def settings_path() -> str:
    """Next to the program (portable). Falls back to the user profile if read-only."""
    local = os.path.join(app_dir(), "settings.json")
    try:
        with open(local, "a", encoding="utf-8"):
            pass
        return local
    except OSError:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        folder = os.path.join(base, "WebcamStream")
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, "settings.json")


def load() -> dict:
    data = dict(DEFAULTS)
    try:
        with open(settings_path(), "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        if isinstance(saved, dict):
            data.update({k: v for k, v in saved.items() if k in DEFAULTS or k.startswith("password")})
    except (OSError, ValueError):
        pass
    data["password"] = decrypt_password(data)
    data.pop("password_enc", None)
    return data


def save(values: dict, remember_password=True) -> str:
    payload = {k: values.get(k, DEFAULTS[k]) for k in DEFAULTS}
    if remember_password:
        payload.update(encrypt_password(values.get("password", "")))
    else:
        payload.update({"password": "", "password_enc": ""})

    path = settings_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path
