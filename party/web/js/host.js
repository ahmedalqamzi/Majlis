// Majlis Party operator (host) interface. The operator is trusted but is not a player.
// All rules, scoring and buzzer order are decided by the server; this page sends host actions.
import { h, mount, makeTransport, simRibbon, connBanner, toast, describeError, pageUrl, roomUrl, hashCode, withBusy, playerName, preserveFocus, confirmDialog, cancelStaleConfirms, setRetryHandler, announce, TERMINAL_ROOM, TERMINAL_SEAT } from './ui.js';
import { saveCredential, loadCredential, clearCredential, lastRoom, normalizeCode, isCodeShape } from './session.js';
import { MODES, modeInfo } from './modes.js';
import { qrBlock, codeBlock, playerChips, scoreboard, modeHeading, strikesRow, feudStatusLine, pausedOverlay, IMPOSTOR_OUTCOMES } from './views.js';
import { isSim } from './ui.js';

const app = document.getElementById('app');
const simEl = document.getElementById('sim');
if (simEl) mount(simEl, simRibbon());

let transport, code = null, token = null, stream = null, snap = null;
const ui = { feudAward: null, feudSteal: null, seen: null, phase: null };

// Announce only transitions the operator must act on: a server-decided buzz and new guesses.
function announceChanges() {
  const g = snap.game || {};
  const winner = g.buzzer && g.buzzer.winner;
  const key = { winner: winner ? snap.roundId + ':' + winner : null, guesses: new Set(g.pendingGuesses || []) };
  const prev = ui.seen; ui.seen = key;
  if (!prev) return;
  if (key.winner && key.winner !== prev.winner) announce(playerName(snap, winner) + ' buzzed first.', { assertive: true });
  for (const id of key.guesses) if (!prev.guesses.has(id)) announce(playerName(snap, id) + ' has a guess.');
}

// `round` is the round the operator was looking at when the control was rendered (captured in
// its closure), not the round current when the request leaves: a confirm dialog can stay open
// while the server moves on, and the action must then be refused, never retargeted. The server
// rejects a stale roundId as well; this check only avoids sending one.
async function act(type, payload = {}, btn, { round } = {}) {
  if (!snap) return;
  if (round !== undefined && round !== snap.roundId) {
    toast(describeError('stale_round'), 'error');
    return { accepted: false, error: 'stale_round' };
  }
  return withBusy(btn, async () => {
    try {
      const r = await transport.act(code, token, type, payload, round);
      if (!r.accepted) toast(describeError(r.error), 'error');
      return r;
    } catch (e) {
      toast(describeError(e.code), 'error');
    }
  });
}

// Confirmation for irreversible operator actions; the invoking button is captured before the
// dialog opens because the board may re-render while it is shown.
function confirmThen(message, fn, opts = {}) {
  return async (ev) => {
    const btn = ev.currentTarget;
    if (await confirmDialog(message, opts)) fn(btn);
  };
}

// ---- start / resume -------------------------------------------------------------------

