"""Fail closed on private source files, local paths, credentials or unsafe packages."""

from pathlib import Path
import argparse
import hashlib
import ipaddress
import json
import re
import subprocess
import tempfile
import zipfile

from desktop_updates import unpack_package

ROOT = Path(__file__).resolve().parent
PRIVATE_PARTS = {"data", "logs", "backups", "dist", "build", ".venv", ".venv-desktop", "__pycache__", "node_modules"}
PRIVATE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".zstd", ".ehcred", ".ehfavorites.json", ".ehcollector.json", ".pem", ".key", ".pfx", ".p12", ".log")
SECRET = re.compile(rb"(?<![A-Za-z0-9_-])(?:github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{30,}|AKIA[A-Z0-9]{16}|sk-proj-[A-Za-z0-9_-]{40,}|sk-[A-Za-z0-9]{48}(?![A-Za-z0-9_-])|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")


def scan_blob(body, label, text=False):
    personal = str(Path.home())
    forms = {personal, personal.replace("\\", "/"), personal.replace("\\", "\\\\")}
    if any(value.encode(encoding).lower() in body.lower() for value in forms for encoding in ("utf-8", "utf-16-le")):
        raise ValueError(f"Private build-machine path found in {label}; the value is omitted.")
    if SECRET.search(body):
        raise ValueError(f"Possible credential found in {label}; the value is omitted.")
    if text:
        content = body.decode("utf-8", errors="replace")
        for match in IPV4.finditer(content):
            if content[max(0,match.start()-7):match.start()] == "Chrome/":
                continue
            try:
                address = ipaddress.ip_address(match.group())
            except ValueError:
                continue
            if not address.is_loopback and not address.is_unspecified:
                raise ValueError(f"Non-loopback IP literal found in {label}; the value is omitted.")


def verify_source():
    result = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT, capture_output=True, check=True)
    files = [name for name in result.stdout.decode("utf-8").split("\0") if name]
    for name in files:
        path = Path(name)
        lower = name.lower()
        if (set(part.lower() for part in path.parts) & PRIVATE_PARTS or lower.endswith(PRIVATE_SUFFIXES)
                or path.name.lower().startswith((".env", "cookie", "tokens")) and path.name.lower() not in {"cookies.py"}):
            raise ValueError(f"Private or generated file would be uploaded: {name}")
        if (ROOT / path).is_file():
            scan_blob((ROOT / path).read_bytes(), name, text=path.suffix.lower() in {".py", ".js", ".cjs", ".html", ".md", ".ps1", ".cmd", ".json"})
    return len(files)


def verify_package(package):
    from PyInstaller.archive.readers import CArchiveReader
    count = 0
    with tempfile.TemporaryDirectory(prefix="excatalog-release-check-") as directory:
        unpacked = unpack_package(package, Path(directory) / "program")
        for path in unpacked.rglob("*"):
            if not path.is_file():
                continue
            name = path.relative_to(unpacked).as_posix()
            scan_blob(path.read_bytes(), name)
            count += 1
            if path.suffix.lower() == ".zip":
                with zipfile.ZipFile(path) as nested:
                    for member in nested.infolist():
                        if not member.is_dir():
                            scan_blob(nested.read(member), f"{name}:{member.filename}")
        embedded = CArchiveReader(str(unpacked / "ExCatalog.exe"))
        module_count = 0
        for name, entry in embedded.toc.items():
            if entry[-1] == "z":
                modules = embedded.open_embedded_archive(name)
                for module in modules.toc:
                    data = modules.extract(module, raw=True)
                    if data:
                        scan_blob(data, "compiled module " + module)
                        module_count += 1
                if "client_version" not in modules.toc or "desktop_updates" not in modules.toc:
                    raise ValueError("Packaged client is missing the update modules.")
            else:
                scan_blob(embedded.extract(name), "executable entry " + name)
        checksum = hashlib.sha256(Path(package).read_bytes()).hexdigest()
    return {"package": Path(package).name, "sha256": checksum, "files_checked": count, "compiled_modules_checked": module_count}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path)
    options = parser.parse_args()
    result = {"source_files_checked": verify_source(), "passed": True}
    if options.package:
        result.update(verify_package(options.package))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
