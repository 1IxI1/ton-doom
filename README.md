# Doom on TON

![Doom E1M1 rendered on-chain, played back live from testnet](docs/demo.gif)

*Live playback from testnet: every frame is rendered by the smart contract in the TVM and read back
from the chain by the viewer ([mp4](docs/demo.mp4)). Watch it live: **https://1ixi1.github.io/ton-doom/**
(polls toncenter once a second without an API key; frames appear while the demo feeder is running).*

Doom E1M1 rendered **inside the TVM** on TON testnet. The smart contract keeps the player state and the
level's BSP tree, receives player inputs as external messages, renders 1-bit frames on-chain and emits
each frame as an external-out message. A browser viewer subscribes to the contract on toncenter's
streaming API and plays the frames back.

Nothing is rendered off-chain: only `turn / forward / strafe` inputs go to the chain.

## How it works

```
 inputs (3 bytes/frame) ──external──▶ Doom.tolk ──┬─▶ FrameEvent (ext-out, 80x60 1-bit)  ──▶ toncenter ──▶ viewer
                                       ▲          └─▶ Continue (internal to itself, 0.1 TON)
                                       └──────────────────┘   one frame per transaction, up to ~10 per block
```

* `contracts/Doom.tolk` — the renderer (Tolk 1.4.2). Doom's algorithm: front-to-back BSP walk,
  bbox culling, solid-column mask, per-column wall/portal drawing with ceiling/floor clips packed into a
  257-bit integer per screen column, ordered dithering by distance and sector light.
  `contracts/render-asm.tolk` — the hot column loops as hand-written TVM assembly, generated and verified
  by `tools/asmgen.py` + `tools/stacksim.py`.
* External `DOOM` message: `op:32 batchId:32 count:8 (turn:int8 fwd:int8 side:int8) x count`.
  Batches must arrive strictly in order (`batchId == lastBatch + 1`); anything else is rejected before
  `ACCEPT` (free). Inputs are queued in the contract state; every transaction pops one input, moves the
  player (noclip), renders and emits the frame, then sends itself a `CONT` message if the queue is not
  empty. Frames therefore continue across blocks at up to the block gas limit (~10 frames per 0.4 s block).
* A gas guard keeps every transaction under 1M gas: when the budget is hit, the frame is emitted partial
  (`flags & 1`) instead of failing.
* **Wanderer AI on-chain** (`STRT` / `STOP` externals): when the input queue is empty the contract drives
  the player itself — three probes (ahead, ±45°) against a Doom-style blockmap of blocking lines, turning on
  the spot when blocked, steering away from walls while walking, a little random drift (LCG in state). The
  self-message chain then runs with no external process at all, until `STOP` or the balance drops below
  0.4 TON. `tools/ai.py` + `tools/blockmap.py` are the bit-exact reference.
* `tools/render.py` — the reference renderer in Python, bit-exact with the contract (the tests compare
  frame hashes). `tools/golden.py` builds golden frames, `tools/level_encode.py` packs E1M1 into cells,
  `tools/wad.py` parses the WAD, `tools/boc.py` is a dependency-free BOC/cell library.
* `viewer/index.html` — the viewer (WebSocket streaming, `min_finality: confirmed`, adaptive playback).
* `tools/doom.py` — CLI: send inputs, run the autopilot demo feeder, dump frames as PNG.

## Running

```bash
acton build && acton test                  # emulator: golden-frame tests, gas numbers
acton script scripts/deploy.tolk --net testnet     # deploy (wallet main-w9), prints DOOM_ADDRESS
python3 tools/doom.py demo --rate 25 --batch 30   # feed the scripted E1M1 walk (address from .env)
python3 tools/viewer_config.py && open viewer/index.html   # viewer config (address, key) from .env
```

`.env` (not committed) holds `TONCENTER_TESTNET_API_KEY=...` and `DOOM_ADDRESS=...` (printed by the deploy
script). Needs `assets/doom1.wad` (shareware; `python3 tools/wad.py assets/doom1.wad E1M1 --json assets/e1m1.json`).

## Numbers (testnet, 80x120 shown at 4:3)

* ~700k gas per frame on average, max ~980k (guarded); ~0.05 TON per frame. Vertical resolution is free (cost is per column).
* Frames reach the viewer ~0.6 s after the block time (`confirmed` finality).
* External messages to one address are rate-limited by the mempool (~30 per 10 s), hence batches of
  20–40 inputs per external and the self-message chain for rendering.
