// SIMULATED FIXTURE adapter: same interface as HttpTransport, backed by sim/sim-worker.js.
// Only loaded when the page URL carries ?sim=1. Every page using it shows a permanent
// "Simulated fixture - not a real server" ribbon. No network traffic is involved.
import { ApiError, newActionId } from './transport.js';

export class SimTransport {
  constructor() {
    this.kind = 'simulated';
    if (typeof SharedWorker === 'undefined') throw new ApiError('simulation_unsupported');
    this.worker = new SharedWorker(new URL('../sim/sim-worker.js', import.meta.url), { name: 'majlis-party-sim' });
    this.port = this.worker.port;
    this.pending = new Map();
    this.streams = new Map();
    this.seq = 0;
    this.offline = false; // test hook: simulates a dropped network
    this.port.onmessage = (m) => this.onMessage(m.data);
    this.port.start();
    addEventListener('pagehide', () => { for (const s of this.streams.values()) s.close(); });
  }
  onMessage(msg) {
    if (msg.id !== undefined && this.pending.has(msg.id)) {
      const { resolve, reject } = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      if (msg.ok) resolve(msg.result); else reject(new ApiError(msg.error.code, msg.error.status));
      return;
    }
    if (msg.sub !== undefined) {
      const s = this.streams.get(msg.sub);
      if (s) s.deliver(msg);
    }
  }
  call(op, args) {
    if (this.offline && op !== 'unsubscribe' && op !== 'sim.dropStreams') return Promise.reject(new ApiError('network_error'));
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.port.postMessage({ id, op, args });
    });
  }
  health() { return this.call('health'); }
  createRoom(operatorName) { return this.call('createRoom', { operatorName }); }
  join(code, name) { return this.call('join', { code, name }); }
  state(code, token) { return this.call('state', { code, token }); }
  async act(code, token, type, payload = {}, roundId) {
    const body = { actionId: newActionId(), type, payload };
    if (roundId !== undefined && roundId !== null) body.roundId = roundId;
    try {
      const r = await this.call('act', { code, token, body });
      if (r && r.accepted === false && r.error && typeof r.error === 'object') return { ...r, error: r.error.code };
      return r;
    } catch (e) {
      if (e.code === 'network_error') throw e;
      return { accepted: false, error: e.code, status: e.status };
    }
  }
  setOffline(v) {
    this.offline = v;
    for (const s of this.streams.values()) if (v) s.drop(); else s.retry();
  }
  subscribe(code, token, { onSnapshot, onStatus }) {
    const subId = ++this.seq;
    let closed = false, timer = null, delay = 500, live = false;
    const status = (s, e) => onStatus && onStatus(s, e);
    const open = async (first) => {
      if (closed) return;
      status(first ? 'connecting' : 'reconnecting');
      try {
        await this.call('subscribe', { code, token, subId });
        delay = 500;
      } catch (e) {
        if (e.code === 'network_error') { schedule(); return; }
        closed = true;
        this.streams.delete(subId);
        status('fatal', e);
      }
    };
    const schedule = () => {
      if (closed) return;
      clearTimeout(timer);
      timer = setTimeout(() => open(false), delay);
      delay = Math.min(delay * 2, 8000);
    };
    const handle = {
      deliver(msg) {
        if (closed) return;
        if (msg.type === 'state') { live = true; status('live'); onSnapshot(msg.snapshot); }
        else if (msg.type === 'dropped') { live = false; status('reconnecting'); schedule(); }
      },
      drop: () => { live = false; this.port.postMessage({ op: 'unsubscribe', args: { subId } }); status('reconnecting'); schedule(); },
      retry: () => { clearTimeout(timer); delay = 500; open(false); },
      close: () => { closed = true; clearTimeout(timer); this.port.postMessage({ op: 'unsubscribe', args: { subId } }); this.streams.delete(subId); },
      kick: () => { if (live) handle.drop(); else handle.retry(); },
    };
    this.streams.set(subId, handle);
    open(true);
    return handle;
  }
}
