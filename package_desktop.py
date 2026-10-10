"""Create a portable ZIP with canonical paths and only public program files."""
from pathlib import Path
import argparse
import os
import zipfile

from client_version import PACKAGE_NAME
from desktop_updates import PROGRAM_FILES


def main():
    project = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--program-directory", type=Path, default=project / "dist" / "ExCatalog")
    directory = parser.parse_args().program_directory.resolve()
    package = project / "dist" / PACKAGE_NAME
    temporary = package.with_suffix(".partial")
    if any(path.name not in PROGRAM_FILES for path in directory.iterdir()):
        raise ValueError("Release directory contains unknown files. Do not package user data.")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(directory.rglob("*")):
                if path.is_symlink():
                    raise ValueError("Release directory contains a linked file.")
                if path.is_file():
                    archive.write(path, "ExCatalog/" + path.relative_to(directory).as_posix())
        os.replace(temporary, package)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
