// Majlis Party player (phone) interface. Identity comes only from the player token the server
// issued on join. The page never decides who buzzed first and never holds the player's own
// Who Am I character before the reveal: the server leaves it out of this role's projection.
import { h, mount, makeTransport, simRibbon, connBanner, toast, describeError, pageUrl, withBusy, playerName, params, vibrate, isSim, preserveFocus, confirmDialog, setRetryHandler, announce, TERMINAL_ROOM, TERMINAL_SEAT } from './ui.js';
import { saveCredential, loadCredential, clearCredential, lastRoom, normalizeCode, isCodeShape } from './session.js';
import { modeInfo } from './modes.js';
import { playerChips, scoreboard, modeHeading, feudBoard, feudStatusLine, pausedOverlay, FeudChanges, announceFeudChanges } from './views.js';

const app = document.getElementById('app');
const simEl = document.getElementById('sim');
if (simEl) mount(simEl, simRibbon());

let transport, code = null, cred = null, stream = null, snap = null;
const local = { buzzRound: null, buzzSending: false, guessDraft: '', guessError: '', lastWin: null, lastGuess: undefined, guessRound: null };
const feudChanges = new FeudChanges();

// `quiet`: refusal codes the screen already explains itself, so no toast is shown for them.
async function act(type, payload, btn, quiet) {
  return withBusy(btn, async () => {
    try {
      const r = await transport.act(code, cred.token, type, payload, snap ? snap.roundId : undefined);
      if (!r.accepted && !(quiet && quiet.includes(r.error))) toast(describeError(r.error), 'error');
      return r;
    } catch (e) {
      toast(describeError(e.code), 'error');
      return { accepted: false, error: e.code };
    }
  });
}

// ---- join / resume --------------------------------------------------------------------

function renderJoin({ error, code: presetCode, name } = {}) {
  const fromUrl = normalizeCode(new URLSearchParams(location.search).get('code')); // live URL: leave() clears it
  const codeIn = h('input', { id: 'join-code', name: 'code', inputmode: 'text', autocapitalize: 'characters', autocomplete: 'off', spellcheck: 'false',
    maxlength: 6, required: true, pattern: '[A-Za-z0-9]{6}', value: presetCode || fromUrl || '', 'aria-describedby': 'code-help', class: 'code-input' });
  const nameIn = h('input', { id: 'join-name', name: 'name', maxlength: 24, required: true, autocomplete: 'nickname', dir: 'auto', value: name || '' });
  const btn = h('button', { class: 'btn btn-pri btn-lg', type: 'submit', text: 'Join game' });
  const errEl = h('p', { class: 'form-error', role: 'alert', id: 'join-error', text: error || '' });
  const form = h('form', { class: 'card stack', novalidate: true, onsubmit: async (e) => {
    e.preventDefault();
    const c = normalizeCode(codeIn.value), n = nameIn.value.replace(/\s+/g, ' ').trim();
    if (!isCodeShape(c)) { errEl.textContent = 'Room codes have 6 letters or numbers.'; codeIn.focus(); return; }
    // This phone already has a seat in that room: go back to it rather than joining twice.
    if (loadCredential('player', c, isSim)) { enter(c); return; }
    if (!n || n.length > 24) { errEl.textContent = describeError('invalid_name'); nameIn.focus(); return; }
    await withBusy(btn, async () => {
      try {
        const r = await transport.join(c, n);
        saveCredential('player', c, { token: r.playerToken, playerId: r.playerId, name: n }, isSim);
        enter(c);
      } catch (err) {
        errEl.textContent = describeError(err.code);
      }
    });
  } },
    h('h1', { class: 'card-title', text: 'Join the party' }),
    h('label', { for: 'join-code', text: 'Room code' }), codeIn,
    h('p', { class: 'muted small', id: 'code-help', text: 'Shown on the host’s screen.' }),
    h('label', { for: 'join-name', text: 'Your name' }), nameIn,
    errEl, btn);
  const resume = lastRoom('player', isSim);
  mount(app,
    h('header', { class: 'topbar' }, h('a', { class: 'brand', href: pageUrl('index.html'), text: 'Majlis Party' })),
    h('main', { class: 'page narrow stack' },
      resume && resume !== (presetCode || fromUrl) ? h('button', { class: 'btn btn-sec', type: 'button', onclick: () => enter(resume), text: 'Back to room ' + resume }) : null,
      form));
  ((presetCode || fromUrl) ? nameIn : codeIn).focus();
}

