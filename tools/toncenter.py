#!/usr/bin/env python3
"""Minimal stdlib-only client for the toncenter v3 HTTP API (TON testnet by default).

Verified against https://testnet.toncenter.com (x-api-version 1.3.0, OpenAPI "TON Index (Go) 1.2.6")
on 2026-09-08. See docs/ notes (scratchpad/notes/toncenter.md) for the endpoint semantics.

Config (env):
  TONCENTER_URL      base URL, default https://testnet.toncenter.com
  TONCENTER_API_KEY  API key, sent as the X-API-Key header (also read from .env: TONCENTER_TESTNET_API_KEY)

Library:
  send_boc(boc_bytes) -> str                       POST /api/v3/message, returns message_hash (base64)
  get_transactions(account, after_lt=None, limit=100) -> list   ascending lt, lt > after_lt, paginates
  get_account(address) -> dict                     GET /api/v3/account (balance/code/data/last_transaction_lt/...)
  get_latest_lt(account) -> int|None               lt of the newest transaction (0 pages, 1 call)
  get_ext_out_messages(account, after_lt=None, limit=100) -> list   GET /api/v3/messages?source=&destination=null
  find_tx_by_message(msg_hash) -> list             GET /api/v3/transactionsByMessage (accepts hash or hash_norm)
  wait_for_tx(msg_hash, timeout=60.0) -> dict|None poll find_tx_by_message

CLI:
  python3 tools/toncenter.py txs <addr> [--limit N] [--after-lt LT] [--desc]
  python3 tools/toncenter.py account <addr>
  python3 tools/toncenter.py extout <addr> [--limit N] [--after-lt LT]
  python3 tools/toncenter.py send <file.boc | base64-string>
  python3 tools/toncenter.py latest-lt <addr>
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "https://testnet.toncenter.com"


def _load_dotenv() -> dict:
    """Read KEY=VALUE pairs from the project's .env (next to tools/), if present."""
    out = {}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


_DOTENV = _load_dotenv()

MAX_PAGE = 1000  # server rejects limit > 1000 ("limit is not allowed: 1001 > 1000")
USER_AGENT = "ton-doom/0.1 (+https://github.com/verdigo/ton-doom)"


class ToncenterError(Exception):
    """HTTP or API-level error. `.status` is the HTTP status, `.payload` the parsed JSON (if any)."""

    def __init__(self, message: str, status: int | None = None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload


def _base_url() -> str:
    return os.environ.get("TONCENTER_URL", DEFAULT_URL).rstrip("/")


def _api_key() -> str:
    for name in ("TONCENTER_API_KEY", "TONCENTER_TESTNET_API_KEY"):
        v = os.environ.get(name) or _DOTENV.get(name)
        if v:
            return v
    return ""


def _request(method: str, path: str, params: dict | None = None, body: dict | None = None,
             timeout: float = 30.0, retries: int = 4):
    """Perform one API call. Retries on 429 and on transient 5xx ("timeout: context deadline exceeded")."""
    url = _base_url() + path
    if params:
        # Multi-valued params (e.g. account=[..]) are passed as repeated keys.
        items = []
        for k, v in params.items():
            if v is None:
                continue
            if isinstance(v, (list, tuple)):
                items.extend((k, str(x)) for x in v)
            else:
                items.append((k, str(v).lower() if isinstance(v, bool) else str(v)))
        url += "?" + urllib.parse.urlencode(items)
    data = None
    # Cloudflare in front of toncenter returns 403 "error code: 1010" for the default "Python-urllib/x.y" UA.
    headers = {"Accept": "application/json", "X-API-Key": _api_key(), "User-Agent": USER_AGENT}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    last_err: ToncenterError | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw)
            except Exception:
                payload = raw.decode("utf-8", "replace")
            msg = payload.get("error") or payload.get("result") if isinstance(payload, dict) else payload
            last_err = ToncenterError(f"HTTP {e.code} {method} {path}: {msg}", e.code, payload)
            transient = e.code == 429 or (e.code >= 500 and "context deadline exceeded" in str(msg))
            if not transient or attempt == retries:
                raise last_err
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = ToncenterError(f"{method} {path}: {e}")
            if attempt == retries:
                raise last_err
        time.sleep(0.5 * (2 ** attempt))
    raise last_err  # pragma: no cover


# --------------------------------------------------------------------------- sending

