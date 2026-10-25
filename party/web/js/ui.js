// Majlis Party: shared DOM helpers, connection banner, toasts and error wording.
// Server strings are only ever inserted with textContent, never innerHTML.
import { HttpTransport } from './transport.js';

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, String(v));
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function mount(root, ...nodes) {
  root.replaceChildren(...nodes.flat(Infinity).filter(Boolean));
}

export const params = new URLSearchParams(location.search);
export const isSim = params.get('sim') === '1';

export async function makeTransport() {
  if (isSim) {
    const { SimTransport } = await import('./sim-transport.js');
    const t = new SimTransport();
    window.__majlisSimTransport = t; // test hook (offline toggle); exists only in simulation mode
    return t;
  }
  return new HttpTransport();
}

// Keeps ?sim=1 on internal links so a simulated session never silently talks to a real server.
export function pageUrl(path, extra = {}) {
  const u = new URL(path, location.href);
  u.search = '';
  u.hash = '';
  for (const [k, v] of Object.entries(extra)) if (v !== null && v !== undefined) u.searchParams.set(k, v);
  if (isSim) u.searchParams.set('sim', '1');
  return u.toString();
}

// Host and TV pages carry their room in the fragment (host.html#code=ROOM, tv.html#code=ROOM):
// a fragment never reaches the server, which accepts no query string on those pages.
export function roomUrl(path, code) {
  const u = new URL(pageUrl(path));
  if (code) u.hash = 'code=' + encodeURIComponent(code);
  return u.toString();
}

export function hashCode() {
  const frag = new URLSearchParams(location.hash.replace(/^#/, ''));
  return frag.get('code') || '';
}

// The join URL is the ONLY thing a QR code ever encodes: page path + room code (+ sim flag in
// simulation). Never tokens, names or roles.
export function joinUrl(code) {
  const u = new URL('play.html', location.href);
  u.search = '';
  u.hash = '';
  u.searchParams.set('code', code);
  if (isSim) u.searchParams.set('sim', '1');
  return u.toString();
}

export function simRibbon() {
  if (!isSim) return null;
  return h('div', { class: 'sim-ribbon', role: 'note' }, 'SIMULATED FIXTURE - not a real server');
}

// Plain wording for the server's stable error codes (party/server/errors.py) and client codes.
const ROOM_GONE = 'This room no longer exists. Ask the host for the new code.';
const SEAT_GONE = 'Your access to this room has expired. Ask the host for a new room.';
const MESSAGES = {
  network_error: 'Can’t reach the game server. Check you’re on the same Wi‑Fi as the host.',
  stream_ended: 'Connection dropped. Reconnecting…',
  bad_response: 'The server sent an unexpected reply.',
  room_not_found: ROOM_GONE,
  not_found: ROOM_GONE,
  room_expired: 'This room has expired.',
  room_closed: 'The host closed this room.',
  name_taken: 'That name is already in this room. Try another.',
  invalid_name: 'Names need 1 to 24 characters.',
  room_full: 'This room is full.',
  game_in_progress: 'A game is in progress. You can join between games.',
  unauthorized: SEAT_GONE,
  token_expired: SEAT_GONE,
  forbidden: 'Only the host can do that.',
  stale_round: 'That was for the previous round. Nothing was changed.',
  round_required: 'That was for the previous round. Nothing was changed.',
  invalid_phase: 'Not available at this point in the game.',
  paused: 'The game is paused.',
  wrong_mode: 'That belongs to a different game.',
  unknown_mode: 'That game isn’t available.',
  mode_unimplemented: 'That game isn’t playable yet.',
  mode_not_selected: 'Choose a game first.',
  content_missing: 'This game’s content is not available on the server.',
  content_invalid: 'This game’s content is not available on the server.',
  content_insufficient: 'This game doesn’t have enough content for this many players.',
  content_exhausted: 'No more rounds left. End the game to show the final scores.',
  not_enough_players: 'More players need to join before starting.',
  too_many_players: 'Too many players for this game.',
  unknown_player: 'That player isn’t in this room.',
  invalid_payload: 'That request wasn’t valid. Nothing was changed.',
  already_revealed: 'Already revealed.',
  no_pending_guess: 'That guess was already cleared.',
  already_awarded: 'Points for this were already given.',
  round_awarded: 'This round’s points were already given.',
  buzzer_closed: 'The buzzer isn’t open.',
  buzzer_taken: 'Someone else buzzed first.',
  unknown_answer: 'That answer isn’t on this board.',
  no_match: 'No hidden answer matches that.',
  max_strikes: 'Already at three strikes.',
  steal_not_available: 'A steal is only possible after three strikes, while answering.',
  no_control: 'Choose who gets the points.',
  rate_limited: 'Too fast – wait a moment.',
  too_many_streams: 'Too many screens are connected right now.',
  too_many_rooms: 'The server is full right now. Try again shortly.',
  server_busy: 'The server is busy. Try again shortly.',
  internal_error: 'The server hit a problem. Try again.',
  simulation_unsupported: 'This browser can’t run the simulated fixture (SharedWorker missing).',
  streaming_unsupported: 'This browser can’t receive live updates.',
};

// Accepts a code string, an ApiError, or an {error: code | {code}} result.
export function errorCode(x) {
  if (!x) return '';
  if (typeof x === 'string') return x;
  if (typeof x.code === 'string') return x.code;
  if (x.error) return errorCode(x.error);
  return '';
}

export function describeError(code) {
  const c = errorCode(code);
  return MESSAGES[c] || 'Something went wrong (' + String(c || 'unknown').replace(/[^a-z0-9_]/gi, '').slice(0, 40) + ').';
}

// Codes after which this room, or this device's seat in it, can never work again.
export const TERMINAL_ROOM = new Set(['room_not_found', 'not_found', 'room_expired', 'room_closed']);
export const TERMINAL_SEAT = new Set(['unauthorized', 'token_expired', 'invalid_token']);

let toastTimer = null;
export function toast(text, kind = 'info', ms = 3200) {
  let el = document.getElementById('toast');
  if (!el) {
    el = h('div', { id: 'toast', class: 'toast', role: 'status', 'aria-live': 'polite' });
    document.body.append(el);
  }
  el.textContent = text;
  el.dataset.kind = kind;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), ms);
}

