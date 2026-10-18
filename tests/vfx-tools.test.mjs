// "VFX" tab: catalog, resolveInside, tool state, web generator static files, ready textures, launch.
// An isolated express on a random port and a temporary root; no processes are started (spawn is stubbed), no studio assets are written.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import http from 'node:http';
import net from 'node:net';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, existsSync, utimesSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import express from 'express';
import { registerVfxTools, resolveInside, validateCatalog, loadCatalog, isTextureName, findInstalled, findExe, defaultSearchRoots } from '../server/vfx-tools.mjs';

const PNG = Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), Buffer.alloc(24, 7)]);
const root = mkdtempSync(path.join(tmpdir(), 'af-vfx-root-'));
const programs = mkdtempSync(path.join(tmpdir(), 'af-vfx-programs-'));
const catalog = loadCatalog();
const calls = [];
const saved = [];
let server;
let base;
let webBase;   // web generators server: a separate port, not the studio origin
let svc;

const put = (rel, body = 'x') => { const file = path.join(root, rel); mkdirSync(path.dirname(file), { recursive: true }); writeFileSync(file, body); return file; };
const fakeSpawn = (exe, args, opts) => {
  calls.push({ exe, args, opts });
  const child = new EventEmitter();
  child.unref = () => { child.unrefCalled = true; };
  process.nextTick(() => child.emit('spawn'));
  return child;
};
const post = (url, body) => fetch(base + url, { method: 'POST', headers: { 'x-astra-client': 'studio', ...(body ? { 'Content-Type': 'application/json' } : {}) }, body: body && JSON.stringify(body) });
const tool = async (id) => (await (await fetch(base + '/api/vfx-tools')).json()).tools.find((t) => t.id === id);

before(async () => {
  put('apps/material-maker/material_maker.exe');
  put('apps/pseudo-volume/Pseudo/UnityCrashHandler64.exe');
  put('apps/pseudo-volume/Pseudo/Volume Generator.exe');
  put('apps/material-maker-files/impact_decal.ptex', '{}');
  put('downloads/PixaFlux.20260528.exe');
  put('web/texture-generator/index.html', '<!doctype html><title>tg</title>');
  put('web/texture-generator/js/app.js', 'console.log(1)');
  mkdirSync(path.join(root, 'web/normalmap'), { recursive: true });   // the folder exists, index.html does not
  put('textures/old.png', PNG);
  put('textures/new.png', PNG);
  put('textures/fake.webp', 'not an image');
  put('textures/notes.txt', 'not a texture');
  const past = new Date(Date.now() - 3600_000);
  utimesSync(path.join(root, 'textures/old.png'), past, past);
  put('secret.txt', 'outside the web folder');
  mkdirSync(path.join(programs, 'PixaFlux', 'bin'), { recursive: true });
  writeFileSync(path.join(programs, 'PixaFlux', 'bin', 'PixaFlux.exe'), 'x');
  mkdirSync(path.join(programs, 'Other'), { recursive: true });
  writeFileSync(path.join(programs, 'Other', 'Slate.exe'), 'x');   // a folder without the "Slate" hint: must not be found

  const app = express();
  app.use(express.json());
  const asyncRoute = (fn) => (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);
  svc = registerVfxTools(app, asyncRoute, { root, webPort: 0, spawn: fakeSpawn, searchRoots: [programs], saveAsset: async (buffer, ext, name) => { const info = { id: 'asset-' + (saved.length + 1), extension: ext, name, bytes: buffer.length }; saved.push(info); return info; } });
  app.use((err, req, res, next) => res.status(err.status || 400).json({ error: err.message }));
  await new Promise((resolve) => { server = app.listen(0, '127.0.0.1', resolve); });
  base = 'http://127.0.0.1:' + server.address().port;
  await svc.startWeb();
  webBase = 'http://127.0.0.1:' + svc.webPort;
});

after(async () => {
  await svc.close();
  await new Promise((resolve) => server.close(resolve));
  rmSync(root, { recursive: true, force: true });
  rmSync(programs, { recursive: true, force: true });
});

