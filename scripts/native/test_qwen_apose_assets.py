import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.native.verify_qwen_apose_assets import verify_assets


class QwenAssetsTest(unittest.TestCase):
    def test_manifest_has_exact_q4_assets(self):
        manifest = json.loads(Path('scripts/native/qwen-apose-assets.json').read_text())
        self.assertEqual([a['bytes'] for a in manifest['assets']], [13065746976, 9384670680, 253806246])
        self.assertTrue(all(a['sha256'] for a in manifest['assets']))

    def test_verifier_rejects_wrong_content_and_accepts_exact_file(self):
        asset = {'path': 'models/vae/test.bin', 'bytes': 3, 'sha256': hashlib.sha256(b'abc').hexdigest()}
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / 'python_embeded').mkdir()
            (runtime / 'python_embeded/python.exe').write_bytes(b'')
            manifest = runtime / 'manifest.json'
            manifest.write_text(json.dumps({'assets': [asset]}))
            target = runtime / asset['path']
            target.parent.mkdir(parents=True)
            target.write_bytes(b'abc')
            self.assertEqual(verify_assets(manifest, runtime)[0]['bytes'], 3)
            target.write_bytes(b'abd')
            with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
                verify_assets(manifest, runtime)


if __name__ == '__main__':
    unittest.main()
