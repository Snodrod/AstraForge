import { readFileSync } from 'node:fs';
import path from 'node:path';

const PURPOSES = new Set(['apose', 'views']);
const PROFILE_KINDS = new Set(['character', 'building', 'generic', 'none']);

function readJson(file, label) {
  let value;
  try { value = JSON.parse(readFileSync(file, 'utf8')); }
  catch (error) { throw new Error(`Unable to read ${label}: ${file}: ${error.message}`); }
  if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error(`${label} must be a JSON object.`);
  return value;
}

function strings(value, field) {
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string' || !item.trim())) {
    throw new Error(`${field} must be an array of non-empty strings.`);
  }
  return value.map((item) => item.trim());
}

export function resolveIdentityConfig({ identityRoot, presetFile, negativeProfile, negativeFile, purpose }) {
  if (!PURPOSES.has(purpose)) throw new Error('Identity purpose must be apose or views.');
  if (negativeProfile && negativeFile) throw new Error('--negative-profile and --negative-file are mutually exclusive.');
  if (!presetFile) throw new Error('--preset is required.');
  const lock = readJson(path.join(identityRoot, 'identity-lock.json'), 'identity-lock');
  const preset = readJson(path.resolve(presetFile), 'object preset');
  if (!lock.version || typeof lock.base !== 'string' || typeof lock.purposes?.[purpose] !== 'string') throw new Error('Invalid identity-lock.');
  if (!['character', 'building', 'generic'].includes(preset.kind)) throw new Error('Preset kind must be character, building or generic.');
  const positive = [preset.positive, ...strings(preset.identityFeatures, 'identityFeatures'), ...strings(preset.inventory, 'inventory'), ...strings(preset.asymmetry, 'asymmetry'), ...strings(preset.viewNotes, 'viewNotes')];
  if (typeof preset.positive !== 'string' || !preset.positive.trim()) throw new Error('Preset positive is required.');
  const profileName = negativeProfile || (['character', 'building'].includes(preset.kind) ? preset.kind : 'generic');
  const profilePath = negativeFile ? path.resolve(negativeFile) : path.join(identityRoot, `negative-${profileName}.json`);
  if (!negativeFile && !PROFILE_KINDS.has(profileName)) throw new Error(`Unknown negative profile: ${profileName}`);
  const profile = readJson(profilePath, 'negative profile');
  if (!profile.version || !Array.isArray(profile.terms) || profile.terms.some((term) => typeof term !== 'string')) throw new Error('Invalid negative profile.');
  if (profile.kind !== 'any' && profile.kind !== 'generic' && profile.kind !== 'custom' && profile.kind !== preset.kind) {
    throw new Error(`Negative profile kind ${profile.kind} does not match preset kind ${preset.kind}.`);
  }
  const kindLock = lock.kinds?.[preset.kind];
  if (kindLock !== undefined && typeof kindLock !== 'string') throw new Error('Invalid kind-specific identity-lock.');
  const prompt = [lock.base, kindLock, lock.purposes[purpose], ...positive].filter(Boolean).join(' ');
  const negative = profile.terms.map((term) => term.trim()).filter(Boolean).join(', ');
  return {
    prompt,
    negative,
    preset,
    negativeProfile: profile,
    identityLock: lock,
    sources: {
      identityLock: path.join(identityRoot, 'identity-lock.json'),
      preset: path.resolve(presetFile),
      negative: profilePath,
      purpose,
    },
  };
}
