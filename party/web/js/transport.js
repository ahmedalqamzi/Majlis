// Majlis Party: HTTP + fetch-SSE client for the protocol v1 room server (party/CONTRACT.md).
// The server is authoritative. This file never decides game outcomes; it only sends actions and
// relays role-projected snapshots. Capability tokens travel only in the Authorization header.

export class ApiError extends Error {
  constructor(code, status = 0, detail = '') {
    super(code);
    this.code = code;       // stable machine code from the server, or a client-side code below
    this.status = status;
    this.detail = detail;
  }
}

// Client-side codes: network_error, bad_response, aborted.
function newActionId() {
  if (globalThis.crypto && crypto.randomUUID) return crypto.randomUUID();
  const b = new Uint8Array(16);
  crypto.getRandomValues(b);
  return Array.from(b, x => x.toString(16).padStart(2, '0')).join('');
}

export { newActionId };

async function readJson(res) {
  const text = await res.text();
  if (!text) return {};
  try { return JSON.parse(text); } catch { throw new ApiError('bad_response', res.status); }
}

function errorCodeFrom(body, status) {
  if (body && typeof body.error === 'string') return body.error;
  if (body && body.error && typeof body.error.code === 'string') return body.error.code;
  if (status === 401) return 'unauthorized';
  if (status === 403) return 'forbidden';
  if (status === 404) return 'not_found';
  if (status === 429) return 'rate_limited';
  return 'http_' + status;
}

// Parses a text/event-stream byte stream. Calls onEvent({id, event, data}) per dispatched event.
export class SseParser {
  constructor(onEvent) {
    this.onEvent = onEvent;
    this.buf = '';
    this.reset();
  }
  reset() { this.id = null; this.event = ''; this.data = []; }
  push(chunk) {
    this.buf += chunk;
    let idx;
    while ((idx = this.buf.search(/\r\n|\r|\n/)) >= 0) {
      const line = this.buf.slice(0, idx);
      const nl = this.buf[idx] === '\r' && this.buf[idx + 1] === '\n' ? 2 : 1;
      // A lone trailing \r may be the first half of \r\n; wait for more input.
      if (this.buf[idx] === '\r' && idx + 1 === this.buf.length) break;
      this.buf = this.buf.slice(idx + nl);
      this.line(line);
    }
  }
  line(line) {
    if (line === '') {
      if (this.data.length) this.onEvent({ id: this.id, event: this.event || 'message', data: this.data.join('\n') });
      this.event = ''; this.data = [];
      return;
    }
    if (line[0] === ':') return; // heartbeat comment
    const c = line.indexOf(':');
    const field = c < 0 ? line : line.slice(0, c);
    let value = c < 0 ? '' : line.slice(c + 1);
    if (value[0] === ' ') value = value.slice(1);
    if (field === 'data') this.data.push(value);
    else if (field === 'event') this.event = value;
    else if (field === 'id' && !value.includes('\0')) this.id = value;
  }
}

export class HttpTransport {
  constructor({ base = '', fetchImpl } = {}) {
    this.base = base;
    this.kind = 'network';
    this.fetch = fetchImpl || ((...a) => fetch(...a));
  }