function renderStart(error) {
  const resume = lastRoom('operator', isSim);
  const nameIn = h('input', { id: 'op-name', name: 'operatorName', maxlength: 24, autocomplete: 'nickname', placeholder: 'Optional', dir: 'auto' });
  const createBtn = h('button', { class: 'btn btn-pri btn-lg', type: 'submit', text: 'Create room' });
  const form = h('form', { class: 'card stack', onsubmit: async (e) => {
    e.preventDefault();
    await withBusy(createBtn, async () => {
      try {
        const r = await transport.createRoom(nameIn.value.trim() || undefined);
        saveCredential('operator', r.code, { token: r.hostToken }, isSim);
        // Kept on this device only, so a TV view opened from here can authenticate.
        saveCredential('display', r.code, { token: r.displayToken }, isSim);
        start(r.code);
      } catch (err) {
        renderStart(describeError(err.code));
      }
    });
  } },
    h('h1', { class: 'card-title', text: 'Host a party' }),
    h('p', { class: 'muted', text: 'You run the game from this screen. Players join on their phones with the room code.' }),
    h('label', { for: 'op-name', text: 'Your name (shown as host)' }), nameIn,
    error ? h('p', { class: 'form-error', role: 'alert', text: error }) : null,
    createBtn);
  mount(app,
    h('header', { class: 'topbar' }, h('a', { class: 'brand', href: pageUrl('index.html'), text: 'Majlis Party' }), h('span', { class: 'pill', text: 'Host' })),
    h('main', { class: 'page narrow stack' },
      resume ? h('section', { class: 'card stack' },
        h('h2', { class: 'card-title', text: 'Resume your room' }),
        h('p', { text: 'You were hosting room ' + resume + ' on this device.' }),
        h('button', { class: 'btn btn-sec', type: 'button', onclick: () => start(resume), text: 'Resume ' + resume })) : null,
      form));
}

function start(c) {
  code = c;
  const cred = loadCredential('operator', code, isSim);
  if (!cred) { renderStart('This device has no host access for room ' + code + '.'); return; }
  token = cred.token;
  history.replaceState(null, '', roomUrl('host.html', code));
  mount(app, h('main', { class: 'page center' }, h('p', { class: 'muted', role: 'status', text: 'Connecting to room ' + code + '…' })));
  if (stream) stream.close();
  setRetryHandler(() => stream && stream.kick());
  stream = transport.subscribe(code, token, {
    onSnapshot: (s) => {
      // Closed is terminal: the room accepts nothing more and its tokens are useless.
      if (s.phase === 'closed') { roomClosed(); return; }
      snap = s; render();
      // Ended is terminal for this page: keep the final scores and stop reconnecting.
      if (s.phase === 'ended' && stream) { stream.close(); stream = null; connBanner('live'); }
    },
    onStatus: (st, err) => {
      const c = err && err.code;
      if (st === 'fatal' && (TERMINAL_ROOM.has(c) || TERMINAL_SEAT.has(c))) {
        stream = null;
        forget();
        connBanner('live');
        renderStart(describeError(c));
        return;
      }
      connBanner(st, c);
      if (st === 'fatal' && !(snap && snap.phase === 'ended')) {
        stream = null;
        renderStart(describeError(c));
      }
    },
  });
}

// Drops this device's host and TV credentials for the current room and returns to the start page.
function forget() {
  if (stream) stream.close();
  stream = null;
  if (code) { clearCredential('operator', code, isSim); clearCredential('display', code, isSim); }
  snap = null; code = null; token = null;
  history.replaceState(null, '', pageUrl('host.html'));
}

function leave(message) {
  forget();
  connBanner('live');
  renderStart(message);
}

function roomClosed() {
  const c = code;
  leave();
  toast('Room ' + c + ' is closed. Players were told.', 'info', 6000);
}

// ---- render ----------------------------------------------------------------------------

function topbar() {
  const m = modeInfo(snap.mode);
  const controls = [];
  // While paused, Resume and End game are in the paused panel (the rest of the page is inert).
  if (snap.phase === 'playing') {
    controls.push(h('button', { id: 'pause-btn', class: 'btn btn-ghost', type: 'button', onclick: e => act('room.pause', {}, e.currentTarget), text: 'Pause' }));
    controls.push(endGameButton());
  }
  return h('header', { class: 'topbar' },
    h('span', { class: 'brand', text: 'Majlis Party' }),
    h('span', { class: 'code-chip', 'aria-label': 'Room code ' + snap.code, text: snap.code }),
    snap.phase !== 'lobby' ? h('span', { class: 'pill', text: m.title }) : h('span', { class: 'pill', text: 'Host' }),
    h('div', { class: 'topbar-actions' }, controls));
}