// Keeps --banner-stack equal to the height of the visible top banners (simulation ribbon,
// connection banner) so overlays and sticky columns start below them.
function trackBannerStack() {
  const els = ['sim', 'conn'].map(id => document.getElementById(id)).filter(Boolean);
  if (!els.length || typeof ResizeObserver === 'undefined') return;
  const update = () => document.documentElement.style.setProperty('--banner-stack',
    els.reduce((sum, e) => sum + (e.hidden ? 0 : e.getBoundingClientRect().height), 0) + 'px');
  const ro = new ResizeObserver(update);
  els.forEach(e => ro.observe(e));
  update();
}
trackBannerStack();

// Connection banner states: connecting | live | reconnecting | fatal. The last snapshot stays
// visible while reconnecting; only its controls are disabled (see party.css).
let retryHandler = null;
export function setRetryHandler(fn) { retryHandler = fn; }
export function connBanner(state, detail) {
  const el = document.getElementById('conn');
  if (!el) return;
  if (document.body.dataset.conn === state && state !== 'fatal') return;
  document.body.dataset.conn = state;
  const text = {
    connecting: 'Connecting to room…',
    live: 'Connected',
    reconnecting: 'Reconnecting… showing last update.',
    fatal: detail ? describeError(detail) : 'Disconnected.',
  }[state] || '';
  mount(el, h('span', { text }),
    state === 'reconnecting' && retryHandler ? h('button', { class: 'btn btn-sm btn-sec conn-retry', type: 'button', onclick: () => retryHandler(), text: 'Retry now' }) : null);
  el.hidden = state === 'live';
}

// One polite and one assertive live region per page, so views never make whole boards live.
export function announce(text, { assertive = false } = {}) {
  const id = assertive ? 'live-assertive' : 'live-polite';
  let el = document.getElementById(id);
  if (!el) {
    el = h('div', { id, class: 'sr-only', role: assertive ? 'alert' : 'status', 'aria-live': assertive ? 'assertive' : 'polite', 'aria-atomic': 'true' });
    document.body.append(el);
  }
  el.textContent = '';
  setTimeout(() => { el.textContent = text; }, 30);
}

// Accessible confirmation: native modal <dialog> (focus trap, Escape closes), Cancel focused
// first for destructive actions, focus returned to the invoking control when it still exists.
// Confirmations that act on one round. When a snapshot moves the room to another round, the
// caller runs cancelStaleConfirms(): the dialog closes, nothing is sent, and the operator is told.
const roundConfirms = new Set();

