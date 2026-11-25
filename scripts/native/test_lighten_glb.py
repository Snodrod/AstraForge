import importlib.util
import tempfile
import unittest
from pathlib import Path

import trimesh


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("lighten_glb", HERE / "lighten_glb.py")
lighten_glb = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lighten_glb)


class LightenGlbTests(unittest.TestCase):
    def test_decimates_geometry_and_writes_a_valid_glb(self):
        source = trimesh.creation.icosphere(subdivisions=3)
        with tempfile.TemporaryDirectory() as folder:
            input_path = Path(folder) / "input.glb"
            output_path = Path(folder) / "light.glb"
            source.export(input_path)

            report = lighten_glb.lighten(input_path, output_path, target_faces=120)

            result = trimesh.load(output_path, force="mesh", process=False)
            self.assertLessEqual(len(result.faces), 120)
            self.assertLess(len(result.faces), len(source.faces))
            self.assertEqual(report["sourceTriangles"], len(source.faces))
            self.assertEqual(report["triangles"], len(result.faces))
            self.assertEqual(report["targetTriangles"], 120)

    def test_rejects_a_target_below_three_faces(self):
        with self.assertRaisesRegex(ValueError, "target_faces"):
            lighten_glb.lighten("unused.glb", "unused-output.glb", target_faces=2)


if __name__ == "__main__":
    unittest.main()