function endGameButton(extra = {}) {
  return h('button', { class: 'btn btn-danger-ghost', type: 'button', ...extra,
    onclick: confirmThen('End the game for everyone?', btn => act('room.end', {}, btn), { confirmLabel: 'End game', danger: true }), text: 'End game' });
}

// Operator pause panel: players and the TV stay blocked by their overlay; the operator's own
// panel carries the controls, and everything behind it is inert so touch, mouse and keyboard
// can only reach Resume / End game (and the connection banner's Retry above it).
function hostPausedOverlay() {
  return pausedOverlay({
    title: 'Paused',
    text: 'Players and the TV are waiting. Resume when everyone is ready.',
    actions: [
      h('button', { id: 'resume-btn', class: 'btn btn-pri btn-lg', type: 'button', onclick: e => act('room.resume', {}, e.currentTarget), text: 'Resume game' }),
      endGameButton({ id: 'overlay-end-btn' }),
    ],
  });
}

// Tab order while paused: Resume -> End game -> Retry (when shown) -> Resume. Suspended while a
// confirmation dialog is open (the dialog keeps its own focus).
function pauseFocusLoop(e) {
  if (e.key !== 'Tab' || !document.getElementById('paused-panel') || document.querySelector('dialog[open]')) return;
  const retry = document.querySelector('#conn:not([hidden]) .conn-retry');
  const order = [document.getElementById('resume-btn'), document.getElementById('overlay-end-btn'), retry].filter(Boolean);
  const i = order.indexOf(document.activeElement);
  e.preventDefault();
  const next = i < 0 ? 0 : (i + (e.shiftKey ? order.length - 1 : 1)) % order.length;
  order[next].focus();
}
document.addEventListener('keydown', pauseFocusLoop);

function render() {
  if (!snap) return;
  announceChanges();
  const paused = snap.phase === 'paused';
  const prevPhase = ui.phase;
  ui.phase = snap.phase;
  let body;
  if (snap.phase === 'lobby') body = lobby();
  else if (snap.phase === 'ended') body = ended();
  else body = playing();
  const bar = topbar(), main = h('main', { class: 'page op-page', inert: paused, 'aria-hidden': paused ? 'true' : null }, body);
  if (paused) { bar.inert = true; bar.setAttribute('aria-hidden', 'true'); }
  preserveFocus(app, () => mount(app, bar, main, paused ? hostPausedOverlay() : null));
  document.body.classList.toggle('pause-open', paused);
  // A confirmation bound to a round closes, unsent, once the room is on another round or over.
  cancelStaleConfirms(snap.phase === 'ended' ? null : snap.roundId);
  // Move focus with the pause so keyboard and screen-reader users land on the control they need.
  if (paused && prevPhase !== 'paused') document.getElementById('resume-btn').focus();
  else if (!paused && prevPhase === 'paused' && !app.contains(document.activeElement)) {
    const target = document.getElementById('pause-btn');
    if (target) target.focus();
  }
}

