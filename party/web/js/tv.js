// Majlis Party TV display: optional, read-only, public. It only renders the display projection,
// which never contains unrevealed Who Am I characters or hidden Feud answers.
// Credential: the display token the server gave the operator at room creation. In this build it
// is handed over only within the same browser (host page -> "Open TV display"); a separate TV
// device needs a pairing step that protocol v1 does not yet define (see party/web/README.md).
import { h, mount, makeTransport, simRibbon, connBanner, describeError, pageUrl, hashCode, playerName, isSim, setRetryHandler, announce, TERMINAL_ROOM, TERMINAL_SEAT } from './ui.js';
import { loadCredential, clearCredential, normalizeCode, isCodeShape } from './session.js';
import { modeInfo } from './modes.js';
import { qrBlock, codeBlock, playerChips, scoreboard, modeHeading, feudBoard, feudStatusLine, pausedOverlay, FeudChanges, announceFeudChanges, IMPOSTOR_OUTCOMES } from './views.js';

const app = document.getElementById('app');
const simEl = document.getElementById('sim');
if (simEl) mount(simEl, simRibbon());

let transport, snap = null;
const feudChanges = new FeudChanges();

function noAccess(message) {
  mount(app, h('main', { class: 'tv-page center' },
    h('section', { class: 'card narrow stack center-text' },
      h('h1', { class: 'card-title', text: 'TV display' }),
      h('p', { text: message }),
      h('p', { class: 'muted', text: 'On the host device, open the room and choose “Open TV display”, then show that window on the TV (HDMI, AirPlay or screen mirroring).' }),
      h('a', { class: 'btn btn-ghost', href: pageUrl('index.html'), text: 'Back' }))));
}

function render() {
  if (!snap) return;
  let body;
  if (snap.phase === 'lobby') body = lobby();
  else if (snap.phase === 'ended') body = ended();
  else if (snap.mode === 'feud') body = feud();
  else if (snap.mode === 'who_am_i') body = whoAmI();
  else if (snap.mode === 'impostor') body = impostor();
  else body = h('p', { text: modeInfo(snap.mode).title });
  mount(app, h('main', { class: 'tv-page' }, body), snap.phase === 'paused' ? pausedOverlay() : null);
}

function lobby() {
  return h('div', { class: 'tv-lobby' },
    h('div', { class: 'tv-join' },
      h('div', { class: 'tv-brand', text: 'Majlis Party' }),
      codeBlock(snap.code, { label: 'Join with code' }),
      qrBlock(snap.code, { size: 'lg' }),
      h('p', { class: 'tv-hint', text: 'Scan with your phone camera, same Wi‑Fi.' })),
    h('div', { class: 'tv-side' },
      h('h2', { class: 'tv-h2', text: snap.mode ? 'Next: ' + modeInfo(snap.mode).title : 'Waiting for the host' }),
      h('h3', { class: 'tv-h3', text: 'Players (' + (snap.players || []).length + ')' }),
      playerChips(snap)));
}

function whoAmI() {
  const g = snap.game || {};
  const solved = new Set(g.solved || []);
  const pending = new Set(g.pendingGuesses || []);
  // The display projection holds only revealed players' characters.
  const assignments = g.assignments || {};
  const asking = g.roundPhase === 'asking';
  return h('div', { class: 'tv-who' },
    modeHeading(snap),
    h('p', { class: 'tv-sub', text: asking ? 'Everyone knows your character except you. Ask yes/no questions!' : 'The characters!' }),
    h('ul', { class: 'tv-who-grid' }, (snap.players || []).map(p => {
      const a = assignments[p.id] || null;
      return h('li', { class: 'tv-who-card' + (solved.has(p.id) ? ' solved' : '') },
        h('span', { class: 'who-name', dir: 'auto', text: p.name }),
        a ? h('span', { class: 'char-label', dir: 'auto', text: a.label }) : h('span', { class: 'mystery-sm', 'aria-hidden': 'true', text: '?' }),
        solved.has(p.id) ? h('span', { class: 'badge-good', text: 'Solved' }) : pending.has(p.id) ? h('span', { class: 'badge-warn', text: 'Guessing' }) : null,
        h('span', { class: 'tv-pts', text: String(p.score) }));
    })));
}

