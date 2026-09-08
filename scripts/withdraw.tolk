// Withdraw the whole balance of a Doom contract back to the owner wallet (internal message WDRW).
//   acton script scripts/withdraw.tolk <address> --net testnet
import "@acton/io"
import "@acton/env"
import "@acton/emulation/network"
import "@acton/emulation/scripts"
import "@acton/testing/assert"

import "@contracts/Doom"

fun main(target: address) {
    val walletName = env<string>("DOOM_WALLET") ?? "main-w9";
    val wallet = scripts.wallet(walletName);
    println("withdrawing {:gram} from {} to {} ({})", scripts.fetchAccountBalance(target), target, wallet.address, walletName);
    val res = net.send(wallet.address, createMessage({ bounce: false, value: grams("0.1"), dest: target, body: Withdraw {} }));
    val tx = res.waitForFirstTransaction();
    if (tx == null) {
        Assert.fail("withdraw transaction not found");
    }
    println("done: contract balance now {:gram}, wallet {:gram}", scripts.fetchAccountBalance(target), scripts.fetchAccountBalance(wallet.address));
}
