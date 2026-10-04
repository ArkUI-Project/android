import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "arkui_graphics_test", Path(__file__).resolve().parents[1] / "arkui.py")
arkui = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(arkui)


class GraphicsTransportTests(unittest.TestCase):
    def test_new_and_existing_avds_use_shared_memory_without_changing_data(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(arkui, "ROOT", Path(temporary)):
            root = Path(temporary)
            cfg = {"avd_name": "ArkUI_Test", "emulator_cores": 6,
                   "emulator_memory_mb": 12288, "gpu": "host"}
            proof = {"api_level": "36", "branch": "lineage-23.2",
                     "device": "sdk_phone_x86_64", "variant": "eng",
                     "image_zip_sha256": "local-test", "build_id": "test"}
            image = root / "local-rom"
            directory = arkui.create_avd(cfg, image, proof)
            profile = directory / "config.ini"
            self.assertIn("hw.gltransport=asg\n", profile.read_text())
            self.assertIn("hw.gpu.mode=host\n", profile.read_text())

            userdata = directory / "userdata-qemu.img.qcow2"
            userdata.write_bytes(b"existing user data")
            profile.write_text(profile.read_text().replace(
                "hw.gltransport=asg", "hw.gltransport=pipe"))
            arkui.create_avd(cfg, image, proof)

            self.assertIn("hw.gltransport=asg\n", profile.read_text())
            self.assertEqual(userdata.read_bytes(), b"existing user data")
            self.assertEqual(arkui.read_json(directory / "arkui-build.json")
                             ["image_zip_sha256"], "local-test")


if __name__ == "__main__":
    unittest.main()