test('catalog: unique ids, kinds from kinds, links and relative local paths', () => {
  assert.deepEqual(validateCatalog(catalog), []);
  const ids = catalog.tools.map((t) => t.id);
  assert.equal(new Set(ids).size, ids.length);
  assert.ok(catalog.tools.length >= 30);
  for (const t of catalog.tools) {
    assert.ok(Object.hasOwn(catalog.kinds, t.kind), t.id);
    assert.match(t.url, /^https?:\/\//, t.id);
    for (const rel of [t.local?.exe, t.local?.installer, t.local?.web, t.local?.file, ...(t.local?.extras || [])].filter(Boolean)) {
      assert.ok(!path.isAbsolute(rel) && !rel.split(/[\\/]/).includes('..'), `${t.id}: ${rel}`);
    }
  }
});

test('validateCatalog finds defects', () => {
  const bad = validateCatalog({ kinds: { desktop: 'x' }, tools: [
    { id: 'a', name: 'A', kind: 'desktop', url: 'https://a', tags: [], ru: 'x', local: { exe: '../evil.exe' } },
    { id: 'a', name: 'B', kind: 'nope', url: 'ftp://b', tags: [], ru: 'x' },
  ] });
  assert.ok(bad.some((p) => /relative/.test(p)));
  assert.ok(bad.some((p) => /duplicate/.test(p)));
  assert.ok(bad.some((p) => /is not described in kinds/.test(p)));
  assert.ok(bad.some((p) => /url/.test(p)));
});

test('resolveInside: a path inside root passes, ".." and absolute paths are rejected', () => {
  assert.equal(resolveInside(root, 'apps/x/y.exe'), path.join(root, 'apps', 'x', 'y.exe'));
  assert.equal(resolveInside(root, 'apps/pseudo-volume/*.exe'), path.join(root, 'apps', 'pseudo-volume', '*.exe'));
  assert.equal(resolveInside(root, 'a/../b'), path.join(root, 'b'));
  for (const bad of ['../x', 'a/../../x', '..\\x', '..', path.resolve(root, '..', 'x'), 'C:\\Windows\\notepad.exe', '/etc/passwd', '', 'a\0b']) {
    assert.throws(() => resolveInside(root, bad), Error, JSON.stringify(bad));
  }
});

test('isTextureName: basename only and PNG/JPG/WEBP', () => {
  for (const ok of ['a.png', 'Smoke 01.PNG', 'noise_(2).jpeg', 'x-y.webp']) assert.ok(isTextureName(ok), ok);
  for (const bad of ['../a.png', '..\\a.png', 'a/b.png', 'a.txt', 'a.png.exe', '.png', '.hidden.png', 'a\0.png', '', null, undefined, 'x'.repeat(200) + '.png']) assert.ok(!isTextureName(bad), String(bad));
});

test('GET /api/vfx-tools: state from the actual files, download is not returned', async () => {
  const res = await fetch(base + '/api/vfx-tools');
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.root, path.resolve(root));
  assert.equal(body.rootExists, true);
  assert.deepEqual(Object.keys(body.kinds), Object.keys(catalog.kinds));
  assert.equal(body.tools.length, catalog.tools.length);
  assert.ok(body.tools.every((t) => !('download' in t)));
  const by = Object.fromEntries(body.tools.map((t) => [t.id, t]));

  assert.equal(by['material-maker'].available, true);
  assert.equal(by['material-maker'].launchable, true);
  assert.equal(by['flowmap-painter'].available, false);
  assert.equal(by['flowmap-painter'].launchable, false);
  assert.equal(by['pseudo-volume'].available, true);          // "*.exe" did not pick UnityCrashHandler

  assert.equal(by['pixaflux'].installerAvailable, true);
  assert.equal(by['pixaflux'].installed, true);                // found in programs/PixaFlux/bin
  assert.equal(by['pixaflux'].launchable, true);
  assert.equal(by['kumoworks'].installerAvailable, false);
  assert.equal(by['kumoworks'].installed, false);
  assert.equal(by['kumoworks'].available, false);
  assert.equal(by['slate'].installed, false);                  // Slate.exe sits in a folder without the hint in its name

  assert.equal(by['texture-generator-online'].available, true);
  assert.ok(svc.webPort > 0 && svc.webPort !== server.address().port);
  assert.equal(body.webError, null);
  assert.equal(by['texture-generator-online'].webUrl, `http://127.0.0.1:${svc.webPort}/vfx-web/texture-generator-online/`);
  assert.equal(by['material-maker'].webUrl, null);
  assert.equal(by['normalmap-online'].available, false);       // the folder has no index.html
  assert.equal(by['displacementx'].available, false);

  assert.equal(by['impact-decal'].available, true);
  assert.equal(by['impact-decal'].launchable, true);

  for (const t of body.tools.filter((x) => ['web-online', 'store', 'paid', 'guide'].includes(x.kind))) {
    assert.equal(t.available, false, t.id);
    assert.equal(t.launchable, false, t.id);
  }
});