function feud() {
  const g = snap.game || {};
  const winner = g.buzzer && g.buzzer.winner;
  const changes = feudChanges.diff(snap);
  announceFeudChanges(changes, g, announce);
  return h('div', { class: 'tv-feud' },
    h('div', { class: 'tv-feud-main' },
      modeHeading(snap),
      feudBoard(g, { tv: true, changes }),
      h('p', { class: 'tv-status', text: feudStatusLine(snap) })),
    h('aside', { class: 'tv-feud-side' },
      h('div', { class: 'tv-buzz' + (g.buzzer && g.buzzer.open ? ' open' : winner ? ' won' : '') },
        h('span', { class: 'eyebrow', text: 'Buzzer' }),
        h('strong', { dir: 'auto', text: g.buzzer && g.buzzer.open ? 'OPEN' : winner ? playerName(snap, winner) : 'Closed' })),
      scoreboard(snap)));
}

function impostor() {
  const g = snap.game || {};
  const phase = g.roundPhase;
  const tally = g.tally || {};
  const votes = g.votes || {};
  const reveal = g.reveal || {};
  const showTally = phase === 'vote' || phase === 'guess_word' || phase === 'reveal';
  const sub = phase === 'discussion' ? 'Discuss! Who doesn’t have the word?'
    : phase === 'vote' ? Object.keys(votes).length + ' of ' + (g.votesNeeded || (snap.players || []).length) + ' votes are in.'
    : phase === 'guess_word' ? playerName(snap, g.nominee) + ' was caught! Guessing the word…'
    : IMPOSTOR_OUTCOMES[g.outcome] || 'Round over.';
  return h('div', { class: 'tv-who' },
    modeHeading(snap),
    h('p', { class: 'tv-sub', text: sub }),
    h('ul', { class: 'tv-who-grid' }, (g.order || []).map(pid => {
      const p = (snap.players || []).find(x => x.id === pid);
      return h('li', { class: 'tv-who-card' },
        h('span', { class: 'who-name', dir: 'auto', text: playerName(snap, pid) }),
        showTally ? h('span', { class: 'char-label', text: String(tally[pid] || 0) + ' vote' + ((tally[pid] || 0) === 1 ? '' : 's') }) : null,
        h('span', { class: 'tv-pts', text: String(p ? p.score : 0) }));
    })),
    reveal.word ? h('p', { class: 'tv-sub', dir: 'auto', text: 'The word was “' + reveal.word + '”' + (reveal.impostor ? ', ' + playerName(snap, reveal.impostor) + ' was the impostor.' : '.') }) : null);
}

function ended() {
  return h('div', { class: 'tv-ended' }, h('h1', { class: 'tv-title', text: 'Final scores' }), scoreboard(snap, { title: 'Final scores' }));
}

(async () => {
  try {
    transport = await makeTransport();
  } catch (e) {
    noAccess(describeError(e.code));
    return;
  }
  const code = normalizeCode(hashCode());
  // Opening the TV for another room from the host reuses this window with a new fragment.
  window.addEventListener('hashchange', () => { if (normalizeCode(hashCode()) !== code) location.reload(); });
  if (!isCodeShape(code)) { noAccess('No room selected.'); return; }
  const cred = loadCredential('display', code, isSim);
  if (!cred) { noAccess('This screen is not paired with room ' + code + '.'); return; }
  mount(app, h('main', { class: 'tv-page center' }, h('p', { class: 'muted', text: 'Connecting…' })));
  let stream = null;
  setRetryHandler(() => stream && stream.kick());
  stream = transport.subscribe(code, cred.token, {
    onSnapshot: (s) => {
      if (s.phase === 'closed') {
        if (stream) { stream.close(); stream = null; }
        clearCredential('display', code, isSim);
        connBanner('live');
        noAccess('The host closed room ' + code + '.');
        return;
      }
      snap = s; render();
      if (s.phase === 'ended' && stream) { stream.close(); stream = null; connBanner('live'); }
    },
    onStatus: (st, err) => {
      const c = err && err.code;
      connBanner(st, c);
      if (st === 'fatal' && !(snap && snap.phase === 'ended')) {
        if (TERMINAL_ROOM.has(c) || TERMINAL_SEAT.has(c)) clearCredential('display', code, isSim);
        stream = null;
        connBanner('live');
        noAccess(describeError(c));
      }
    },
  });
  // After the game ends the stream is closed and dropped (final scores stay on screen), so a
  // later hide/show must not try to reconnect it.
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible' && document.body.dataset.conn !== 'live' && stream) stream.kick(); });
})();
