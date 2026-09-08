#!/usr/bin/env python3
"""Doom-on-TON client tools: build/send tick messages, decode frames, run the demo feeder.

  python3 tools/doom.py send <addr> --batch 1 --inputs "0,1,0;0,1,0"    send one tick with explicit inputs
  python3 tools/doom.py demo <addr> [--rate 20] [--batch 8]              feed the scripted E1M1 walk forever
  python3 tools/doom.py frames <addr> [--limit 20] [--png-dir DIR]       fetch recent frames, dump PNGs
  python3 tools/doom.py state <addr>                                     print get-method state
  python3 tools/doom.py start|stop [<addr>]                              on-chain wanderer AI on/off

Message formats (must match contracts/Doom.tolk):
  tick (external in):  op:32=0x444F4F4D batchId:32 count:8  count x (turn:int8 fwd:int8 side:int8)
  frame (external out): op:32=0x4652414D frameNo:32 w:16 h:16 px:int32 py:int32 angle:16 viewz:int16
                        flags:8 queued:16  ref0 -> pixel cell chain (COLS_PER_CELL columns of H bits)
"""
from __future__ import annotations

import argparse
import base64
import itertools
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import toncenter  # noqa: E402
from boc import Address, Cell, begin_cell, deserialize_boc, external_message  # noqa: E402

OP_TICK = 0x444F4F4D
OP_FRAME = 0x4652414D
OP_RESET = 0x52534554
OP_START = 0x53545254
OP_STOP = 0x53544F50
START = (1056, -3616, 128)   # E1M1 player start (x, y, angle units)
ANGLES = 512


# --------------------------------------------------------------------------- #
# tick messages
# --------------------------------------------------------------------------- #
def tick_body(batch_id: int, inputs) -> Cell:
    inputs = list(inputs)
    assert 1 <= len(inputs) <= 39
    b = begin_cell().store_uint(OP_TICK, 32).store_uint(batch_id, 32).store_uint(len(inputs), 8)
    for turn, fwd, side in inputs:
        b.store_int(turn, 8).store_int(fwd, 8).store_int(side, 8)
    return b.end_cell()


def send_tick(addr: Address, batch_id: int, inputs) -> str:
    msg = external_message(addr, tick_body(batch_id, inputs))
    return toncenter.send_boc(msg.to_boc())


def send_control(addr: Address, batch_id: int, op: int) -> str:
    body = begin_cell().store_uint(op, 32).store_uint(batch_id, 32).end_cell()
    return toncenter.send_boc(external_message(addr, body).to_boc())


def cmd_control(args, op):
    st = get_state(args.addr)
    batch_id = (st["lastBatch"] or 0) + 1
    print("sent", hex(op), "batch", batch_id, "->", send_control(Address.parse(args.addr), batch_id, op)[:12])
    return 0


def send_reset(addr: Address, batch_id: int, x=START[0], y=START[1], angle=START[2]) -> str:
    body = begin_cell().store_uint(OP_RESET, 32).store_uint(batch_id, 32).store_int(x << 16, 32).store_int(y << 16, 32).store_uint(angle, 16).end_cell()
    return toncenter.send_boc(external_message(addr, body).to_boc())


# --------------------------------------------------------------------------- #
# frames
# --------------------------------------------------------------------------- #
def decode_frame(body: Cell):
    """Parse a FrameEvent body cell -> dict with columns (list of ints) or None if not a frame."""
    s = body.begin_parse()
    if s.remaining_bits() < 32 or s.preload_uint(32) != OP_FRAME:
        return None
    s.load_uint(32)
    f = {
        "frameNo": s.load_uint(32),
        "w": s.load_uint(16),
        "h": s.load_uint(16),
        "px": s.load_int(32),
        "py": s.load_int(32),
        "angle": s.load_uint(16),
        "viewz": s.load_int(16),
        "flags": s.load_uint(8),
        "queued": s.load_uint(16),
    }
    cols = []
    c = s.load_ref()
    W, H = f["w"], f["h"]
    while c is not None and len(cols) < W:
        cs = c.begin_parse()
        while cs.remaining_bits() >= H and len(cols) < W:
            cols.append(cs.load_uint(H))
        c = cs.load_ref() if cs.remaining_refs() else None
    f["columns"] = cols
    return f


def frames_from_tx(tx: dict):
    out = []
    for m in tx.get("out_msgs") or []:
        if m.get("destination") is not None:
            continue
        body_b64 = (m.get("message_content") or {}).get("body")
        if not body_b64:
            continue
        try:
            cell = deserialize_boc(base64.b64decode(body_b64))
        except Exception:
            continue
        f = decode_frame(cell)
        if f is None:
            # body may be the whole message; try to find the FrameEvent inside
            continue
        f["lt"] = int(tx["lt"])
        f["now"] = int(tx["now"])
        f["gas"] = int((((tx.get("description") or {}).get("compute_ph") or {}).get("gas_used")) or 0)
        out.append(f)
    return out