function enter(c) {
  code = c;
  cred = loadCredential('player', code, isSim);
  if (!cred) { renderJoin({ code }); return; }
  history.replaceState(null, '', pageUrl('play.html', { code }));
  mount(app, h('main', { class: 'page center' }, h('p', { class: 'muted', role: 'status', text: 'Connecting…' })));
  if (stream) stream.close();
  setRetryHandler(() => stream && stream.kick());
  stream = transport.subscribe(code, cred.token, {
    onSnapshot: (s) => {
      if (s.phase === 'closed') { closedByHost(); return; }
      snap = s; render();
      // Ended is terminal: keep the final scores and stop reconnecting.
      if (s.phase === 'ended' && stream) { stream.close(); stream = null; connBanner('live'); }
    },
    onStatus: (st, err) => {
      const c = err && err.code;
      if (st === 'fatal' && c === 'room_closed') { closedByHost(); return; }
      connBanner(st, c);
      if (st === 'fatal' && !(snap && snap.phase === 'ended')) {
        const name = cred && cred.name;
        // Only a code that can never work again drops this phone's seat.
        if (TERMINAL_ROOM.has(c) || TERMINAL_SEAT.has(c)) clearCredential('player', code, isSim);
        stream = null; snap = null;
        connBanner('live');
        renderJoin({ error: describeError(c), code: TERMINAL_ROOM.has(c) ? '' : code, name });
      }
    },
  });
}

// Leaving the view keeps this phone's seat (its token stays on this device), so "Back to room"
// resumes it. The server keeps the seat and the name; there is no way to free them here.
function leave() {
  if (stream) stream.close();
  stream = null; snap = null; cred = null;
  connBanner('live');
  history.replaceState(null, '', pageUrl('play.html'));
  renderJoin();
}

// The host closed the room: nothing can be done in it any more, so its token is dropped.
function closedByHost() {
  const c = code;
  if (stream) stream.close();
  stream = null; snap = null; cred = null;
  clearCredential('player', c, isSim);
  connBanner('live');
  history.replaceState(null, '', pageUrl('play.html'));
  mount(app,
    h('header', { class: 'topbar' }, h('a', { class: 'brand', href: pageUrl('index.html'), text: 'Majlis Party' })),
    h('main', { class: 'page narrow stack' },
      h('section', { class: 'card stack center-text', role: 'alert' },
        h('h1', { class: 'card-title', text: 'Room closed' }),
        h('p', { text: 'The host closed room ' + c + '. Thanks for playing!' }),
        h('button', { class: 'btn btn-pri', type: 'button', onclick: () => renderJoin(), text: 'Join another room' }))));
}

function myScore() {
  const me = snap.self && (snap.players || []).find(p => p.id === snap.self.id);
  return me ? me.score : 0;
}

// ---- render ----------------------------------------------------------------------------

function header() {
  return h('header', { class: 'topbar' },
    h('span', { class: 'brand', text: 'Majlis Party' }),
    h('span', { class: 'code-chip', 'aria-label': 'Room code ' + snap.code, text: snap.code }),
    h('span', { class: 'me-chip', dir: 'auto', text: snap.self ? snap.self.name + ' · ' + myScore() : '' }));
}

function render() {
  if (!snap) return;
  let body;
  if (snap.phase === 'lobby') body = lobby();
  else if (snap.phase === 'ended') body = ended();
  else if (snap.mode === 'who_am_i') body = whoAmI();
  else if (snap.mode === 'feud') body = feud();
  else body = h('p', { text: 'This game is not playable yet.' });
  const paused = snap.phase === 'paused';
  const top = header(), main = h('main', { class: 'page narrow pl-page', inert: paused }, body);
  if (paused) top.inert = true; // nothing behind the pause notice is operable
  preserveFocus(app, () => mount(app, top, main, paused ? pausedOverlay() : null));
  trackBuzzSpace();
}

