// Majlis Party: per-role, per-room resume credentials.
// Tokens are stored only in this device's localStorage, keyed by role and room code, and are
// never written to URLs, QR codes, the DOM or logs. A device can hold one credential per role
// per room; the operator token is never readable by the player or display pages' code paths.

const PREFIX = 'majlisParty.v1';
const MAX_AGE_MS = 12 * 60 * 60 * 1000;
const ROLES = new Set(['operator', 'player', 'display']);

function key(role, code, sim) {
  if (!ROLES.has(role)) throw new Error('bad_role');
  return `${PREFIX}.${sim ? 'sim.' : ''}${role}.${code}`;
}

function store() {
  try { return window.localStorage; } catch { return null; }
}

export function saveCredential(role, code, cred, sim = false) {
  const s = store();
  if (!s) return false;
  const rec = { token: cred.token, savedAt: Date.now() };
  if (role === 'player') { rec.playerId = cred.playerId; rec.name = cred.name; }
  s.setItem(key(role, code, sim), JSON.stringify(rec));
  s.setItem(`${PREFIX}.${sim ? 'sim.' : ''}last.${role}`, code);
  return true;
}

export function loadCredential(role, code, sim = false) {
  const s = store();
  if (!s || !code) return null;
  try {
    const rec = JSON.parse(s.getItem(key(role, code, sim)) || 'null');
    if (!rec || typeof rec.token !== 'string') return null;
    if (Date.now() - rec.savedAt > MAX_AGE_MS) { clearCredential(role, code, sim); return null; }
    return rec;
  } catch { return null; }
}

export function clearCredential(role, code, sim = false) {
  const s = store();
  if (!s) return;
  s.removeItem(key(role, code, sim));
  const lastKey = `${PREFIX}.${sim ? 'sim.' : ''}last.${role}`;
  if (s.getItem(lastKey) === code) s.removeItem(lastKey);
}

export function lastRoom(role, sim = false) {
  const s = store();
  if (!s) return null;
  const code = s.getItem(`${PREFIX}.${sim ? 'sim.' : ''}last.${role}`);
  return code && loadCredential(role, code, sim) ? code : null;
}

// Room codes: six characters, normalised to upper case, ambiguous characters left to the server.
export function normalizeCode(raw) {
  return String(raw || '').toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 6);
}

export function isCodeShape(code) {
  return /^[A-Z0-9]{6}$/.test(code);
}
