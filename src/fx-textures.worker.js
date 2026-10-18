// Texture generation worker: a heavy frame (1024 px, flipbook) does not freeze the studio UI.
// The worker runs with the default language; errors carry errorKey/errorVars so the page translates them itself.
import { generateFxTexture } from './fx-textures.js';
self.onmessage = (e) => {
  const { id, type, params } = e.data || {};
  try { const r = generateFxTexture(type, params); self.postMessage({ id, ok: true, ...r }, [r.data.buffer]); }
  catch (err) { self.postMessage({ id, ok: false, error: String(err?.message || err), errorKey: err?.i18nKey, errorVars: err?.i18nVars }); }
};
