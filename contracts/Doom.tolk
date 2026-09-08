tolk 1.4.2

// Doom on TON: a wall-only 1-bit renderer of Doom E1M1 running inside TVM.
//
// Every external "tick" message carries a batch of player inputs. For each input the contract
// moves the player (noclip), renders the view with the level's BSP tree and emits the frame as an
// external-out message (FrameEvent). State is committed after every frame so that running out of
// gas in the middle of a batch keeps the frames already rendered.
//
// The integer math mirrors tools/render.py bit for bit. The hot column loops are generated TVM
// assembly (tools/asmgen.py, verified against tools/render.py with tools/stacksim.py).

import "@stdlib/gas-payments"
import "render-asm"

contract Doom {
    author: "ton-doom"
    version: "0.2.0"
    description: "Doom E1M1 rendered on-chain; frames are external-out messages"
    storage: Storage
    incomingExternal: Tick
    emittedEvents: FrameEvent
}

// ---------------------------------------------------------------------------------------------
// Screen / fixed point constants
// ---------------------------------------------------------------------------------------------
const W = 80;                     // columns
const H = 120;                    // rows (multiple of 8); render-asm.tolk is generated for this H
const ASPECT_Y = 2;               // rows per column unit: the viewer shows each row at half height (4:3)
const CX = W / 2;
const CY = H / 2;
const FOCAL = W / 2;              // 90 degree horizontal FOV
const FULL = (1 << H) - 1;        // all rows
const FULLW = (1 << W) - 1;       // all columns
const OPEN_ALL = FULL << H;       // framebuffer column: pixels = 0, opening = all rows
const COLS_PER_CELL = 1023 / H;   // columns packed per output cell
const ANGLES = 512;
const ANGLE_MASK = ANGLES - 1;
const QUARTER = ANGLES / 4;
const VIEWHEIGHT = 41;
const SPEED_FWD = 8;              // map units per frame per input unit
const SPEED_SIDE = 6;
const SIN_PER_LEAF = 56;
const SIN_BITS = 18;
const MAX_INPUTS = 40;            // per external message (count:8 + 40 x 24 bits fit in one cell)
const GAS_BUDGET = 950000;        // hard stop for rendering work (leaves ~50k to pack, emit, save, continue)
const GAS_NODE_MARGIN = 25000;    // a node/subsector visit may add this much before the next check
// (the per-run column guard, 2600 gas per column, is baked into the generated seg drawers)
const CONT_VALUE = 100000000;     // 0.1 TON attached to each self-continuation (buys the 1M gas limit)
const SEG_TAIL_BITS = 66;         // bits of a seg record after kind + 4 coords
const SHADE_UNIT2 = 2 * 96 << 16; // (d1 + d2) in 16.16 divided by this gives the shade level step (96 map units)

// dither patterns: 8-row byte (4-row nibble with each bit doubled for ASPECT_Y = 2) replicated over the
// column; even / odd column phases
const REP = FULL / 255;                    // 0x01 0x01 ... (H/8 bytes)
const PAT_L2_0 = 0b11001100 * REP;         // 25 %    (floor, far walls)
const PAT_L2_1 = 0;
const PAT_L3_0 = 0b11001100 * REP;         // 50 %
const PAT_L3_1 = 0b00110011 * REP;
const PAT_L4_0 = 0b11001100 * REP;         // 75 %
const PAT_L4_1 = 0b11111111 * REP;
const PAT_L5_0 = FULL;                     // 100 %
const PAT_L5_1 = FULL;
const FLOOR01 = PAT_L2_0 | (PAT_L2_1 << H); // floor pattern pair packed for the asm seg drawers

// Doom checkcoord table packed: nibble per boxpos = x1sel y1sel x2sel y2sel (1 = right / bottom)
const CHECKCOORD = 9 | (8 << 4) | (12 << 8) | (1 << 16) | (14 << 24) | (3 << 32) | (7 << 36) | (6 << 40);