// --buzz-space follows the sticky buzzer's real height so scrolling and focus keep answers and
// scores clear of it (fallback in CSS when ResizeObserver is missing).
let buzzObserver = null, buzzWatched = null;
function trackBuzzSpace() {
  const wrap = app.querySelector('.buzz-wrap');
  if (wrap === buzzWatched || typeof ResizeObserver === 'undefined') return;
  if (!buzzObserver) buzzObserver = new ResizeObserver(() => {
    if (buzzWatched && buzzWatched.isConnected) document.documentElement.style.setProperty('--buzz-space', Math.ceil(buzzWatched.getBoundingClientRect().height) + 'px');
  });
  if (buzzWatched) buzzObserver.unobserve(buzzWatched);
  buzzWatched = wrap;
  if (wrap) buzzObserver.observe(wrap);
}

function lobby() {
  const m = snap.mode ? modeInfo(snap.mode) : null;
  return h('div', { class: 'stack' },
    h('section', { class: 'card center-text' },
      h('div', { class: 'big-emoji', 'aria-hidden': 'true', text: '✓' }),
      h('h1', { class: 'card-title', dir: 'auto', text: 'You’re in, ' + (snap.self ? snap.self.name : '') + '!' }),
      h('p', { class: 'muted', role: 'status', text: m ? 'Next game: ' + m.title + '. Waiting for the host to start…' : 'Waiting for the host to choose a game…' })),
    h('section', { class: 'card' }, h('h2', { class: 'card-title', text: 'In the room' }), playerChips(snap, { highlight: snap.self && snap.self.id })),
    h('button', { class: 'btn btn-ghost', type: 'button', onclick: async () => { if (await confirmDialog('Leave this screen? Your seat stays in the room, and “Back to room” on this phone brings you back.', { confirmLabel: 'Leave', danger: true })) leave(); }, text: 'Leave room' }));
}

