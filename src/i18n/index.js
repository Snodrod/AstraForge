// Tiny i18n layer of the studio UI (no dependency). Dictionaries are flat {key: string}
// objects in ./en.js and ./ru.js; English is the default and the fallback.
//
//   t('app.save')                       -> "Save"
//   t('forge.jobs.count', {n: 3})        -> "3 jobs" ({name} placeholders are interpolated)
//   t(key, vars) with a plural entry     -> {one, few, many, other} picked by vars.n (or vars.count)
//   tOr(key, fallback, vars)             -> dictionary entry when it exists, else `fallback` as is
//                                           (used for strings that come from the server registry)
//   hasT(key)                            -> is there an entry for the key
//
// Plain modules (forge-core.js, fx-core.js, ...) call t() at call time, never at import time, so
// a language switch takes effect on the next render. React components use useT() (or useLang()),
// which re-renders them when the language changes.
import { useSyncExternalStore } from 'react';
import en from './en.js';
import ru from './ru.js';

export const LANGS = [
  { id: 'en', label: 'EN' },
  { id: 'ru', label: 'RU' },
];
const DICTS = { en, ru };
const STORAGE_KEY = 'astra.lang';
const DEFAULT_LANG = 'en';

function stored() {
  try {
    const v = globalThis.window?.localStorage?.getItem(STORAGE_KEY);
    return DICTS[v] ? v : null;
  } catch {
    return null;
  }
}

let lang = stored() || DEFAULT_LANG;
const listeners = new Set();

function applyHtmlLang() {
  try {
    if (globalThis.document?.documentElement) globalThis.document.documentElement.lang = lang;
  } catch { /* no DOM */ }
}
applyHtmlLang();

export function getLang() {
  return lang;
}

/** Switch the UI language; remembered in localStorage, mirrored to <html lang>. */
export function setLang(next) {
  if (!DICTS[next] || next === lang) return;
  lang = next;
  try {
    globalThis.window?.localStorage?.setItem(STORAGE_KEY, next);
  } catch { /* private mode / blocked storage */ }
  applyHtmlLang();
  for (const fn of listeners) fn(lang);
}

export function subscribeLang(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

const pluralRules = {};
function pluralForm(entry, vars) {
  const n = Number(vars?.n ?? vars?.count ?? 0);
  const rules = (pluralRules[lang] ||= new Intl.PluralRules(lang));
  return entry[rules.select(n)] ?? entry.other ?? entry.many ?? entry.one ?? '';
}

function interpolate(text, vars) {
  if (!vars) return text;
  return text.replace(/\{(\w+)\}/g, (m, name) => (vars[name] === undefined || vars[name] === null ? m : String(vars[name])));
}

function lookup(key) {
  const own = DICTS[lang][key];
  if (own !== undefined) return own;
  return DICTS[DEFAULT_LANG][key];
}

function render(entry, vars) {
  if (entry && typeof entry === 'object') entry = pluralForm(entry, vars);
  return interpolate(String(entry), vars);
}

/** Translate a key; missing keys come back as the key itself so they are easy to spot. */
export function t(key, vars) {
  const entry = lookup(key);
  return entry === undefined ? key : render(entry, vars);
}

/** Dictionary entry when there is one, else the fallback (e.g. a label sent by the server). */
export function tOr(key, fallback, vars) {
  const entry = lookup(key);
  return entry === undefined ? fallback : render(entry, vars);
}

export function hasT(key) {
  return lookup(key) !== undefined;
}

/** Current language as React state: the component re-renders on a switch. */
export function useLang() {
  return useSyncExternalStore(subscribeLang, getLang, getLang);
}

/** `const t = useT();` in a component: same t(), plus a re-render on a language switch. */
export function useT() {
  useLang();
  return t;
}
