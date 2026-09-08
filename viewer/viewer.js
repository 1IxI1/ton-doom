// Doom on TON viewer: subscribes to the contract's transactions on toncenter (WebSocket streaming API,
// min_finality = confirmed), decodes FrameEvent external-out messages and plays them back smoothly.
//
// Frames arrive in bursts (up to ~10 per 0.4 s block); we buffer them and play at a steady rate,
// speeding up when the buffer grows and slowing down when it runs dry.

(() => {
  const OP_FRAME = 0x4652414d;
  // config.js is generated from .env by `python3 tools/viewer_config.py` (not committed):
  //   window.DOOM_CONFIG = { addr: DOOM_ADDRESS, key: TONCENTER_TESTNET_API_KEY }
  // Without it (e.g. on GitHub Pages) the viewer polls toncenter once a second without an API key.
  const CFG = window.DOOM_CONFIG || {};
  // servers.js (committed): window.DOOM_SERVERS = { servers: [{ name, addr, relays: [...] }], demo: { name, addr } }
  const SRV = window.DOOM_SERVERS || { servers: [], demo: null };
  const DEFAULTS = {
    base: 'testnet.toncenter.com',
    pollMs: 1100,          // keyless polling interval (toncenter allows ~1 request/s without a key)
    pollLimit: 60,
    freeAfterS: 120,       // a server with no frame for this long is free to take
  };

  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);

  // ---- API key: optional (toncenter, free from @tonapibot); with it: streaming + fast input, without: 1 req/s
  try { const k = localStorage.getItem('doom.key'); $('key').value = k !== null ? k : (CFG.key || ''); } catch (e) { $('key').value = CFG.key || ''; }
  const apiKey = () => $('key').value.trim();
  const hasKey = () => apiKey() !== '';
  $('key').onchange = () => { try { localStorage.setItem('doom.key', apiKey()); } catch (e) {} applyMode(); };
  const usePending = () => hasKey() && $('pending').checked;
  $('pending').onchange = () => { if (ws) connectWs(); };

  // ---- keyless rate limiter: every toncenter request goes through one queue, ~1 per second, sends first
  const rl = { q: [], busy: false, nextAt: 0, gapMs: 1200 };
  function limited(fn, prio) {
    if (hasKey()) return fn();
    return new Promise((resolve, reject) => {
      rl.q.push({ fn, prio, resolve, reject });
      rl.q.sort((a, b) => b.prio - a.prio);
      pumpRl();
    });
  }
  function pumpRl() {
    if (rl.busy || !rl.q.length) return;
    const wait = Math.max(0, rl.nextAt - performance.now());
    rl.busy = true;
    setTimeout(async () => {
      const job = rl.q.shift();
      rl.nextAt = performance.now() + rl.gapMs;
      try { job.resolve(await job.fn()); } catch (e) { job.reject(e); }
      rl.busy = false;
      pumpRl();
    }, wait);
  }

  // ---- servers -----------------------------------------------------------------------------------
  const servers = [...SRV.servers];
  if (SRV.demo) servers.push({ ...SRV.demo, watchOnly: true });
  if (params.get('addr')) servers.unshift({ name: 'custom ' + params.get('addr').slice(0, 8), addr: params.get('addr'), relays: [] });
  const status = new Map();   // addr -> { lastStep, frameNo, at }
  let current = servers[0] || { name: '-', addr: '', relays: [] };
  const currentAddr = () => current.addr;
  const relays = () => (current.relays || []);
  function serverLabel(sv) {
    const st = status.get(sv.addr);
    let tag = '';
    if (sv.watchOnly) tag = 'watch only';
    else if (!st) tag = 'checking…';
    else if (st.lastStep === 0) tag = 'free';
    else {
      const idle = Math.max(0, Math.floor(Date.now() / 1000 - st.lastStep));
      tag = idle > DEFAULTS.freeAfterS ? `free (${idle >= 3600 ? Math.floor(idle / 3600) + ' h' : idle >= 60 ? Math.floor(idle / 60) + ' min' : idle + ' s'} idle)` : `busy (moved ${idle} s ago)`;
    }
    return `${sv.name} · ${tag}`;
  }
  function renderServers() {
    const sel = $('server');
    const cur = sel.value;
    sel.innerHTML = '';
    for (const sv of servers) {
      const o = document.createElement('option');
      o.value = sv.addr; o.textContent = serverLabel(sv);
      sel.appendChild(o);
    }
    sel.value = cur && servers.some(sv => sv.addr === cur) ? cur : current.addr;
    $('explorer').href = 'https://testnet.tonviewer.com/' + current.addr;
  }
  async function probe(sv) {
    try {
      const r = await limited(() => api('/api/v3/runGetMethod', { address: sv.addr, method: 'autoState', stack: [] }), 0);
      const lastStep = parseInt(r.stack[2].value, 16);
      status.set(sv.addr, { lastStep, at: Date.now() });
    } catch (e) { status.set(sv.addr, { lastStep: -1, at: Date.now() }); }
    renderServers();
  }
  async function probeAll() { for (const sv of servers) if (!sv.watchOnly) await probe(sv); }
  function isFree(sv) { const st = status.get(sv.addr); return st && (st.lastStep === 0 || Date.now() / 1000 - st.lastStep > DEFAULTS.freeAfterS); }
  function selectServer(addr) {
    const sv = servers.find(x => x.addr === addr);
    if (!sv) return;
    if (playing) stopPlay();
    current = sv;
    seen.clear(); queue = []; lastShown = 0; pollLt = 0;
    renderServers();
    applyMode();
    log(`server: ${sv.name} ${sv.addr}`);
  }
  $('server').onchange = () => selectServer($('server').value);
  setInterval(() => { if (!current.watchOnly) probe(current); renderServers(); }, 20000);

  // ---- transport by mode: key -> streaming (WebSocket, ~0.4 s), no key -> polling (~1 req/s) ---------
  function applyMode() {
    const key = hasKey();
    $('pendingopt').hidden = !key;
    $('modeinfo').textContent = key ? 'streaming (api key)' : 'polling, no api key: ~1 request/s shared by frames and inputs; a free key from @tonapibot makes it ~4x faster';
    $('fps').value = key ? '25' : '15';
    if (key) connectWs(); else startPoll();
  }

  const canvas = $('screen');
  const ctx = canvas.getContext('2d');
  ctx.imageSmoothingEnabled = false;
  let off = null; // offscreen canvas at native resolution

  const seen = new Map();   // frameNo -> frame
  let queue = [];           // frames waiting to be shown (sorted by frameNo)
  let lastShown = 0;
  let paused = false;
  let ws = null, pollTimer = null, pollLt = 0;
  const stat = { received: 0, dup: 0, shown: 0, partial: 0, lastNow: 0, latency: 0, gas: 0, queued: 0, playFps: 25, confirmed: 0, mispredict: 0 };

  function log(msg, cls) {
    const el = document.createElement('div');
    if (cls) el.className = cls;
    el.textContent = new Date().toLocaleTimeString() + ' ' + msg;
    $('log').prepend(el);
    while ($('log').childNodes.length > 200) $('log').lastChild.remove();
  }
  function setStatus(s, cls) { $('status').textContent = s; $('status').className = cls || ''; }

  // ---- decoding -------------------------------------------------------------------------------
  function decodeFrameBody(cell) {
    const s = cell.beginParse();
    if (s.remainingBits < 32 || s.loadUint(32) !== OP_FRAME) return null;
    const f = {
      frameNo: s.loadUint(32), w: s.loadUint(16), h: s.loadUint(16),
      px: s.loadInt(32), py: s.loadInt(32), angle: s.loadUint(16), viewz: s.loadInt(16),
      flags: s.loadUint(8), queued: s.loadUint(16),
    };
    f.kills = s.remainingBits >= 8 ? s.loadUint(8) : null;   // targets killed so far (contracts with monsters)
    const cols = [];
    let c = s.loadRef();
    while (c && cols.length < f.w) {
      const cs = c.beginParse();
      while (cs.remainingBits >= f.h && cols.length < f.w) cols.push(cs.loadBits(f.h));
      c = cs.remainingRefs ? cs.loadRef() : null;
    }
    f.columns = cols;
    return f;
  }

  function framesFromTx(tx) {
    const out = [];
    for (const m of tx.out_msgs || []) {
      if (m.destination) continue;
      const b64 = m.message_content && m.message_content.body;
      if (!b64) continue;
      let f = null;
      try { f = decodeFrameBody(Boc.parse(Boc.fromBase64(b64))); } catch (e) { continue; }
      if (!f) continue;
      f.lt = Number(tx.lt); f.now = Number(tx.now);
      f.gas = Number(((tx.description || {}).compute_ph || {}).gas_used || 0);
      out.push(f);
    }
    return out;
  }

  function sameColumns(a, b) {
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) { const x = a[i], y = b[i]; if (x.length !== y.length) return false; for (let j = 0; j < x.length; j++) if (x[j] !== y[j]) return false; }
    return true;
  }
  function ingestTx(tx, finality) {
    if (finality !== 'pending') ackFromTx(tx);
    for (const f of framesFromTx(tx)) {
      f.finality = finality || 'confirmed';
      stat.received++;
      const old = seen.get(f.frameNo);
      if (old) {
        // an emulated (pending) frame gets replaced by the real one; count and redraw if it differed
        if (old.finality === 'pending' && f.finality !== 'pending') {
          const same = sameColumns(old.columns, f.columns) && old.px === f.px && old.py === f.py && old.angle === f.angle;
          if (!same) { stat.mispredict++; log(`emulated frame ${f.frameNo} differed from the chain, corrected`, 'warn'); }
          old.finality = f.finality; old.columns = f.columns; old.px = f.px; old.py = f.py; old.angle = f.angle; old.flags = f.flags; old.now = f.now;
          stat.confirmed = Math.max(stat.confirmed, f.frameNo);
          if (f.frameNo === lastShown) drawFrame(old);   // refresh the badge (and the picture if it differed)
        } else { stat.dup++; }
        continue;
      }
      if (f.finality !== 'pending') stat.confirmed = Math.max(stat.confirmed, f.frameNo);
      seen.set(f.frameNo, f);
      if (f.frameNo <= lastShown) continue; // already past it (e.g. history replay)
      queue.push(f);
      queue.sort((a, b) => a.frameNo - b.frameNo);
      stat.lastNow = f.now;
      stat.latency = Date.now() / 1000 - f.now;
    }
    if (seen.size > 5000) { // forget old frames
      const keys = [...seen.keys()].sort((a, b) => a - b).slice(0, seen.size - 3000);
      for (const k of keys) seen.delete(k);
    }
  }

  // ---- rendering ------------------------------------------------------------------------------
  function drawFrame(f) {
    if (!off || off.width !== f.w || off.height !== f.h) {
      off = document.createElement('canvas'); off.width = f.w; off.height = f.h;
    }
    const octx = off.getContext('2d');
    const img = octx.createImageData(f.w, f.h);
    const d = img.data;
    for (let x = 0; x < f.w; x++) {
      const col = f.columns[x];
      if (!col) continue;
      for (let y = 0; y < f.h; y++) {
        const bit = (col[y >> 3] >> (7 - (y & 7))) & 1; // MSB first = top row
        const i = (y * f.w + x) * 4;
        const v = bit ? 235 : 0;
        d[i] = v; d[i + 1] = v; d[i + 2] = v; d[i + 3] = 255;
      }
    }
    octx.putImageData(img, 0, 0);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(off, 0, 0, canvas.width, canvas.height);
    stat.shown++;
    if (f.flags & 1) stat.partial++;
    stat.gas = f.gas; stat.queued = f.queued;
    canvas.classList.toggle('emulated', f.finality === 'pending');
    $('stats').textContent =
      `frame      ${f.frameNo}${f.finality === 'pending' ? '  EMULATED (toncenter, not yet in a block)' : ''}\n` +
      `res        ${f.w}x${f.h}${f.flags & 1 ? '  PARTIAL (gas budget hit)' : ''}\n` +
      `pos        ${(f.px / 65536).toFixed(1)}, ${(f.py / 65536).toFixed(1)}  z=${f.viewz}\n` +
      `angle      ${(f.angle * 360 / 512).toFixed(1)} deg\n` +
      `tx gas     ${f.gas}\n` +
      `on-chain q ${f.queued} inputs${f.kills !== null ? `   kills ${f.kills}` : ''}\n` +
      `block time ${new Date(f.now * 1000).toLocaleTimeString()}  (latency ${stat.latency.toFixed(1)} s)\n` +
      `buffer     ${queue.length} frames, play ${stat.playFps.toFixed(1)} fps\n` +
      `received   ${stat.received} (dup ${stat.dup}), shown ${stat.shown}, partial ${stat.partial}` +
      (usePending() ? `\nconfirmed  up to frame ${stat.confirmed}, emulated frames corrected: ${stat.mispredict}` : '');
  }

  // playback loop: adaptive rate around the nominal fps
  let nextAt = 0;
  function tick(ts) {
    requestAnimationFrame(tick);
    if (paused) return;
    const nominal = Math.max(1, Number($('fps').value) || 25);
    let fps = nominal;
    if (playing) fps = queue.length > 4 ? nominal * 3 : queue.length > 1 ? nominal * 1.5 : nominal;   // latency first
    else if (queue.length > 40) fps = nominal * 3;
    else if (queue.length > 20) fps = nominal * 1.6;
    else if (queue.length > 12) fps = nominal * 1.2;
    else if (queue.length < 3) fps = nominal * 0.7;
    stat.playFps = fps;
    if (ts < nextAt || !queue.length) return;
    nextAt = Math.max(nextAt + 1000 / fps, ts - 100);
    const f = queue.shift();
    lastShown = f.frameNo;
    drawFrame(f);
  }
  requestAnimationFrame(tick);

  // ---- transport: WebSocket streaming ---------------------------------------------------------
  function connectWs() {
    disconnect();
    const addr = currentAddr(), key = apiKey();
    if (!addr || !key) { setStatus('streaming needs an api key', 'err'); return; }
    const url = `wss://${DEFAULTS.base}/api/streaming/v2/ws?api_key=${encodeURIComponent(key)}`;
    setStatus('connecting…', 'warn');
    ws = new WebSocket(url);
    const sock = ws;
    let pingTimer = null;
    ws.onopen = () => {
      ws.send(JSON.stringify({ operation: 'subscribe', id: '1', addresses: [addr], types: ['transactions'], min_finality: usePending() ? 'pending' : 'confirmed' }));
      pingTimer = setInterval(() => { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ operation: 'ping' })); }, 10000);
      setStatus(usePending() ? 'subscribed (ws, pending = emulated frames)' : 'subscribed (ws)', 'on');
      $('connect').classList.add('on');
      log('ws open, subscribed to ' + addr);
      loadHistory(addr, key, 60);
    };
    ws.onmessage = (ev) => {
      let d; try { d = JSON.parse(ev.data); } catch (e) { return; }
      if (d.status) return;
      if (d.type === 'transactions') {
        const txs = [...(d.transactions || [])].sort((a, b) => Number(a.lt) - Number(b.lt));
        for (const tx of txs) ingestTx(tx, d.finality);
      } else if (d.type === 'trace_invalidated') {
        log('trace invalidated ' + d.trace_external_hash_norm, 'warn');
      }
    };
    ws.onclose = (e) => {
      clearInterval(pingTimer);
      if (ws !== sock) return;   // replaced or closed on purpose (disconnect / resubscribe)
      ws = null;
      setStatus('ws closed ' + e.code, 'err'); $('connect').classList.remove('on'); log('ws closed ' + e.code + ' ' + e.reason, 'err');
      setTimeout(() => { if (!ws && !pollTimer) connectWs(); }, 2000);
    };
    ws.onerror = () => { if (ws === sock) log('ws error', 'err'); };
  }

  // ---- transport: REST polling fallback -------------------------------------------------------
  async function fetchTxs(addr, key, qs) {
    const headers = key ? { 'X-API-Key': key } : {};
    return limited(async () => {
      const r = await fetch(`https://${DEFAULTS.base}/api/v3/transactions?account=${encodeURIComponent(addr)}&${qs}`, { headers });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return (await r.json()).transactions || [];
    }, 1);
  }
  async function loadHistory(addr, key, n) {
    try {
      const txs = await fetchTxs(addr, key, `limit=${n}&sort=desc`);
      txs.sort((a, b) => Number(a.lt) - Number(b.lt));
      for (const tx of txs) { ingestTx(tx); pollLt = Math.max(pollLt, Number(tx.lt)); }
      // show the newest frame immediately, keep only a short tail to play
      if (queue.length > 5) { queue = queue.slice(-5); }
      log(`history: ${txs.length} txs, ${queue.length} frames queued`);
    } catch (e) { log('history failed: ' + e.message, 'err'); }
  }
  function startPoll() {
    disconnect();
    const addr = currentAddr(), key = apiKey();
    if (!addr) { setStatus('no server', 'err'); return; }
    setStatus(key ? 'polling' : 'polling (no api key)', 'on'); $('poll').classList.add('on');
    loadHistory(addr, key, 60);
    let busy = false, backoff = 0;
    const interval = key ? 700 : DEFAULTS.pollMs;
    pollTimer = setInterval(async () => {
      if (busy || addr !== currentAddr()) return;
      if (backoff > 0) { backoff--; return; }
      busy = true;
      try {
        const txs = await fetchTxs(addr, key, `limit=${DEFAULTS.pollLimit}&sort=asc&start_lt=${pollLt + 1}`);
        for (const tx of txs) { ingestTx(tx); pollLt = Math.max(pollLt, Number(tx.lt)); }
      } catch (e) {
        log('poll failed: ' + e.message, 'err');
        backoff = /429/.test(e.message) ? 2 : 1;   // rate limited: skip a couple of ticks
      } finally { busy = false; }
    }, interval);
  }
  function disconnect() {
    if (ws) { const w = ws; ws = null; try { w.close(); } catch (e) {} }
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    $('connect').classList.remove('on'); $('poll').classList.remove('on');
  }

  // ---- play: keyboard -> inputs -> batches (one external per 0.5 s) -> the contract ----------------
  // Externals need no signature. Batch ids are consecutive; the contract applies lastBatch+1 at once and keeps
  // batches that arrive ahead of it in a 3-slot reorder buffer; when the buffer is full or the oldest waited
  // 2 s, the batches before it are declared lost (skipped), so a batch the mempool never delivers costs half
  // a second of inputs instead of a stall. Batches are confirmed from
  // the transaction stream (the landed external names its batch id; a landed id confirms everything
  // before it) with the `lastBatch` get method as a fallback. Everything sent keeps a minimum gap of
  // PLAY.sendGapMs (the mempool caps externals per address). With input relays (config.js `relays`, see
  // contracts/Relay.tolk) batches go to the relays in turn, so the gap can be much shorter.
  const OP_TICK = 0x444f4f4d;
  // Input rate: every input is one on-chain frame (~0.9M gas, ~10 per block), so 20 inputs/s nearly saturates
  // the shard and the queue (latency) grows; 10/s with doubled steps moves just as fast at half the gas.
  // Lost externals: the mempool sometimes never delivers a message (and rejects an identical resend as a
  // duplicate), so a resend carries a fresh nonce after the inputs (the contract ignores trailing bits).
  // tail: idle inputs after the last key so the flash/bob settle; tailFire: a shot may start a target's
  // death animation (20 frames), which only advances on rendered frames, so keep frames coming
  const PLAY = { rate: 10, sendGapMs: 500, relayGapMs: 300, keylessGapMs: 1600, ackMs: 2000, keylessAckMs: 20000, maxBatch: 29, inFlight: 6, resendMs: 1500, resendEveryMs: 1000, maxResends: 1, holdMs: 1500, maxPending: 40, fireEvery: 4, tail: 2, tailFire: 24 };
  const stepMul = () => (PLAY.rate >= 20 ? 1 : 2);   // fwd/side units and turn per input
  const KEYMAP = { ArrowUp: 'fwd', KeyW: 'fwd', ArrowDown: 'back', KeyS: 'back', ArrowLeft: 'left', ArrowRight: 'right',
                   KeyA: 'sleft', KeyD: 'sright', Space: 'fire', ControlLeft: 'fire', ControlRight: 'fire', KeyF: 'fire' };
  const keys = new Set();
  let playing = false, timers = [], pending = [], sent = [], lastAck = 0, fireEdge = false, fireHold = 0, tail = 0;
  let playAddr = '', lastSendAt = 0, sending = false, holdUntil = 0;
  let relayIx = 0;
  const sendGap = () => (!hasKey() ? PLAY.keylessGapMs : relays().length ? PLAY.relayGapMs : PLAY.sendGapMs);
  const nextRelay = () => (relays().length ? relays()[relayIx++ % relays().length] : null);
  const pstat = { sentInputs: 0, batches: 0, resent: 0, rejected: 0, dropped: 0, lost: 0 };

  function onKey(e, down) {
    const k = KEYMAP[e.code];
    if (!k || !playing || e.metaKey || e.altKey) return;
    e.preventDefault();
    if (down && k === 'fire' && !keys.has('fire')) fireEdge = true;
    if (down) keys.add(k); else keys.delete(k);
  }
  function sample() {
    const m = stepMul();
    const turn = ((keys.has('left') ? 6 : 0) - (keys.has('right') ? 6 : 0)) * m;   // angle grows counter-clockwise
    const fwd = ((keys.has('fwd') ? 1 : 0) - (keys.has('back') ? 1 : 0)) * m;
    const side = ((keys.has('sright') ? 1 : 0) - (keys.has('sleft') ? 1 : 0)) * m;
    let fire = 0;
    if (fireEdge) { fire = 1; fireEdge = false; fireHold = PLAY.fireEvery; }
    else if (keys.has('fire') && --fireHold <= 0) { fire = 1; fireHold = PLAY.fireEvery; }
    if (fire) tail = PLAY.tailFire;
    else if (turn || fwd || side) tail = Math.max(tail, PLAY.tail);
    if (turn || fwd || side || fire) { pending.push([turn, fwd, side, fire]); }
    else if (tail > 0) { tail--; pending.push([turn, fwd, side, fire]); }   // a few idle frames after the last key: lets the flash/bob settle
    if (pending.length > PLAY.maxPending) { pstat.dropped += pending.length - PLAY.maxPending; pending = pending.slice(-PLAY.maxPending); }   // stay responsive when the chain lags
    maybeSend();
    maybeResend();
  }
  function maybeSend() {
    const now = performance.now();
    if (sending || !pending.length || sent.length >= PLAY.inFlight || now < holdUntil || now - lastSendAt < sendGap()) return;
    flush(playAddr);
  }
  async function api(path, body) {
    const headers = { 'Content-Type': 'application/json' };
    if (hasKey()) headers['X-API-Key'] = apiKey();
    const r = await fetch(`https://${DEFAULTS.base}${path}`, { method: 'POST', headers, body: JSON.stringify(body) });
    const text = await r.text();
    if (!r.ok) {
      const m = /exitcode=(\d+)/.exec(text);
      const why = m ? ' exit ' + m[1] : ' ' + (text.split(/message\s*:|error"\s*:\s*"/).pop() || '').replace(/[\s"}]+/g, ' ').trim().slice(0, 80);
      throw new Error('HTTP ' + r.status + why);
    }
    return JSON.parse(text);
  }
  async function getLastBatch(addr) {
    const r = await limited(() => api('/api/v3/runGetMethod', { address: addr, method: 'lastBatch', stack: [] }), 1);
    return parseInt(r.stack[0].value, 16);
  }
  // The command external: straight to the contract, or to a relay with the contract's address in front
  // (contracts/Relay.tolk forwards the rest as an internal message to that address).
  function tickMessage(doomAddr, relayAddr, id, inputs, nonce) {
    const body = Boc.beginCell();
    if (relayAddr) body.storeAddress(doomAddr);
    body.storeUint(OP_TICK, 32).storeUint(id, 32).storeUint(inputs.length, 8);
    for (const [t, f, sd, fire] of inputs) body.storeInt(t, 8).storeInt(f, 8).storeInt(sd, 8).storeUint(fire, 8);
    if (nonce) body.storeUint(nonce, 32);   // resend: a different message hash gets a fresh broadcast
    // ext_in_msg_info$10 src:addr_none dest import_fee:0 init:no body:^Cell
    return Boc.beginCell().storeUint(2, 2).storeUint(0, 2).storeAddress(relayAddr || doomAddr).storeCoins(0).storeBit(0).storeBit(1).storeRef(body.endCell()).endCell();
  }
  async function flush(addr) {
    sending = true;
    const inputs = pending.splice(0, PLAY.maxBatch);
    const id = lastAck + 1 + sent.length;   // consecutive ids after the last confirmed one
    let rec = null;
    try {
      rec = { id, inputs, boc: Boc.toBase64(Boc.serialize(tickMessage(addr, nextRelay(), id, inputs, 0))), at: performance.now(), sentAt: performance.now(), tries: 1 };
      sent.push(rec); lastSendAt = rec.at;
      await limited(() => api('/api/v3/message', { boc: rec.boc }), 3); pstat.batches++; pstat.sentInputs += inputs.length;
    } catch (e) {
      pstat.rejected++;
      log(`batch ${id} rejected: ${e.message}`, 'err');
      if (rec) sent = sent.filter(r => r !== rec);
      pending.unshift(...inputs);
      holdUntil = performance.now() + PLAY.holdMs;   // wait for a confirmation (ack clears it) before trying again
    } finally { sending = false; }
    showPlayStats();
  }
  // called for every transaction we see (ws / poll): the external's body names the batch that landed
  function ackFromTx(tx) {
    if (!playing) return;
    const im = tx.in_msg;
    if (!im || !im.message_content || !im.message_content.body) return;   // external, or a relay's internal message
    let id;
    try { const sl = Boc.parse(Boc.fromBase64(im.message_content.body)).beginParse(); if (sl.loadUint(32) !== OP_TICK) return; id = sl.loadUint(32); } catch (e) { return; }
    landed(id);
    // contiguous landed batches from the front are applied for sure; a landed batch further ahead may still wait
    while (sent.length && sent[0].landed && sent[0].id === lastAck + 1) { lastAck = sent.shift().id; holdUntil = 0; }
  }
  function landed(id) {   // the external of batch `id` is in a block: it was applied, or it waits in the reorder buffer
    for (const r of sent) if (r.id === id) r.landed = true;
  }
  function confirmUpTo(id) {   // the contract's lastBatch reached id: batches before it that never landed were skipped
    if (id <= lastAck) return;
    const gone = sent.filter(r => r.id <= id && !r.landed);
    if (gone.length) { pstat.lost += gone.length; log(`batch${gone.length > 1 ? 'es' : ''} ${gone.map(r => r.id).join(',')} lost in the mempool, skipped by the contract`, 'warn'); }
    lastAck = id; holdUntil = 0;
    sent = sent.filter(r => r.id > lastAck);
  }
  async function ack(addr) {   // fallback: the get method (lags the stream by ~1 s)
    let v;
    try { v = await getLastBatch(addr); } catch (e) { return; }
    confirmUpTo(v);
    if (sent.length && sent[0].id !== lastAck + 1) {   // ids drifted (someone else is sending?): start over from the confirmed state
      log(`batch ids out of sync (confirmed ${lastAck}, in flight ${sent.map(r => r.id).join(',')}), requeueing`, 'warn');
      pending.unshift(...sent.flatMap(r => r.inputs)); sent = [];
    }
    maybeResend();
    showPlayStats();
  }
  function maybeResend() {
    const now = performance.now();
    const r = sent.find(x => !x.landed);   // the oldest batch not seen in a block: the contract waits for it up to 2 s
    if (!r || sending || now - lastSendAt < sendGap() || r.tries > PLAY.maxResends) return;
    if (now - r.sentAt < PLAY.resendMs || now - r.at < PLAY.resendEveryMs) return;
    r.at = now; lastSendAt = now; r.tries++; pstat.resent++;
    const boc = Boc.toBase64(Boc.serialize(tickMessage(playAddr, nextRelay(), r.id, r.inputs, (Math.random() * 0xffffffff) >>> 0)));
    limited(() => api('/api/v3/message', { boc }), 2).catch(e => { if (!/exit 132/.test(e.message)) log(`resend ${r.id}: ${e.message}`, 'warn'); });
  }
  function showPlayStats() {
    $('playstats').textContent = playing
      ? `keys ${[...keys].join(' ') || '-'}   pending ${pending.length}   unconfirmed ${sent.length} (confirmed batch ${lastAck})   sent ${pstat.sentInputs} inputs in ${pstat.batches} batches, resent ${pstat.resent}, lost ${pstat.lost}, rejected ${pstat.rejected}, dropped ${pstat.dropped}`
      : '';
  }
  async function startPlay() {
    const addr = currentAddr();
    if (current.watchOnly) { log('this server is watch only (the on-chain AI plays)', 'warn'); return; }
    if (!isFree(current)) log('someone moved here less than 2 minutes ago; you are sharing the player', 'warn');
    try { lastAck = await getLastBatch(addr); } catch (e) { log('cannot read lastBatch: ' + e.message, 'err'); return; }
    sent = []; pending = []; keys.clear(); tail = 0; playAddr = addr; lastSendAt = 0; holdUntil = 0; sending = false;
    playing = true; $('play').classList.add('on'); $('hint').hidden = false; $('about').hidden = true; $('about-play').hidden = false;
    PLAY.rate = Number($('rate').value) || 10;
    timers = [setInterval(sample, 1000 / PLAY.rate), setInterval(() => ack(addr), hasKey() ? PLAY.ackMs : PLAY.keylessAckMs)];
    log(`play: confirmed batch ${lastAck}; ${PLAY.rate} inputs/s${relays().length ? `, ${relays().length} input relays` : ''}${hasKey() ? '' : ', no api key: inputs go out every ~1.6 s'}; arrows/WASD move, space fires`);
    showPlayStats();
  }
  function stopPlay() {
    playing = false; timers.forEach(clearInterval); timers = []; keys.clear();
    $('play').classList.remove('on'); $('hint').hidden = true; $('about').hidden = false; $('about-play').hidden = true; showPlayStats();
  }
  window.addEventListener('keydown', (e) => onKey(e, true));
  window.addEventListener('keyup', (e) => onKey(e, false));
  window.addEventListener('blur', () => keys.clear());
  // RSET: teleport to the level start and revive the targets; goes through a relay like the inputs
  async function sendReset() {
    const addr = currentAddr();
    if (current.watchOnly) { log('this server is watch only', 'warn'); return; }
    let id;
    if (playing) { pending = []; sent = []; id = lastAck + 1; }
    else { try { id = (await getLastBatch(addr)) + 1; } catch (e) { log('cannot read lastBatch: ' + e.message, 'err'); return; } }
    const relay = nextRelay();
    const body = Boc.beginCell();
    if (relay) body.storeAddress(addr);
    body.storeUint(0x52534554, 32).storeUint(id, 32).storeInt(1056 << 16, 32).storeInt(-3616 << 16, 32).storeUint(128, 16);
    const msg = Boc.beginCell().storeUint(2, 2).storeUint(0, 2).storeAddress(relay || addr).storeCoins(0).storeBit(0).storeBit(1).storeRef(body.endCell()).endCell();
    try { await limited(() => api('/api/v3/message', { boc: Boc.toBase64(Boc.serialize(msg)) }), 3); log(`reset sent (batch ${id}): back to the start, targets revived`); if (playing) { lastAck = id; holdUntil = 0; } }
    catch (e) { log('reset rejected: ' + e.message, 'err'); }
  }
  $('reset').onclick = sendReset;
  $('play').onclick = () => (playing ? stopPlay() : startPlay());
  $('rate').onchange = () => { if (playing) { stopPlay(); startPlay(); } };

  $('connect').onclick = () => (ws ? disconnect() : connectWs());
  $('poll').onclick = () => (pollTimer ? disconnect() : startPoll());
  $('pause').onclick = () => { paused = !paused; $('pause').classList.toggle('on', paused); };

  // start: pick the first free server once the statuses are in (the demo if none), then connect
  renderServers();
  (async () => {
    applyMode();
    await probeAll();
    const free = servers.find(sv => !sv.watchOnly && isFree(sv));
    if (free && free.addr !== current.addr && !params.get('addr')) selectServer(free.addr);
    else if (!free && !params.get('addr') && SRV.demo) selectServer(SRV.demo.addr);
  })();
})();