  async request(method, path, { token, body, signal } = {}) {
    const headers = { Accept: 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (token) headers.Authorization = 'Bearer ' + token;
    let res;
    try {
      res = await this.fetch(this.base + path, {
        method, headers, signal, credentials: 'same-origin', cache: 'no-store',
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (e) {
      if (e && e.name === 'AbortError') throw new ApiError('aborted');
      throw new ApiError('network_error');
    }
    const json = await readJson(res);
    if (!res.ok) throw new ApiError(errorCodeFrom(json, res.status), res.status);
    return json;
  }

  health() { return this.request('GET', '/api/health'); }
  createRoom(operatorName) {
    return this.request('POST', '/api/rooms', { body: operatorName ? { operatorName } : {} });
  }
  join(code, name) {
    return this.request('POST', `/api/rooms/${encodeURIComponent(code)}/join`, { body: { name } });
  }
  state(code, token) {
    return this.request('GET', `/api/rooms/${encodeURIComponent(code)}/state`, { token });
  }

  // Sends one action. Network failures are retried with the SAME actionId, so the server's
  // idempotency makes the retry safe; a server rejection is returned, never retried.
  async act(code, token, type, payload = {}, roundId, { attempts = 3 } = {}) {
    const body = { actionId: newActionId(), type, payload };
    if (roundId !== undefined && roundId !== null) body.roundId = roundId;
    let last;
    for (let i = 0; i < attempts; i++) {
      try {
        const r = await this.request('POST', `/api/rooms/${encodeURIComponent(code)}/actions`, { token, body });
        // Normalise {error:{code,message}} to the bare stable code the pages switch on.
        if (r && r.accepted === false) return { accepted: false, error: errorCodeFrom(r, 409), revision: r.revision };
        return r;
      } catch (e) {
        last = e;
        // A server rejection (4xx with a machine code) is a result, not a transport failure.
        if (e.code !== 'network_error') return { accepted: false, error: e.code, status: e.status };
        await new Promise(r => setTimeout(r, 300 * (i + 1)));
      }
    }
    throw last;
  }

  // Opens the role-projected event stream with fetch so the token stays in a header.
  // Reconnects with capped backoff. Returns {close()}.
  subscribe(code, token, { onSnapshot, onStatus }) {
    let closed = false, lastId = null, ctrl = null, delay = 500, timer = null, watchdog = null;
    const status = (s, extra) => onStatus && onStatus(s, extra);
    // The server writes a heartbeat comment every 15 s. A stream that stays silent for longer than
    // SILENCE_MS is treated as dropped (a phone that lost Wi-Fi can leave a socket open but dead).
    const SILENCE_MS = 40000;
    const feed = () => { clearTimeout(watchdog); watchdog = setTimeout(() => { if (ctrl) ctrl.abort(); }, SILENCE_MS); };
    // Codes after which reconnecting can never succeed: the page is told once and the stream stops.
    const fatal = new Set(['unauthorized', 'token_expired', 'forbidden', 'not_found', 'room_not_found', 'room_closed', 'room_expired', 'invalid_token']);
    let streamError = null;
    const run = async () => {
      if (closed) return;
      ctrl = new AbortController();
      status(lastId === null ? 'connecting' : 'reconnecting');
      const headers = { Accept: 'text/event-stream', Authorization: 'Bearer ' + token };
      if (lastId !== null) headers['Last-Event-ID'] = lastId;
      try {
        const res = await this.fetch(`${this.base}/api/rooms/${encodeURIComponent(code)}/events`, {
          headers, signal: ctrl.signal, cache: 'no-store', credentials: 'same-origin',
        });
        if (!res.ok) {
          const json = await readJson(res).catch(() => ({}));
          const codeStr = errorCodeFrom(json, res.status);
          if (fatal.has(codeStr) || res.status === 401 || res.status === 403 || res.status === 404) {
            closed = true;
            status('fatal', new ApiError(codeStr, res.status));
            return;
          }
          throw new ApiError(codeStr, res.status);
        }
        if (!res.body || !res.body.getReader) throw new ApiError('streaming_unsupported');
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        feed();
        const parser = new SseParser(ev => {
          if (ev.id !== null) lastId = ev.id;
          // The server ends a stream with `event: error` {"ok":false,"error":{"code"}} on room or
          // token expiry; it then closes the connection.
          if (ev.event === 'error') {
            let body = null;
            try { body = JSON.parse(ev.data); } catch { /* unreadable */ }
            streamError = errorCodeFrom(body, 0);
            return;
          }
          if (ev.event !== 'state') return;
          let snap;
          try { snap = JSON.parse(ev.data); } catch { return; }
          delay = 500;
          status('live');
          onSnapshot(snap);
        });
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          feed();
          parser.push(dec.decode(value, { stream: true }));
        }
        throw new ApiError(streamError || 'stream_ended');
      } catch (e) {
        clearTimeout(watchdog);
        if (closed) return;
        if (e && fatal.has(e.code)) {
          closed = true;
          status('fatal', e);
          return;
        }
        status('reconnecting', e);
        timer = setTimeout(() => { timer = null; run(); }, delay + Math.floor(Math.random() * 250));
        delay = Math.min(delay * 2, 8000);
      }
    };
    // Reconnect now: skips a pending backoff wait, or drops a stalled stream (e.g. after the tab wakes).
    const kick = () => {
      if (closed) return;
      if (timer) { clearTimeout(timer); timer = null; delay = 500; run(); }
      else if (ctrl) ctrl.abort();
    };
    // The browser's own network signal: drop the stream at once when it goes offline (so the page
    // says "Reconnecting" instead of looking live), and retry immediately when it comes back.
    const onOffline = () => { if (!closed && ctrl) ctrl.abort(); };
    const onOnline = () => kick();
    if (typeof addEventListener === 'function') { addEventListener('offline', onOffline); addEventListener('online', onOnline); }
    run();
    return {
      close() {
        closed = true; clearTimeout(timer); clearTimeout(watchdog); if (ctrl) ctrl.abort();
        if (typeof removeEventListener === 'function') { removeEventListener('offline', onOffline); removeEventListener('online', onOnline); }
      },
      kick,
    };
  }
}
