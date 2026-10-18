// UI dictionaries (src/i18n): same keys in en and ru, same placeholders, no hard-coded Cyrillic in src/.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import en from '../src/i18n/en.js';
import ru from '../src/i18n/ru.js';
import { t, tOr, hasT, getLang, setLang } from '../src/i18n/index.js';

const root = fileURLToPath(new URL('../', import.meta.url));
const placeholders = (v) => {
  const text = typeof v === 'object' ? Object.values(v).join(' ') : String(v);
  return [...new Set([...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]))].sort();
};

test('i18n: en and ru have identical key sets', () => {
  const a = Object.keys(en).sort();
  const b = Object.keys(ru).sort();
  assert.deepEqual(a.filter((k) => !(k in ru)), [], 'keys missing in ru.js');
  assert.deepEqual(b.filter((k) => !(k in en)), [], 'keys missing in en.js');
  assert.ok(a.length > 100, `only ${a.length} keys`);
});

test('i18n: entries are non-empty strings or plural objects with the same placeholders', () => {
  for (const [key, v] of Object.entries(en)) {
    for (const [name, dict] of [['en', en], ['ru', ru]]) {
      const e = dict[key];
      if (typeof e === 'object') {
        assert.ok(e && typeof e.other === 'string' && e.other, `${name}:${key} plural needs "other"`);
      } else {
        assert.equal(typeof e, 'string', `${name}:${key}`);
        assert.ok(e.length > 0 || v === '', `${name}:${key} is empty`);
      }
    }
    assert.deepEqual(placeholders(ru[key]), placeholders(v), `placeholders differ: ${key}`);
  }
});

test('i18n: English entries contain no Cyrillic', () => {
  for (const [key, v] of Object.entries(en)) {
    const text = typeof v === 'object' ? Object.values(v).join(' ') : v;
    assert.doesNotMatch(text, /[Ѐ-ӿ]/, key);
  }
});

test('i18n: t() interpolates, falls back to English, then to the key; plurals; tOr', () => {
  const before = getLang();
  try {
    setLang('en');
    assert.equal(t('lang.ru'), 'Russian');
    assert.equal(t('no.such.key'), 'no.such.key');
    assert.equal(tOr('no.such.key', 'Server label'), 'Server label');
    assert.equal(hasT('lang.ru'), true);
    setLang('ru');
    assert.equal(getLang(), 'ru');
    assert.equal(t('lang.ru'), 'Русский');
    setLang('xx');
    assert.equal(getLang(), 'ru', 'unknown languages are ignored');
  } finally {
    setLang(before);
  }
});

// Files that may contain Cyrillic, with the reason. Keep this list short.
const CYRILLIC_WHITELIST = new Map([
  ['src/i18n/ru.js', 'the Russian dictionary'],
]);

function walk(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full, out);
    else if (/\.(jsx?|mjs)$/.test(entry.name)) out.push(full);
  }
  return out;
}

test('i18n: no .js/.jsx file under src/ contains Cyrillic outside the Russian dictionary', () => {
  const offenders = [];
  for (const file of walk(path.join(root, 'src'))) {
    const rel = path.relative(root, file).split(path.sep).join('/');
    if (CYRILLIC_WHITELIST.has(rel)) continue;
    const lines = fs.readFileSync(file, 'utf8').split('\n');
    lines.forEach((line, i) => {
      if (/[Ѐ-ӿ]/.test(line)) offenders.push(`${rel}:${i + 1}: ${line.trim().slice(0, 80)}`);
    });
  }
  assert.deepEqual(offenders, [], `Cyrillic outside src/i18n/ru.js:\n${offenders.slice(0, 40).join('\n')}`);
});
