// Top up the Doom contract from the configured wallet.
//   acton script scripts/topup.tolk <address> <grams> --net testnet
import "@acton/io"
import "@acton/env"
import "@acton/emulation/network"
import "@acton/emulation/scripts"
import "@acton/testing/assert"

fun main(target: address, amountGrams: int) {
    val walletName = env<string>("DOOM_WALLET") ?? "main-w9";
    val wallet = scripts.wallet(walletName);
    val value = amountGrams * 1000000000;
    println("sending {:gram} from {} ({}) to {}", value, wallet.address, walletName, target);
    val res = net.send(wallet.address, createMessage({ bounce: false, value, dest: target }));
    val tx = res.waitForFirstTransaction();
    if (tx == null) {
        Assert.fail("top-up transaction not found");
    }
    println("done: balance now {:gram}", scripts.fetchAccountBalance(target));
}