const OP_TICK = 0x444F4F4D;     // "DOOM"  external: enqueue inputs
const OP_CONT = 0x434F4E54;     // "CONT"  internal from self: render the next queued input
const OP_RESET = 0x52534554;    // "RSET"  external: teleport the player back to the level start, drop the queue

const ERR_BAD_OP = 130;
const ERR_BAD_COUNT = 131;
const ERR_OLD_BATCH = 132;

// ---------------------------------------------------------------------------------------------
// Storage & messages
// ---------------------------------------------------------------------------------------------
struct Storage {
    frameNo: uint32
    px: int32          // 16.16 fixed point map units
    py: int32
    angle: uint16      // 0..ANGLES-1
    lastBatch: uint32  // highest batchId accepted so far (externals must carry a larger one)
    qpos: uint8        // inputs already consumed from the head queue cell
    queue: cell?       // FIFO of inputs: cells [count:8, count x (turn:int8 fwd:int8 side:int8)], ref0 = next
    level: cell        // see tools/level_encode.py
}

fun Storage.load(): Storage {
    return Storage.fromCell(contract.getData());
}

fun Storage.save(self) {
    contract.setData(self.toCell());
}

// external-in body: op:32 batchId:32 count:8 then count x (turn:int8 forward:int8 strafe:int8)
struct (0x444F4F4D) Tick {
    batchId: uint32
    count: uint8
    inputs: RemainingBitsAndRefs
}

struct (0x4652414D) FrameEvent {   // "FRAM"
    frameNo: uint32
    w: uint16
    h: uint16
    px: int32
    py: int32
    angle: uint16
    viewz: int16
    flags: uint8       // bit 0: partial frame (gas budget hit before the BSP walk finished)
    queued: uint16     // inputs still queued after this frame
    pixels: cell       // chain of cells, COLS_PER_CELL columns of H bits each, next cell in ref 0
}

struct (0x434F4E54) Continue {     // "CONT" internal self-message
    hop: uint32
}

// ---------------------------------------------------------------------------------------------
// TVM helpers
// ---------------------------------------------------------------------------------------------
@pure
fun ubitSize(x: int): int
    asm "UBITSIZE"

@pure
fun slice.refAt(self, idx: int): cell
    asm "PLDREFVAR"

// ---------------------------------------------------------------------------------------------
// Frame-global state
// ---------------------------------------------------------------------------------------------
global gFb: array<int>;   // W entries: pixels | (opening << H)
global gSolid: int;       // W-bit mask of fully closed columns
global gPats: array<int>; // wall patterns: [lvl*2] even columns, [lvl*2+1] odd columns, lvl 0 (near) .. 4 (far)
global gRows: array<int>; // gRows[n] = mask of the top n rows, n in 0..H
global gPartial: bool;    // set when the gas budget stopped the BSP walk

const DEBUG = false;
global gProfNodes: int;
global gProfSegs: int;
global gProfSegsDrawn: int;
global gProfCols: int;
global gProfGasCols: int;
global gProfGasSegs: int;
global gProfGasCulled: int;
global gProfGasSetup: int;

struct (0x50524F46) ProfEvent {   // "PROF"
    gasInit: uint32
    gasBsp: uint32
    gasPack: uint32
    gasTotal: uint32
    gasCols: uint32
    gasSegs: uint32
    gasCulled: uint32
    gasSetup: uint32
    nodes: uint16
    segs: uint16
    segsDrawn: uint16
    cols: uint32
}

// ---------------------------------------------------------------------------------------------
// Trig table
// ---------------------------------------------------------------------------------------------
fun sinAt(table: cell, a: int): int {
    val leaf = a / SIN_PER_LEAF;
    val off = (a - leaf * SIN_PER_LEAF) * SIN_BITS;
    val branch = table.beginParse().refAt(leaf >> 2);
    val leafCell = branch.beginParse().refAt(leaf & 3);
    var s = leafCell.beginParse();
    s.skipBits(off);
    return s.preloadInt(SIN_BITS);
}

