"""Narrow checks for staging over multiple DAXQO base APK versions."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_native.py"
spec = importlib.util.spec_from_file_location("collector_prepare_native", SCRIPT)
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class NativeVersionCodeTests(unittest.TestCase):
    def test_reads_legacy_and_next_daxqo_integer_codes(self):
        self.assertEqual(prepare.parse_base_version_code("defaultConfig {\n        versionCode 5\n}"), 5)
        self.assertEqual(prepare.parse_base_version_code("defaultConfig {\n        versionCode 11\n}"), 11)
        for base in (5, 11):
            staged = prepare.collector_version_gradle(f"defaultConfig {{\n        versionCode {base}\n}}", 12)
            self.assertIn(f"versionCode project.hasProperty('trajectoryBuild') ? 12 : {base}", staged)

    def test_rejects_expression_duplicate_and_non_increasing_collector_codes(self):
        for gradle in (
            "versionCode project.hasProperty('x') ? 12 : 5",
            "versionCode 5\nversionCode 11",
        ):
            with self.subTest(gradle=gradle), self.assertRaises(ValueError):
                prepare.parse_base_version_code(gradle)
        for code in (5, 11, 2_100_000_001):
            with self.subTest(code=code), self.assertRaises(ValueError):
                prepare.validate_collector_version_code(code, 11)
        prepare.validate_collector_version_code(12, 11)

    def test_omitted_code_preserves_legacy_default_and_advances_new_base(self):
        self.assertEqual(prepare.select_collector_version_code(5), 8)
        self.assertEqual(prepare.select_collector_version_code(8), 9)
        self.assertEqual(prepare.select_collector_version_code(11), 12)
        self.assertEqual(prepare.select_collector_version_code(11, 13), 13)
        with self.assertRaises(ValueError):
            prepare.select_collector_version_code(11, 11)


if __name__ == "__main__":
    unittest.main()
