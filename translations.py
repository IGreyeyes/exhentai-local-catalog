"""Names from EhTagTranslation/Database, keyed by canonical English tags.

Upstream text: CC BY-NC-SA 3.0 CN, all EhTagTranslation contributors.
See THIRD_PARTY_NOTICES.md. This module does not render upstream HTML or images.
"""

from pathlib import Path
import json
import sys

DEFAULT_TRANSLATIONS = Path(__file__).resolve().parent / "data" / "tag-translations.json"
SOURCE = "https://github.com/EhTagTranslation/Database"
LICENSE = "CC BY-NC-SA 3.0 CN"
SEARCH_ALIASES = {
    "language:chinese": ("中文", "汉语", "汉化", "简体中文", "繁体中文"),
    "language:english": ("英文", "英语"),
    "language:japanese": ("日文", "日语"),
    "language:korean": ("韩文", "韩语"),
    "other:anthology": ("合集", "合志", "选集"),
}


def parse_translation_data(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("译文文件格式不正确：缺少 data 列表。")
    names, namespaces = {}, {}
    for group in payload["data"]:
        if not isinstance(group, dict) or not isinstance(group.get("namespace"), str) or not isinstance(group.get("data"), dict):
            raise ValueError("译文文件中存在无效的命名空间。")
        namespace = group["namespace"]
        for key, entry in group["data"].items():
            name = entry.get("name") if isinstance(entry, dict) else None
            if not isinstance(name, str) or not name.strip():
                continue
            if namespace == "rows":
                namespaces[key] = name.strip()
            else:
                names[f"{namespace}:{key}"] = name.strip()
    if not names:
        raise ValueError("译文文件没有有效的标签名称。")
    return names, namespaces


class Translations:
    def __init__(self, path, available_tags):
        self.names, self.namespaces = {}, {}
        self.metadata = {}
        self.available = available_tags
        if path and Path(path).is_file():
            try:
                payload = json.loads(Path(path).read_text(encoding="utf-8"))
                self.names, self.namespaces = parse_translation_data(payload)
                head = payload.get("head", {})
                self.metadata = {
                    "format_version": payload.get("version"),
                    "revision": head.get("sha", ""),
                    "updated_at": head.get("committer", {}).get("when", ""),
                }
            except (OSError, ValueError, TypeError, AttributeError) as error:
                self.names, self.namespaces = {}, {}
                print(f"Translation dictionary unavailable: {error}", file=sys.stderr)
        self.catalog_names = {tag: name for tag, name in self.names.items() if tag in self.available}
        self.namespace_keys = {value.casefold(): key for key, value in self.namespaces.items()}
        self.records = []
        self.exact = {}
        for tag, label in self.catalog_names.items():
            namespace, original = tag.split(":", 1)
            terms = tuple(dict.fromkeys((label.casefold(), original.casefold(), *(alias.casefold() for alias in SEARCH_ALIASES.get(tag, ())))))
            self.records.append((tag, namespace, terms))
            for term in terms:
                self.exact.setdefault(term, []).append(tag)

    def status(self):
        return {
            "available": bool(self.names), "entry_count": len(self.names),
            "matched_tag_count": len(self.catalog_names), "source": SOURCE,
            "license": LICENSE, **self.metadata,
        }

    def describe(self, tag):
        namespace = tag.split(":", 1)[0]
        name = self.names.get(tag, "")
        return {"name": name, "namespace_name": self.namespaces.get(namespace, namespace), "translated": bool(name)}

    def labels(self, tags):
        return {tag: self.describe(tag) for tag in dict.fromkeys(tags)}

    def split_query(self, value):
        value = value.strip().casefold().replace("：", ":")
        namespace = ""
        if ":" in value:
            namespace, value = value.split(":", 1)
            namespace = self.namespace_keys.get(namespace, namespace)
        return namespace, value

    def resolve(self, value):
        if value in self.available:
            return value
        namespace, term = self.split_query(value)
        candidates = list(dict.fromkeys(tag for tag in self.exact.get(term, []) if not namespace or tag.startswith(namespace + ":")))
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise ValueError(f"“{value}”对应多个标签，请从建议中选择具体分类：{'、'.join(candidates[:5])}")
        return value

    def matches(self, query, limit=12):
        namespace, term = self.split_query(query)
        if not term:
            return []
        found = []
        for tag, ns, terms in self.records:
            if namespace and ns != namespace:
                continue
            if term in terms:
                rank = 0
            elif any(text.startswith(term) for text in terms):
                rank = 1
            elif any(term in text for text in terms):
                rank = 2
            else:
                continue
            found.append((rank, tag))
        found.sort()
        return [tag for _, tag in found[:limit]]
