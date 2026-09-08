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
    addr: 'kQBxJSn7hpqEbRTEeY57W840wOJNJIvXovFrXzmdphW-Yc_Q',   // current testnet deployment (public); override with ?addr=
    pollMs: 1100,          // keyless polling interval (toncenter allows ~1 request/s without a key)
    pollLimit: 60,
  };

  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  $('addr').value = params.get('addr') || CFG.addr || DEFAULTS.addr;
  const apiKey = () => CFG.key || '';   // never shown in the UI
  const hosted = !apiKey();
  if (hosted) { $('fps').value = '15'; $('connect').hidden = true; }

  const canvas = $('screen');
  const ctx = canvas.getContext('2d');
  ctx.imageSmoothingEnabled = false;
  let off = null; // offscreen canvas at native resolution

  const seen = new Map();   // frameNo -> frame
  let queue = [];           // frames waiting to be shown (sorted by frameNo)
  let lastShown = 0;
  let paused = false;
  let ws = null, pollTimer = null, pollLt = 0;
  const stat = { received: 0, dup: 0, shown: 0, partial: 0, lastNow: 0, latency: 0, gas: 0, queued: 0, playFps: 25 };

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

  function ingestTx(tx) {
    for (const f of framesFromTx(tx)) {
      stat.received++;
      if (seen.has(f.frameNo)) { stat.dup++; continue; }
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
    $('stats').textContent =
      `frame      ${f.frameNo}\n` +
      `res        ${f.w}x${f.h}${f.flags & 1 ? '  PARTIAL (gas budget hit)' : ''}\n` +
      `pos        ${(f.px / 65536).toFixed(1)}, ${(f.py / 65536).toFixed(1)}  z=${f.viewz}\n` +
      `angle      ${(f.angle * 360 / 512).toFixed(1)} deg\n` +
      `tx gas     ${f.gas}\n` +
      `on-chain q ${f.queued} inputs\n` +
      `block time ${new Date(f.now * 1000).toLocaleTimeString()}  (latency ${stat.latency.toFixed(1)} s)\n` +
      `buffer     ${queue.length} frames, play ${stat.playFps.toFixed(1)} fps\n` +
      `received   ${stat.received} (dup ${stat.dup}), shown ${stat.shown}, partial ${stat.partial}`;
  }

  // playback loop: adaptive rate around the nominal fps
  let nextAt = 0;
  function tick(ts) {
    requestAnimationFrame(tick);
    if (paused) return;
    const nominal = Math.max(1, Number($('fps').value) || 25);
    let fps = nominal;
    if (queue.length > 40) fps = nominal * 3;
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
    let pingTimer = null;
    ws.onopen = () => {
      ws.send(JSON.stringify({ operation: 'subscribe', id: '1', addresses: [addr], types: ['transactions'], min_finality: 'confirmed' }));
      pingTimer = setInterval(() => { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ operation: 'ping' })); }, 10000);
      setStatus('subscribed (ws)', 'on');
      $('connect').classList.add('on');
      log('ws open, subscribed to ' + addr);
      loadHistory(addr, key, 60);
    };
    ws.onmessage = (ev) => {
      let d; try { d = JSON.parse(ev.data); } catch (e) { return; }
      if (d.status) return;
      if (d.type === 'transactions') {
        const txs = [...(d.transactions || [])].sort((a, b) => Number(a.lt) - Number(b.lt));
        for (const tx of txs) ingestTx(tx);
      } else if (d.type === 'trace_invalidated') {
        log('trace invalidated ' + d.trace_external_hash_norm, 'warn');
      }
    };
    ws.onclose = (e) => { clearInterval(pingTimer); setStatus('ws closed ' + e.code, 'err'); $('connect').classList.remove('on'); log('ws closed ' + e.code + ' ' + e.reason, 'err'); if (ws) setTimeout(connectWs, 2000); };
    ws.onerror = () => { log('ws error', 'err'); };
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

  $('connect').onclick = () => (ws ? disconnect() : connectWs());
  $('poll').onclick = () => (pollTimer ? disconnect() : startPoll());
  $('pause').onclick = () => { paused = !paused; $('pause').classList.toggle('on', paused); };
  if (hosted) startPoll(); else connectWs();
})();
