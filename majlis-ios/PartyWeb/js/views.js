// Majlis Party: view pieces shared by operator, player and TV. Pure render from a role-projected
// snapshot; no view ever derives hidden information or decides a winner locally.
import { h, sortedScores, playerName, joinUrl } from './ui.js';
import { encodeText, toSvg } from './qr.js';
import { modeInfo } from './modes.js';

export function qrBlock(code, { size = 'md' } = {}) {
  const url = joinUrl(code);
  const holder = h('div', { class: 'qr qr-' + size, 'data-join-url': url });
  // Our own encoder output; the SVG contains only path data, no external or user markup.
  holder.innerHTML = toSvg(encodeText(url, 'M'), { title: 'QR code to join room ' + code });
  return holder;
}

export function codeBlock(code, { label = 'Room code' } = {}) {
  return h('div', { class: 'code-block' },
    h('div', { class: 'eyebrow', text: label }),
    h('div', { class: 'room-code', 'aria-label': 'Room code ' + code.split('').join(' '), text: code }));
}

export function playerChips(snap, { showScores = false, highlight } = {}) {
  const players = snap.players || [];
  if (!players.length) return h('p', { class: 'muted', text: 'No players yet. Share the code or QR.' });
  return h('ul', { class: 'chips', 'aria-label': 'Players' },
    players.map(p => h('li', { class: 'chip' + (p.connected ? '' : ' offline') + (highlight === p.id ? ' hl' : '') },
      h('span', { class: 'dot', 'aria-hidden': 'true' }),
      h('span', { class: 'chip-name', dir: 'auto', text: p.name }),
      showScores ? h('span', { class: 'chip-score', text: String(p.score) }) : null,
      p.connected ? null : h('span', { class: 'sr-only', text: '(disconnected)' }))));
}

export function scoreboard(snap, { title = 'Scores', selfId } = {}) {
  const rows = sortedScores(snap);
  return h('section', { class: 'card scores', 'aria-label': title },
    h('h2', { class: 'card-title', text: title }),
    h('ol', { class: 'score-list' },
      rows.map((p, i) => h('li', { class: 'score-row' + (p.id === selfId ? ' me' : '') + (p.connected ? '' : ' offline') },
        h('span', { class: 'rank', text: String(i + 1) }),
        h('span', { class: 'sname', dir: 'auto', text: p.name + (p.id === selfId ? ' (you)' : '') }),
        h('span', { class: 'spts', text: String(p.score) })))));
}

export function modeHeading(snap) {
  const m = modeInfo(snap.mode);
  return h('div', { class: 'mode-heading' },
    h('h1', { class: 'mode-title', text: m.title }),
    m.ar ? h('span', { class: 'mode-ar', lang: 'ar', dir: 'rtl', text: m.ar }) : null,
    roundLabel(snap) ? h('span', { class: 'pill', text: roundLabel(snap) }) : null);
}

// Human round label from the projection (the opaque roundId is never shown).
export function roundLabel(snap) {
  const g = snap.game || {};
  if (snap.mode === 'feud' && g.board) return 'Question ' + g.board + ' of ' + g.boardCount;
  if (snap.mode === 'who_am_i' && g.round) return 'Round ' + g.round;
  if (snap.mode === 'impostor' && g.round) return 'Round ' + g.round;
  return '';
}

// Plain wording for impostor round outcomes (party/server/modes/impostor.py); shared by the
// operator, player and TV views so the three pages never disagree on what happened.
export const IMPOSTOR_OUTCOMES = {
  impostor_survived: 'Nobody caught the impostor.',
  impostor_guessed_word: 'The impostor guessed the word!',
  crew_wrong_guess: 'The impostor guessed wrong.',
  crew_forfeit: 'The host ended the guess.',
  aborted: 'The round ended early.',
};

export function strikesRow(n, { big = false, fresh = false, max = 3 } = {}) {
  return h('div', { class: 'strikes' + (big ? ' big' : ''), role: 'img', 'aria-label': 'Strike ' + n + ' of ' + max },
    Array.from({ length: max }, (_, i) => i).map(i => h('span', { class: 'strike' + (i < n ? ' on' : '') + (fresh && i === n - 1 ? ' fresh' : ''), 'aria-hidden': 'true', text: '✕' })));
}

