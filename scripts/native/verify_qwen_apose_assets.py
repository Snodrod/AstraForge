import hashlib
import json
from pathlib import Path


def verify_assets(manifest_path, runtime_root):
    manifest_path = Path(manifest_path)
    runtime_root = Path(runtime_root).resolve()
    if not runtime_root.is_absolute() or not (runtime_root / 'python_embeded/python.exe').is_file():
        raise RuntimeError('An absolute native runtime with python_embeded/python.exe is required.')
    records = []
    for asset in json.loads(manifest_path.read_text(encoding='utf8'))['assets']:
        target = (runtime_root / asset['path']).resolve()
        if runtime_root not in target.parents:
            raise RuntimeError(f'Asset escapes native runtime: {asset["path"]}')
        if not target.is_file() or target.stat().st_size != asset['bytes']:
            raise RuntimeError(f'Missing or wrong-size asset: {target}')
        digest = hashlib.sha256()
        with target.open('rb') as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
                digest.update(block)
        if digest.hexdigest() != asset['sha256']:
            raise RuntimeError(f'SHA-256 mismatch: {target}')
        records.append({'path': str(target), 'bytes': asset['bytes'], 'sha256': asset['sha256']})
    return records


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime-root', required=True)
    parser.add_argument('--manifest', default=str(Path(__file__).with_name('qwen-apose-assets.json')))
    args = parser.parse_args()
    print(json.dumps({'assets': verify_assets(args.manifest, args.runtime_root)}, indent=2))