test('GET /api/vfx-tools: without a root rootExists=false and nothing is available', async () => {
  const app = express();
  const empty = registerVfxTools(app, (fn) => (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next), { root: path.join(root, 'no such folder'), webPort: 0, searchRoots: [] });
  const s = await new Promise((resolve) => { const x = app.listen(0, '127.0.0.1', () => resolve(x)); });
  try {
    const body = await (await fetch(`http://127.0.0.1:${s.address().port}/api/vfx-tools`)).json();
    assert.equal(body.rootExists, false);
    assert.ok(body.tools.every((t) => !t.available));
  } finally { await empty.close(); await new Promise((resolve) => s.close(resolve)); }
});

test('findExe and findInstalled', async () => {
  assert.match(await findExe(root, 'apps/pseudo-volume/*.exe'), /Volume Generator\.exe$/);
  assert.equal(await findExe(root, 'apps/nope/*.exe'), null);
  assert.equal(await findExe(root, 'apps/material-maker/absent.exe'), null);
  await assert.rejects(findExe(root, '../outside/*.exe'));
  assert.match(await findInstalled({ hints: ['pixaflux'], exe: 'pixaflux.exe' }, [programs]), /PixaFlux\.exe$/);
  assert.equal(await findInstalled({ hints: ['Slate'], exe: 'Slate.exe' }, [programs]), null);
  assert.equal(await findInstalled({ hints: ['Slate'], exe: 'Slate.exe' }, [path.join(programs, 'no')]), null);
  assert.ok(defaultSearchRoots({ ProgramFiles: 'C:\\P', LOCALAPPDATA: 'C:\\L' }).some((p) => /Programs$/.test(p)));
});

test('web generators live on a separate port: /vfx-web/:id serves files, everything else returns 404', async () => {
  const page = await fetch(webBase + '/vfx-web/texture-generator-online/index.html');
  assert.equal(page.status, 200);
  assert.equal(page.headers.get('x-content-type-options'), 'nosniff');
  assert.match(await page.text(), /<title>tg<\/title>/);
  assert.equal((await fetch(webBase + '/vfx-web/texture-generator-online/')).status, 200);
  assert.equal((await fetch(webBase + '/vfx-web/texture-generator-online/js/app.js')).status, 200);
  const missing = await fetch(webBase + '/vfx-web/texture-generator-online/missing.js');
  assert.equal(missing.status, 404);
  assert.equal(missing.headers.get('x-content-type-options'), 'nosniff');
  assert.equal((await fetch(webBase + '/vfx-web/unknown/index.html')).status, 404);
  assert.equal((await fetch(webBase + '/vfx-web/material-maker/index.html')).status, 404);   // the id exists, but it is not web-local
  assert.equal((await fetch(webBase + '/vfx-web/impact-decal/apps')).status, 404);
  const escape = await fetch(webBase + '/vfx-web/texture-generator-online/..%2F..%2Fsecret.txt');
  assert.notEqual(escape.status, 200);
  assert.doesNotMatch(await escape.text(), /outside/);
  // nothing but /vfx-web/:id is served: no studio API, no root, no write
  for (const url of ['/', '/api/vfx-tools', '/api/vfx-tools/textures', '/secret.txt', '/vfx-web', '/vfx-web/']) assert.equal((await fetch(webBase + url)).status, 404, url);
  assert.equal((await fetch(webBase + '/vfx-web/texture-generator-online/index.html', { method: 'POST' })).status, 404);
});

test('the main studio server no longer serves generator pages', async () => {
  for (const url of ['/vfx-web/texture-generator-online/index.html', '/vfx-web/texture-generator-online/', '/vfx-web/texture-generator-online/js/app.js']) {
    const res = await fetch(base + url);
    assert.equal(res.status, 404, url);
    assert.doesNotMatch(await res.text(), /<title>tg<\/title>|console\.log\(1\)/);
  }
});

