import importlib.util
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent

def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

verify = load("verify_pixal3d_amd")
install = load("install_pixal3d_amd")

class Props:
    def __init__(self, value=None):
        if value is not None:
            self.gcnArchName = value

class NativeProbeTests(unittest.TestCase):
    def test_architecture_comes_from_hip_properties(self):
        self.assertEqual(verify.gpu_architecture(Props("gfx1201:sramecc+:xnack-")), "gfx1201")
        self.assertEqual(verify.gpu_architecture(Props()), "unknown")

    def test_readiness_requires_every_mandatory_probe(self):
        checks = [{"name": "hip_import", "status": "pass"}, {"name": "sdpa", "status": "fail"}]
        self.assertFalse(verify.native_ready(checks, ["hip_import", "sdpa"]))
        checks[1]["status"] = "pass"
        self.assertTrue(verify.native_ready(checks, ["hip_import", "sdpa"]))
        self.assertFalse(verify.native_ready(checks, ["hip_import", "memory_cleanup"]))

class NativePatchContractTests(unittest.TestCase):
    def test_active_custom_node_matches_source_and_has_amd_shape_branches(self):
        native_root = HERE.parents[1] / "runtime" / "ComfyUI-Pixal3D-AMD-Native"
        source_root = native_root / "src" / "ComfyUI-Trellis2"
        active_root = native_root / "ComfyUI" / "custom_nodes" / "ComfyUI-Trellis2"
        patched_files = [
            "nodes.py",
            "moge/model/utils.py",
            "projection/texture_projection_multiview.py",
            "trellis2/modules/sparse/attention/windowed_attn.py",
            "trellis2/modules/sparse/config.py",
            "trellis2/pipelines/trellis2_image_to_3d.py",
            "trellis2/pipelines/trellis2_texturing.py",
            "trellis2/representations/mesh/base.py",
        ]
        for relative in patched_files:
            with self.subTest(file=relative):
                source = (source_root / relative).read_text(encoding="utf-8")
                active = (active_root / relative).read_text(encoding="utf-8")
                self.assertEqual(source.replace("\r\n", "\n"), active.replace("\r\n", "\n"))
        nodes = (source_root / "nodes.py").read_text(encoding="utf-8")
        pipeline = (source_root / "trellis2/pipelines/trellis2_image_to_3d.py").read_text(encoding="utf-8")
        self.assertIn("sparse_conv_config.FLEX_GEMM_ALGO = 'explicit_gemm'", nodes)
        self.assertIn("decoder.blocks.float()", pipeline)
        self.assertIn("elif not bake_on_vertices:", pipeline)
        self.assertIn("face_idx, bary, mask = _rasterize_uv_barycentric", pipeline)
        self.assertIn("image_cond_model.naf_tile_factor", pipeline)
        texture_pipeline = pipeline.split("def texture_mesh_pixal3d", 1)[1]
        multiview_branch = texture_pipeline.split("if views is not None:", 1)[1].split("else:", 1)[0]
        self.assertIn(
            "image_cond_model.naf_tile_factor = int(os.environ.get('ASTRA_AMD_NAF_TILE', '2'))",
            multiview_branch,
        )

    def test_active_cumesh_matches_source_and_uses_explicit_vertex_indices(self):
        native_root = HERE.parents[1] / "runtime" / "ComfyUI-Pixal3D-AMD-Native"
        source = (native_root / "src" / "CuMesh" / "cumesh" / "remeshing.py").read_text(encoding="utf-8")
        active = (native_root / "python_embeded" / "Lib" / "site-packages" / "cumesh" / "remeshing.py").read_text(encoding="utf-8")
        self.assertEqual(source.replace("\r\n", "\n"), active.replace("\r\n", "\n"))
        self.assertIn("active_vertices = torch.unique(quad_indices)", source)
        self.assertIn("used_vertices = torch.unique(quad_indices)", source)
        self.assertNotIn("vert_map[active] = torch.arange(", source)
        self.assertNotIn("new_vert_idx[used_verts] = torch.arange(used_verts.sum(),", source)


class NativeInstallTests(unittest.TestCase):
    def test_runtime_paths_fail_closed(self):
        install.validate_runtime_root(r"C:\AstraForge\runtime\ComfyUI-Pixal3D-AMD-Native")
        for value in (r"relative\px", "/mnt/c/px", r"C:\AstraForge\ComfyUI-Pixal3D-AMD", r"C:\AstraForge\ComfyUI-Pixal3D"):
            with self.assertRaises(ValueError, msg=value):
                install.validate_runtime_root(value)

    def test_wheel_set_is_complete_and_amd_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in install.REQUIRED_WHEELS:
                (root / name).write_bytes(b"wheel")
            wheels = install.find_wheel_set(root)
            self.assertEqual(len(wheels), 6)
            self.assertTrue(all("cu128" not in item.name.lower() for item in wheels))
            (root / install.REQUIRED_WHEELS[0]).unlink()
            with self.assertRaises(FileNotFoundError):
                install.find_wheel_set(root)

    def test_rocm_meta_package_is_required(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(FileNotFoundError):
                install.find_meta_package(root)
            meta = root / install.ROCM_META_PACKAGE
            meta.write_bytes(b"source")
            self.assertEqual(install.find_meta_package(root), meta)

if __name__ == "__main__":
    unittest.main()
