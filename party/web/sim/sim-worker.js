// SIMULATED FIXTURE - NOT THE MAJLIS SERVER.
// A bounded in-browser stand-in for the protocol v1 room server, used only to develop and test
// the party/web interfaces before the real Python server (party/server/) is integrated.
// It runs in a SharedWorker so separate tabs in ONE browser can act as operator, players and TV.
// It is not networked, not persistent and not a substitute for real HTTP/SSE or device testing.
// Content below is a tiny TEST fixture, not the authored party/content packs.
'use strict';

const FIXTURE_WHO = [
  { id: 'w1', label: 'Falcon', ar: 'صقر', difficulty: 1 },
  { id: 'w2', label: 'Coffee pot (dallah)', ar: 'دلة', difficulty: 1 },
  { id: 'w3', label: 'Camel', ar: 'جمل', difficulty: 1 },
  { id: 'w4', label: 'Lighthouse', ar: 'منارة', difficulty: 2 },
  { id: 'w5', label: 'Pearl diver', ar: 'غواص', difficulty: 2 },
  { id: 'w6', label: 'Sandstorm', ar: 'عاصفة رملية', difficulty: 2 },
  { id: 'w7', label: 'Astronaut', ar: 'رائد فضاء', difficulty: 1 },
  { id: 'w8', label: 'Date palm', ar: 'نخلة', difficulty: 1 },
  { id: 'w9', label: 'Traffic light', ar: 'إشارة مرور', difficulty: 1 },
  { id: 'w10', label: 'Wind tower (barjeel)', ar: 'برجيل', difficulty: 3 },
  { id: 'w11', label: 'Goalkeeper', ar: 'حارس مرمى', difficulty: 1 },
  { id: 'w12', label: 'Oud', ar: 'عود', difficulty: 2 },
  { id: 'w13', label: 'Penguin', ar: 'بطريق', difficulty: 1 },
  { id: 'w14', label: 'Volcano', ar: 'بركان', difficulty: 2 },
];

const FIXTURE_FEUD = [
  { id: 'f1', question: 'Name something people bring to a desert camping trip', label: 'House answers',
    answers: [
      { id: 'a1', text: 'Tent', aliases: ['tents'], points: 30 },
      { id: 'a2', text: 'Firewood', aliases: ['wood', 'charcoal'], points: 25 },
      { id: 'a3', text: 'Water', aliases: [], points: 20 },
      { id: 'a4', text: 'Blankets', aliases: ['blanket'], points: 15 },
      { id: 'a5', text: 'Torch', aliases: ['flashlight'], points: 8 },
      { id: 'a6', text: 'Portable gas stove with a spare canister', aliases: ['stove'], points: 2 },
    ] },
  { id: 'f2', question: 'Name a dish you would find at a big family lunch', label: 'House answers',
    answers: [
      { id: 'b1', text: 'Machboos', aliases: ['majboos', 'kabsa'], points: 35 },
      { id: 'b2', text: 'Harees', aliases: [], points: 25 },
      { id: 'b3', text: 'Salad', aliases: [], points: 15 },
      { id: 'b4', text: 'Luqaimat', aliases: [], points: 15 },
      { id: 'b5', text: 'Thareed', aliases: ['threed'], points: 7 },
      { id: 'b6', text: 'Grilled hammour with lemon rice and daqoos', aliases: ['fish'], points: 3 },
    ] },
];

const MAX_PLAYERS = 12, MIN_PLAYERS = 3, ACTION_LOG_MAX = 500;
const rooms = new Map();
const subs = new Map(); // subKey -> {port, subId, code, token}
let subSeq = 0;

