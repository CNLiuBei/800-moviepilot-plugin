import json
import unittest
from pathlib import Path


class PluginVersionTests(unittest.TestCase):
    def test_plugin_version_matches_package_manifest(self):
        plugin_root = Path(__file__).resolve().parents[1]
        package_path = plugin_root.parents[1] / "package.v2.json"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        package_version = package["CloudUploader"]["version"]

        init_text = (plugin_root / "__init__.py").read_text(encoding="utf-8")
        marker = 'plugin_version = "'
        start = init_text.index(marker) + len(marker)
        end = init_text.index('"', start)
        plugin_version = init_text[start:end]

        self.assertEqual(package_version, plugin_version)


if __name__ == "__main__":
    unittest.main()