test('the web generators server checks Host', async () => {
  const get = (host) => new Promise((resolve, reject) => {
    const req = http.get({ host: '127.0.0.1', port: svc.webPort, path: '/vfx-web/texture-generator-online/index.html', headers: { Host: host } }, (res) => { res.resume(); res.on('end', () => resolve(res.statusCode)); });
    req.on('error', reject);
  });
  assert.equal(await get('127.0.0.1:' + svc.webPort), 200);
  assert.equal(await get('localhost:' + svc.webPort), 200);
  assert.equal(await get('evil.example'), 403);
  assert.equal(await get('evil.example:' + svc.webPort), 403);
});

test('a busy web generators port does not crash the studio: webUrl = null and webError', async (t) => {
  const warn = t.mock.method(console, 'warn', () => {});
  const blocker = net.createServer();
  await new Promise((resolve) => blocker.listen(0, '127.0.0.1', resolve));
  const busyPort = blocker.address().port;
  const app = express();
  const busy = registerVfxTools(app, (fn) => (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next), { root, webPort: busyPort, searchRoots: [] });
  const s = await new Promise((resolve) => { const x = app.listen(0, '127.0.0.1', () => resolve(x)); });
  const url = `http://127.0.0.1:${s.address().port}/api/vfx-tools`;
  try {
    const body = await (await fetch(url)).json();
    assert.match(body.webError, /is used/);
    const tg = body.tools.find((x) => x.id === 'texture-generator-online');
    assert.equal(tg.available, true);
    assert.equal(tg.webUrl, null);
    assert.match(tg.webError, /is used/);
    assert.equal(body.tools.find((x) => x.id === 'material-maker').available, true);   // the rest works
    assert.equal(warn.mock.callCount(), 1);
    // the port was freed: "Refresh" (?fresh=1) starts the server
    await new Promise((resolve) => blocker.close(resolve));
    const again = await (await fetch(url + '?fresh=1')).json();
    assert.equal(again.webError, null);
    assert.equal(again.tools.find((x) => x.id === 'texture-generator-online').webUrl, `http://127.0.0.1:${busyPort}/vfx-web/texture-generator-online/`);
    assert.equal((await fetch(again.tools.find((x) => x.id === 'texture-generator-online').webUrl + 'index.html')).status, 200);
  } finally {
    await busy.close();
    await new Promise((resolve) => s.close(resolve));
    if (blocker.listening) await new Promise((resolve) => blocker.close(resolve));
  }
});