# --------------------------------------------------------------------------- #
# demo path: a loop around the E1M1 start area (turn, forward, strafe per frame)
# --------------------------------------------------------------------------- #
def demo_path(ai_seed=None):
    """Endless generator of inputs. ai_seed=None: the waypoint loop from tools/path.py;
    otherwise the wanderer AI from tools/ai.py simulated off-chain (collisions included) with that seed."""
    import json
    from path import DEFAULT_ROUTE, autopilot
    from render import ANGLES, FRAC, Level
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "..", "assets", "e1m1.json")) as f:
        data = json.load(f)
    level = Level(data)
    if ai_seed is None:
        for inp, _ in autopilot(level, DEFAULT_ROUTE, max_frames=10**9, loop=True):
            yield inp
        return
    from ai import AiState, ai_step, frame_begin
    from blockmap import Blockmap
    level.blockmap = Blockmap(data)
    sx, sy, sa = level.player_start
    st = AiState(sx << FRAC, sy << FRAC, sa * ANGLES // 360, rnd=ai_seed & 0xFFFFFFFF)
    while True:
        frame_begin(st)
        turn, fwd = ai_step(level, st)
        yield (turn, fwd, 0)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def cmd_send(args):
    addr = Address.parse(args.addr)
    inputs = []
    for rec in args.inputs.split(";"):
        t, f, s = (int(v) for v in rec.split(","))
        inputs.append((t, f, s))
    h = send_tick(addr, args.batch, inputs)
    print("sent batch", args.batch, "inputs", len(inputs), "hash", h)
    return 0


def get_state(addr_str: str):
    r = toncenter._request("POST", "/api/v3/runGetMethod", body={"address": addr_str, "method": "lastBatch", "stack": []})
    last_batch = int(r["stack"][0]["value"], 16) if r.get("stack") else None
    r2 = toncenter._request("POST", "/api/v3/runGetMethod", body={"address": addr_str, "method": "queued", "stack": []})
    queued = int(r2["stack"][0]["value"], 16) if r2.get("stack") else None
    r3 = toncenter._request("POST", "/api/v3/runGetMethod", body={"address": addr_str, "method": "frameNo", "stack": []})
    frame_no = int(r3["stack"][0]["value"], 16) if r3.get("stack") else None
    return {"lastBatch": last_batch, "queued": queued, "frameNo": frame_no}


def cmd_state(args):
    print(get_state(args.addr))
    acc = toncenter.get_account(args.addr)
    print("balance:", int(acc.get("balance", 0)) / 1e9, "TON", "status:", acc.get("status"))
    return 0


def send_batch(addr, batch_id, inputs, t0, note=""):
    """One send attempt. Returns True if toncenter accepted the message (it may still get lost)."""
    try:
        h = send_tick(addr, batch_id, inputs)
        print(f"[{time.time()-t0:7.1f}s] batch {batch_id} ({len(inputs)} inputs){note} -> {h[:12]}...", flush=True)
        return True
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "exitcode=132" not in msg:
            print(f"send failed: {msg[:140]}", flush=True)
            time.sleep(0.5)
        return False


class Pipeline:
    """Keeps up to two batches in flight (the contract holds lastBatch+2 until lastBatch+1 lands).
    A batch is re-sent if the contract's lastBatch has not reached it within ack_timeout seconds."""

    def __init__(self, addr, addr_str, first_batch, t0, ack_timeout=3.0):
        self.addr, self.addr_str, self.t0, self.ack_timeout = addr, addr_str, t0, ack_timeout
        self.next_id = first_batch
        self.inflight = []   # [batch_id, inputs, sent_at, attempts]
        self.last = first_batch - 1

    def refresh(self):
        try:
            last = get_state(self.addr_str)["lastBatch"]
            if last is not None:
                self.last = max(self.last, last)
        except Exception:  # noqa: BLE001
            pass
        self.inflight = [f for f in self.inflight if f[0] > self.last]

    def push(self, inputs):
        """Blocks until there is room in the window, then sends the batch."""
        while True:
            self.refresh()
            now = time.time()
            for f in self.inflight:
                if now - f[2] > self.ack_timeout:
                    f[3] += 1
                    if send_batch(self.addr, f[0], f[1], self.t0, f" resend #{f[3]}"):
                        f[2] = now
            if len(self.inflight) < 2 and (not self.inflight or self.next_id <= self.last + 2):
                break
            time.sleep(0.3)
        bid = self.next_id
        self.next_id += 1
        sent_at = time.time() if send_batch(self.addr, bid, inputs, self.t0) else 0
        self.inflight.append([bid, inputs, sent_at, 0])


def cmd_demo(args):
    addr = Address.parse(args.addr)
    st = get_state(args.addr)
    batch_id = (st["lastBatch"] or 0) + 1
    print("starting at batch", batch_id, "state", st)
    if not args.no_reset:
        while True:
            try:
                print("reset ->", send_reset(addr, batch_id)[:12], flush=True)
                batch_id += 1
                break
            except Exception as e:  # noqa: BLE001
                print("reset failed:", str(e)[:120], flush=True)
                time.sleep(1.5)
                batch_id = (get_state(args.addr)["lastBatch"] or 0) + 1
        time.sleep(2.0)
    seed = None if args.route == "loop" else (int(time.time()) if args.seed is None else args.seed)
    print("route:", args.route, "seed:", seed, flush=True)
    path = demo_path(seed)
    if args.skip:
        for _ in range(args.skip):
            next(path)
    interval = args.batch / args.rate
    t0 = time.time()
    pipe = Pipeline(addr, args.addr, batch_id, t0)
    next_at = time.time()
    while True:
        inputs = list(itertools.islice(path, args.batch))
        pipe.push(inputs)
        next_at += interval
        delay = next_at - time.time()
        if delay > 0:
            time.sleep(delay)
        elif delay < -5:
            next_at = time.time()


def cmd_frames(args):
    txs = toncenter.get_transactions(args.addr, limit=args.limit)
    frames = []
    for tx in txs:
        frames.extend(frames_from_tx(tx))
    frames.sort(key=lambda f: f["frameNo"])
    for f in frames:
        print(f"frame {f['frameNo']} lt={f['lt']} now={f['now']} gas={f['gas']} pos=({f['px']/65536:.1f},{f['py']/65536:.1f}) "
              f"angle={f['angle']} z={f['viewz']} flags={f['flags']} queued={f['queued']} cols={len(f['columns'])}")
    if args.png_dir and frames:
        from png import write_bitmap_png
        os.makedirs(args.png_dir, exist_ok=True)
        for f in frames:
            write_bitmap_png(os.path.join(args.png_dir, f"frame{f['frameNo']:06d}.png"), f["columns"], f["w"], f["h"], 4)
        print("wrote", len(frames), "PNGs to", args.png_dir)
    return 0


def main(argv=None):
    default_addr = toncenter._DOTENV.get("DOOM_ADDRESS") or os.environ.get("DOOM_ADDRESS")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("send"); p.add_argument("addr", nargs="?", default=default_addr); p.add_argument("--batch", type=int, required=True)
    p.add_argument("--inputs", required=True); p.set_defaults(fn=cmd_send)
    p = sub.add_parser("demo"); p.add_argument("addr", nargs="?", default=default_addr); p.add_argument("--rate", type=float, default=20.0, help="inputs per second")
    p.add_argument("--batch", type=int, default=36, help="inputs per external (<= 39; keep externals <= ~2/s)"); p.add_argument("--max-queue", type=int, default=80)
    p.add_argument("--skip", type=int, default=0, help="skip this many inputs of the route (resume position)")
    p.add_argument("--no-reset", action="store_true", help="do not teleport the player to the start first")
    p.add_argument("--route", choices=["ai", "loop"], default="ai", help="ai: off-chain wanderer simulation (default); loop: fixed waypoints")
    p.add_argument("--seed", type=int, help="wanderer seed (default: current time)"); p.set_defaults(fn=cmd_demo)
    p = sub.add_parser("reset"); p.add_argument("addr", nargs="?", default=default_addr); p.add_argument("--batch", type=int, required=True); p.set_defaults(fn=lambda a: print(send_reset(Address.parse(a.addr), a.batch)))
    p = sub.add_parser("start", help="let the on-chain AI walk by itself"); p.add_argument("addr", nargs="?", default=default_addr); p.set_defaults(fn=lambda a: cmd_control(a, OP_START))
    p = sub.add_parser("stop", help="stop the on-chain AI"); p.add_argument("addr", nargs="?", default=default_addr); p.set_defaults(fn=lambda a: cmd_control(a, OP_STOP))
    p = sub.add_parser("frames"); p.add_argument("addr", nargs="?", default=default_addr); p.add_argument("--limit", type=int, default=20)
    p.add_argument("--png-dir"); p.set_defaults(fn=cmd_frames)
    p = sub.add_parser("state"); p.add_argument("addr", nargs="?", default=default_addr); p.set_defaults(fn=cmd_state)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
