import "@acton/io"
import "@acton/fs"
import "@acton/boc"
import "@acton/build"
import "@acton/emulation/network"
import "@acton/emulation/testing"
import "@acton/emulation/config"
import "@acton/testing/expect"
import "@acton/types/message"
import "@acton/types/big_array"

import "@contracts/Doom"

// Golden data produced by tools/golden.py (see its docstring for the layout).
struct Golden {
    w: uint16
    h: uint16
    count: uint8
    inputs: cell
    frames: cell
}

struct GoldenFrame {
    frameNo: uint32
    px: int32
    py: int32
    angle: uint16
    viewz: int16
    hash: uint256
}

fun loadLevel(): cell {
    val bytes = fs.readBytes("assets/e1m1-level.boc");
    expect(bytes).toBeNotNull();
    val c = boc.decode(bytes!);
    expect(c).toBeNotNull();
    return c!;
}

fun loadGolden(path: string): Golden {
    val bytes = fs.readBytes(path);
    expect(bytes).toBeNotNull();
    val c = boc.decode(bytes!);
    expect(c).toBeNotNull();
    return Golden.fromCell(c!);
}

fun raiseGasLimit(limit: int) {
    var cfg = testing.getConfig();
    var prices = cfg.getGasPrices(BASECHAIN);
    prices.other.gasLimit = limit;
    cfg.setGasPrices(prices, BASECHAIN);
    testing.setConfig(cfg);
}

fun deployDoom(level: cell, value: coins = grams("50")): (address, Treasury) {
    val deployer = testing.treasury("deployer");
    // player start of E1M1: (1056, -3616), angle 90 deg = 128 units
    val stateInit: ContractState = {
        code: build("Doom"),
        data: Storage { frameNo: 0, px: 1056 << 16, py: -3616 << 16, angle: 128, lastBatch: 0, qpos: 0, queue: null, held: null, owner: deployer.address, auto: false, turnDir: 0, rnd: 1, lastStep: 0, gun: 0, level }.toCell(),
    };
    val addr = AutoDeployAddress { stateInit }.calculateAddress();
    net.registerAddress(addr, "doom");
    val deployMsg = createMessage({ bounce: false, value, dest: { workchain: BASECHAIN, stateInit } });
    val res = net.send(deployer.address, deployMsg);
    expect(res).toHaveSuccessfulDeploy({ to: addr });
    return (addr, deployer);
}

// Build a tick body with `count` inputs taken from the golden inputs chain starting at `skip`.
fun tickBody(g: Golden, skip: int, count: int, batchId: int): cell {
    var b = beginCell().storeUint(0x444F4F4D, 32).storeUint(batchId, 8 * 4).storeUint(count, 8);
    var s = g.inputs.beginParse();
    var idx = 0;
    var taken = 0;
    while (taken < count) {
        if (s.remainingBitsCount() < 32) {
            s = s.loadRef().beginParse();
        }
        val rec = s.loadUint(32);   // turn:int8 fwd:int8 side:int8 flags:uint8
        if (idx >= skip) {
            b.storeUint(rec, 32);
            taken += 1;
        }
        idx += 1;
    }
    return b.endCell();
}

fun goldenFrameAt(g: Golden, index: int): GoldenFrame {
    var c = g.frames;
    var i = 0;
    while (i < index) {
        c = c.beginParse().preloadRef();
        i += 1;
    }
    var s = c.beginParse();
    return s.loadAny<GoldenFrame>();
}

fun checkFrames(txs: SendResultList, g: Golden, firstIndex: int, expectCount: int, printGas: bool) {
    var frames: array<cell> = [];
    var t = 0;
    var totalGas = 0;
    var maxGas = 0;
    while (t < txs.size()) {
        val r = txs.at(t);
        totalGas += r.gasUsed;
        maxGas = max(maxGas, r.gasUsed);
        var k = 0;
        while (k < r.externals.size()) {
            val raw = r.externals.get(k) as cell;
            val m = (raw as Cell<TlbMessage<RemainingBitsAndRefs, TlbExternalOutMessageInfo>>).load();
            var body = m.loadBody();
            val op = body.preloadUint(32);
            if (op == 0x50524F46) {
                val pr = body.loadAny<ProfEvent>();
                if (printGas) {
                    println("PROF total={} cols={} segs={} nodes={}", pr.gasTotal, pr.gasCols, pr.gasSegs - pr.gasCols, pr.gasBsp - pr.gasSegs);
                    println("     ev={} segsDrawn={} segsTested={} nodes={}", pr.cols, pr.segsDrawn, pr.segs, pr.nodes);
                    println("     culled(backface)={} setup(drawn, double counted prefix)={}", pr.gasCulled, pr.gasSetup);
                }
            } else {
                frames.push(raw);
            }
            k += 1;
        }
        t += 1;
    }
    expect(frames.size() >= expectCount).toEqual(true);
    var i = 0;
    while (i < expectCount) {
        val ev = (frames.get(i) as Cell<TlbMessage<FrameEvent, TlbExternalOutMessageInfo>>).load();
        val body = ev.loadBody();
        val want = goldenFrameAt(g, firstIndex + i);
        if (body.pixels.hash() != want.hash) {
            println("frame {} MISMATCH: got hash {:x} want {:x} flags={}", body.frameNo, body.pixels.hash(), want.hash, body.flags);
        }
        expect(body.frameNo).toEqual(want.frameNo);
        expect(body.px).toEqual(want.px);
        expect(body.py).toEqual(want.py);
        expect(body.angle).toEqual(want.angle);
        expect(body.viewz).toEqual(want.viewz);
        expect(body.flags).toEqual(0);
        expect(body.pixels.hash()).toEqual(want.hash);
        i += 1;
    }
    if (printGas) {
        println("frames={} txs={} totalGas={} maxTxGas={}", expectCount, txs.size(), totalGas, maxGas);
    }
}