function lobby() {
  const chosen = snap.mode;
  const min = (snap.playerLimits && snap.playerLimits.min) || 3;
  const enough = (snap.players || []).length >= min;
  const picker = h('fieldset', { class: 'card mode-picker' },
    h('legend', { class: 'card-title', text: 'Choose a game' }),
    h('div', { class: 'mode-grid' }, MODES.map(m => {
      if (!m.playable) {
        return h('div', { class: 'mode-tile disabled', 'aria-disabled': 'true', 'data-mode': m.id },
          h('span', { class: 'mode-name', text: m.title }),
          h('span', { class: 'mode-ar', lang: 'ar', dir: 'rtl', text: m.ar }),
          h('span', { class: 'badge-later', text: 'Not implemented yet' }),
          h('span', { class: 'mode-blurb', text: 'Coming later - not playable yet' }));
      }
      return h('button', { class: 'mode-tile' + (chosen === m.id ? ' selected' : ''), type: 'button', 'aria-pressed': chosen === m.id ? 'true' : 'false', 'data-mode': m.id,
        onclick: e => act('room.set_mode', { mode: m.id }, e.currentTarget) },
        h('span', { class: 'mode-name', text: m.title }),
        h('span', { class: 'mode-ar', lang: 'ar', dir: 'rtl', text: m.ar }),
        h('span', { class: 'mode-blurb', text: m.blurb }),
        h('span', { class: 'badge-good', text: 'Playable' }));
    })),
    h('p', { class: 'muted small', text: '3 of 10 games are playable in this build. The other 7 are not implemented yet and cannot be started.' }));
  const startBtn = h('button', { class: 'btn btn-pri btn-lg', type: 'button', disabled: !chosen || !enough,
    onclick: e => act('room.start', {}, e.currentTarget), text: chosen ? 'Start ' + modeInfo(chosen).title : 'Choose a game to start' });
  return h('div', { class: 'op-lobby' },
    h('section', { class: 'card join-card' },
      codeBlock(snap.code),
      qrBlock(snap.code),
      h('p', { class: 'muted small join-hint', text: 'Scan to join, or open the player page and type the code. Same Wi‑Fi as this device.' }),
      h('button', { class: 'btn btn-ghost', type: 'button', onclick: openTv, text: 'Open TV display' })),
    h('section', { class: 'card' },
      h('h2', { class: 'card-title', text: 'Players (' + (snap.players || []).length + ')' }),
      playerChips(snap),
      enough ? null : h('p', { class: 'muted small', text: 'At least ' + min + ' players are needed to start.' })),
    picker,
    h('div', { class: 'sticky-actions' }, startBtn,
      h('button', { class: 'btn btn-ghost', type: 'button', onclick: confirmThen('Close this room for everyone? Players will be told the host closed it, and the code stops working.', async btn => { const r = await act('room.close', {}, btn); if (r && r.accepted && code) roomClosed(); }, { confirmLabel: 'Close room', danger: true }), text: 'Close room' })));
}

function openTv() {
  window.open(roomUrl('tv.html', snap.code), 'majlis-tv');
}

function playing() {
  const panel = snap.mode === 'who_am_i' ? whoAmIPanel() : snap.mode === 'feud' ? feudPanel()
    : snap.mode === 'impostor' ? impostorPanel() : h('p', { text: 'Unsupported mode.' });
  return h('div', { class: 'op-play' },
    modeHeading(snap),
    panel,
    scoreboard(snap),
    h('section', { class: 'card row-actions' },
      h('button', { class: 'btn btn-ghost', type: 'button', onclick: openTv, text: 'TV display' }),
      h('span', { class: 'muted small', text: 'Players: ' + (snap.players || []).filter(p => p.connected).length + ' connected of ' + (snap.players || []).length })));
}

// ---- Who Am I ----------------------------------------------------------------------------