function whoAmI() {
  const g = snap.game || {};
  const me = snap.self ? snap.self.id : null;
  const you = g.you || {};
  const solved = new Set(g.solved || []);
  const pending = new Set(g.pendingGuesses || []);
  const revealedSet = new Set(g.revealed || []);
  const ready = new Set(g.ready || []);
  const asking = g.roundPhase === 'asking';
  const assignments = g.assignments || {};
  // Own card: rendered from the projection only. `you.assignment` exists only after this player
  // is revealed; before that the server sends nothing about it.
  const mine = you.assignment || null;
  // A pending guess that disappears without a reveal was cleared by the host as wrong.
  if (local.guessRound !== snap.roundId) { local.guessRound = snap.roundId; local.lastGuess = undefined; local.guessError = ''; }
  const myGuess = you.guess === undefined ? null : you.guess;
  if (local.lastGuess !== undefined && local.lastGuess !== null && myGuess === null && !you.revealed && !you.solved) {
    announce('Not quite. The host cleared your guess. Try again.', { assertive: true });
    local.guessError = 'Not quite – the host cleared your guess. Try again.';
  }
  if (myGuess !== null) local.guessError = '';
  local.lastGuess = myGuess;
  const myCard = h('section', { class: 'card my-card' + (mine ? ' revealed' : ''), 'aria-label': 'Your character' },
    h('div', { class: 'eyebrow', text: 'Your character' }),
    mine
      ? [h('div', { class: 'char-big', dir: 'auto', text: mine.label }), mine.ar ? h('div', { class: 'char-ar', lang: 'ar', dir: 'rtl', text: mine.ar }) : null,
        you.solved ? h('p', { class: 'status-line', text: 'You got it!' }) : null]
      : [h('div', { class: 'mystery', 'aria-hidden': 'true', text: '?' }), h('p', { class: 'muted', text: 'Hidden from you. Ask yes/no questions to work it out.' })]);
  const others = (g.order || Object.keys(assignments)).filter(pid => pid !== me && assignments[pid]);
  const list = h('ul', { class: 'who-cards' }, others.map(pid => h('li', { class: 'who-card' + (solved.has(pid) ? ' solved' : '') },
    h('span', { class: 'who-name', dir: 'auto', text: playerName(snap, pid) }),
    h('span', { class: 'char-label', dir: 'auto', text: assignments[pid].label }),
    assignments[pid].ar ? h('span', { class: 'char-ar', lang: 'ar', dir: 'rtl', text: assignments[pid].ar }) : null,
    solved.has(pid) ? h('span', { class: 'badge-good', text: 'Solved' }) : pending.has(pid) ? h('span', { class: 'badge-warn', text: 'Guessing' })
      : ready.has(pid) && !revealedSet.has(pid) ? h('span', { class: 'badge-later', text: 'Ready' }) : null)));
  let guessUi = null;
  if (asking && !you.revealed && !you.solved) {
    // The guess is free text (1-80 characters) that only the host and this player ever see.
    // Sending a new one replaces the pending one.
    const inp = h('input', { id: 'guess', maxlength: 80, required: true, dir: 'auto', autocomplete: 'off', placeholder: 'I think I am…', value: local.guessDraft,
      'aria-describedby': 'guess-help guess-error', oninput: e => { local.guessDraft = e.target.value; } });
    const btn = h('button', { class: 'btn btn-pri', type: 'submit', text: myGuess !== null ? 'Change guess' : 'Send guess' });
    const errEl = h('p', { class: 'form-error', id: 'guess-error', role: 'alert', text: local.guessError });
    const form = h('form', { class: 'guess-form', novalidate: true, onsubmit: async e => {
      e.preventDefault();
      const text = inp.value.replace(/\s+/g, ' ').trim();
      if (!text) { local.guessError = 'Type who you think you are first.'; errEl.textContent = local.guessError; inp.focus(); return; }
      const r = await act('whoami.guess', { text }, btn);
      if (r && r.accepted) { local.guessDraft = ''; local.guessError = ''; }
    } }, h('label', { for: 'guess', class: 'small', text: 'Your guess' }), inp, btn);
    const readyBtn = h('button', { class: 'btn btn-ghost', type: 'button', 'aria-pressed': you.ready ? 'true' : 'false',
      onclick: e => act('whoami.ready', { ready: !you.ready }, e.currentTarget), text: you.ready ? 'Ready ✓ (tap to undo)' : 'I’m ready' });
    guessUi = h('section', { class: 'card stack guess-card' },
      myGuess !== null ? h('p', { class: 'status-line', role: 'status', dir: 'auto', text: 'Your guess “' + myGuess + '” is with the host. Say it out loud!' }) : null,
      form, errEl,
      h('p', { class: 'muted small', id: 'guess-help', text: 'Only the host sees your guess. “I’m ready” just tells the host you are ready; it sends no guess.' }),
      readyBtn);
  }
  return h('div', { class: 'stack' },
    modeHeading(snap),
    myCard,
    guessUi,
    h('section', { class: 'card' },
      h('h2', { class: 'card-title', text: asking ? 'Everyone else' : 'Everyone’s characters' }),
      others.length ? list : h('p', { class: 'muted', text: 'Waiting for characters…' })),
    scoreboard(snap, { selfId: me }));
}

async function sendBuzz() {
  if (local.buzzSending) return;
  local.buzzSending = true; vibrate(20); render();
  // Losing a race is not an error: the buzzer itself turns into "<winner> buzzed first" (and it is
  // announced), so a toast would only linger and could contradict the next round's result.
  await act('feud.buzz', {}, undefined, ['buzzer_taken', 'buzzer_closed']);
  local.buzzSending = false;
  render();
}

