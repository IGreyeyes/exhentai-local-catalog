"""Download and validate the official plain-text EhTagTranslation dictionary."""

from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
import argparse
import gzip
import hashlib
import json
import os
import shutil
import tempfile

from translations import DEFAULT_TRANSLATIONS, parse_translation_data

DEFAULT_URL = "https://github.com/EhTagTranslation/Database/releases/latest/download/db.text.json.gz"


def update(url=DEFAULT_URL, expected_sha256=None, destination=DEFAULT_TRANSLATIONS):
    print("Downloading official Tag translations...", flush=True)
    request = Request(url, headers={"User-Agent": "LocalTagCatalog/1"})
    with urlopen(request, timeout=40) as response:
        compressed = response.read(32 * 1024 * 1024 + 1)
    if len(compressed) > 32 * 1024 * 1024:
        raise ValueError("Translation download is larger than expected.")
    digest = hashlib.sha256(compressed).hexdigest()
    if expected_sha256 and digest != expected_sha256.lower():
        raise ValueError("Translation archive checksum mismatch.")
    raw = gzip.decompress(compressed)
    if len(raw) > 64 * 1024 * 1024:
        raise ValueError("Translation JSON is larger than expected.")
    payload = json.loads(raw)
    names, namespaces = parse_translation_data(payload)
    destination = Path(destination)
    destination.parent.mkdir(exist_ok=True, parents=True)
    descriptor, temporary = tempfile.mkstemp(prefix="translation-", suffix=".partial", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
        if destination.exists():
            shutil.copy2(destination, destination.with_suffix(".previous.json"))
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    report = {
        "source_url": url, "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "archive_sha256": digest, "entry_count": len(names), "namespace_count": len(namespaces),
        "revision": payload.get("head", {}).get("sha"),
    }
    destination.with_name("tag-translations-info.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    print("Ready. Restart the local service to load the updated dictionary.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--sha256")
    options = parser.parse_args()
    update(options.url, options.sha256)
