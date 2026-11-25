import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / 'installer' / 'mvadapter' / 'patch' / 'astra_views.py'
NATIVE = ROOT / 'runtime' / 'ComfyUI-Pixal3D-AMD-Native' / 'ComfyUI' / 'custom_nodes' / 'ComfyUI-MVAdapter' / 'astra_views.py'


class MVAdapterLightTest(unittest.TestCase):
    def test_cleanup_does_not_move_the_whole_pipeline_back_to_cpu(self):
        tree = ast.parse(PATCH.read_text(encoding='utf-8'))
        run = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == 'run')
        finally_blocks = [handler for handler in run.body if isinstance(handler, ast.Try) for handler in [handler.finalbody] if handler]
        calls = [node for block in finally_blocks for statement in block for node in ast.walk(statement)]
        self.assertFalse(any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'to'
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == 'pipe'
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == 'cpu'
            for node in calls
        ), 'Cleanup must release GPU tensors directly instead of copying the whole pipeline to system RAM.')

    def test_native_node_matches_the_tracked_lightweight_patch(self):
        self.assertEqual(NATIVE.read_text(encoding='utf-8'), PATCH.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