function whoAmIPanel() {
  const g = snap.game || {};
  const rid = snap.roundId;
  const solved = new Set(g.solved || []);
  const revealedSet = new Set(g.revealed || []);
  const ready = new Set(g.ready || []);
  const guesses = g.guesses || {};
  const assignments = g.assignments || {};
  const asking = g.roundPhase === 'asking';
  const pts = g.awardPoints || 1;
  const order = (g.order || Object.keys(assignments)).filter(pid => assignments[pid]);
  const rows = order.map(pid => {
    const a = assignments[pid];
    const name = playerName(snap, pid);
    const hasGuess = Object.prototype.hasOwnProperty.call(guesses, pid);
    return h('li', { class: 'who-row' + (solved.has(pid) ? ' solved' : ''), 'data-player': pid },
      h('div', { class: 'who-player' }, h('span', { class: 'who-name', dir: 'auto', text: name }),
        solved.has(pid) ? h('span', { class: 'badge-good', text: 'Solved' })
          : revealedSet.has(pid) ? h('span', { class: 'badge-later', text: 'Revealed' })
            : ready.has(pid) ? h('span', { class: 'badge-warn', text: 'Ready' }) : null),
      h('div', { class: 'who-char' }, h('span', { class: 'char-label', dir: 'auto', text: a.label }), a.ar ? h('span', { class: 'char-ar', lang: 'ar', dir: 'rtl', text: a.ar }) : null),
      hasGuess ? h('div', { class: 'who-guess' }, h('span', { class: 'eyebrow', text: 'Guess' }), h('span', { dir: 'auto', text: '“' + guesses[pid] + '”' })) : null,
      !solved.has(pid) ? h('div', { class: 'who-acts' },
        h('button', { class: 'btn btn-good', type: 'button', 'aria-label': 'Correct: give ' + name + ' ' + pts + (pts === 1 ? ' point' : ' points'), onclick: e => act('whoami.award', { playerId: pid }, e.currentTarget, { round: rid }), text: 'Correct +' + pts }),
        hasGuess ? h('button', { class: 'btn btn-ghost', type: 'button', 'aria-label': 'Not it: clear ' + name + '’s guess', onclick: e => act('whoami.dismiss', { playerId: pid }, e.currentTarget, { round: rid }), text: 'Not it' }) : null,
        !revealedSet.has(pid) ? h('button', { class: 'btn btn-ghost', type: 'button', 'aria-label': 'Reveal ' + name + '’s character to everyone',
          onclick: confirmThen('Reveal ' + name + '’s character on every screen? No points are given.', btn => act('whoami.reveal', { playerId: pid }, btn, { round: rid }), { confirmLabel: 'Reveal', roundId: rid, action: 'Reveal ' + name }), text: 'Reveal' }) : null) : null);
  });
  const remaining = g.roundsRemaining;
  const nextBtn = remaining === 0
    ? h('p', { class: 'muted small', text: 'No characters left for another round. End the game to show the final scores.' })
    : h('button', { class: 'btn btn-pri btn-lg', type: 'button', onclick: e => act('round.next', {}, e.currentTarget, { round: rid }), text: 'Next round' });
  return h('section', { class: 'card stack' },
    h('div', { class: 'panel-head' },
      h('h2', { class: 'card-title', text: asking ? 'Characters (only you see all of them)' : 'Everyone’s characters' }),
      h('span', { class: 'pill', text: asking ? 'Asking' : 'Revealed' })),
    h('p', { class: 'muted small', text: asking ? 'Each player sees everyone else’s character but not their own. Guesses arrive here only. Give the point when someone names theirs, or clear a wrong guess so they can try again.' : 'Every character is now visible on every screen. Guesses stay private to you and the player.' }),
    h('ul', { class: 'who-list' }, rows),
    h('div', { class: 'row-actions' },
      asking ? h('button', { class: 'btn btn-sec', type: 'button', onclick: confirmThen('Reveal every character to all players and the TV?', btn => act('whoami.reveal', {}, btn, { round: rid }), { confirmLabel: 'Reveal characters', roundId: rid, action: 'Reveal all characters' }), text: 'Reveal all' }) : nextBtn));
}

// ---- Feud --------------------------------------------------------------------------------

function playerSelect(id, label, value, onChange, exclude) {
  const sel = h('select', { id, onchange: e => onChange(e.target.value) },
    h('option', { value: '', text: 'Choose player…' }),
    (snap.players || []).filter(p => p.id !== exclude).map(p => h('option', { value: p.id, selected: p.id === value, text: p.name })));
  return [h('label', { for: id, class: 'small', text: label }), sel];
}