// Tracks what this page has already shown so only NEW reveals and strikes animate and are
// announced. The first snapshot of a round (initial load or reconnect) never animates.
export class FeudChanges {
  constructor() { this.round = null; this.revealed = new Set(); this.strikes = 0; }
  diff(snap) {
    const g = snap.game || {};
    const ids = (g.slots || []).filter(s => s.revealed).map(s => String(s.index));
    if (this.round !== snap.roundId) {
      this.round = snap.roundId; this.revealed = new Set(ids); this.strikes = g.strikes || 0;
      return { fresh: new Set(), freshStrike: false, newSlots: [] };
    }
    const fresh = new Set(ids.filter(i => !this.revealed.has(i)));
    const freshStrike = (g.strikes || 0) > this.strikes;
    this.revealed = new Set(ids); this.strikes = g.strikes || 0;
    return { fresh, freshStrike, newSlots: (g.slots || []).filter(s => fresh.has(String(s.index))) };
  }
}

// Feud board for player and TV: only revealed slots carry text, matching the projection.
// Slot `index` is zero-based on the wire; people see 1, 2, 3…
export function feudBoard(game, { tv = false, changes } = {}) {
  const fresh = changes ? changes.fresh : new Set();
  const slots = game.slots || [];
  return h('section', { class: 'feud-board' + (tv ? ' tv' : '') + (slots.length <= 4 ? ' one-col' : ''), 'aria-label': 'Answer board' },
    h('div', { class: 'house-label', text: game.label || 'House answers' }),
    h('p', { class: 'feud-q', dir: 'auto', text: game.question }),
    h('ol', { class: 'slots', style: tv ? '--rows:' + Math.max(1, Math.ceil(slots.length / 2)) : null },
      slots.map(s => s.revealed
        ? h('li', { class: 'slot revealed' + (fresh.has(String(s.index)) ? ' fresh' : '') },
          h('span', { class: 'slot-text', dir: 'auto', text: s.text }), h('span', { class: 'slot-pts', text: String(s.points) }))
        : h('li', { class: 'slot', 'aria-label': 'Answer ' + (s.index + 1) + ', hidden' },
          h('span', { class: 'slot-num', 'aria-hidden': 'true', text: String(s.index + 1) }), h('span', { class: 'slot-q', 'aria-hidden': 'true', text: '?' })))),
    h('div', { class: 'feud-meta' },
      strikesRow(game.strikes || 0, { big: tv, fresh: changes && changes.freshStrike, max: game.maxStrikes || 3 }),
      h('div', { class: 'bank' }, h('span', { class: 'eyebrow', text: 'Round points' }), h('span', { class: 'bank-num', text: String(game.roundPoints || 0) }))));
}

export function announceFeudChanges(changes, game, announce) {
  for (const s of changes.newSlots) announce('Answer ' + (s.index + 1) + ' revealed: ' + s.text + ', ' + s.points + ' points.');
  if (changes.freshStrike) announce('Strike ' + (game.strikes || 0) + ' of ' + (game.maxStrikes || 3) + '.');
}

export function feudStatusLine(snap) {
  const g = snap.game || {};
  const name = id => playerName(snap, id);
  const winner = g.buzzer && g.buzzer.winner;
  switch (g.roundPhase) {
    case 'closed': return 'Get ready – the host will open the buzzer.';
    case 'buzzer_open': return 'Buzzer is OPEN!';
    case 'answering':
      if ((g.strikes || 0) >= (g.maxStrikes || 3)) return 'Three strikes! The host may offer a steal.';
      return g.control ? name(g.control) + (winner === g.control ? ' buzzed first.' : ' is answering.') : 'Answering…';
    case 'steal': return g.control ? name(g.control) + ' is trying to steal' + (g.stealFrom ? ' from ' + name(g.stealFrom) : '') + '.' : 'Steal attempt.';
    case 'awarded': return g.awardedTo ? name(g.awardedTo) + ' won ' + (g.awardedPoints || 0) + ' points this round.' : 'Round over.';
    case 'ended': return 'Game over.';
    default: return '';
  }
}

// Players and the TV get a passive notice. The operator passes `actions`: the panel then becomes a
// modal dialog holding the only reachable controls (the page behind it is made inert by the caller).
export function pausedOverlay({ title = 'Paused', text = 'Paused by operator.', actions = null } = {}) {
  if (!actions) {
    return h('div', { class: 'overlay', role: 'alert' },
      h('div', { class: 'overlay-card' }, h('div', { class: 'overlay-title', text: title }), h('p', { text })));
  }
  // Not aria-modal: the connection banner's Retry (outside this panel) must stay operable.
  return h('div', { class: 'overlay op-pause', id: 'paused-panel' },
    h('div', { class: 'overlay-card', role: 'dialog', tabindex: '-1', 'aria-labelledby': 'paused-title', 'aria-describedby': 'paused-text' },
      h('h2', { class: 'overlay-title', id: 'paused-title', text: title }),
      h('p', { id: 'paused-text', text }),
      h('div', { class: 'op-pause-actions' }, actions)));
}