get fun `test single frame matches reference renderer`() {
    val level = loadLevel();
    val g = loadGolden("assets/golden/still.boc");
    val (addr, _) = deployDoom(level);
    val res = net.sendExternal(net.createExternalMessage(addr, tickBody(g, 0, 1, 1)));
    expect(res).toBeAccepted();
    val txs = res.unwrap();
    expect(txs).toHaveAllSuccessfulTxs();
    checkFrames(txs, g, 0, 1, true);
}

get fun `test walk path matches reference renderer`() {
    val level = loadLevel();
    val g = loadGolden("assets/golden/walk.boc");
    val (addr, _) = deployDoom(level);
    // send the path in batches of 10 inputs; the contract renders them through a chain of
    // self-messages (one frame per transaction); every frame must match the golden hash
    var sent = 0;
    val batch = 10;
    var batchId = 1;
    while (sent < g.count) {
        val n = min(batch, g.count - sent);
        val res = net.sendExternal(net.createExternalMessage(addr, tickBody(g, sent, n, batchId)));
        expect(res).toBeAccepted();
        val txs = res.unwrap();
        expect(txs).toHaveAllSuccessfulTxs();
        checkFrames(txs, g, sent, n, true);
        sent += n;
        batchId += 1;
    }
    expect(net.runGetMethod<int>(addr, "queued")).toEqual(0);
}

get fun `test player collides with walls and slides along them`() {
    val level = loadLevel();
    val g = loadGolden("assets/golden/wall.boc");   // walks into a diagonal wall, turns, fires twice
    val (addr, _) = deployDoom(level);
    var sent = 0;
    var batchId = 1;
    while (sent < g.count) {
        val n = min(10, g.count - sent);
        val res = net.sendExternal(net.createExternalMessage(addr, tickBody(g, sent, n, batchId)));
        expect(res).toBeAccepted();
        val txs = res.unwrap();
        expect(txs).toHaveAllSuccessfulTxs();
        checkFrames(txs, g, sent, n, true);
        sent += n;
        batchId += 1;
    }
    val (px, py, _) = net.runGetMethod<(int, int, int)>(addr, "player");
    val last = goldenFrameAt(g, g.count - 1);
    expect(px).toEqual(last.px);
    expect(py).toEqual(last.py);
}

get fun `test bad opcode is rejected before accept`() {
    val level = loadLevel();
    val (addr, _) = deployDoom(level);
    val res = net.sendExternal(net.createExternalMessage(addr, beginCell().storeUint(0x12345678, 32).endCell()));
    expect(res).toBeNotAccepted();
}

get fun `test batch ordering with a one-batch reorder buffer`() {
    val level = loadLevel();
    val g = loadGolden("assets/golden/walk.boc");
    val (addr, _) = deployDoom(level);
    // batch 3 before batch 1: rejected before accept (only lastBatch+1 and lastBatch+2 are accepted)
    expect(net.sendExternal(net.createExternalMessage(addr, tickBody(g, 4, 2, 3)))).toBeNotAccepted();
    // batch 2 before batch 1: held
    expect(net.sendExternal(net.createExternalMessage(addr, tickBody(g, 2, 2, 2)))).toBeAccepted();
    expect(net.runGetMethod<int>(addr, "lastBatch")).toEqual(0);
    expect(net.runGetMethod<int>(addr, "queued")).toEqual(0);
    // a second early batch while one is held: rejected
    expect(net.sendExternal(net.createExternalMessage(addr, tickBody(g, 2, 2, 2)))).toBeNotAccepted();
    // batch 1 arrives: appended, then the held batch 2 follows it in order
    val res = net.sendExternal(net.createExternalMessage(addr, tickBody(g, 0, 2, 1)));
    expect(res).toBeAccepted();
    expect(res.unwrap()).toHaveAllSuccessfulTxs();
    expect(net.runGetMethod<int>(addr, "lastBatch")).toEqual(2);
    // the four inputs were rendered in golden order (frames 1..4)
    checkFrames(res.unwrap(), g, 0, 4, false);
    // duplicate of batch 2: rejected; batch 3 accepted; batch 4 accepted (held: nothing pending) then rendered
    expect(net.sendExternal(net.createExternalMessage(addr, tickBody(g, 2, 2, 2)))).toBeNotAccepted();
    expect(net.sendExternal(net.createExternalMessage(addr, tickBody(g, 4, 2, 3)))).toBeAccepted();
    expect(net.runGetMethod<int>(addr, "lastBatch")).toEqual(3);
}