// ---------------------------------------------------------------------------------------------
// Camera: (px, py) 16.16 position, (sn, cs) 16.16 sin/cos of the view angle, vz integer view z
// ---------------------------------------------------------------------------------------------
struct Cam {
    px: int
    py: int
    sn: int
    cs: int
    vz: int
}

@inline
fun project(s: int, d: int): int {
    return ((CX << 16) + mulDivFloor(s * FOCAL, 1 << 16, d) + (1 << 15)) >> 16;
}

@inline
fun colMask(x1: int, x2: int): int {
    return ((1 << (x2 - x1)) - 1) << x1;
}

// R_PointOnSide: 0 = front (child 0), 1 = back (child 1)
@inline
fun pointOnSide(px: int, py: int, nx: int, ny: int, ndx: int, ndy: int): int {
    return ((py - (ny << 16)) * ndx < ndy * (px - (nx << 16))) ? 0 : 1;
}

// floor height of the subsector containing (px, py), integer map units
fun floorAt(root: cell, rootIsSS: bool, px: int, py: int): int {
    var c = root;
    var isSS = rootIsSS;
    while (!isSS) {
        var s = c.beginParse();
        val flags = s.loadUint(6);
        val nx = s.loadInt(16);
        val ny = s.loadInt(16);
        val ndx = s.loadInt(16);
        val ndy = s.loadInt(16);
        val side = pointOnSide(px, py, nx, ny, ndx, ndy);
        c = s.refAt(side);
        isSS = ((flags >> (side * 3)) & 1) == 1;
    }
    return c.beginParse().preloadInt(16);
}

// Doom R_CheckBBox in view space. bbox = (top, bottom, left, right) in map units.
@inline_ref
fun checkBBox(top: int, bottom: int, left: int, right: int, cam: Cam): bool {
    val vx = cam.px >> 16;
    val vy = cam.py >> 16;
    val boxx = vx <= left ? 0 : (vx < right ? 1 : 2);
    val boxy = vy >= top ? 0 : (vy > bottom ? 1 : 2);
    val boxpos = boxy * 4 + boxx;
    if (boxpos == 5) {
        return true;
    }
    val sel = (CHECKCOORD >> (boxpos * 4)) & 15;
    val x1 = ((sel & 8) != 0) ? right : left;
    val y1 = ((sel & 4) != 0) ? bottom : top;
    val x2 = ((sel & 2) != 0) ? right : left;
    val y2 = ((sel & 1) != 0) ? bottom : top;
    val dx1 = (x1 << 16) - cam.px;
    val dy1 = (y1 << 16) - cam.py;
    val s1 = (dx1 * cam.sn - dy1 * cam.cs) >> 16;
    val d1 = (dx1 * cam.cs + dy1 * cam.sn) >> 16;
    val dx2 = (x2 << 16) - cam.px;
    val dy2 = (y2 << 16) - cam.py;
    val s2 = (dx2 * cam.sn - dy2 * cam.cs) >> 16;
    val d2 = (dx2 * cam.cs + dy2 * cam.sn) >> 16;
    if (s1 * d2 - d1 * s2 > 0) {
        return true;   // spans >= 180 degrees: viewer on the box edge line
    }
    val in1 = (s1 + d1 >= 0) & (s1 <= d1);
    val in2 = (s2 + d2 >= 0) & (s2 <= d2);
    if (!in1 & !in2) {
        val lIn = (s1 + d1 <= 0) & (s2 + d2 >= 0);
        val rIn = (s1 <= d1) & (s2 >= d2);
        if (!lIn & !rIn) {
            return false;
        }
    }
    val cx1 = in1 ? project(s1, d1) : 0;
    val cx2 = in2 ? project(s2, d2) : W;
    if (cx1 >= cx2) {
        return false;
    }
    return (colMask(cx1, cx2) & ~gSolid) != 0;
}