function feudPanel() {
  const g = snap.game || {};
  const rid = snap.roundId;
  const phase = g.roundPhase;
  const over = phase === 'awarded';
  const winner = g.buzzer && g.buzzer.winner;
  const max = g.maxStrikes || 3;
  const strikes = g.strikes || 0;
  const points = g.roundPoints || 0;
  if (ui.feudAwardRound !== snap.roundId) { ui.feudAward = null; ui.feudSteal = null; ui.feudAwardRound = snap.roundId; }
  // A choice that is no longer valid (for example the steal target now in control) is dropped.
  if (ui.feudSteal && ui.feudSteal === g.control) ui.feudSteal = null;
  const awardTarget = ui.feudAward || g.control || '';

  // Operator board: the full house answers, only on this page (operator projection `answers`).
  const board = h('ol', { class: 'op-slots' }, (g.answers || []).map((a, i) => h('li', { class: 'op-slot' + (a.revealed ? ' revealed' : '') },
    h('span', { class: 'slot-num', text: String(i + 1) }),
    h('div', { class: 'op-slot-body' },
      h('span', { class: 'slot-text', dir: 'auto', text: a.text }),
      a.aliases && a.aliases.length ? h('span', { class: 'aliases small', dir: 'auto', text: 'Also: ' + a.aliases.join(', ') }) : null),
    h('span', { class: 'slot-pts', text: String(a.points) }),
    a.revealed ? h('span', { class: 'badge-good', text: 'Shown' })
      : h('button', { class: 'btn btn-sec btn-sm', type: 'button', 'aria-label': (over ? 'Show answer ' : 'Reveal answer ') + (i + 1) + ': ' + a.text + (over ? ' (no points)' : ''),
        onclick: e => act('feud.reveal', { answerId: a.id }, e.currentTarget, { round: rid }), text: over ? 'Show' : 'Reveal' }))));

  const buzzerCtl = h('div', { class: 'buzz-ctl' },
    h('div', { class: 'buzz-state' },
      h('span', { class: 'eyebrow', text: 'Buzzer' }),
      h('strong', { text: phase === 'buzzer_open' ? 'OPEN – waiting for a buzz' : winner ? playerName(snap, winner) + ' buzzed first' : 'Closed' })),
    h('div', { class: 'row-actions' },
      h('button', { class: 'btn btn-pri', type: 'button', disabled: phase !== 'closed' || !!winner, onclick: e => act('feud.open_buzzer', {}, e.currentTarget, { round: rid }), text: 'Open buzzer' }),
      h('button', { class: 'btn btn-ghost', type: 'button', disabled: over,
        onclick: winner ? confirmThen('Clear ' + playerName(snap, winner) + '’s buzz and close the buzzer?', btn => act('feud.reset_buzzer', {}, btn, { round: rid }), { confirmLabel: 'Reset buzzer', danger: true, roundId: rid, action: 'Reset buzzer' })
          : e => act('feud.reset_buzzer', {}, e.currentTarget, { round: rid }), text: 'Reset buzzer' })));

  const strikeCtl = h('div', { class: 'strike-ctl' },
    strikesRow(strikes, { max }),
    h('button', { class: 'btn btn-danger', type: 'button', disabled: !(phase === 'answering' || phase === 'steal') || strikes >= max, onclick: e => act('feud.strike', {}, e.currentTarget, { round: rid }), text: 'Strike ✕' }));

  const stealCtl = strikes >= max && phase === 'answering' ? h('div', { class: 'steal-ctl stack' },
    h('div', { class: 'eyebrow', text: 'Steal' }),
    ...playerSelect('steal-player', 'Who gets the steal?', ui.feudSteal || '', v => { ui.feudSteal = v; }, g.control),
    h('button', { class: 'btn btn-sec', type: 'button', onclick: e => {
      const pid = ui.feudSteal;
      if (!pid) { toast('Choose who gets the steal.'); return; }
      act('feud.steal', { playerId: pid }, e.currentTarget, { round: rid });
    }, text: 'Give steal chance' })) : phase === 'steal' && g.control ? h('p', { class: 'status-line', text: 'Steal: ' + playerName(snap, g.control) + (g.stealFrom ? ' (from ' + playerName(snap, g.stealFrom) + ')' : '') }) : null;

  // The server awards the sum of answers revealed when it receives the action. The dialog names
  // that sum; if it changes while the dialog is open (another reveal), the dialog is cancelled
  // unsent so the operator confirms the new number. There is no server-side expected-points check.
  const awardCtl = !over ? h('div', { class: 'award-ctl stack' },
    h('div', { class: 'eyebrow', text: 'Award round points' }),
    ...playerSelect('award-player', 'Give the current ' + points + ' points to', awardTarget, v => { ui.feudAward = v; }),
    h('button', { class: 'btn btn-good', type: 'button', 'aria-label': awardTarget ? 'Award the current ' + points + ' points to ' + playerName(snap, awardTarget) : 'Award the current ' + points + ' points',
      onclick: async e => {
        const btn = e.currentTarget;
        const pid = ui.feudAward || g.control;
        if (!pid) { toast('Choose who gets the points.'); return; }
        // Player, round and the points named are fixed before the dialog opens.
        const named = points;
        const label = 'Award the current ' + named + ' points to ' + playerName(snap, pid);
        const ok = await confirmDialog(label + '? This ends the round.', { confirmLabel: 'Award points', roundId: rid, action: label,
          stillValid: () => (snap && snap.game && snap.game.roundPhase !== 'awarded' && (snap.game.roundPoints || 0) === named) || 'Round points changed.' });
        if (ok) act('feud.award', { playerId: pid }, btn, { round: rid });
      }, text: 'Award ' + points + ' pts' })) : null;

  const next = over ? (g.boardsRemaining === 0
    ? h('p', { class: 'muted small', text: 'That was the last question. End the game to show the final scores.' })
    : h('button', { class: 'btn btn-pri btn-lg', type: 'button', onclick: e => act('round.next', {}, e.currentTarget, { round: rid }), text: 'Next question' })) : null;

  return h('section', { class: 'card stack feud-op' },
    h('div', { class: 'panel-head' }, h('span', { class: 'house-label', text: g.label || 'House answers' }), h('span', { class: 'pill', text: 'Only you see the full board' })),
    h('p', { class: 'feud-q', dir: 'auto', text: g.question }),
    h('p', { class: 'status-line', text: feudStatusLine(snap) }),
    board,
    h('div', { class: 'bank-line' }, h('span', { class: 'eyebrow', text: 'Round points' }), h('span', { class: 'bank-num', text: String(points) })),
    buzzerCtl, strikeCtl, stealCtl, awardCtl, next);
}