def send_boc(boc_bytes: bytes | str) -> str:
    """Send an external message. `boc_bytes` is the serialized BOC (bytes) or a base64 string.

    POST /api/v3/message {"boc": "<base64>"} -> {"message_hash": "...", "message_hash_norm": "..."}.
    Returns message_hash (base64). The server pre-executes the message against the current state and
    rejects it with HTTP 500 and a VM log in `error` if the contract does not accept it
    (e.g. "...inbound external message rejected by transaction ...: exitcode=11, steps=4 ...").
    """
    b64 = boc_bytes if isinstance(boc_bytes, str) else base64.b64encode(boc_bytes).decode()
    res = _request("POST", "/api/v3/message", body={"boc": b64})
    if not isinstance(res, dict) or "message_hash" not in res:
        raise ToncenterError(f"unexpected sendMessage response: {res!r}", payload=res)
    return res["message_hash"]


def send_boc_full(boc_bytes: bytes | str) -> dict:
    """Like send_boc but returns the whole response dict (message_hash, message_hash_norm)."""
    b64 = boc_bytes if isinstance(boc_bytes, str) else base64.b64encode(boc_bytes).decode()
    return _request("POST", "/api/v3/message", body={"boc": b64})


# --------------------------------------------------------------------------- reading

def get_transactions(account: str, after_lt: int | None = None, limit: int | None = 100,
                     page_size: int = 100) -> list:
    """Transactions of `account` with lt > after_lt, ascending by lt, up to `limit` (None = all).

    Uses GET /api/v3/transactions?account=&sort=asc&start_lt=<after_lt+1>&limit=<page>, paginating by
    lt cursor (start_lt is inclusive; lt is unique per account so `last_lt + 1` is an exact cursor).
    Each transaction carries `in_msg` and `out_msgs[]` with `message_content.body` (base64 BOC), so one
    call returns both the inbound external and every outbound external of each tx, in lt order.
    """
    out: list = []
    cursor = (int(after_lt) + 1) if after_lt is not None else None
    while True:
        want = page_size if limit is None else min(page_size, limit - len(out))
        if want <= 0:
            break
        want = min(want, MAX_PAGE)
        res = _request("GET", "/api/v3/transactions", params={
            "account": account, "sort": "asc", "start_lt": cursor, "limit": want,
        })
        txs = res.get("transactions", [])
        out.extend(txs)
        if len(txs) < want:
            break
        cursor = int(txs[-1]["lt"]) + 1
    return out


def get_latest_lt(account: str) -> int | None:
    """lt of the most recent transaction of `account` (None if no transactions)."""
    res = _request("GET", "/api/v3/transactions", params={"account": account, "sort": "desc", "limit": 1})
    txs = res.get("transactions", [])
    return int(txs[0]["lt"]) if txs else None


def get_ext_out_messages(account: str, after_lt: int | None = None, limit: int | None = 100,
                         page_size: int = 100) -> list:
    """External-out ("log") messages emitted by `account`, ascending by created_lt, created_lt > after_lt.

    GET /api/v3/messages?source=<account>&destination=null&sort=asc&start_lt=... Each message has
    `created_lt`, `created_at`, `out_msg_tx_hash` and `message_content.body` (base64 BOC).
    """
    out: list = []
    cursor = (int(after_lt) + 1) if after_lt is not None else None
    while True:
        want = page_size if limit is None else min(page_size, limit - len(out))
        if want <= 0:
            break
        want = min(want, MAX_PAGE)
        res = _request("GET", "/api/v3/messages", params={
            "source": account, "destination": "null", "sort": "asc", "start_lt": cursor, "limit": want,
        })
        msgs = res.get("messages", [])
        out.extend(msgs)
        if len(msgs) < want:
            break
        cursor = int(msgs[-1]["created_lt"]) + 1
    return out


def get_account(address: str) -> dict:
    """GET /api/v3/account?address= -> {balance, code, data, last_transaction_lt, last_transaction_hash,
    frozen_hash, status}. `code`/`data` are base64 BOCs (or null when uninit)."""
    return _request("GET", "/api/v3/account", params={"address": address})


def get_account_state(address: str, include_boc: bool = False) -> dict:
    """GET /api/v3/accountStates -> first entry (status, balance, last_transaction_lt/hash, code_hash,
    data_hash, contract_methods, optionally code_boc/data_boc)."""
    res = _request("GET", "/api/v3/accountStates", params={"address": address, "include_boc": include_boc})
    accts = res.get("accounts", [])
    if not accts:
        raise ToncenterError(f"no account state returned for {address}", payload=res)
    return accts[0]


def find_tx_by_message(msg_hash: str, direction: str | None = None) -> list:
    """GET /api/v3/transactionsByMessage?msg_hash= (hash or hash_norm of the message, base64/hex)."""
    res = _request("GET", "/api/v3/transactionsByMessage", params={"msg_hash": msg_hash, "direction": direction})
    return res.get("transactions", [])


