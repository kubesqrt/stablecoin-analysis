"""Tier 3: read USDC/USDT balances directly from contracts via Multicall3.

DefiLlama covers most teams, but not all: new chains its adapters do not track,
treasury wallets excluded from TVL, and anything you need measured rather than
estimated. List those addresses in config/addresses.yml and this reads them
straight from the chain.

One eth_call per chain covers hundreds of (token, holder) pairs, pinned to a
single block so every balance in a batch is one atomic snapshot. Decimals are
read on-chain, never assumed - BSC USDC and USDT are 18 decimals, and treating
them as 6 would overstate a balance by a factor of a trillion.

    python onchain.py --probe     # verify addresses and decimals only
    python onchain.py             # read balances -> data/onchain.json
"""
from __future__ import annotations

import argparse
import sys
import time

import requests
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode

from common import DATA_DIR, load_all, utcnow_iso, write_json

AGGREGATE3 = "0x82ad56cb"    # aggregate3((address,bool,bytes)[])
BALANCE_OF = bytes.fromhex("70a08231")   # balanceOf(address)
DECIMALS = bytes.fromhex("313ce567")     # decimals()
SYMBOL = bytes.fromhex("95d89b41")       # symbol()

# Which config token keys roll up into which headline asset.
ASSET_OF = {"USDC": "usdc", "USDC.e": "usdc",
            "USDT": "usdt", "USDT.e": "usdt", "USDT-legacy": "usdt"}


def rpc_call(urls: list, payload: dict, timeout: int = 30):
    """POST to the first endpoint that answers; public RPCs fail often enough
    that a per-chain failover list is worth having."""
    last = None
    for url in urls:
        for attempt in range(2):
            try:
                r = requests.post(url, json=payload, timeout=timeout)
                data = r.json()
                if "error" in data:
                    last = f"{url}: {data['error'].get('message', data['error'])}"
                    break
                return data["result"]
            except Exception as exc:  # noqa: BLE001
                last = f"{url}: {type(exc).__name__}"
                time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(last or "no endpoint answered")


def build_aggregate3(calls: list) -> str:
    """calls: [(target_address, calldata_bytes)] -> aggregate3 payload.

    allowFailure is always True: a token that is not deployed on a chain must
    come back as one failed sub-call, not revert the whole batch.
    """
    tuples = [(target, True, data) for target, data in calls]
    return AGGREGATE3 + abi_encode(["(address,bool,bytes)[]"], [tuples]).hex()