function rand(n) {
  const b = new Uint8Array(n);
  crypto.getRandomValues(b);
  return b;
}
function token() {
  return btoa(String.fromCharCode(...rand(24))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}
const CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';
function roomCode() {
  for (;;) {
    const c = Array.from(rand(6), b => CODE_ALPHABET[b % CODE_ALPHABET.length]).join('');
    if (!rooms.has(c)) return c;
  }
}
function shuffle(arr) {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = rand(1)[0] % (i + 1);
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

class Fail extends Error {
  constructor(code, status = 400) { super(code); this.code = code; this.status = status; }
}

// ---- modes -------------------------------------------------------------------------------
// The projections below copy the shape of the real server's (party/server/README.md, N01 revised):
// Who Am I `whoami.*`, Feud `feud.*`, keyed assignments, zero-based Feud slot indexes. The rules
// are a sketch; only the real server's behaviour is evidence.

const MODES = {
  who_am_i: {
    prefix: 'whoami.',
    initialize(room) {
      const items = shuffle(FIXTURE_WHO);
      const assignments = {};
      room.players.forEach((p, i) => { assignments[p.id] = items[i % items.length]; });
      return { roundPhase: 'asking', assignments, solved: {}, guesses: {}, ready: {}, revealed: {}, awards: {}, ended: false };
    },
    reduce(room, actor, type, payload) {
      const g = room.game;
      if (g.ended) throw new Fail('invalid_phase', 409);
      if (type === 'whoami.ready') {
        if (actor.role !== 'player') throw new Fail('forbidden', 403);
        if (g.roundPhase !== 'asking') throw new Fail('invalid_phase', 409);
        if (payload.ready === false) delete g.ready[actor.id]; else g.ready[actor.id] = true;
        return;
      }
      if (type === 'whoami.guess') {
        if (actor.role !== 'player') throw new Fail('forbidden', 403);
        const text = String(payload.text || '').replace(/\s+/g, ' ').trim();
        if (!text || text.length > 80) throw new Fail('invalid_payload', 400);
        if (g.revealed[actor.id]) throw new Fail('already_revealed', 409);
        g.guesses[actor.id] = text;
        return;
      }
      if (actor.role !== 'operator') throw new Fail('forbidden', 403);
      const target = payload.playerId;
      const known = id => room.players.some(p => p.id === id);
      if (type === 'whoami.dismiss') {
        if (!known(target)) throw new Fail('unknown_player', 400);
        if (!(target in g.guesses)) throw new Fail('no_pending_guess', 409);
        delete g.guesses[target];
        return;
      }
      if (type === 'whoami.reveal') {
        if (target === undefined) {
          if (room.players.every(p => g.revealed[p.id])) throw new Fail('already_revealed', 409);
          room.players.forEach(p => { g.revealed[p.id] = true; });
        } else {
          if (!known(target)) throw new Fail('unknown_player', 400);
          if (g.revealed[target]) throw new Fail('already_revealed', 409);
          g.revealed[target] = true;
        }
        if (room.players.every(p => g.revealed[p.id])) g.roundPhase = 'revealed';
        return;
      }
      if (type === 'whoami.award') {
        if (!known(target)) throw new Fail('unknown_player', 400);
        if (g.awards[target]) throw new Fail('already_awarded', 409);
        g.awards[target] = true;
        g.solved[target] = true;
        g.revealed[target] = true;
        room.players.find(p => p.id === target).score += 1;
        if (room.players.every(p => g.revealed[p.id])) g.roundPhase = 'revealed';
        return;
      }
      throw new Fail('unknown_action', 400);
    },
    end(room) { room.game.ended = true; room.players.forEach(p => { room.game.revealed[p.id] = true; }); },
    project(room, viewer) {
      const g = room.game;
      const order = room.players.map(p => p.id);
      const pub = a => (a.ar ? { label: a.label, ar: a.ar } : { label: a.label });
      const out = {
        title: 'Who Am I (fixture)', round: room.roundIndex + 1,
        roundPhase: g.ended ? 'ended' : g.roundPhase, order,
        ready: order.filter(id => g.ready[id]), revealed: order.filter(id => g.revealed[id]),
        solved: order.filter(id => g.solved[id]), pendingGuesses: order.filter(id => id in g.guesses),
      };
      if (viewer.role === 'operator') {
        out.guesses = Object.fromEntries(order.filter(id => id in g.guesses).map(id => [id, g.guesses[id]]));
        out.assignments = Object.fromEntries(order.map(id => [id, { ...g.assignments[id] }]));
        out.roundsRemaining = 3;
        out.awardPoints = 1;
      } else if (viewer.role === 'player') {
        out.assignments = Object.fromEntries(order.filter(id => id !== viewer.id).map(id => [id, pub(g.assignments[id])]));
        out.you = { revealed: !!g.revealed[viewer.id], ready: !!g.ready[viewer.id], solved: !!g.solved[viewer.id], guess: viewer.id in g.guesses ? g.guesses[viewer.id] : null };
        if (g.revealed[viewer.id]) out.you.assignment = pub(g.assignments[viewer.id]);
      } else {
        out.assignments = Object.fromEntries(order.filter(id => g.revealed[id]).map(id => [id, pub(g.assignments[id])]));
      }
      return out;
    },
  },

  feud: {
    prefix: 'feud.',
    initialize(room) {
      const board = FIXTURE_FEUD[(room.roundIndex) % FIXTURE_FEUD.length];
      return {
        board, revealed: {}, strikes: 0, buzzer: { open: false, winner: null },
        roundPhase: 'closed', control: null, stealFrom: null, awardedTo: null, awardedPoints: null, ended: false,
      };
    },
    reduce(room, actor, type, payload) {
      const g = room.game;
      if (g.ended) throw new Fail('invalid_phase', 409);
      const sum = () => g.board.answers.filter(a => g.revealed[a.id]).reduce((t, a) => t + a.points, 0);
      if (type === 'feud.buzz') {
        if (actor.role !== 'player') throw new Fail('forbidden', 403);
        if (g.buzzer.winner) throw new Fail('buzzer_taken', 409);
        if (g.roundPhase !== 'buzzer_open') throw new Fail('buzzer_closed', 409);
        g.buzzer = { open: false, winner: actor.id };
        g.control = actor.id;
        g.roundPhase = 'answering';
        return;
      }
      if (actor.role !== 'operator') throw new Fail('forbidden', 403);
      switch (type) {
        case 'feud.open_buzzer':
          if (g.buzzer.winner) throw new Fail('buzzer_taken', 409);
          if (g.roundPhase !== 'closed') throw new Fail('invalid_phase', 409);
          g.buzzer.open = true; g.roundPhase = 'buzzer_open';
          return;
        case 'feud.reset_buzzer':
          if (g.roundPhase === 'awarded') throw new Fail('round_awarded', 409);
          g.buzzer = { open: false, winner: null }; g.control = null; g.stealFrom = null; g.roundPhase = 'closed';
          return;
        case 'feud.reveal': {
          const a = g.board.answers.find(x => x.id === payload.answerId);
          if (!a) throw new Fail('unknown_answer', 400);
          if (g.revealed[a.id]) throw new Fail('already_revealed', 409);
          g.revealed[a.id] = true;
          return;
        }
        case 'feud.strike':
          if (g.roundPhase !== 'answering' && g.roundPhase !== 'steal') throw new Fail('invalid_phase', 409);
          if (g.strikes >= 3) throw new Fail('max_strikes', 409);
          g.strikes += 1;
          return;
        case 'feud.steal': {
          const p = room.players.find(x => x.id === payload.playerId);
          if (!p) throw new Fail('unknown_player', 400);
          if (g.roundPhase !== 'answering') throw new Fail('invalid_phase', 409);
          if (g.strikes < 3) throw new Fail('steal_not_available', 409);
          if (p.id === g.control) throw new Fail('invalid_payload', 400);
          g.stealFrom = g.control; g.control = p.id; g.roundPhase = 'steal';
          return;
        }
        case 'feud.award': {
          if (g.roundPhase === 'awarded') throw new Fail('round_awarded', 409);
          const target = payload.playerId === undefined ? g.control : payload.playerId;
          if (!target) throw new Fail('no_control', 409);
          const p = room.players.find(x => x.id === target);
          if (!p) throw new Fail('unknown_player', 400);
          const pts = sum();
          p.score += pts;
          g.awardedTo = p.id; g.awardedPoints = pts; g.buzzer.open = false; g.roundPhase = 'awarded';
          return;
        }
        default:
          throw new Fail('unknown_action', 400);
      }
    },
    end(room) { room.game.ended = true; room.game.buzzer.open = false; },
    project(room, viewer) {
      const g = room.game;
      const pts = g.board.answers.filter(a => g.revealed[a.id]).reduce((t, a) => t + a.points, 0);
      const out = {
        title: 'Feud (fixture)', label: g.board.label, answerSource: 'house',
        board: room.roundIndex + 1, boardCount: FIXTURE_FEUD.length, question: g.board.question,
        roundPhase: g.ended ? 'ended' : g.roundPhase,
        slots: g.board.answers.map((a, i) => (g.revealed[a.id] ? { index: i, revealed: true, id: a.id, text: a.text, points: a.points } : { index: i, revealed: false })),
        strikes: g.strikes, maxStrikes: 3, roundPoints: g.awardedTo ? g.awardedPoints : pts,
        buzzer: { open: g.buzzer.open, winner: g.buzzer.winner }, control: g.control, stealFrom: g.stealFrom,
        awardedTo: g.awardedTo, awardedPoints: g.awardedPoints,
      };
      if (viewer.role === 'operator') {
        out.boardId = g.board.id;
        out.boardsRemaining = FIXTURE_FEUD.length - room.roundIndex - 1;
        out.answers = g.board.answers.map(a => ({ id: a.id, text: a.text, aliases: a.aliases.slice(), points: a.points, revealed: !!g.revealed[a.id] }));
      }
      return out;
    },
  },
};

// ---- room ops -------------------------------------------------------------------------

function findRoom(code) {
  const room = rooms.get(String(code || '').toUpperCase());
  if (!room) throw new Fail('room_not_found', 404);
  return room;
}

function actorFor(room, tok) {
  if (!tok) throw new Fail('unauthorized', 401);
  if (tok === room.hostToken) return { role: 'operator' };
  if (tok === room.displayToken) return { role: 'display' };
  const p = room.players.find(x => x.token === tok);
  if (p) return { role: 'player', id: p.id };
  throw new Fail('unauthorized', 401);
}

function project(room, viewer) {
  const self = viewer.role === 'player' ? room.players.find(p => p.id === viewer.id) : null;
  return {
    protocol: 1, code: room.code, revision: room.revision, role: viewer.role,
    self: self ? { id: self.id, name: self.name } : null,
    mode: room.mode, phase: room.phase, roundId: room.phase === 'lobby' || room.phase === 'closed' ? null : room.roundId,
    playerLimits: { min: MIN_PLAYERS, max: MAX_PLAYERS },
    players: room.players.map(p => ({ id: p.id, name: p.name, connected: isConnected(room, p), score: p.score })),
    game: room.game && room.phase !== 'lobby' && room.phase !== 'closed' ? MODES[room.mode].project(room, viewer) : null,
  };
}

function isConnected(room, p) {
  for (const s of subs.values()) if (s.code === room.code && s.token === p.token) return true;
  return false;
}

function publish(room) {
  for (const s of subs.values()) {
    if (s.code !== room.code) continue;
    let viewer;
    try { viewer = actorFor(room, s.token); } catch { continue; }
    s.port.postMessage({ sub: s.subId, type: 'state', id: String(room.revision), snapshot: project(room, viewer) });
  }
}

function bump(room) { room.revision += 1; publish(room); }

const HOST_ACTIONS = new Set(['room.set_mode', 'room.start', 'room.pause', 'room.resume', 'room.end', 'room.close', 'round.next']);
const roundIdFor = room => (room.mode === 'feud' ? 'g1b' : 'g1r') + (room.roundIndex + 1);

function applyAction(room, actor, body) {
  const { type, payload = {}, roundId } = body;
  if (typeof type !== 'string') throw new Fail('bad_request');
  if (HOST_ACTIONS.has(type)) {
    if (actor.role !== 'operator') throw new Fail('forbidden', 403);
    switch (type) {
      case 'room.set_mode':
        if (room.phase !== 'lobby') throw new Fail('invalid_phase', 409);
        if (!MODES[payload.mode]) throw new Fail('mode_unimplemented', 409);
        room.mode = payload.mode;
        return;
      case 'room.start':
        if (room.phase !== 'lobby') throw new Fail('invalid_phase', 409);
        if (!room.mode) throw new Fail('mode_not_selected', 409);
        if (room.players.length < MIN_PLAYERS) throw new Fail('not_enough_players', 409);
        room.phase = 'playing'; room.roundIndex = 0; room.roundId = roundIdFor(room);
        room.game = MODES[room.mode].initialize(room);
        return;
      case 'room.pause':
        if (room.phase !== 'playing') throw new Fail('invalid_phase', 409);
        room.phase = 'paused'; return;
      case 'room.resume':
        if (room.phase !== 'paused') throw new Fail('invalid_phase', 409);
        room.phase = 'playing'; return;
      case 'room.end':
        if (room.phase !== 'playing' && room.phase !== 'paused') throw new Fail('invalid_phase', 409);
        room.phase = 'ended';
        if (room.game) MODES[room.mode].end(room);
        return;
      case 'room.close':
        room.phase = 'closed'; room.game = null;
        return;
      case 'round.next':
        if (room.phase === 'paused') throw new Fail('paused', 409);
        if (room.phase !== 'playing') throw new Fail('invalid_phase', 409);
        if (roundId === undefined) throw new Fail('round_required', 400);
        // A round.next aimed at a round that has already moved on (double tap, two operator
        // tabs, a retry after a lost reply) must not skip a second round.
        if (roundId !== room.roundId) throw new Fail('stale_round', 409);
        if (room.mode === 'feud' && room.roundIndex + 1 >= FIXTURE_FEUD.length) throw new Fail('content_exhausted', 409);
        room.roundIndex += 1; room.roundId = roundIdFor(room);
        room.game = MODES[room.mode].initialize(room);
        return;
    }
  }
  if (room.phase === 'paused') throw new Fail('paused', 409);
  if (room.phase !== 'playing') throw new Fail('invalid_phase', 409);
  if (roundId === undefined) throw new Fail('round_required', 400);
  if (roundId !== room.roundId) throw new Fail('stale_round', 409);
  if (!type.startsWith(MODES[room.mode].prefix)) throw new Fail('wrong_mode', 409);
  MODES[room.mode].reduce(room, actor, type, payload);
}

const OPS = {
  health() { return { ok: true, protocol: 1, simulated: true }; },
  createRoom({ operatorName }) {
    const code = roomCode();
    const room = {
      code, hostToken: token(), displayToken: token(), operatorName: String(operatorName || '').slice(0, 24),
      players: [], mode: null, phase: 'lobby', roundId: 0, roundIndex: 0, revision: 1, game: null,
      log: new Map(), nextPlayer: 1,
    };
    rooms.set(code, room);
    return { code, hostToken: room.hostToken, displayToken: room.displayToken, revision: room.revision };
  },
  join({ code, name }) {
    const room = findRoom(code);
    if (room.phase === 'closed') throw new Fail('room_closed', 409);
    if (room.phase === 'playing' || room.phase === 'paused') throw new Fail('game_in_progress', 409);
    const clean = String(name || '').replace(/\s+/g, ' ').trim();
    if (clean.length < 1 || clean.length > 24) throw new Fail('invalid_name', 400);
    if (room.players.some(p => p.name.toLowerCase() === clean.toLowerCase())) throw new Fail('name_taken', 409);
    if (room.players.length >= MAX_PLAYERS) throw new Fail('room_full', 409);
    const p = { id: 'p' + (room.nextPlayer++) + Array.from(rand(2), b => b.toString(16)).join(''), name: clean, token: token(), score: 0 };
    room.players.push(p);
    bump(room);
    return { playerId: p.id, playerToken: p.token, revision: room.revision };
  },
  state({ code, token: tok }) {
    const room = findRoom(code);
    return project(room, actorFor(room, tok));
  },
  act({ code, token: tok, body }) {
    const room = findRoom(code);
    const actor = actorFor(room, tok);
    if (!body || typeof body.actionId !== 'string' || !body.actionId) throw new Fail('bad_request');
    const logKey = (actor.role === 'player' ? actor.id : actor.role) + ':' + body.actionId;
    if (room.log.has(logKey)) return room.log.get(logKey);
    let result;
    try {
      applyAction(room, actor, body);
      room.revision += 1;
      result = { accepted: true, revision: room.revision };
    } catch (e) {
      if (!(e instanceof Fail)) throw e;
      result = { accepted: false, revision: room.revision, error: { code: e.code, message: 'simulated' } };
    }
    room.log.set(logKey, result);
    if (room.log.size > ACTION_LOG_MAX) room.log.delete(room.log.keys().next().value);
    if (result.accepted) publish(room);
    return result;
  },
};

function subscribe(port, { code, token: tok, subId }) {
  const room = findRoom(code);
  const viewer = actorFor(room, tok);
  const k = 's' + (++subSeq);
  subs.set(k, { port, subId, code: room.code, token: tok });
  port.postMessage({ sub: subId, type: 'state', id: String(room.revision), snapshot: project(room, viewer) });
  bump(room); // connected flag changed
  return k;
}

function unsubscribe(port, subId) {
  for (const [k, s] of subs) {
    if (s.port === port && s.subId === subId) {
      subs.delete(k);
      const room = rooms.get(s.code);
      if (room) bump(room);
    }
  }
}

self.onconnect = (e) => {
  const port = e.ports[0];
  port.onmessage = (m) => {
    const { id, op, args } = m.data || {};
    try {
      let result;
      if (op === 'subscribe') result = subscribe(port, args);
      else if (op === 'unsubscribe') result = unsubscribe(port, args.subId);
      else if (op === 'sim.dropStreams') {
        // Test hook: drops every stream for a room to exercise client reconnect.
        for (const [k, s] of subs) if (s.code === args.code) { s.port.postMessage({ sub: s.subId, type: 'dropped' }); subs.delete(k); }
        const room = rooms.get(args.code);
        if (room) bump(room);
        result = true;
      } else if (OPS[op]) result = OPS[op](args || {});
      else throw new Fail('unknown_op');
      port.postMessage({ id, ok: true, result });
    } catch (err) {
      const f = err instanceof Fail ? err : new Fail('internal_error', 500);
      port.postMessage({ id, ok: false, error: { code: f.code, status: f.status } });
    }
  };
  port.start();
};
