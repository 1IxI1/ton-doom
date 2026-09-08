// Deploy the Doom contract with the E1M1 level data.
//   acton script scripts/deploy.tolk                 -> local emulation (dry run)
//   acton script scripts/deploy.tolk --net testnet   -> testnet, paid by the global wallet "main-w9"
import "@acton/io"
import "@acton/fs"
import "@acton/boc"
import "@acton/env"
import "@acton/build"
import "@acton/emulation/network"
import "@acton/emulation/scripts"
import "@acton/testing/assert"

import "@contracts/Doom"

fun main() {
    val walletName = env<string>("DOOM_WALLET") ?? "main-w9";
    val wallet = scripts.wallet(walletName);
    println("wallet: {} ({}) broadcasting={}", wallet.address, walletName, net.isBroadcasting());

    val bytes = fs.readBytes("assets/e1m1-level.boc");
    if (bytes == null) {
        Assert.fail("assets/e1m1-level.boc not found (run: python3 tools/level_encode.py assets/e1m1.json assets/e1m1-level.boc)");
    }
    val level = boc.decode(bytes!);
    if (level == null) {
        Assert.fail("cannot decode level boc");
    }
    val salt = env<int>("DOOM_SALT") ?? 0;   // change to get a fresh contract address
    val stateInit: ContractState = {
        code: build("Doom"),
        // player 1 start of E1M1: (1056, -3616), facing north (90 deg = 128 angle units)
        data: Storage { frameNo: salt, px: 1056 << 16, py: -3616 << 16, angle: 128, lastBatch: 0, qpos: 0, queue: null, level: level! }.toCell(),
    };
    val addr = AutoDeployAddress { stateInit }.calculateAddress();
    println("doom address: {}", addr);
    println("DOOM_ADDRESS={}", addr);

    if (scripts.isContractDeployed(addr)) {
        println("already deployed");
    } else {
        val value = env<coins>("DOOM_DEPLOY_VALUE_NANOS") ?? grams("2000");
        val msg = createMessage({ bounce: false, value, dest: { workchain: BASECHAIN, stateInit } });
        val res = net.send(wallet.address, msg);
        val tx = res.waitForFirstTransaction();
        if (tx == null) {
            Assert.fail("deploy transaction not found");
        }
        println("deployed with {:gram}", value);
    }
    println("frameNo = {}", net.runGetMethod<int>(addr, "frameNo"));
}
