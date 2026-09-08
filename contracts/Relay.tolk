tolk 1.4.2

// Input relay for Doom on TON. The mempool caps external messages per address, so a player sends input
// batches to many relays in turn; each relay forwards the body unchanged to the Doom contract as an
// internal message (same shard prefix, see scripts/deploy-relays.tolk: processed in the same block). The
// attached value buys the gas limit of the Doom transaction and stays there, so a relay needs a balance.
//
// External body: any Doom command (op DOOM / STRT / STOP / RSET, see Doom.tolk). No signature: the Doom
// contract's batch ids reject replays.

import "@stdlib/gas-payments"

contract Relay {
    author: "ton-doom"
    version: "0.1.0"
    description: "Input relay for Doom on TON: forwards command externals as internal messages"
    storage: RelayStorage
}

const RELAY_VALUE = 70000000;    // 0.07 TON attached to the forwarded command: buys Doom's 1M gas limit
const OP_WITHDRAW = 0x57445257;  // "WDRW" internal from the owner: send the whole balance back

struct RelayStorage {
    doom: address
    owner: address
    id: uint16         // distinct ids give distinct addresses
}

fun RelayStorage.load(): RelayStorage {
    return RelayStorage.fromCell(contract.getData());
}

fun onExternalMessage(inBody: slice) {
    val st = lazy RelayStorage.load();
    assert (inBody.remainingBitsCount() >= 64) throw 130;
    val op = inBody.preloadUint(32);
    assert ((op == 0x444F4F4D) | (op == 0x53545254) | (op == 0x53544F50) | (op == 0x52534554)) throw 130;
    acceptExternalMessage();
    val fwd = createMessage({
        bounce: false,
        value: RELAY_VALUE,
        dest: st.doom,
        body: beginCell().storeSlice(inBody).endCell(),
    });
    fwd.send(SEND_MODE_REGULAR);
}

fun onInternalMessage(in: InMessage) {
    if (in.body.isEmpty()) {
        return;   // top-up or the refund from Doom
    }
    if (in.body.preloadUint(32) == OP_WITHDRAW) {
        val st = lazy RelayStorage.load();
        if (in.senderAddress != st.owner) {
            return;
        }
        val back = createMessage({ bounce: false, value: 0, dest: st.owner });
        back.send(SEND_MODE_CARRY_ALL_BALANCE);
    }
}

get fun relayTarget(): address {
    val st = lazy RelayStorage.load();
    return st.doom;
}
