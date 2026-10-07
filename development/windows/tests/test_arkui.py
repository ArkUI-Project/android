import importlib.util
import contextlib
import io
import os
import shutil
import subprocess
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile


MODULE_PATH = Path(__file__).resolve().parents[1] / "arkui.py"
SPEC = importlib.util.spec_from_file_location("arkui", MODULE_PATH)
arkui = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(arkui)


class LocalRomWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root_patch = patch.object(arkui, "ROOT", self.root)
        self.state_patch = patch.object(arkui, "STATE", self.root / ".state")
        self.logs_patch = patch.object(arkui, "LOGS", self.root / "logs")
        self.root_patch.start()
        self.state_patch.start()
        self.logs_patch.start()
        self.cfg = {"manifest_url": "https://github.com/ArkUI-Project/android.git",
                    "branch": "lineage-23.2", "device": "sdk_phone_x86_64", "variant": "eng",
                    "avd_name": "ArkUI_Test", "emulator_cores": 4, "emulator_memory_mb": 4096,
                    "gpu": "auto", "source_dir": str(self.root / "source"), "sync_jobs": 4}

    def tearDown(self):
        self.logs_patch.stop()
        self.state_patch.stop()
        self.root_patch.stop()
        self.temporary.cleanup()

    def artifact(self):
        directory = self.root / "artifacts/test"
        image = directory / "image"
        image.mkdir(parents=True)
        for name in arkui.REQUIRED_IMAGES + ("vendor.img", "source.properties", "build.prop"):
            (image / name).write_bytes((name + " local-build fixture").encode())
        lock = directory / "source-lock.xml"
        lock.write_text("<manifest />")
        diff = directory / "source.diff"
        diff.write_text("")
        proof = {**self.cfg, "schema": 1, "local_build": True, "image_directory": "image",
                 "lineage_version": "23.2-local-test", "api_level": "36", "build_id": "test",
                 "image_zip_sha256": "test-archive", "source_lock_sha256": arkui.sha256(lock),
                 "source_diff_sha256": arkui.sha256(diff),
                 "files": {path.name: arkui.sha256(path) for path in image.iterdir()}}
        arkui.write_json(directory / "build.json", proof)
        arkui.write_json(self.root / ".state/current-artifact.json", {"path": "artifacts/test"})
        return directory, image, proof

    def test_missing_build_never_starts_emulator(self):
        with patch.object(arkui.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(RuntimeError, "No locally built ROM"):
                arkui.windows_start(self.cfg)
            launch.assert_not_called()

    def test_local_rom_starts_with_writable_system_without_wiping_data(self):
        _, image, _ = self.artifact()
        self.cfg.update({"sdk_dir": str(self.root / "sdk"), "emulator_port": 5554})
        with patch.object(arkui, "sdk_tool", return_value="emulator.exe"), \
             patch.object(arkui, "capture", return_value="0\nAcceleration available"), \
             patch.object(arkui.socket, "socket"), \
             patch.object(arkui.subprocess, "CREATE_NO_WINDOW", 0, create=True), \
             patch.object(arkui.subprocess, "Popen") as launch:
            launch.return_value.pid = 1234
            arkui.windows_start(self.cfg, headless=True, no_wait=True)
        arguments = launch.call_args.args[0]
        self.assertIn("-writable-system", arguments)
        self.assertIn("-no-snapshot", arguments)
        self.assertNotIn("-wipe-data", arguments)
        self.assertEqual(arguments[arguments.index("-system") + 1], str(image.resolve() / "system.img"))
        self.assertEqual(arguments[arguments.index("-vendor") + 1], str(image.resolve() / "vendor.img"))

    def bridge_fixture(self):
        self.cfg.update({"emulator_port": 5554, "boot_timeout_seconds": 600})
        saved = {"pid": 1234, "started_at": "test-start", "serial": "emulator-5554",
                 "avd_name": self.cfg["avd_name"], "lineage_version": "23.2-local-test"}
        bridge = {"vm_pid": 1234, "vm_started_at": "test-start", "serial": "emulator-5554",
                  "transport": "wsl-stdio", "host": "127.0.0.1", "port": 50123}
        arkui.write_json(arkui.STATE / "emulator.json", saved)
        arkui.write_json(arkui.STATE / "adb-bridge.json", bridge)
        return bridge

    def test_sync_uses_native_adb_and_private_bridge(self):
        self.bridge_fixture()
        executable = Path(self.cfg["source_dir"]) / "out/host/linux-x86/bin/adb"
        executable.parent.mkdir(parents=True)
        executable.touch()
        with patch.object(arkui, "linux_env", return_value={}), \
             patch.object(arkui.os, "access", return_value=True):
            env = arkui.linux_device_env(self.cfg)
        self.assertEqual(env["ARKUI_ADB"], str(executable))
        self.assertEqual(env["ANDROID_SERIAL"], "emulator-5554")
        self.assertEqual(env["ADB_SERVER_SOCKET"], "tcp:127.0.0.1:50123")
        self.assertNotIn("ANDROID_PRODUCT_OUT", env)

    def test_sync_refuses_a_stale_bridge(self):
        bridge = self.bridge_fixture()
        bridge["vm_started_at"] = "previous-vm"
        arkui.write_json(arkui.STATE / "adb-bridge.json", bridge)
        with self.assertRaisesRegex(RuntimeError, "missing or stale"):
            arkui.linux_device_env(self.cfg)

    def test_sync_refuses_public_or_unspecified_bridge_address(self):
        bridge = self.bridge_fixture()
        for host in ("0.0.0.0", "8.8.8.8", "172.17.224.1"):
            bridge["host"] = host
            arkui.write_json(arkui.STATE / "adb-bridge.json", bridge)
            with self.assertRaisesRegex(RuntimeError, "Invalid loopback"):
                arkui.linux_device_env(self.cfg)

    def test_bridge_listens_only_on_linux_loopback_and_uses_interop_pipes(self):
        self.bridge_fixture()
        with patch.object(arkui, "AdbRelay") as server, \
             patch.object(arkui.threading, "Thread"), \
             patch.object(arkui.threading, "Event") as event, \
             patch.object(arkui, "write_json") as write:
            event.return_value.is_set.return_value = True
            server.return_value.__enter__.return_value.server_address = ("127.0.0.1", 50123)
            arkui.linux_adb_bridge(self.cfg, "/mnt/c/python.exe", "D:\\arkui.py", 12345)
            server.assert_called_once_with(("127.0.0.1", 0), arkui.AdbRelayHandler)
            self.assertEqual(server.return_value.__enter__.return_value.tunnel_command,
                             ["/mnt/c/python.exe", "D:\\arkui.py", "--adb-tunnel"])
            self.assertEqual(write.call_args.args[1]["transport"], "wsl-stdio")

    def test_sync_refuses_legacy_tcp_bridge(self):
        bridge = self.bridge_fixture()
        bridge.pop("transport")
        arkui.write_json(arkui.STATE / "adb-bridge.json", bridge)
        with self.assertRaisesRegex(RuntimeError, "Invalid loopback"):
            arkui.linux_device_env(self.cfg)

    def test_windows_adb_sync_dispatches_to_linux(self):
        with patch.object(arkui, "config", return_value=self.cfg), \
             patch.object(arkui, "ensure_adb_bridge") as bridge, \
             patch.object(arkui, "worker") as worker, \
             patch.object(arkui.subprocess, "call") as windows_adb:
            arkui.main(["adb", "sync", "-l", "system"])
        bridge.assert_called_once_with(self.cfg)
        worker.assert_called_once_with("adb-sync", self.cfg, ["-l", "system"])
        windows_adb.assert_not_called()

    def test_real_sync_records_current_version_only_for_its_managed_vm(self):
        self.bridge_fixture()
        with patch.object(arkui, "linux_device_env", return_value={"ARKUI_ADB": "native-adb"}), \
             patch.object(arkui, "run"), \
             patch.object(arkui, "capture", side_effect=["23.2-next-local-build", "next-fingerprint"]):
            arkui.linux_worker("sync-device", self.cfg, ["system_ext"])
        saved = arkui.managed_vm(self.cfg)
        self.assertEqual(arkui.vm_version(saved), "23.2-next-local-build")
        proof = arkui.read_json(arkui.STATE / "device-sync-success.json")
        self.assertTrue(proof["boot_verified"])
        self.assertFalse(proof["exported_rom_updated"])
        saved["started_at"] = "next-vm-start"
        self.assertEqual(arkui.vm_version(saved), "23.2-local-test")

    def test_dry_run_does_not_record_a_transferred_rom(self):
        self.bridge_fixture()
        with patch.object(arkui, "linux_device_env", return_value={}), \
             patch.object(arkui, "run"), patch.object(arkui, "capture") as capture:
            arkui.linux_worker("sync-device", self.cfg, ["--dry-run"])
        capture.assert_not_called()
        self.assertFalse((arkui.STATE / "device-sync-success.json").exists())

    def test_image_tampering_is_rejected(self):
        _, image, _ = self.artifact()
        (image / "system.img").write_bytes(b"replaced image")
        with self.assertRaisesRegex(RuntimeError, "ROM file changed"):
            arkui.verify_artifact(self.cfg)

    def test_non_local_image_receipt_is_rejected(self):
        directory, _, proof = self.artifact()
        proof["local_build"] = False
        arkui.write_json(directory / "build.json", proof)
        with self.assertRaisesRegex(RuntimeError, "provenance"):
            arkui.verify_artifact(self.cfg)

    def test_valid_receipt_is_accepted(self):
        directory, image, _ = self.artifact()
        actual, actual_image, _ = arkui.verify_artifact(self.cfg)
        self.assertEqual(actual, directory.resolve())
        self.assertEqual(actual_image, image.resolve())

    def test_zip_path_traversal_is_rejected(self):
        archive = self.root / "unsafe.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("x86_64/system.img", b"fixture")
            output.writestr("x86_64/../../escape", b"unsafe")
        with self.assertRaisesRegex(RuntimeError, "Unsafe path"):
            arkui.extract_images(archive, self.root / "unpacked")
        self.assertFalse((self.root / "escape").exists())

    def test_wrong_abi_is_rejected(self):
        archive = self.root / "wrong-abi.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("arm64-v8a/system.img", b"fixture")
        with self.assertRaisesRegex(RuntimeError, "Wrong emulator ABI"):
            arkui.extract_images(archive, self.root / "unpacked")

    def test_upstream_zip_without_userdata_is_supported(self):
        archive = self.root / "local-rom.zip"
        with zipfile.ZipFile(archive, "w") as output:
            for name in ("system.img", "vendor.img", "ramdisk.img", "kernel-ranchu", "build.prop"):
                output.writestr("x86_64/" + name, b"local fixture")
            output.writestr("x86_64/source.properties", "AndroidVersion.ApiLevel=36\n"
                            "SystemImage.Abi=x86_64\nSystemImage.TagId=lineage\n")
            output.writestr("x86_64/data/local/tmp/.keep", b"")
        metadata, _ = arkui.extract_images(archive, self.root / "unpacked")
        self.assertEqual(metadata["AndroidVersion.ApiLevel"], "36")
        self.assertFalse((self.root / "unpacked/userdata.img").exists())

    def test_generic_sdk_tag_is_rejected(self):
        archive = self.root / "wrong-tag.zip"
        with zipfile.ZipFile(archive, "w") as output:
            for name in ("system.img", "vendor.img", "ramdisk.img", "kernel-ranchu", "build.prop"):
                output.writestr("x86_64/" + name, b"fixture")
            output.writestr("x86_64/source.properties", "AndroidVersion.ApiLevel=36\n"
                            "SystemImage.Abi=x86_64\nSystemImage.TagId=google_apis\n")
        with self.assertRaisesRegex(RuntimeError, "not a LineageOS"):
            arkui.extract_images(archive, self.root / "unpacked")

    def test_incremental_rom_keeps_compatible_avd_data(self):
        _, image, proof = self.artifact()
        directory = arkui.create_avd(self.cfg, image, proof)
        user_data = directory / "userdata-qemu.img"
        user_data.write_bytes(b"persistent user data")
        proof["image_zip_sha256"] = "next-build"
        proof["build_id"] = "next-build"
        arkui.create_avd(self.cfg, image, proof)
        self.assertEqual(user_data.read_bytes(), b"persistent user data")
        self.assertIn(str(image.as_posix()), (directory / "config.ini").read_text())

    def test_incompatible_avd_requires_explicit_reset(self):
        _, image, proof = self.artifact()
        arkui.create_avd(self.cfg, image, proof)
        proof["api_level"] = "37"
        with self.assertRaisesRegex(RuntimeError, "incompatible ROM"):
            arkui.create_avd(self.cfg, image, proof)

    @unittest.skipIf(os.name == "nt", "Linux process group behavior")
    def test_failed_command_terminates_network_children(self):
        program = "import subprocess, sys; subprocess.Popen([sys.executable, '-c', " \
                  "'import time; time.sleep(10)']); sys.exit(7)"
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "exit code 7"):
            arkui.run([sys.executable, "-c", program])
        self.assertLess(time.monotonic() - started, 5)

    def test_quiet_command_reports_that_it_is_still_running(self):
        console = io.StringIO()
        with contextlib.redirect_stdout(console):
            arkui.run([sys.executable, "-c", "import time; time.sleep(1.25)"],
                      log="quiet.log", heartbeat_seconds=0.1)
        self.assertIn("Still running", console.getvalue())
        self.assertIn("Still running", (self.root / "logs/quiet.log").read_text())

    def test_transient_sync_failure_recovers_and_records_success(self):
        attempts = []

        def simulate(command, **kwargs):
            if "sync" in command:
                attempts.append(command)
                if len(attempts) < 3:
                    raise RuntimeError("temporary network failure")
            if "manifest" in command:
                path = Path(command[-1])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("<manifest />")

        with patch.object(arkui, "linux_init"), patch.object(arkui, "linux_env", return_value={}), \
             patch.object(arkui, "repo_command", return_value="repo"), \
             patch.object(arkui, "configure_forks"), patch.object(arkui, "run", side_effect=simulate), \
             patch.object(arkui.time, "sleep"):
            arkui.linux_sync(self.cfg)
        self.assertEqual(len(attempts), 3)
        self.assertTrue((self.root / ".state/sync-success.json").is_file())
        self.assertNotIn("--force-sync", attempts[-1])

    def test_repeated_sync_failure_does_not_authorize_build(self):
        stamp = self.root / ".state/sync-success.json"
        arkui.write_json(stamp, {"old": "success"})
        with patch.object(arkui, "linux_init"), patch.object(arkui, "linux_env", return_value={}), \
             patch.object(arkui, "repo_command", return_value="repo"), \
             patch.object(arkui, "run", side_effect=RuntimeError("network failure")), \
             patch.object(arkui.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "network failure"):
                arkui.linux_sync(self.cfg)
        self.assertFalse(stamp.exists())


