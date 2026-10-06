"""Export and import collector credentials encrypted for the current Windows user."""

from base64 import b64decode, b64encode
import ctypes
from ctypes import wintypes
import json
import os


FORMAT = "eh-collector-credentials"
VERSION = 1
ENTROPY = b"local-tag-catalog:collector-credentials:v1"
COOKIE_NAMES = ("ipb_member_id", "ipb_pass_hash", "igneous")


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _input_blob(data):
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _dpapi(data, protect):
    if os.name != "nt":
        raise ValueError("加密登录文件只支持 Windows。")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    source, source_buffer = _input_blob(data)
    entropy, entropy_buffer = _input_blob(ENTROPY)
    output = DATA_BLOB()
    if protect:
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        success = crypt32.CryptProtectData(
            ctypes.byref(source), "藏目 ExHentai 登录信息", ctypes.byref(entropy),
            None, None, 0x1, ctypes.byref(output),
        )
    else:
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.POINTER(DATA_BLOB),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        success = crypt32.CryptUnprotectData(
            ctypes.byref(source), None, ctypes.byref(entropy),
            None, None, 0x1, ctypes.byref(output),
        )
    # Keep the input buffers alive through the native call.
    _ = source_buffer, entropy_buffer
    if not success:
        raise OSError(ctypes.get_last_error(), "Windows DPAPI operation failed")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(output.pbData)


def _cookies(cookie_header):
    result = {}
    for part in str(cookie_header or "").split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name in COOKIE_NAMES:
            result[name] = value
    return result


def export_credentials(settings):
    if not isinstance(settings, dict):
        raise ValueError("请先应用一次登录设置，再导出登录文件。")
    values = _cookies(settings.get("cookies"))
    if not values:
        raise ValueError("当前设置中没有可导出的登录信息。")
    payload = {
        "host": settings.get("host", "exhentai.org"),
        "proxy": settings.get("proxy", ""),
        **{name: values.get(name, "") for name in COOKIE_NAMES},
    }
    plaintext = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    protected = _dpapi(plaintext, True)
    envelope = {"format": FORMAT, "version": VERSION, "protected": b64encode(protected).decode("ascii")}
    return (json.dumps(envelope, separators=(",", ":")) + "\n").encode("ascii")


def import_credentials(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= 32768:
        raise ValueError("登录文件大小不正确。")
    try:
        envelope = json.loads(raw.decode("ascii"))
        if not isinstance(envelope, dict) or envelope.get("format") != FORMAT or envelope.get("version") != VERSION:
            raise ValueError
        protected = b64decode(envelope.get("protected", ""), validate=True)
        if not 1 <= len(protected) <= 16384:
            raise ValueError
        plaintext = _dpapi(protected, False)
        if len(plaintext) > 8192:
            raise ValueError
        payload = json.loads(plaintext.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        raise ValueError("无法解密登录文件。请确认文件由当前 Windows 用户在这台电脑上导出，且没有损坏。") from None
    if not isinstance(payload, dict) or set(payload) != {"host", "proxy", *COOKIE_NAMES}:
        raise ValueError("登录文件内容不完整或版本不受支持。")
    if any(not isinstance(value, str) or len(value) > 2048 for value in payload.values()):
        raise ValueError("登录文件内容不正确。")
    return payload