test('ready textures: list, file, a name with ".." is rejected', async () => {
  const list = await (await fetch(base + '/api/vfx-tools/textures')).json();
  assert.deepEqual(list.map((x) => x.name).sort(), ['fake.webp', 'new.png', 'old.png']);
  assert.ok(!list.some((x) => x.name === 'notes.txt'));
  assert.ok(list.findIndex((x) => x.name === 'old.png') > list.findIndex((x) => x.name === 'new.png'), 'newest first');
  assert.equal(list.find((x) => x.name === 'new.png').size, PNG.length);
  assert.match(list[0].url, /^\/api\/vfx-tools\/textures\//);
  assert.ok(!Number.isNaN(Date.parse(list[0].mtime)));

  const file = await fetch(base + list.find((x) => x.name === 'new.png').url);
  assert.equal(file.status, 200);
  assert.equal(file.headers.get('content-type'), 'image/png');
  assert.deepEqual(Buffer.from(await file.arrayBuffer()), PNG);

  for (const bad of ['..%2Fa.png', '..%5Csecret.png', '%2E%2E%2Fsecret.txt', 'notes.txt']) {
    const res = await fetch(base + '/api/vfx-tools/textures/' + bad);
    assert.equal(res.status, 400, bad);
    assert.match((await res.json()).error, /Invalid file name/);
  }
  assert.equal((await fetch(base + '/api/vfx-tools/textures/absent.png')).status, 404);
  const imp = await post('/api/vfx-tools/textures/..%2Fa.png/import');
  assert.equal(imp.status, 400);
  assert.equal(saved.length, 0);
});

test('texture import: format check and saving through saveAsset', async () => {
  const bad = await post('/api/vfx-tools/textures/fake.webp/import');
  assert.equal(bad.status, 400);
  assert.match((await bad.json()).error, /its format/);
  assert.equal(saved.length, 0);
  const ok = await post('/api/vfx-tools/textures/new.png/import');
  assert.equal(ok.status, 200);
  const asset = await ok.json();
  assert.equal(asset.name, 'new.png');
  assert.equal(asset.extension, '.png');
  assert.equal(asset.bytes, PNG.length);
  assert.equal(saved.length, 1);
});

test('launch: desktop, an installed program, a template in Material Maker; errors in English', async () => {
  calls.length = 0;
  assert.equal((await post('/api/vfx-tools/material-maker/launch')).status, 200);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].exe, path.join(root, 'apps', 'material-maker', 'material_maker.exe'));
  assert.deepEqual(calls[0].args, []);
  assert.equal(calls[0].opts.cwd, path.join(root, 'apps', 'material-maker'));
  assert.equal(calls[0].opts.detached, true);
  assert.equal(calls[0].opts.stdio, 'ignore');

  calls.length = 0;
  assert.equal((await post('/api/vfx-tools/pseudo-volume/launch')).status, 200);
  assert.match(calls[0].exe, /Volume Generator\.exe$/);

  calls.length = 0;
  assert.equal((await post('/api/vfx-tools/pixaflux/launch')).status, 200);
  assert.equal(calls[0].exe, path.join(programs, 'PixaFlux', 'bin', 'PixaFlux.exe'));

  calls.length = 0;
  assert.equal((await post('/api/vfx-tools/impact-decal/launch')).status, 200);
  assert.equal(calls[0].exe, path.join(root, 'apps', 'material-maker', 'material_maker.exe'));
  assert.deepEqual(calls[0].args, [path.join(root, 'apps', 'material-maker-files', 'impact_decal.ptex')]);

  calls.length = 0;
  const missing = await post('/api/vfx-tools/flowmap-painter/launch');
  assert.equal(missing.status, 400);
  assert.match((await missing.json()).error, /is not downloaded/);
  const notInstalled = await post('/api/vfx-tools/slate/launch');
  assert.equal(notInstalled.status, 400);
  assert.match((await notInstalled.json()).error, /is not installed/);
  const online = await post('/api/vfx-tools/tgen/launch');
  assert.equal(online.status, 400);
  assert.equal((await post('/api/vfx-tools/no-such-tool/launch')).status, 404);
  assert.equal((await post('/api/vfx-tools/..%2F..%2Fx/launch')).status, 404);
  assert.equal(calls.length, 0);
});

test('install and folders: from the catalog only, explorer.exe with the tool folder', async () => {
  calls.length = 0;
  assert.equal((await post('/api/vfx-tools/pixaflux/install')).status, 200);
  assert.equal(calls[0].exe, path.join(root, 'downloads', 'PixaFlux.20260528.exe'));

  calls.length = 0;
  const noInstaller = await post('/api/vfx-tools/kumoworks/install');
  assert.equal(noInstaller.status, 400);
  assert.match((await noInstaller.json()).error, /is not downloaded/);
  assert.equal((await post('/api/vfx-tools/material-maker/install')).status, 400);   // not an installer
  assert.equal(calls.length, 0);

  assert.equal((await post('/api/vfx-tools/material-maker/folder')).status, 200);
  assert.deepEqual([calls[0].exe, calls[0].args], ['explorer.exe', [path.join(root, 'apps', 'material-maker')]]);
  assert.equal((await post('/api/vfx-tools/texture-generator-online/folder')).status, 200);
  assert.deepEqual(calls[1].args, [path.join(root, 'web', 'texture-generator')]);
  assert.equal((await post('/api/vfx-tools/flowmap-painter/folder')).status, 400);
  assert.equal((await post('/api/vfx-tools/tgen/folder')).status, 400);

  calls.length = 0;
  rmSync(path.join(root, 'textures'), { recursive: true, force: true });
  assert.equal((await post('/api/vfx-tools/folder')).status, 200);
  assert.deepEqual([calls[0].exe, calls[0].args], ['explorer.exe', [path.resolve(root)]]);
  assert.ok(existsSync(path.join(root, 'textures')), 'textures/ created');
  assert.equal((await post('/api/vfx-tools/folder', { where: 'textures' })).status, 200);
  assert.deepEqual(calls[1].args, [path.join(root, 'textures')]);
  assert.equal(svc.catalog.tools.length, catalog.tools.length);
});
