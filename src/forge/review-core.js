// Pure helpers of the review view (src/forge/ForgeReview.jsx). Tests: tests/forge-review.test.mjs.
import { t } from '../i18n/index.js';

export const DECISION_TITLES = {
  get approved() { return t('forge.review.decision.approved'); },
  get rework() { return t('forge.review.decision.rework'); },
  get rejected() { return t('forge.review.decision.rejected'); },
};

export const ROLE_TITLES = {
  get caster() { return t('forge.review.role.caster'); },
  get ranged() { return t('forge.review.role.ranged'); },
  get melee_shield() { return t('forge.review.role.meleeShield'); },
  get melee() { return t('forge.review.role.melee'); },
  get melee_spear() { return t('forge.review.role.meleeSpear'); },
  get creature() { return t('forge.review.role.creature'); },
  get zombie() { return t('forge.review.role.zombie'); },
  get skip() { return t('forge.review.role.skip'); },
};

/** Faction labels: none built in (factions belong to the game), so the raw id is shown. */
export const FACTION_TITLES = {};

/** Stable id of a row: the asset id, or the concept file for rows without an asset (skipped). */
export const rowKey = (item) => item.assetId || 'file:' + (item.file || item.name || '');

export const roleTitle = (role) => ROLE_TITLES[role] || role || '—';
export const factionTitle = (faction) => FACTION_TITLES[faction] || faction || '—';

const REWORK_KEYS = ['clothTears', 'weaponNotInHand', 'unlikeConcept', 'badAnimation', 'noSkeleton'];

/**
 * Preset reasons of the rework panel, in the current language (getters, read at call time); the
 * owner can add free text next to them. They are stored in qa.comment as plain text.
 */
export const REWORK_PRESETS = [];
REWORK_KEYS.forEach((key, i) => {
  Object.defineProperty(REWORK_PRESETS, i, { get: () => t('forge.review.reason.' + key), enumerable: true });
});

export const BUILD_FILTERS = [
  { id: 'all', get title() { return t('forge.review.filter.all'); } },
  { id: 'built', get title() { return t('forge.review.filter.built'); }, get hint() { return t('forge.review.filter.builtHint'); } },
  { id: 'torn', get title() { return t('forge.review.filter.torn'); }, get hint() { return t('forge.review.filter.tornHint'); } },
  { id: 'failed', get title() { return t('forge.review.filter.failed'); }, get hint() { return t('forge.review.filter.failedHint'); } },
];

export const DECISION_FILTERS = [
  { id: 'all', get title() { return t('forge.review.filter.all'); } },
  { id: 'pending', get title() { return t('forge.review.filter.pending'); } },
  { id: 'approved', get title() { return t('forge.review.filter.approved'); } },
  { id: 'rework', get title() { return t('forge.review.decision.rework'); } },
  { id: 'rejected', get title() { return t('forge.review.decision.rejected'); } },
];

/** Does a row belong to a build filter? Skipped / missing rows only show under "All". */
export function matchesBuild(item, id) {
  if (!id || id === 'all') return true;
  if (id === 'built') return item.status === 'built' || item.status === 'failed_qa';
  if (id === 'torn') return item.status === 'failed_qa';
  if (id === 'failed') return item.status === 'failed_forge';
  return true;
}

export function matchesDecision(item, id) {
  if (!id || id === 'all') return true;
  if (!item.reviewable) return false;
  if (id === 'pending') return !item.decision;
  return item.decision?.decision === id;
}

/** Filters: {build, decision, faction, role, query}; faction / role / query are optional. */
export function filterItems(items, filters = {}) {
  const query = String(filters.query || '').trim().toLowerCase();
  return (items || []).filter((item) => {
    if (!matchesBuild(item, filters.build)) return false;
    if (!matchesDecision(item, filters.decision)) return false;
    if (filters.faction && filters.faction !== 'all' && item.faction !== filters.faction) return false;
    if (filters.role && filters.role !== 'all' && item.role !== filters.role) return false;
    if (query) {
      const text = [item.name, item.file, item.role, roleTitle(item.role), item.faction, factionTitle(item.faction), item.decision?.game_unit, item.decision?.comment]
        .filter(Boolean).join(' ').toLowerCase();
      if (!text.includes(query)) return false;
    }
    return true;
  });
}

/** Counters above the grid (rows that cannot be reviewed — skipped, no passport — are not counted). */
export function reviewCounts(items) {
  const c = { total: 0, approved: 0, rework: 0, rejected: 0, pending: 0 };
  for (const item of items || []) {
    if (!item.reviewable) continue;
    c.total += 1;
    if (item.decision) c[item.decision.decision] += 1;
    else c.pending += 1;
  }
  return c;
}

