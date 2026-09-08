// Deploy N input relays (contracts/Relay.tolk: they forward to any address named in the message) with the
// same top address bits as the Doom contract (so that a shard split keeps them together), and print them
// (RELAY n id addr lines; put the addresses into .env as DOOM_RELAYS=a,b,c:
// `... | awk '/^RELAY/{for(i=1;i<=NF;i++) if ($i ~ /^kQ/) print $i}' | paste -sd, -`).
//   DOOM_ADDRESS=... RELAY_COUNT=50 RELAY_GRAMS=100 acton script scripts/deploy-relays.tolk --net testnet
import "@acton/io"
import "@acton/env"
import "@acton/build"
import "@acton/emulation/network"
import "@acton/emulation/scripts"
import "@acton/testing/assert"

import "@contracts/Relay"

const PREFIX_BITS = 2;

fun main() {
    val walletName = env<string>("DOOM_WALLET") ?? "main-w9";
    val wallet = scripts.wallet(walletName);
    val doomOpt = env<address>("DOOM_ADDRESS");
    if (doomOpt == null) {
        Assert.fail("DOOM_ADDRESS not set");
    }
    val doom = doomOpt!;
    val count = env<int>("RELAY_COUNT") ?? 50;
    val value = (env<int>("RELAY_GRAMS") ?? 100) * 1000000000;
    val (_, doomHash) = doom.getWorkchainAndHash();
    val wantPrefix = doomHash >> (256 - PREFIX_BITS);
    println("doom {} prefix {} ; deploying {} relays with {:gram} each", doom, wantPrefix, count, value);
    val code = build("Relay");
    var found = 0;
    var id = 0;
    while (found < count) {
        val stateInit: ContractState = { code, data: RelayStorage { owner: wallet.address, id }.toCell() };
        val addr = AutoDeployAddress { stateInit }.calculateAddress();
        val (_, h) = addr.getWorkchainAndHash();
        if ((h >> (256 - PREFIX_BITS)) == wantPrefix) {
            if (!scripts.isContractDeployed(addr)) {
                val res = net.send(wallet.address, createMessage({ bounce: false, value, dest: { workchain: BASECHAIN, stateInit } }));
                if (res.waitForFirstTransaction() == null) {
                    Assert.fail("deploy failed");
                }
            }
            println("RELAY {} id {} {}", found, id, addr);
            found += 1;
        }
        id += 1;
    }
    println("done: {} relays (collect the addresses into .env DOOM_RELAYS=a,b,c)", found);
}