// ---------------------------------------------------------------------------------------------
// BSP traversal
// ---------------------------------------------------------------------------------------------
@inline_ref
fun renderNode(c: cell, isSS: bool, cam: Cam): void {
    if (getGasConsumedAtTheMoment() > GAS_BUDGET - GAS_NODE_MARGIN) {
        gPartial = true;
        return;
    }
    if (isSS) {
        renderSubsector(c, cam);
        return;
    }
    var s = c.beginParse();
    if (DEBUG) { gProfNodes += 1; }
    val flags = s.loadUint(6);   // d0:3 d1:3, see tools/level_encode.py
    val nx = s.loadInt(16);
    val ny = s.loadInt(16);
    val ndx = s.loadInt(16);
    val ndy = s.loadInt(16);
    val side = pointOnSide(cam.px, cam.py, nx, ny, ndx, ndy);
    val d = (flags >> (side * 3)) & 7;   // bit0: near is subsector, bit1: far is subsector, bit2: test far bbox
    renderNode(s.refAt(side), (d & 1) == 1, cam);
    if ((gSolid == FULLW) | gPartial) {
        return;   // screen full (or out of gas): nothing behind can be visible
    }
    val far = side ^ 1;
    val farIsSS = (d & 2) == 2;
    if ((d & 4) == 0) {
        renderNode(s.refAt(far), farIsSS, cam);   // small subtree: not worth a bbox test
        return;
    }
    // bbox of the far child: bbox0 at bit 70, bbox1 at bit 134
    if (far == 1) {
        s.skipBits(64);
    }
    val top = s.loadInt(16);
    val bottom = s.loadInt(16);
    val left = s.loadInt(16);
    val right = s.loadInt(16);
    if (checkBBox(top, bottom, left, right, cam)) {
        renderNode(s.refAt(far), farIsSS, cam);
    }
}

@inline_ref
fun renderSubsector(c: cell, cam: Cam): void {
    var s = c.beginParse();
    s.skipBits(32);
    val n = s.loadUint(4);
    repeat (n) {
        if (DEBUG) {
            val gasA = getGasConsumedAtTheMoment();
            drawSeg(mutate s, cam);
            gProfGasSegs += getGasConsumedAtTheMoment() - gasA;
        } else {
            drawSeg(mutate s, cam);
        }
    }
    if (s.remainingRefsCount() > 0) {
        renderSubsector(s.loadRef(), cam);
    }
}