/** Counts shown on the filter chips (each chip counts rows that pass the OTHER filters). */
export function chipCounts(items, filters, kind) {
  const ids = (kind === 'build' ? BUILD_FILTERS : DECISION_FILTERS).map((f) => f.id);
  const out = {};
  for (const id of ids) out[id] = filterItems(items, { ...filters, [kind]: id }).length;
  return out;
}

export function distinct(items, field) {
  return [...new Set((items || []).map((i) => i[field]).filter(Boolean))].sort();
}

const STOP_WORDS = new Set([
  'holding', 'wielding', 'standing', 'shooting', 'floating', 'drawing', 'casting', 'wearing', 'carrying',
  'with', 'in', 'on', 'and', 'character', 'concept',
]);

/**
 * Suggestion for the unit id in the game from the asset name: the words before the first verb or
 * preposition, snake_case, at most three words; a trailing "#2" becomes "_2".
 * "Knight archer holding long bow #2" -> "knight_archer_2".
 */
export function suggestUnit(name) {
  let text = String(name || '').trim();
  const numbered = text.match(/#\s*(\d+)\s*$/);
  if (numbered) text = text.slice(0, numbered.index);
  const words = text.toLowerCase().split(/[^a-z0-9\u0430-\u044f\u0451]+/).filter(Boolean);
  const head = [];
  for (const word of words) {
    if (STOP_WORDS.has(word)) {
      if (head.length) break;
      continue;
    }
    head.push(word);
    if (head.length === 3) break;
  }
  if (!head.length) return '';
  return head.join('_') + (numbered ? '_' + numbered[1] : '');
}

// ---------------------------------------------------------------------------- reasons for rework

export const splitReasons = (comment) => String(comment || '').split(';').map((part) => part.trim()).filter(Boolean);
export const hasReason = (comment, reason) => splitReasons(comment).includes(reason);

/** Add the preset to the comment, or remove it when it is already there. */
export function toggleReason(comment, reason) {
  const parts = splitReasons(comment);
  const next = parts.includes(reason) ? parts.filter((p) => p !== reason) : [...parts, reason];
  return next.join('; ');
}

// ---------------------------------------------------------------------------- keyboard

const KEY_STEP = { ArrowLeft: -1, ArrowRight: 1 };

/**
 * Next selected id for an arrow key over the visible cards: left/right move by one, up/down by a
 * grid row (`columns` cards). Nothing selected -> the first card. Edges stay where they are.
 */
export function moveSelection(ids, current, key, columns = 1) {
  if (!ids?.length) return null;
  const at = ids.indexOf(current);
  if (at < 0) return ids[0];
  const cols = Math.max(1, Math.floor(columns) || 1);
  let step = KEY_STEP[key];
  if (key === 'ArrowDown') step = cols;
  if (key === 'ArrowUp') step = -cols;
  if (!step) return current;
  const target = at + step;
  if (target < 0 || target >= ids.length) {
    // Down from the last row stays on the last card, up from the first row on the first one.
    if (key === 'ArrowDown' && at < ids.length - 1) return ids[ids.length - 1];
    return current;
  }
  return ids[target];
}

/**
 * Card to select after the current one left the list or was decided: the next one, or the
 * previous one at the end of the list. `ids` is the list BEFORE the change.
 */
export function neighbourId(ids, current) {
  const at = ids.indexOf(current);
  if (at < 0) return ids[0] || null;
  return ids[at + 1] ?? ids[at - 1] ?? null;
}

/** Keys of the review view: A approve, R rework (with a comment), Enter opens the large view. */
export function reviewKeyAction(code) {
  if (code === 'KeyA') return 'approve';
  if (code === 'KeyR') return 'rework';
  if (code === 'Enter' || code === 'NumpadEnter') return 'open';
  if (code === 'ArrowLeft' || code === 'ArrowRight' || code === 'ArrowUp' || code === 'ArrowDown') return 'move';
  return null;
}

// ---------------------------------------------------------------------------- numbers

/** 30.7 -> "30,7%"; null -> "—". */
export const percent = (value) => (typeof value === 'number' && Number.isFinite(value) ? String(value).replace('.', ',') + '%' : '—');

/** Body of PATCH /assets/:id for a decision (docs/forge/REVIEW.md); `decision: null` clears it. */
export function decisionPatch(decision, { comment = '', unit = '' } = {}) {
  if (!decision) return { qa: { reviewed: false, decision: null } };
  return { qa: { reviewed: true, decision, comment: String(comment || '').trim(), game_unit: String(unit || '').trim() } };
}

/** Local copy of a row with the decision the server stored (manifest.qa). */
export function withDecision(item, qa) {
  const decision = qa && ['approved', 'rework', 'rejected'].includes(qa.decision)
    ? { decision: qa.decision, comment: qa.comment || '', game_unit: qa.game_unit || '', at: qa.at || qa.reviewedAt || null }
    : null;
  return { ...item, decision };
}