function feud() {
  const g = snap.game || {};
  const me = snap.self ? snap.self.id : null;
  if (local.buzzRound !== snap.roundId) { local.buzzRound = snap.roundId; local.buzzSending = false; }
  const winner = g.buzzer && g.buzzer.winner;
  let state, label, sub;
  if (local.buzzSending && !winner) { state = 'sending'; label = 'Sending…'; sub = 'Waiting for the server to decide who was first.'; }
  else if (winner === me) { state = 'won'; label = 'You buzzed first!'; sub = 'Say your answer out loud.'; }
  else if (winner) { state = 'lost'; label = playerName(snap, winner); sub = 'buzzed first.'; }
  else if (g.roundPhase === 'buzzer_open' && g.buzzer && g.buzzer.open) { state = 'open'; label = 'BUZZ!'; sub = 'Tap as fast as you can.'; }
  else { state = 'locked'; label = 'Buzzer locked'; sub = g.roundPhase === 'awarded' ? 'Round over.' : 'Wait for the host to open it.'; }
  // During a steal the face follows control, not the original buzz winner.
  if (g.roundPhase === 'steal' && g.control) {
    if (g.control === me) { state = 'won'; label = 'Steal!'; sub = 'Your chance to steal! Say your answer.'; }
    else { state = 'lost'; label = playerName(snap, g.control); sub = 'is trying to steal.'; }
  }
  const buzz = h('button', { id: 'buzzer', class: 'buzzer state-' + state, type: 'button', disabled: state !== 'open', 'aria-describedby': 'buzz-sub', 'aria-busy': state === 'sending' ? 'true' : null,
    // Buzz on press, not on release: a snapshot that arrives while the finger is down re-renders
    // this button, and a release on a replaced node never becomes a click, so a click-only buzzer
    // can silently drop a tap that was in time. Keyboard and assistive-technology activation still
    // arrive as a click; a click right after a press is the same tap and is ignored.
    onpointerdown: e => { if (e.isPrimary && e.button === 0) { local.buzzPressAt = performance.now(); sendBuzz(); } },
    onclick: () => { if (performance.now() - (local.buzzPressAt || -1e9) > 1500) sendBuzz(); },
  }, h('span', { class: 'buzz-label', dir: 'auto', text: label }));
  // Announce and vibrate once per server-decided winner (never for a local guess).
  const winKey = winner ? snap.roundId + ':' + winner : null;
  if (winKey && local.lastWin !== winKey) {
    const first = local.lastWin === null && !local.buzzSeen;
    local.lastWin = winKey;
    if (!first) {
      if (winner === me) { vibrate([40, 50, 40]); announce('You buzzed first! Say your answer.', { assertive: true }); }
      else { vibrate(20); announce(playerName(snap, winner) + ' buzzed first.', { assertive: true }); }
    }
  }
  local.buzzSeen = true;
  const changes = feudChanges.diff(snap);
  announceFeudChanges(changes, g, announce);
  return h('div', { class: 'stack pl-feud' },
    h('div', { class: 'stack feud-content' },
      modeHeading(snap),
      feudBoard(g, { changes }),
      h('p', { class: 'status-line', text: feudStatusLine(snap) }),
      scoreboard(snap, { selfId: me })),
    h('div', { class: 'buzz-wrap' }, buzz, h('p', { id: 'buzz-sub', class: 'buzz-sub', dir: 'auto', text: sub })));
}

function ended() {
  return h('div', { class: 'stack' },
    h('h1', { class: 'mode-title', text: 'Game over' }),
    snap.mode === 'who_am_i' && snap.game ? whoAmIRevealList() : null,
    scoreboard(snap, { title: 'Final scores', selfId: snap.self && snap.self.id }),
    h('button', { class: 'btn btn-pri', type: 'button', onclick: leave, text: 'Done' }));
}

function whoAmIRevealList() {
  const g = snap.game;
  const me = snap.self && snap.self.id;
  const you = g.you || {};
  const assignments = g.assignments || {};
  const items = (g.order || []).map(pid => pid === me ? (you.assignment ? [pid, you.assignment] : null) : assignments[pid] ? [pid, assignments[pid]] : null).filter(Boolean);
  return h('section', { class: 'card' }, h('h2', { class: 'card-title', text: 'Characters' }),
    h('ul', { class: 'who-cards' }, items.map(([pid, a]) => h('li', { class: 'who-card' },
      h('span', { class: 'who-name', dir: 'auto', text: playerName(snap, pid) + (pid === me ? ' (you)' : '') }), h('span', { class: 'char-label', dir: 'auto', text: a.label })))));
}

(async () => {
  try {
    transport = await makeTransport();
  } catch (e) {
    mount(app, h('main', { class: 'page center' }, h('p', { class: 'form-error', role: 'alert', text: describeError(e.code) })));
    return;
  }
  const c = normalizeCode(params.get('code'));
  if (isCodeShape(c) && loadCredential('player', c, isSim)) enter(c);
  else renderJoin();
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible' && stream && document.body.dataset.conn !== 'live') stream.kick(); });
})();