// Reads one seg record from `s` and draws it. Transform / clipping / projection here (early exits for
// culled segs); the run and column loops are in render-asm.tolk.
@inline_ref
fun drawSeg(mutate s: slice, cam: Cam): void {
    val gasE = getGasConsumedAtTheMoment();
    if (DEBUG) { gProfSegs += 1; }
    val kind = s.loadUint(2);
    val x1w = s.loadInt(16);
    val y1w = s.loadInt(16);
    val x2w = s.loadInt(16);
    val y2w = s.loadInt(16);
    val dx1 = (x1w << 16) - cam.px;
    val dy1 = (y1w << 16) - cam.py;
    // back-face test in world space: cross(v2 - v1, p - v1) >= 0 means facing away / edge-on
    if ((x2w - x1w) * (-dy1) - (y2w - y1w) * (-dx1) >= 0) {
        s.skipBits(SEG_TAIL_BITS);
        if (DEBUG) { gProfGasCulled += getGasConsumedAtTheMoment() - gasE; }
        return;
    }
    val dx2 = (x2w << 16) - cam.px;
    val dy2 = (y2w << 16) - cam.py;
    val s1 = (dx1 * cam.sn - dy1 * cam.cs) >> 16;
    val d1 = (dx1 * cam.cs + dy1 * cam.sn) >> 16;
    val s2 = (dx2 * cam.sn - dy2 * cam.cs) >> 16;
    val d2 = (dx2 * cam.cs + dy2 * cam.sn) >> 16;
    val C = s1 * d2 - d1 * s2;
    if ((C >= 0) | ((d1 <= 0) & (d2 <= 0))) {   // edge-on after rounding, or entirely behind
        s.skipBits(SEG_TAIL_BITS);
        return;
    }
    var x1 = 0;
    if (d1 > 0) {
        if (s1 > d1) {
            s.skipBits(SEG_TAIL_BITS);
            return;
        }
        if (s1 + d1 >= 0) {
            x1 = project(s1, d1);
        }
    }
    var x2 = W;
    if (d2 > 0) {
        if (s2 + d2 < 0) {
            s.skipBits(SEG_TAIL_BITS);
            return;
        }
        if (s2 <= d2) {
            x2 = project(s2, d2);
        }
    }
    if (x1 >= x2) {
        s.skipBits(SEG_TAIL_BITS);
        return;
    }
    val cols = colMask(x1, x2) & ~gSolid;
    if (cols == 0) {
        s.skipBits(SEG_TAIL_BITS);
        return;
    }
    if (DEBUG) { gProfSegsDrawn += 1; gProfGasSetup += getGasConsumedAtTheMoment() - gasE; }
    val worldbottom = (s.loadInt(16) - cam.vz) * ASPECT_Y;
    val worldtop = (s.loadInt(16) - cam.vz) * ASPECT_Y;
    val lightAdj = s.loadInt(2);
    val worldlow = (s.loadInt(16) - cam.vz) * ASPECT_Y;
    val worldhigh = (s.loadInt(16) - cam.vz) * ASPECT_Y;

    val dd = d2 - d1;
    val negC = -C;
    // scale24 at column i: (numBase - i*dd2) * 2^39 / negC
    val numBase = 2 * FOCAL * (s2 - s1) + (2 * CX - 1) * dd;
    val dd2 = 2 * dd;
    val step24 = (dd << 40) / C;
    val topstep = -worldtop * step24;
    val botstep = -worldbottom * step24;

    // shade from the mean endpoint depth (map units): level 0 (near) .. 4 (far), then sector light
    var lvl = min(4, ubitSize(max(d1 + d2, 2) / SHADE_UNIT2));
    lvl = min(4, max(0, lvl - lightAdj));
    val pats = gPats;
    val w01 = pats.get(lvl * 2) | (pats.get(lvl * 2 + 1) << H);

    val gasB = getGasConsumedAtTheMoment();
    if (DEBUG) { gProfGasSetup += gasB - gasE; }
    var fb2: array<int> = [];
    var solidAdd = 0;
    var partial = 0;
    if (kind == 0) {
        (fb2, solidAdd, partial) = drawSegSolid(gRows, w01, FLOOR01, topstep, botstep, worldtop, worldbottom, numBase, dd2, negC, gFb, cols);
    } else if (kind == 1) {
        (fb2, solidAdd, partial) = drawSegUpper(gRows, w01, FLOOR01, topstep, botstep, -worldhigh * step24, worldtop, worldbottom, worldhigh, numBase, dd2, negC, gFb, cols);
    } else if (kind == 2) {
        (fb2, solidAdd, partial) = drawSegLower(gRows, w01, FLOOR01, topstep, botstep, -worldlow * step24, worldtop, worldbottom, worldlow, numBase, dd2, negC, gFb, cols);
    } else {
        (fb2, solidAdd, partial) = drawSegBoth(gRows, w01, FLOOR01, topstep, botstep, -worldhigh * step24, -worldlow * step24, worldtop, worldbottom, worldhigh, worldlow, numBase, dd2, negC, gFb, cols);
    }
    gFb = fb2;
    gSolid |= solidAdd;
    if (partial != 0) {
        gPartial = true;
    }
    if (DEBUG) { gProfGasCols += getGasConsumedAtTheMoment() - gasB; }
}