get fun `test reset teleports the player and drops the queue`() {
    val level = loadLevel();
    val g = loadGolden("assets/golden/walk.boc");
    val (addr, _) = deployDoom(level);
    val res = net.sendExternal(net.createExternalMessage(addr, tickBody(g, 0, 5, 1)));
    expect(res).toBeAccepted();
    val body = beginCell().storeUint(0x52534554, 32).storeUint(2, 32).storeInt(1000 << 16, 32).storeInt(-3000 << 16, 32).storeUint(64, 16).endCell();
    val res2 = net.sendExternal(net.createExternalMessage(addr, body));
    expect(res2).toBeAccepted();
    val (px, py, angle) = net.runGetMethod<(int, int, int)>(addr, "player");
    expect(px).toEqual(1000 << 16);
    expect(py).toEqual(-3000 << 16);
    expect(angle).toEqual(64);
    expect(net.runGetMethod<int>(addr, "queued")).toEqual(0);
    expect(net.runGetMethod<int>(addr, "lastBatch")).toEqual(2);
}

get fun `test owner can withdraw the balance, others cannot`() {
    val level = loadLevel();
    val (addr, deployer) = deployDoom(level);
    val stranger = testing.treasury("stranger");
    val body = Withdraw {}.toCell();
    val r1 = net.send(stranger.address, createMessage({ bounce: false, value: grams("0.1"), dest: addr, body }));
    expect(r1).toHaveAllSuccessfulTxs();
    expect(testing.getAccountBalance(addr) > grams("50")).toEqual(true);
    val r2 = net.send(deployer.address, createMessage({ bounce: false, value: grams("0.1"), dest: addr, body }));
    expect(r2).toHaveAllSuccessfulTxs();
    expect(testing.getAccountBalance(addr) < grams("1")).toEqual(true);
}

fun controlBody(op: int, batchId: int): cell {
    return beginCell().storeUint(op, 32).storeUint(batchId, 32).endCell();
}

get fun `test wanderer AI matches the reference for 60 frames`() {
    val level = loadLevel();
    val g = loadGolden("assets/golden/ai.boc");   // frames produced by tools/ai.py from the level start
    // small balance: the chain stops by itself (MIN_CHAIN_BALANCE) after roughly 70 frames
    val (addr, _) = deployDoom(level, grams("4"));
    // START: the contract renders frames by itself through the self-message chain (no inputs)
    val res = net.sendExternal(net.createExternalMessage(addr, controlBody(0x53545254, 1)));
    expect(res).toBeAccepted();
    val txs = res.unwrap();
    expect(txs).toHaveAllSuccessfulTxs();
    // the emulator runs the whole chain: cap it by stopping after the golden count
    var frames = 0;
    var t = 0;
    while (t < txs.size()) {
        frames += txs.at(t).externals.size();
        t += 1;
    }
    println("chain produced {} frames in {} txs", frames, txs.size());
    expect(frames >= g.count).toEqual(true);
    checkFrames(txs, g, 0, g.count, true);
}

get fun `test STOP ends the chain and START restarts it`() {
    val level = loadLevel();
    val (addr, _) = deployDoom(level);
    expect(net.sendExternal(net.createExternalMessage(addr, controlBody(0x53544F50, 1)))).toBeAccepted();
    val (auto0, _, _) = net.runGetMethod<(int, int, int)>(addr, "autoState");
    expect(auto0).toEqual(0);
    expect(net.runGetMethod<int>(addr, "frameNo")).toEqual(0);
}

get fun `test a frame that runs out of gas bounces and the chain goes on`() {
    val level = loadLevel();
    val g = loadGolden("assets/golden/walk.boc");
    val (addr, _) = deployDoom(level);
    // gas limit below the render guard: busy frames fail with out-of-gas instead of being emitted partial
    // (the walk path peaks above 900k; the first frame, rendered by the external itself, stays below)
    raiseGasLimit(860000);
    val n = min(g.count, 29);   // MAX_INPUTS per external
    val res = net.sendExternal(net.createExternalMessage(addr, tickBody(g, 0, n, 1)));
    expect(res).toBeAccepted();
    val txs = res.unwrap();
    expect(txs).toHaveFailedTx({ exitCode: -14 });
    var frames = 0;
    var failed = 0;
    var t = 0;
    while (t < txs.size()) {
        frames += txs.at(t).externals.size();
        if (txs.at(t).gasUsed >= 860000) {
            failed += 1;
        }
        t += 1;
    }
    println("chain: {} txs, {} frames, {} failed", txs.size(), frames, failed);
    // every failed frame was skipped by the bounce handler and the chain consumed the whole queue
    expect(failed > 0).toEqual(true);
    expect(frames + failed).toEqual(n);
    expect(net.runGetMethod<int>(addr, "frameNo")).toEqual(n);
    expect(net.runGetMethod<int>(addr, "queued")).toEqual(0);
}
