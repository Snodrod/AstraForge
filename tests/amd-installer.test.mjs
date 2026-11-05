import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {resolve} from 'node:path';

test('AMD installer PowerShell unit tests', () => {
  const script = resolve('tests/amd-installer.test.ps1');
  const run = spawnSync('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script], {encoding: 'utf8'});
  assert.equal(run.status, 0, `${run.stdout}\n${run.stderr}`);
  assert.match(run.stdout, /AMD installer unit tests passed/);
});
