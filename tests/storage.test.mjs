import test from 'node:test';
import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { writeJson } from '../server/storage.mjs';

test('writeJson retries transient Windows rename sharing errors', async (t) => {
  const directory = await fs.mkdtemp(path.join(process.cwd(), 'data', 'runtime', 'storage-test-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const file = path.join(directory, 'job.json');
  const realRename = fs.rename.bind(fs);
  let attempts = 0;
  t.mock.method(fs, 'rename', async (temp, destination) => {
    attempts += 1;
    if (attempts < 3) throw Object.assign(new Error('transient sharing violation'), { code: 'EPERM' });
    return realRename(temp, destination);
  });

  await writeJson(file, { status: 'running' });

  assert.equal(attempts, 3);
  assert.deepEqual(JSON.parse(await fs.readFile(file, 'utf8')), { status: 'running' });
  assert.deepEqual(await fs.readdir(directory), ['job.json']);
});