def multicall(urls: list, multicall_addr: str, calls: list, block: str = "latest") -> list:
    result = rpc_call(urls, {
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": multicall_addr, "data": build_aggregate3(calls)}, block],
    })
    decoded = abi_decode(["(bool,bytes)[]"], bytes.fromhex(result[2:]))[0]
    return list(decoded)


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def probe_tokens(chain: str, spec: dict, multicall_addr: str) -> dict:
    """Confirm each configured token really exists and learn its decimals.

    Several addresses in config/rpc.yml could not be verified against a primary
    source. Anything that fails here is dropped rather than guessed at.
    """
    tokens = spec.get("tokens") or {}
    if not tokens:
        return {}
    names = list(tokens)
    calls = []
    for name in names:
        addr = tokens[name]["address"]
        calls.append((addr, DECIMALS))
        calls.append((addr, SYMBOL))

    try:
        results = multicall(spec["rpc"], multicall_addr, calls)
    except RuntimeError as exc:
        print(f"  {chain:12} UNREACHABLE - {exc}")
        return {}

    out = {}
    for i, name in enumerate(names):
        ok_dec, dec_raw = results[i * 2]
        ok_sym, sym_raw = results[i * 2 + 1]
        if not ok_dec or len(dec_raw) < 32:
            print(f"  {chain:12} {name:12} FAILED probe - dropped "
                  f"({tokens[name]['address']})")
            continue
        decimals = int.from_bytes(dec_raw[-32:], "big")
        symbol = ""
        if ok_sym and sym_raw:
            try:
                symbol = abi_decode(["string"], sym_raw)[0]
            except Exception:  # noqa: BLE001 - some tokens return bytes32
                symbol = sym_raw.rstrip(b"\x00").decode("utf8", "ignore")
        out[name] = {"address": tokens[name]["address"], "decimals": decimals,
                     "symbol": symbol}
        print(f"  {chain:12} {name:12} ok  decimals={decimals:<3} symbol={symbol!r}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Read USDC/USDT balances on-chain")
    ap.add_argument("--probe", action="store_true",
                    help="only verify token addresses and decimals")
    ap.add_argument("--chunk", type=int, default=400, help="pairs per eth_call")
    ap.add_argument("--chain", action="append",
                    help="limit to these chains (repeatable)")
    args = ap.parse_args()

    cfg = load_all()
    rpc_cfg = cfg["rpc"] or {}
    multicall_addr = rpc_cfg.get("multicall3")
    chain_cfg = rpc_cfg.get("chains") or {}
    book = (cfg["addresses"] or {}).get("teams") or {}

    wanted = set(args.chain or [])
    chains = {c: s for c, s in chain_cfg.items() if not wanted or c in wanted}

    print(f"Probing token contracts on {len(chains)} chains "
          f"(decimals read on-chain, never assumed) ...")
    verified = {}
    for chain, spec in chains.items():
        got = probe_tokens(chain, spec, multicall_addr)
        if got:
            verified[chain] = got

    if args.probe:
        total = sum(len(v) for v in verified.values())
        print(f"\n{total} token contracts verified across {len(verified)} chains.")
        return 0

    if not book:
        print("\nconfig/addresses.yml lists no teams, so there is nothing to read.")
        print("Add contract addresses there to measure balances directly; "
              "the dashboard runs on DefiLlama data without it.")
        return 0

    results: dict[str, dict] = {}
    for chain, tokens in verified.items():
        pairs = []
        for team_key, spec in book.items():
            for addr in ((spec or {}).get("addresses") or {}).get(chain, []) or []:
                for token_name, token in tokens.items():
                    pairs.append((team_key, addr, token_name, token))
        if not pairs:
            continue

        print(f"\n{chain}: {len(pairs)} (token, holder) pairs")
        try:
            block = rpc_call(chains[chain]["rpc"],
                             {"jsonrpc": "2.0", "id": 1,
                              "method": "eth_blockNumber", "params": []})
        except RuntimeError as exc:
            print(f"  block number unavailable: {exc}")
            continue

        for batch in chunks(pairs, args.chunk):
            calls = [(t["address"], BALANCE_OF + abi_encode(["address"], [holder]))
                     for _, holder, _, t in batch]
            try:
                # Pinned to one block so every balance in the batch is one snapshot.
                decoded = multicall(chains[chain]["rpc"], multicall_addr, calls, block)
            except RuntimeError as exc:
                print(f"  batch failed: {exc}")
                continue
            for (team_key, holder, token_name, token), (ok, raw) in zip(batch, decoded):
                if not ok or len(raw) < 32:
                    continue
                value = int.from_bytes(raw[-32:], "big") / (10 ** token["decimals"])
                if value <= 0:
                    continue
                asset = ASSET_OF.get(token_name)
                if not asset:
                    continue
                slot = results.setdefault(team_key, {}).setdefault(
                    chain, {"usdc": 0.0, "usdt": 0.0})
                slot[asset] += value

        got = sum(1 for k in results if chain in results[k])
        print(f"  {got} teams with a balance on {chain} at block {int(block, 16)}")

    write_json(DATA_DIR / "onchain.json", results)
    print(f"\nWrote data/onchain.json for {len(results)} teams. "
          "Re-run build.py to merge into the dashboard.")
    print("Balances are treated as USD 1:1; these are dollar stablecoins.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