// A confirmation may also carry stillValid(): true while what the dialog promised still holds
// (for example the number of points it names), otherwise a short reason.
export function cancelStaleConfirms(currentRound) {
  let cancelled = null, reason = '';
  for (const c of [...roundConfirms]) {
    let why = c.roundId === currentRound ? true : 'Round changed.';
    if (why === true && c.stillValid) why = c.stillValid();
    if (why === true) continue;
    cancelled = c.action; reason = why;
    c.cancel();
  }
  if (cancelled === null) return false;
  const heading = document.querySelector('#app main h1');
  if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); }
  toast(reason + ' “' + cancelled + '” was cancelled. No action was taken.', 'info', 6000);
  return true;
}

export function confirmDialog(message, { confirmLabel = 'Confirm', danger = false, roundId, action, stillValid } = {}) {
  const invoker = document.activeElement;
  return new Promise(resolve => {
    const cancel = h('button', { class: 'btn btn-sec', type: 'button', value: 'cancel', text: 'Cancel' });
    const ok = h('button', { class: 'btn ' + (danger ? 'btn-danger' : 'btn-pri'), type: 'button', value: 'ok', text: confirmLabel });
    const dlg = h('dialog', { class: 'confirm', 'aria-labelledby': 'confirm-msg' },
      h('p', { id: 'confirm-msg', class: 'confirm-msg', dir: 'auto', text: message }),
      h('div', { class: 'row-actions confirm-actions' }, cancel, ok));
    // Settle from the buttons directly: Chrome delivers the dialog 'close' event on a rendering
    // frame, which never comes while the page is hidden. 'close' still covers Escape.
    let done = false;
    const entry = roundId === undefined ? null : { roundId, stillValid, action: action || confirmLabel, cancel: () => finish(false, true) };
    const finish = (result, stale = false) => {
      if (done) return;
      done = true;
      if (entry) roundConfirms.delete(entry);
      if (dlg.open) dlg.close();
      dlg.remove();
      if (!stale && invoker && invoker.isConnected && typeof invoker.focus === 'function') invoker.focus();
      resolve(result);
    };
    if (entry) roundConfirms.add(entry);
    cancel.addEventListener('click', () => finish(false));
    ok.addEventListener('click', () => finish(true));
    dlg.addEventListener('close', () => finish(false));
    document.body.append(dlg);
    dlg.showModal();
    (danger ? cancel : ok).focus();
  });
}

export function vibrate(pattern) {
  try { if (navigator.vibrate) navigator.vibrate(pattern); } catch { /* not supported */ }
}

export function playerName(snap, id) {
  const p = (snap.players || []).find(x => x.id === id);
  return p ? p.name : 'A player';
}

export function sortedScores(snap) {
  return (snap.players || []).slice().sort((a, b) => (b.score - a.score) || a.name.localeCompare(b.name));
}

// Busy-guard for a control: disables while the action is in flight.
export async function withBusy(btn, fn) {
  if (btn) { btn.disabled = true; btn.setAttribute('aria-busy', 'true'); }
  try { return await fn(); } finally {
    if (btn && btn.isConnected) { btn.disabled = false; btn.removeAttribute('aria-busy'); }
  }
}

// Re-rendering on every snapshot must not throw keyboard or screen-reader focus away.
function focusKey(el) {
  if (!el || el === document.body) return null;
  if (el.id) return { id: el.id };
  return { tag: el.tagName, label: el.getAttribute('aria-label') || '', text: (el.textContent || '').trim().slice(0, 60) };
}
export function preserveFocus(root, fn) {
  const active = document.activeElement;
  const k = root.contains(active) ? focusKey(active) : null;
  const sel = active && 'selectionStart' in active ? [active.selectionStart, active.selectionEnd] : null;
  fn();
  if (!k) return;
  let target = null;
  if (k.id) target = document.getElementById(k.id);
  else {
    target = Array.from(root.querySelectorAll(k.tag)).find(e => (e.getAttribute('aria-label') || '') === k.label && (e.textContent || '').trim().slice(0, 60) === k.text) || null;
  }
  if (target && !target.disabled) {
    target.focus({ preventScroll: true });
    if (sel && 'setSelectionRange' in target) { try { target.setSelectionRange(sel[0], sel[1]); } catch { /* not a text input */ } }
  }
}