// ---------------------------------------------------------------------------------------------
// Frame
// ---------------------------------------------------------------------------------------------
fun renderFrame(root: cell, rootIsSS: bool, cam: Cam): cell {
    val gas0 = getGasConsumedAtTheMoment();
    var fb: array<int> = [];
    repeat (W) {
        fb.push(OPEN_ALL);
    }
    gFb = fb;
    gSolid = 0;
    gPartial = false;
    var rows: array<int> = [];
    var m = 0;
    repeat (H + 1) {
        rows.push(m);
        m = (m >> 1) | (1 << (H - 1));
    }
    gRows = rows;
    gPats = [PAT_L5_0, PAT_L5_1, PAT_L4_0, PAT_L4_1, PAT_L3_0, PAT_L3_1, PAT_L2_0, PAT_L2_1, PAT_L2_0, PAT_L2_1];
    if (DEBUG) { gProfNodes = 0; gProfSegs = 0; gProfSegsDrawn = 0; gProfCols = 0; gProfGasCols = 0; gProfGasSegs = 0; gProfGasCulled = 0; gProfGasSetup = 0; }
    val gas1 = getGasConsumedAtTheMoment();
    renderNode(root, rootIsSS, cam);
    val gas2 = getGasConsumedAtTheMoment();
    // pack columns into a cell chain, built from the last cell backwards
    fb = gFb;
    var next: cell? = null;
    var x = W;
    while (x > 0) {
        val start = max(x - COLS_PER_CELL, 0);
        var b = beginCell();
        var i = start;
        repeat (x - start) {
            b.storeUint(fb.get(i) & FULL, H);
            i += 1;
        }
        if (next != null) {
            b.storeRef(next);
        }
        next = b.endCell();
        x = start;
    }
    if (DEBUG) {
        val gas3 = getGasConsumedAtTheMoment();
        val prof = createExternalLogMessage({
            dest: createAddressNone(),
            body: ProfEvent {
                gasInit: gas1 - gas0, gasBsp: gas2 - gas1, gasPack: gas3 - gas2, gasTotal: gas3,
                gasCols: gProfGasCols, gasSegs: gProfGasSegs, gasCulled: gProfGasCulled, gasSetup: gProfGasSetup,
                nodes: gProfNodes, segs: gProfSegs, segsDrawn: gProfSegsDrawn, cols: gProfCols,
            },
        });
        prof.send(SEND_MODE_REGULAR);
    }
    return next!;
}

fun emitFrame(st: Storage, viewz: int, pixels: cell, queued: int) {
    val msg = createExternalLogMessage({
        dest: createAddressNone(),
        body: FrameEvent {
            frameNo: st.frameNo,
            w: W,
            h: H,
            px: st.px,
            py: st.py,
            angle: st.angle,
            viewz: viewz,
            flags: gPartial ? 1 : 0,
            queued: queued,
            pixels: pixels,
        },
    });
    msg.send(SEND_MODE_REGULAR);
}

// ---------------------------------------------------------------------------------------------
// Input queue
// ---------------------------------------------------------------------------------------------
// Append a batch cell [count:8, inputs...] at the tail of the queue (rebuilds the spine).
fun appendBatch(queue: cell?, batch: cell): cell {
    if (queue == null) {
        return batch;
    }
    var s = queue.beginParse();
    val tail = s.remainingRefsCount() > 0 ? appendBatch(s.loadRef(), batch) : batch;
    return beginCell().storeSlice(s).storeRef(tail).endCell();
}

// Number of inputs left in the queue (bounded walk; the queue is short).
fun queuedCount(queue: cell?, qpos: int): int {
    var n = 0;
    var c = queue;
    var first = true;
    while (c != null) {
        var s = c.beginParse();
        n += s.loadUint(8) - (first ? qpos : 0);
        first = false;
        c = s.remainingRefsCount() > 0 ? s.loadRef() : null;
    }
    return n;
}

// Pop one input. Returns (turn, fwd, side, found).
fun popInput(mutate st: Storage): (int, int, int, bool) {
    if (st.queue == null) {
        return (0, 0, 0, false);
    }
    var s = st.queue.beginParse();
    val count = s.loadUint(8);
    s.skipBits(st.qpos * 24);
    val turn = s.loadInt(8);
    val fwd = s.loadInt(8);
    val side = s.loadInt(8);
    st.qpos += 1;
    if (st.qpos >= count) {
        st.qpos = 0;
        st.queue = s.remainingRefsCount() > 0 ? s.loadRef() : null;
    }
    return (turn, fwd, side, true);
}

