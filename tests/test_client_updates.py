from pathlib import Path
from unittest.mock import patch
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile

import desktop_updates as updates
from client_version import APP_VERSION, PACKAGE_NAME, REPOSITORY


def archive_bytes(extra=()):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("ExCatalog/ExCatalog.exe", b"new-exe")
        archive.writestr("ExCatalog/_internal/python314.dll", b"new-runtime")
        archive.writestr("ExCatalog/README.md", "public instructions")
        for name, value in extra:
            archive.writestr(name, value)
    return output.getvalue()


class ClientUpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.updater = updates.ClientUpdater(self.root)

    def tearDown(self):
        if self.updater.workspace:
            import shutil
            shutil.rmtree(self.updater.workspace, ignore_errors=True)
        self.directory.cleanup()

    def release(self, raw=b"test"):
        return {"tag_name": "v1.2.0", "body": "更新内容", "assets": [{"name": PACKAGE_NAME, "size": len(raw),
            "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "browser_download_url": f"https://github.com/{REPOSITORY}/releases/download/v1.2.0/{PACKAGE_NAME}"}]}

    def test_release_selection_and_version_comparison(self):
        result = updates.release_info(self.release())
        self.assertTrue(result["available"])
        self.assertEqual(result["latest_version"], "1.2.0")
        self.assertFalse(updates.release_info({"tag_name": "v1.0.0"})["available"])
        self.assertFalse(updates.release_info({"tag_name": APP_VERSION})["available"])

    def test_reject_missing_checksum_and_wrong_repository(self):
        for key, value in (("digest", ""), ("size", True), ("browser_download_url", "https://github.com/other/repo/releases/download/v1.2.0/package.zip")):
            document = self.release()
            document["assets"][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                updates.release_info(document)
        for version in ("v1.2.0-beta", "v1.2.0/../evil", None):
            with self.assertRaises(ValueError):
                updates.version_tuple(version)

    def test_read_notes_persists_per_version_across_instances(self):
        self.assertTrue(self.updater.info()["show_notes"])
        self.updater.acknowledge()
        self.assertFalse(updates.ClientUpdater(self.root).info()["show_notes"])
        with patch("desktop_updates.APP_VERSION", "1.2.0"):
            self.assertTrue(self.updater.info()["show_notes"])
        (self.root / "data" / "client-update-state.json").write_text("broken", encoding="utf-8")
        self.assertTrue(self.updater.info()["show_notes"])

    def unpack(self, raw):
        archive = self.root / "package.zip"
        archive.write_bytes(raw)
        return updates.unpack_package(archive, self.root / "payload")

    def test_valid_package_and_private_files_are_rejected_before_extraction(self):
        for name in ("ExCatalog/data/favorites.sqlite3", "ExCatalog/../secret", "ExCatalog/_internal/../../data/secret",
                     "ExCatalog/_internal/C:secret", "ExCatalog/_internal/CON.txt"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.unpack(archive_bytes([(name, b"bad")]))
            self.assertFalse((self.root / "payload").exists())
        payload = self.unpack(archive_bytes())
        self.assertEqual((payload / "ExCatalog.exe").read_bytes(), b"new-exe")

    def test_raw_backslash_path_rejected(self):
        raw = archive_bytes([("ExCatalog/_internal/a-secret", b"bad")]).replace(b"a-secret", b"a\\secret")
        with self.assertRaises(ValueError):
            self.unpack(raw)

    def test_symlink_and_duplicate_members_rejected(self):
        symlink = zipfile.ZipInfo("ExCatalog/_internal/link")
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        for extras in ([(symlink, "../../data")], [("ExCatalog/EXCATALOG.EXE", b"duplicate")]):
            with self.assertRaises(ValueError):
                self.unpack(archive_bytes(extras))

    def installed_fixture(self):
        target = self.root / "installed"
        (target / "_internal").mkdir(parents=True)
        (target / "_internal" / "python314.dll").write_bytes(b"old-runtime")
        (target / "ExCatalog.exe").write_bytes(b"old-exe")
        (target / "data").mkdir()
        (target / "data" / "favorites.sqlite3").write_bytes(b"private-records")
        (target / "README.md").write_bytes(b"old-readme")
        return target

    def test_install_preserves_personal_files_and_saves_old_program(self):
        target = self.installed_fixture()
        payload = self.unpack(archive_bytes())
        backup = self.root / "previous-program"
        updates.replace_program(payload, target, backup)
        self.assertEqual((target / "data" / "favorites.sqlite3").read_bytes(), b"private-records")
        self.assertEqual((target / "ExCatalog.exe").read_bytes(), b"new-exe")
        self.assertEqual((backup / "ExCatalog.exe").read_bytes(), b"old-exe")

    def test_partial_install_rolls_back_all_program_files(self):
        target = self.installed_fixture()
        payload = self.unpack(archive_bytes())
        with patch("desktop_updates.shutil.copytree", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                updates.replace_program(payload, target, self.root / "previous-program")
        self.assertEqual((target / "ExCatalog.exe").read_bytes(), b"old-exe")
        self.assertEqual((target / "_internal" / "python314.dll").read_bytes(), b"old-runtime")
        self.assertEqual((target / "README.md").read_bytes(), b"old-readme")
        self.assertEqual((target / "data" / "favorites.sqlite3").read_bytes(), b"private-records")

    def download(self, raw, checksum=None):
        program = self.installed_fixture()
        self.updater.release = updates.release_info(self.release(raw))
        if checksum:
            self.updater.release["sha256"] = checksum
        with patch.object(self.updater.opener, "open", return_value=io.BytesIO(raw)), patch("sys.frozen", True, create=True), patch("sys.executable", str(program / "ExCatalog.exe")):
            self.updater.prepare()
            self.assertTrue(self.updater.lock.acquire(timeout=5))
            self.updater.lock.release()
        return self.updater.state

    def test_checksum_failure_leaves_original_program_untouched(self):
        state = self.download(archive_bytes(), "0" * 64)
        self.assertEqual(state["phase"], "error")
        self.assertIn("SHA-256", state["message"])
        self.assertIsNone(self.updater.workspace)
        self.assertEqual((self.root / "installed" / "ExCatalog.exe").read_bytes(), b"old-exe")

    def test_download_prepares_trusted_helper_without_modifying_installation(self):
        self.assertEqual(self.download(archive_bytes())["phase"], "ready")
        self.assertEqual((self.updater.workspace / "helper" / "ExCatalog.exe").read_bytes(), b"old-exe")
        self.assertEqual((self.updater.workspace / "payload" / "ExCatalog.exe").read_bytes(), b"new-exe")
        self.updater.state["phase"] = "installing"
        with patch("desktop_updates.subprocess.Popen") as launch:
            self.updater.launch_installer()
        self.assertEqual(launch.call_args.args[0][1],"--apply-client-update")
        self.assertEqual(launch.call_args.kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"],"1")

    @unittest.skipUnless(os.name == "nt", "Windows update helper")
    def test_native_helper_waits_for_exited_parent_and_installs(self):
        target = self.installed_fixture()
        workspace = self.root / "excatalog-update-test"
        workspace.mkdir()
        archive = workspace / "package.zip"
        archive.write_bytes(archive_bytes())
        updates.unpack_package(archive,workspace / "payload")
        parent = subprocess.Popen([sys.executable,"-c","pass"],creationflags=subprocess.CREATE_NO_WINDOW)
        parent.wait(timeout=5)
        plan = workspace / "plan.json"
        plan.write_text(json.dumps({"parent_pid":parent.pid,"target":str(target),"data_root":str(target)}),encoding="utf-8")
        self.assertEqual(updates.apply_update(plan,relaunch=False),0)
        self.assertEqual((target / "ExCatalog.exe").read_bytes(),b"new-exe")
        self.assertEqual((target / "data" / "favorites.sqlite3").read_bytes(),b"private-records")


if __name__ == "__main__":
    unittest.main()