@unittest.skipIf(os.name == "nt", "Native Bash device synchronization")
class DeviceSyncScriptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "build").mkdir()
        (self.root / ".repo").mkdir()
        (self.root / "out/target/product/emu64x/system").mkdir(parents=True)
        fixtures = MODULE_PATH.parent / "tests/fixtures"
        shutil.copyfile(fixtures / "device-envsetup.sh", self.root / "build/envsetup.sh")
        self.adb = self.root / "adb"
        shutil.copyfile(fixtures / "fake-adb.sh", self.adb)
        self.adb.chmod(0o755)
        self.log = self.root / "adb.log"
        self.env = {**os.environ, "ARKUI_SOURCE": str(self.root), "ARKUI_DEVICE": "sdk_phone_x86_64",
                    "ARKUI_VARIANT": "eng", "ARKUI_ADB": str(self.adb), "ARKUI_BOOT_TIMEOUT": "2",
                    "ARKUI_EXPECTED_VERSION": "23.2-local-test", "ANDROID_SERIAL": "emulator-5554",
                    "TEST_ADB_LOG": str(self.log)}

    def tearDown(self):
        self.temporary.cleanup()

    def run_script(self, *args):
        return subprocess.run(["bash", str(MODULE_PATH.parent / "sync-device.sh"), *args],
                              env=self.env, capture_output=True, text=True, timeout=20)

    def test_dry_run_resolves_output_and_recovers_remount_reboot_exit(self):
        result = self.run_script("--dry-run", "system")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(str(self.root / "out/target/product/emu64x"), result.stdout)
        self.assertIn("No files transferred", result.stdout)
        commands = self.log.read_text().splitlines()
        self.assertIn("remount -R", commands)
        self.assertIn("remount", commands)
        self.assertIn("sync -l system", commands)
        self.assertNotIn("reboot", commands)

    def test_failed_final_remount_never_transfers_files(self):
        self.env["TEST_REMOUNT_FAIL"] = "1"
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("sync all", self.log.read_text())

    def test_wrong_rom_never_roots_or_syncs(self):
        self.env["TEST_WRONG_ROM"] = "1"
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("root", self.log.read_text())

    def test_invalid_partition_never_contacts_device(self):
        result = self.run_script("unknown-partition")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_reported_sync_error_is_failure_even_when_adb_returns_zero(self):
        self.env["TEST_SYNC_ERROR"] = "1"
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("reboot", self.log.read_text().splitlines())

    def test_real_sync_reboots_and_verifies_boot(self):
        result = self.run_script("system_ext")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Device sync complete; boot verified", result.stdout)
        self.assertIn("sync system_ext", self.log.read_text().splitlines())
        self.assertIn("reboot", self.log.read_text().splitlines())

    def test_old_boot_completed_property_is_not_a_verified_reboot(self):
        self.env["TEST_STUCK_BOOT"] = "1"
        result = self.run_script("system")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Device sync complete; boot verified", result.stdout)
        self.assertIn("boot timed out", result.stderr)


if __name__ == "__main__":
    unittest.main()