// Render one queued input (if any) and keep the chain going.
fun step(mutate st: Storage, hop: int) {
    val (turn, fwd, side, found) = popInput(mutate st);
    if (!found) {
        return;
    }
    var lvl = st.level.beginParse();
    val rootIsSS = lvl.loadUint(1) == 1;
    val root = lvl.loadRef();
    val sinTable = lvl.loadRef();
    // move (noclip)
    val angle = (st.angle + turn) & ANGLE_MASK;
    val sn = sinAt(sinTable, angle);
    val cs = sinAt(sinTable, (angle + QUARTER) & ANGLE_MASK);
    val px = st.px + fwd * SPEED_FWD * cs + side * SPEED_SIDE * sn;
    val py = st.py + fwd * SPEED_FWD * sn - side * SPEED_SIDE * cs;
    st.angle = angle;
    st.px = px;
    st.py = py;
    st.frameNo += 1;
    val viewz = floorAt(root, rootIsSS, px, py) + VIEWHEIGHT;
    val cam: Cam = { px, py, sn, cs, vz: viewz };
    val pixels = renderFrame(root, rootIsSS, cam);
    val queued = queuedCount(st.queue, st.qpos);
    emitFrame(st, viewz, pixels, queued);
    if (queued > 0) {
        val cont = createMessage({
            bounce: false,
            value: CONT_VALUE,
            dest: contract.getAddress(),
            body: Continue { hop: hop + 1 },
        });
        cont.send(SEND_MODE_REGULAR);
    }
}

// ---------------------------------------------------------------------------------------------
// Entrypoints
// ---------------------------------------------------------------------------------------------
fun onInternalMessage(in: InMessage) {
    if (in.body.isEmpty()) {
        return;   // plain top-up
    }
    val msg = lazy Continue.fromSlice(in.body);
    match (msg) {
        Continue => {
            if (in.senderAddress != contract.getAddress()) {
                return;
            }
            var st = Storage.load();
            step(mutate st, msg.hop);
            st.save();
        }
        else => {
            // unknown internal message: ignore (keep the value)
        }
    }
}

fun onExternalMessage(inBody: slice) {
    var body = inBody;
    val op = body.loadUint(32);
    val batchId = body.loadUint(32);
    var st = Storage.load();
    // strict ordering: exactly the next batch id. Out-of-order / duplicate externals are rejected before
    // ACCEPT (free), and the sender re-sends them after the previous batch lands.
    assert (batchId == st.lastBatch + 1) throw ERR_OLD_BATCH;
    if (op == OP_RESET) {
        // body: op:32 batchId:32 px:int32 py:int32 angle:uint16 (16.16 map units)
        val px = body.loadInt(32);
        val py = body.loadInt(32);
        val angle = body.loadUint(16);
        acceptExternalMessage();
        st.px = px;
        st.py = py;
        st.angle = angle & ANGLE_MASK;
        st.queue = null;
        st.qpos = 0;
        st.lastBatch = batchId;
        st.save();
        return;
    }
    assert (op == OP_TICK) throw ERR_BAD_OP;
    val count = body.loadUint(8);
    assert ((count > 0) & (count <= MAX_INPUTS)) throw ERR_BAD_COUNT;
    acceptExternalMessage();

    // enqueue the batch: [count:8, count x 24 bits]
    val batch = beginCell().storeUint(count, 8).storeSlice(body.getFirstBits(count * 24)).endCell();
    st.queue = appendBatch(st.queue, batch);
    st.lastBatch = batchId;
    st.save();
    commitContractDataAndActions();
    step(mutate st, 0);
    st.save();
}

// ---------------------------------------------------------------------------------------------
// Get methods
// ---------------------------------------------------------------------------------------------
get fun frameNo(): int {
    val st = lazy Storage.load();
    return st.frameNo;
}

get fun player(): (int, int, int) {
    val st = Storage.load();
    return (st.px, st.py, st.angle);
}

get fun queued(): int {
    val st = Storage.load();
    return queuedCount(st.queue, st.qpos);
}

get fun lastBatch(): int {
    val st = Storage.load();
    return st.lastBatch;
}

get fun screen(): (int, int) {
    return (W, H);
}
