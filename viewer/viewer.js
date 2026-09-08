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
  const DEFAULTS = {
    base: 'testnet.toncenter.com',
    addr: 'kQBfL0vdKQ0cZlSfqPyni47l_unOrWQJk5R71c7mBefTYKy2',   // current testnet deployment (on-chain wanderer AI); override with ?addr=
    pollMs: 1100,          // keyless polling interval (toncenter allows ~1 request/s without a key)
    pollLimit: 60,
  };

  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  $('addr').value = params.get('addr') || CFG.addr || DEFAULTS.addr;
  $('explorer').href = 'https://testnet.tonviewer.com/' + $('addr').value;
  const apiKey = () => CFG.key || '';   // never shown in the UI
  const hosted = !apiKey();
  if (hosted) { $('fps').value = '15'; $('connect').hidden = true; $('pendingopt').hidden = true; }
  const usePending = () => !hosted && $('pending').checked;
  $('pending').onchange = () => { if (ws) connectWs(); };

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
      `on-chain q ${f.queued} inputs\n` +
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
    const addr = $('addr').value.trim(), key = apiKey();
    if (!addr || !key) { setStatus('no config: run python3 tools/viewer_config.py (reads .env)', 'err'); return; }
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
    const r = await fetch(`https://${DEFAULTS.base}/api/v3/transactions?account=${encodeURIComponent(addr)}&${qs}`, { headers });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return (await r.json()).transactions || [];
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
    const addr = $('addr').value.trim(), key = apiKey();
    if (!addr) { setStatus('no contract address (?addr=...)', 'err'); return; }
    setStatus(key ? 'polling' : 'polling (no api key, 1 req/s)', 'on'); $('poll').classList.add('on');
    loadHistory(addr, key, 60);
    let busy = false, backoff = 0;
    const interval = key ? 700 : DEFAULTS.pollMs;
    pollTimer = setInterval(async () => {
      if (busy) return;
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

  // ---- play: keyboard -> inputs (20/s) -> batches (one external per 0.5 s) -> the contract ---------
  // Externals need no signature: batchId must be lastBatch+1 (or +2, one batch may overtake). We keep at
  // most two batches in flight, confirm them through the `lastBatch` get method and resend after 3 s.
  const OP_TICK = 0x444f4f4d;
  const PLAY = { rate: 20, turn: 6, batchMs: 400, maxBatch: 29, inFlight: 2, resendMs: 3000, fireEvery: 8, tail: 3 };
  const KEYMAP = { ArrowUp: 'fwd', KeyW: 'fwd', ArrowDown: 'back', KeyS: 'back', ArrowLeft: 'left', ArrowRight: 'right',
                   KeyA: 'sleft', KeyD: 'sright', Space: 'fire', ControlLeft: 'fire', ControlRight: 'fire', KeyF: 'fire' };
  const keys = new Set();
  let playing = false, timers = [], pending = [], sent = [], nextBatch = 0, lastAck = 0, fireEdge = false, fireHold = 0, tail = 0;
  let playAddr = '', lastFlushAt = 0;
  const pstat = { sentInputs: 0, batches: 0, resent: 0 };

  function onKey(e, down) {
    const k = KEYMAP[e.code];
    if (!k || !playing || e.metaKey || e.altKey) return;
    e.preventDefault();
    if (down && k === 'fire' && !keys.has('fire')) fireEdge = true;
    if (down) keys.add(k); else keys.delete(k);
  }
  function sample() {
    const turn = (keys.has('left') ? PLAY.turn : 0) - (keys.has('right') ? PLAY.turn : 0);   // angle grows counter-clockwise
    const fwd = (keys.has('fwd') ? 1 : 0) - (keys.has('back') ? 1 : 0);
    const side = (keys.has('sright') ? 1 : 0) - (keys.has('sleft') ? 1 : 0);
    let fire = 0;
    if (fireEdge) { fire = 1; fireEdge = false; fireHold = PLAY.fireEvery; }
    else if (keys.has('fire') && --fireHold <= 0) { fire = 1; fireHold = PLAY.fireEvery; }
    if (turn || fwd || side || fire) tail = PLAY.tail;
    else if (tail > 0) tail--;          // a few idle frames after the last key: lets the flash/bob settle
    else return;
    pending.push([turn, fwd, side, fire]);
    // first input after a pause: do not wait for the batch timer
    if (pending.length === 1 && performance.now() - lastFlushAt > PLAY.batchMs) flush(playAddr);
  }
  async function api(path, body) {
    const r = await fetch(`https://${DEFAULTS.base}${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-API-Key': apiKey() }, body: JSON.stringify(body) });
    const text = await r.text();
    if (!r.ok) { const m = /exitcode=(\d+)/.exec(text); throw new Error('HTTP ' + r.status + (m ? ' exit ' + m[1] : '')); }
    return JSON.parse(text);
  }
  async function getLastBatch(addr) {
    const r = await api('/api/v3/runGetMethod', { address: addr, method: 'lastBatch', stack: [] });
    return parseInt(r.stack[0].value, 16);
  }
  function tickMessage(addr, id, inputs) {
    const body = Boc.beginCell().storeUint(OP_TICK, 32).storeUint(id, 32).storeUint(inputs.length, 8);
    for (const [t, f, sd, fire] of inputs) body.storeInt(t, 8).storeInt(f, 8).storeInt(sd, 8).storeUint(fire, 8);
    // ext_in_msg_info$10 src:addr_none dest import_fee:0 init:no body:^Cell
    return Boc.beginCell().storeUint(2, 2).storeUint(0, 2).storeAddress(addr).storeCoins(0).storeBit(0).storeBit(1).storeRef(body.endCell()).endCell();
  }
  async function flush(addr) {
    if (!pending.length || sent.length >= PLAY.inFlight) return;
    const inputs = pending.splice(0, PLAY.maxBatch);
    const id = nextBatch++;
    lastFlushAt = performance.now();
    const rec = { id, inputs, boc: Boc.toBase64(Boc.serialize(tickMessage(addr, id, inputs))), at: performance.now() };
    sent.push(rec);
    try { await api('/api/v3/message', { boc: rec.boc }); pstat.batches++; pstat.sentInputs += inputs.length; }
    catch (e) {
      log(`batch ${id} rejected: ${e.message}`, 'err');
      sent = sent.filter(r => r !== rec); pending.unshift(...inputs);
      try { lastAck = await getLastBatch(addr); sent = sent.filter(r => r.id > lastAck); nextBatch = lastAck + 1 + sent.length; } catch (e2) {}
    }
    showPlayStats();
  }
  async function ack(addr) {
    try { lastAck = await getLastBatch(addr); } catch (e) { return; }
    sent = sent.filter(r => r.id > lastAck);
    const now = performance.now();
    for (const r of sent) {
      if (now - r.at > PLAY.resendMs) {
        r.at = now; pstat.resent++;
        log(`batch ${r.id} not applied in ${PLAY.resendMs / 1000}s, resending`, 'warn');
        api('/api/v3/message', { boc: r.boc }).catch(e => log(`resend ${r.id} failed: ${e.message}`, 'err'));
      }
    }
    showPlayStats();
  }
  function showPlayStats() {
    $('playstats').textContent = playing
      ? `keys ${[...keys].join(' ') || '-'}   pending ${pending.length}   in flight ${sent.length} (last acked batch ${lastAck})   sent ${pstat.sentInputs} inputs in ${pstat.batches} batches, resent ${pstat.resent}`
      : '';
  }
  async function startPlay() {
    const addr = $('addr').value.trim();
    if (!apiKey()) { log('play needs the toncenter API key: run python3 tools/viewer_config.py and open the viewer locally', 'err'); return; }
    try { lastAck = await getLastBatch(addr); } catch (e) { log('cannot read lastBatch: ' + e.message, 'err'); return; }
    nextBatch = lastAck + 1; sent = []; pending = []; keys.clear(); tail = 0; playAddr = addr; lastFlushAt = 0;
    playing = true; $('play').classList.add('on'); $('hint').hidden = false; $('about').hidden = true; $('about-play').hidden = false;
    timers = [setInterval(sample, 1000 / PLAY.rate), setInterval(() => flush(addr), PLAY.batchMs), setInterval(() => ack(addr), 1000)];
    log(`play: batches start at ${nextBatch}; arrows/WASD move, space fires`);
    showPlayStats();
  }
  function stopPlay() {
    playing = false; timers.forEach(clearInterval); timers = []; keys.clear();
    $('play').classList.remove('on'); $('hint').hidden = true; $('about').hidden = false; $('about-play').hidden = true; showPlayStats();
  }
  window.addEventListener('keydown', (e) => onKey(e, true));
  window.addEventListener('keyup', (e) => onKey(e, false));
  window.addEventListener('blur', () => keys.clear());
  $('play').onclick = () => (playing ? stopPlay() : startPlay());
  if (hosted) $('play').hidden = true;

  $('connect').onclick = () => (ws ? disconnect() : connectWs());
  $('poll').onclick = () => (pollTimer ? disconnect() : startPoll());
  $('pause').onclick = () => { paused = !paused; $('pause').classList.toggle('on', paused); };
  if (hosted) startPoll(); else connectWs();
})();