// ---- Impostor ------------------------------------------------------------------------------

function impostorPanel() {
  const g = snap.game || {};
  const rid = snap.roundId;
  const phase = g.roundPhase;
  const votes = g.votes || {};
  const tally = g.tally || {};
  const order = g.order || [];
  const votedCount = Object.keys(votes).length;
  const needed = g.votesNeeded || order.length;

  const voteRows = h('ul', { class: 'who-list' }, order.map(pid => h('li', { class: 'who-row' },
    h('div', { class: 'who-player' },
      h('span', { class: 'who-name', dir: 'auto', text: playerName(snap, pid) }),
      votes[pid] !== undefined ? h('span', { class: 'badge-good', text: 'Voted' }) : null),
    h('div', { class: 'who-char' }, h('span', { class: 'char-label', text: String(tally[pid] || 0) + ' vote' + ((tally[pid] || 0) === 1 ? '' : 's') })))));

  let body;
  if (phase === 'discussion') {
    body = h('div', { class: 'stack' },
      h('p', { class: 'muted small', text: 'Players are discussing out loud. Open voting once they are ready to accuse someone.' }),
      h('button', { class: 'btn btn-pri btn-lg', type: 'button', onclick: e => act('impostor.advance', {}, e.currentTarget, { round: rid }), text: 'Open voting' }));
  } else if (phase === 'vote') {
    body = h('div', { class: 'stack' },
      h('p', { class: 'muted small', text: votedCount + ' of ' + needed + ' players have voted.' }),
      voteRows,
      h('button', { class: 'btn btn-sec', type: 'button', onclick: e => act('impostor.advance', {}, e.currentTarget, { round: rid }), text: 'Close voting now' }));
  } else if (phase === 'guess_word') {
    body = h('div', { class: 'stack' },
      h('p', { class: 'status-line', dir: 'auto', text: playerName(snap, g.nominee) + ' was caught! Waiting for their word guess.' }),
      h('button', { class: 'btn btn-ghost', type: 'button',
        onclick: confirmThen('End the guess now? The impostor hasn’t answered, so crew gets the points.', btn => act('impostor.advance', {}, btn, { round: rid }), { confirmLabel: 'Forfeit guess', roundId: rid, action: 'Forfeit guess' }),
        text: 'Forfeit (no guess)' }));
  } else {
    // reveal
    const reveal = g.reveal || {};
    const pts = g.roundPoints || {};
    const remaining = g.roundsRemaining;
    const nextBtn = remaining === 0
      ? h('p', { class: 'muted small', text: 'No words left for another round. End the game to show the final scores.' })
      : h('button', { class: 'btn btn-pri btn-lg', type: 'button', onclick: e => act('round.next', {}, e.currentTarget, { round: rid }), text: 'Next round' });
    body = h('div', { class: 'stack' },
      h('p', { class: 'status-line', text: IMPOSTOR_OUTCOMES[g.outcome] || 'Round over.' }),
      reveal.impostor ? h('p', { dir: 'auto', text: playerName(snap, reveal.impostor) + ' was the impostor.' }) : null,
      reveal.word ? h('p', { dir: 'auto', text: 'The word was “' + reveal.word + '”.' }) : null,
      h('ul', { class: 'who-list' }, order.map(pid => h('li', { class: 'who-row' },
        h('span', { class: 'who-name', dir: 'auto', text: playerName(snap, pid) }),
        h('span', { class: 'who-char', text: '+' + (pts[pid] || 0) })))),
      nextBtn);
  }

  return h('section', { class: 'card stack' },
    h('div', { class: 'panel-head' },
      h('h2', { class: 'card-title', text: 'Impostor' }),
      h('span', { class: 'pill', text: phase === 'discussion' ? 'Discussion' : phase === 'vote' ? 'Voting' : phase === 'guess_word' ? 'Guessing' : 'Reveal' })),
    body);
}

