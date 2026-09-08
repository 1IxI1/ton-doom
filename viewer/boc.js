// Minimal BOC (bag of cells) parser for the browser. No dependencies.
// Usage: const root = Boc.parse(Boc.fromBase64(b64)); const s = root.beginParse(); s.loadUint(32) ...
const Boc = (() => {
  function fromBase64(b64) {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  class Cell {
    constructor(data, nbits, refs) {
      this.data = data;   // Uint8Array, MSB-first, may have unused low bits in last byte
      this.nbits = nbits; // number of valid bits
      this.refs = refs;   // Cell[]
    }
    beginParse() { return new Slice(this); }
  }

  class Slice {
    constructor(cell) { this.cell = cell; this.pos = 0; this.refPos = 0; }
    get remainingBits() { return this.cell.nbits - this.pos; }
    get remainingRefs() { return this.cell.refs.length - this.refPos; }
    bit(i) { return (this.cell.data[i >> 3] >> (7 - (i & 7))) & 1; }
    // returns Number for n <= 52, BigInt otherwise
    loadUint(n) {
      if (this.pos + n > this.cell.nbits) throw new Error('slice underflow');
      if (n <= 52) {
        let v = 0;
        for (let i = 0; i < n; i++) v = v * 2 + this.bit(this.pos + i);
        this.pos += n;
        return v;
      }
      let v = 0n;
      for (let i = 0; i < n; i++) v = (v << 1n) | BigInt(this.bit(this.pos + i));
      this.pos += n;
      return v;
    }
    loadInt(n) {
      let v = this.loadUint(n);
      if (n <= 52) { if (v >= 2 ** (n - 1)) v -= 2 ** n; return v; }
      if (v >= (1n << BigInt(n - 1))) v -= (1n << BigInt(n));
      return v;
    }
    // load n bits into a Uint8Array (MSB-first)
    loadBits(n) {
      if (this.pos + n > this.cell.nbits) throw new Error('slice underflow');
      const out = new Uint8Array((n + 7) >> 3);
      for (let i = 0; i < n; i++) if (this.bit(this.pos + i)) out[i >> 3] |= 0x80 >> (i & 7);
      this.pos += n;
      return out;
    }
    skipBits(n) { this.pos += n; return this; }
    loadRef() {
      if (this.refPos >= this.cell.refs.length) throw new Error('ref underflow');
      return this.cell.refs[this.refPos++];
    }
    loadCoins() { const n = this.loadUint(4); return n ? this.loadUint(8 * n) : 0; }
    loadAddress() {
      const tag = this.loadUint(2);
      if (tag === 0) return null;
      if (tag !== 2) throw new Error('unsupported address');
      if (this.loadUint(1)) throw new Error('anycast unsupported');
      const wc = this.loadInt(8);
      const h = this.loadBits(256);
      return wc + ':' + Array.from(h).map(b => b.toString(16).padStart(2, '0')).join('');
    }
  }

  function parse(bytes) {
    if (bytes[0] !== 0xb5 || bytes[1] !== 0xee || bytes[2] !== 0x9c || bytes[3] !== 0x72) throw new Error('bad BOC magic');
    const flags = bytes[4];
    const hasIdx = !!(flags & 0x80);
    const refSize = flags & 7;
    const offSize = bytes[5];
    let p = 6;
    const rd = (sz) => { let v = 0; for (let i = 0; i < sz; i++) v = v * 256 + bytes[p++]; return v; };
    const n = rd(refSize);
    const nroots = rd(refSize);
    rd(refSize); // absent
    rd(offSize); // total size
    const roots = [];
    for (let i = 0; i < nroots; i++) roots.push(rd(refSize));
    if (hasIdx) p += n * offSize;
    const raw = [];
    for (let i = 0; i < n; i++) {
      const d1 = bytes[p], d2 = bytes[p + 1];
      p += 2;
      if (d1 & 8) throw new Error('exotic cell');
      const nrefs = d1 & 7;
      const nbytes = (d2 + 1) >> 1;
      const data = bytes.slice(p, p + nbytes);
      p += nbytes;
      let nbits = nbytes * 8;
      if (d2 & 1) {
        // strip padding: last set bit
        let last = nbytes - 1;
        while (last >= 0 && data[last] === 0) last--;
        const b = data[last];
        let tz = 0;
        while (((b >> tz) & 1) === 0) tz++;
        nbits = last * 8 + (8 - tz) - 1;
      }
      const refs = [];
      for (let r = 0; r < nrefs; r++) refs.push(rd(refSize));
      raw.push({ data, nbits, refs });
    }
    const cells = new Array(n);
    for (let i = n - 1; i >= 0; i--) {
      const r = raw[i];
      cells[i] = new Cell(r.data, r.nbits, r.refs.map(j => cells[j]));
    }
    return cells[roots[0]];
  }

  return { parse, fromBase64, Cell, Slice };
})();
if (typeof module !== 'undefined') module.exports = Boc;
