"""Readiness contract tests for native TRELLIS.2 extension probes."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from probe_trellis2_extensions import MANDATORY_SHAPE_CHECKS, shape_ready


class ExtensionReadinessTests(unittest.TestCase):
    def test_shape_generation_requires_every_mandatory_operation(self):
        checks = [{"name": name, "status": "pass"} for name in MANDATORY_SHAPE_CHECKS]
        self.assertTrue(shape_ready(checks))
        for missing in MANDATORY_SHAPE_CHECKS:
            with self.subTest(missing=missing):
                reduced = [item for item in checks if item["name"] != missing]
                self.assertFalse(shape_ready(reduced))

    def test_failed_or_unsupported_mandatory_operation_fails_closed(self):
        for status in ("fail", "unsupported"):
            with self.subTest(status=status):
                checks = [{"name": name, "status": "pass"} for name in MANDATORY_SHAPE_CHECKS]
                checks[0]["status"] = status
                self.assertFalse(shape_ready(checks))

    def test_optional_nvidia_components_do_not_unlock_shape_generation(self):
        self.assertFalse(shape_ready([
            {"name": "natten", "status": "pass"},
            {"name": "nvdiffrast", "status": "pass"},
        ]))


if __name__ == "__main__":
    unittest.main()
