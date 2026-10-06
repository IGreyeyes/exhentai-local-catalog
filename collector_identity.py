"""Stable anonymous collector identities and portable identity files."""

import json
from uuid import UUID

FORMAT = "local-tag-catalog-collector-identity"
VERSION = 1
MAX_IDENTITY_BYTES = 16 * 1024
MAX_NAME_LENGTH = 40


def validate_collector(collector_id, collector_name, allow_unknown=False):
    if not isinstance(collector_id, str) or not isinstance(collector_name, str):
        raise ValueError("采集者 ID 和昵称必须是文本。")
    if collector_id == "":
        if not allow_unknown or collector_name:
            raise ValueError("采集者 ID 不能为空；未知采集者的昵称也须为空。")
    else:
        try:
            identifier = UUID(collector_id)
        except ValueError:
            raise ValueError("采集者 ID 必须是有效的 UUID。") from None
        if str(identifier) != collector_id or identifier.version != 4:
            raise ValueError("采集者 ID 必须是标准的小写 UUID v4。")
    if len(collector_name) > MAX_NAME_LENGTH or collector_name != collector_name.strip():
        raise ValueError("昵称最多 40 个字符，首尾不能包含空白。")
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF for char in collector_name):
        raise ValueError("昵称不能包含换行或控制字符。")
    return {"collector_id": collector_id, "collector_name": collector_name}


def read_identity(db):
    values = dict(db.execute("SELECT key,value FROM app_settings WHERE key IN ('collector_id','collector_name')"))
    return validate_collector(values.get("collector_id", ""), values.get("collector_name", ""))


def export_identity(identity):
    identity = validate_collector(**identity)
    return (json.dumps({"format": FORMAT, "version": VERSION, **identity}, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("采集身份文件包含重复字段。")
        result[key] = value
    return result


def parse_identity(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_IDENTITY_BYTES:
        raise ValueError("请选择非空且不超过 16 KiB 的采集身份文件。")
    try:
        payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_keys)
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("采集身份文件不是有效的 UTF-8 JSON，或包含重复字段。") from None
    if (not isinstance(payload, dict)
            or set(payload) != {"format", "version", "collector_id", "collector_name"}
            or payload["format"] != FORMAT or type(payload["version"]) is not int
            or payload["version"] != VERSION):
        raise ValueError("采集身份文件格式或版本不受支持，请使用「导出我的采集身份」生成的文件。")
    return validate_collector(payload["collector_id"], payload["collector_name"])