def wait_for_tx(msg_hash: str, timeout: float = 60.0, interval: float = 1.0) -> dict | None:
    """Poll transactionsByMessage until the tx consuming `msg_hash` (from send_boc) is indexed."""
    deadline = time.time() + timeout
    while True:
        txs = find_tx_by_message(msg_hash, direction="in")
        if txs:
            return txs[0]
        if time.time() >= deadline:
            return None
        time.sleep(interval)


# --------------------------------------------------------------------------- helpers

def msg_kind(msg: dict | None) -> str:
    if msg is None:
        return "none"
    if msg.get("source") is None:
        return "ext-in"
    if msg.get("destination") is None:
        return "ext-out"
    return "internal"


def body_boc(msg: dict) -> bytes:
    """Decode message_content.body (base64 BOC) to bytes."""
    return base64.b64decode(msg["message_content"]["body"])


# --------------------------------------------------------------------------- CLI

def _cmd_txs(args: argparse.Namespace) -> int:
    if args.desc:
        res = _request("GET", "/api/v3/transactions",
                       params={"account": args.addr, "sort": "desc", "limit": min(args.limit, MAX_PAGE)})
        txs = res.get("transactions", [])
    else:
        txs = get_transactions(args.addr, after_lt=args.after_lt, limit=args.limit)
    for t in txs:
        im = t.get("in_msg")
        cp = (t.get("description") or {}).get("compute_ph") or {}
        line = (f"lt={t['lt']} now={t['now']} block={t['block_ref']['workchain']}:{t['block_ref']['seqno']} "
                f"in_msg={msg_kind(im)}")
        if im and im.get("opcode"):
            line += f" op={im['opcode']}"
        line += f" exit={cp.get('exit_code')} gas={cp.get('gas_used')} outs={len(t.get('out_msgs', []))}"
        print(line)
        for m in t.get("out_msgs", []):
            if m.get("destination") is None:
                b64 = m["message_content"]["body"]
                print(f"    ext-out created_lt={m['created_lt']} op={m.get('opcode')} "
                      f"body_b64_len={len(b64)} body_bytes={len(base64.b64decode(b64))}")
    return 0


def _cmd_account(args: argparse.Namespace) -> int:
    a = get_account(args.addr)
    for k in ("status", "balance", "last_transaction_lt", "last_transaction_hash", "frozen_hash"):
        print(f"{k}={a.get(k)}")
    print(f"code_b64_len={len(a.get('code') or '')} data_b64_len={len(a.get('data') or '')}")
    return 0


def _cmd_extout(args: argparse.Namespace) -> int:
    for m in get_ext_out_messages(args.addr, after_lt=args.after_lt, limit=args.limit):
        b64 = m["message_content"]["body"]
        print(f"created_lt={m['created_lt']} created_at={m['created_at']} tx={m.get('out_msg_tx_hash')} "
              f"op={m.get('opcode')} body_b64_len={len(b64)} body_bytes={len(base64.b64decode(b64))}")
    return 0


def _cmd_send(args: argparse.Namespace) -> int:
    src = args.boc
    if os.path.exists(src):
        with open(src, "rb") as f:
            raw = f.read()
        boc = raw if raw[:4] == b"\xb5\xee\x9c\x72" else base64.b64decode(raw.strip())
    else:
        boc = base64.b64decode(src)
    try:
        res = send_boc_full(boc)
    except ToncenterError as e:
        print(f"send failed: {e}", file=sys.stderr)
        return 1
    print(json.dumps(res))
    if args.wait:
        tx = wait_for_tx(res.get("message_hash_norm") or res["message_hash"], timeout=args.wait)
        print("tx:", json.dumps({k: tx[k] for k in ("account", "lt", "now", "hash")}) if tx else "not found")
    return 0


def _cmd_latest_lt(args: argparse.Namespace) -> int:
    print(get_latest_lt(args.addr))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("txs", help="list transactions (ascending lt by default)")
    s.add_argument("addr")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--after-lt", type=int, default=None, help="only lt > this")
    s.add_argument("--desc", action="store_true", help="newest first, single page")
    s.set_defaults(fn=_cmd_txs)

    s = sub.add_parser("account", help="account state")
    s.add_argument("addr")
    s.set_defaults(fn=_cmd_account)

    s = sub.add_parser("extout", help="external-out messages emitted by the account")
    s.add_argument("addr")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--after-lt", type=int, default=None)
    s.set_defaults(fn=_cmd_extout)

    s = sub.add_parser("send", help="send external message BOC (file path or base64)")
    s.add_argument("boc")
    s.add_argument("--wait", type=float, default=0, help="seconds to wait for the tx to be indexed")
    s.set_defaults(fn=_cmd_send)

    s = sub.add_parser("latest-lt", help="lt of newest transaction")
    s.add_argument("addr")
    s.set_defaults(fn=_cmd_latest_lt)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except ToncenterError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