// Ended is final for this build (changing game needs a new room). "Start a new room" closes this
// one on the server first, so its code stops working, then forgets it on this device.
function ended() {
  return h('div', { class: 'stack' },
    h('h1', { class: 'mode-title', text: 'Game over' }),
    scoreboard(snap, { title: 'Final scores' }),
    h('p', { class: 'muted small', text: 'To play again or switch games, start a new room. Room ' + snap.code + ' will be closed.' }),
    h('button', { class: 'btn btn-pri btn-lg', type: 'button', onclick: e => withBusy(e.currentTarget, async () => {
      try { await transport.act(code, token, 'room.close', {}); } catch { /* the room expires on its own */ }
      leave();
    }), text: 'Start a new room' }));
}

(async () => {
  try {
    transport = await makeTransport();
  } catch (e) {
    mount(app, h('main', { class: 'page center' }, h('p', { class: 'form-error', role: 'alert', text: describeError(e.code) })));
    return;
  }
  const c = normalizeCode(hashCode());
  if (isCodeShape(c) && loadCredential('operator', c, isSim)) start(c);
  else renderStart(c && isCodeShape(c) ? 'This device has no host access for room ' + c + '.' : undefined);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible' && stream && document.body.dataset.conn !== 'live') stream.kick(); });
})();
