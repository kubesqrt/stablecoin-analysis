"""Answer the question "what are we missing?" with evidence rather than a guess.

Two independent gaps exist, and they need different treatment:

  1. Teams we know about but never fetched a token breakdown for. Measurable
     directly from the snapshot - see the "fetch coverage" section.

  2. Contracts holding tracked stablecoins that we cannot tie to any team. Those
     are invisible to a DefiLlama-driven view by construction, so this walks the
     largest on-chain holders of each token (free Blockscout API, no key) and
     subtracts everything we can attribute. What remains is the honest blind
     spot, ranked by size.

Most of the remainder is expected to be exchanges, bridges and ordinary wallets,
which are not teams. The value is the tail: protocol contracts holding real money
that nothing in our pipeline accounts for. Those are the ones worth adding to
config/addresses.yml.

    python coverage.py                 # full report
    python coverage.py --chain Arbitrum --top 200
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from collections import Counter

import requests

from common import DATA_DIR, DOCS_DIR, load_all, read_json, utcnow_iso, write_json

UA = {"User-Agent": "stablecoin-dashboard/1.0 (coverage audit)"}

# Labels that mark a holder as "not a team we could sell to" - exchange floats,
# bridge escrows, and the token contracts themselves.
NOT_A_TEAM = ("bridge", "gateway", "exchange", "binance", "coinbase", "okx",
              "bybit", "kraken", "bitfinex", "crypto.com", "gate.io", "htx",
              "portal", "wormhole", "layerzero", "stargate", "across",
              "circle", "tether", "treasury", "l1standardbridge", "l2standardbridge",
              "optimismportal", "escrow", "custody", "multisig", "safe")


def get(url: str, params: dict | None = None, tries: int = 3):
    """Blockscout's keyless tier is ~5 rps shared, so back off rather than hammer."""
    for attempt in range(tries):
        try:
            r = requests.get(url, params=params, timeout=30, headers=UA)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 502, 503, 504):
                time.sleep(2.0 * (attempt + 1))
                continue
            return None
        except Exception:  # noqa: BLE001
            time.sleep(1.5 * (attempt + 1))
    return None


def top_holders(base: str, token: str, want: int) -> list:
    """Page through the holders endpoint until we have `want` of them."""
    out, params, url = [], None, f"{base}/api/v2/tokens/{token}/holders"
    while len(out) < want:
        data = get(url, params)
        if not data:
            break
        items = data.get("items") or []
        if not items:
            break
        out.extend(items)
        nxt = data.get("next_page_params")
        if not nxt:
            break
        params = nxt
        time.sleep(0.25)
    return out[:want]


# Contract names that describe a pattern, not an owner - matching on these would
# tie half the chain to whichever team happens to share the word.
GENERIC = {"erc1967proxy", "transparentupgradeableproxy", "proxy", "vault",
           "markettoken", "token", "pool", "unnamed contract", "beaconproxy",
           "initializableimmutableadminupgradeabilityproxy", "erc20", "safeproxy",
           "upgradeableproxy", "factory", "router", "gnosissafeproxy"}

# Smart-contract wallets. These are contracts, but they belong to individuals,
# not teams - the two largest "unattributed" holders on Arbitrum are SingleOwnerMSCA
# accounts. Counting them as missing protocols would badly overstate the gap.
SMART_ACCOUNT = ("msca", "simpleaccount", "smartaccount", "accountproxy",
                 "erc6900", "kernel", "lightaccount", "biconomy", "soulwallet",
                 "argent", "ambire", "gnosissafe", "safeproxy")


def enrich(base: str, address: str) -> str:
    """Fetch extra naming for a contract whose own name says nothing.

    A bare ERC1967Proxy reveals its owner two ways: the implementation contract's
    name, and - for tokenised positions - the token name ("GMX Market" -> GMX).
    """
    data = get(f"{base}/api/v2/addresses/{address}")
    if not data:
        return ""
    parts = [i.get("name") or "" for i in (data.get("implementations") or [])]
    token = data.get("token") or {}
    parts.append(token.get("name") or "")
    parts.extend(t.get("display_name", "") if isinstance(t, dict) else str(t)
                 for t in (data.get("public_tags") or []))
    return " ".join(p for p in parts if p)


# Team names that are ordinary nouns. A team really is called "Market", and
# without this it claims every contract named "GMX Market".
GENERIC_TEAM_NAMES = {"market", "markets", "vault", "vaults", "pool", "pools",
                      "token", "tokens", "bridge", "swap", "finance", "protocol",
                      "wallet", "stake", "staking", "yield", "lend", "lending",
                      "exchange", "liquidity", "money", "capital", "index",
                      "reserve", "treasury", "router", "core", "hub", "node"}


def build_name_index(teams: list) -> list:
    """Team names we can safely match on.

    Sorted most-specific first: more words beats fewer, then longer beats
    shorter, so "Aave v3" wins over "Aave" and "GMX" is not shadowed by "Market".
    """
    idx = []
    for t in teams:
        name = (t.get("name") or "").strip()
        low = name.lower()
        if len(name) < 3 or low in GENERIC or low in GENERIC_TEAM_NAMES:
            continue
        parts = words(low)
        # A single generic word is not evidence of ownership.
        if len(parts) == 1 and parts[0] in GENERIC_TEAM_NAMES:
            continue
        idx.append((low, t.get("name"), t.get("key")))
    idx.sort(key=lambda x: (-len(words(x[0])), -len(x[0])))
    return idx


def words(text: str) -> list:
    """Split a contract name into comparable words.

    Contract names run words together, so camelCase has to be broken up:
    "UniswapV3Pool" -> ["uniswap", "v", "3", "pool"].
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text or "")
    return [w for w in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if w]


def match_label(label: str, name_index: list) -> tuple[str, str] | None:
    """Reconcile a contract's on-chain label to a team we already track.

    Blockscout often names the contract after its owner ("Aave v3 USDT",
    "GMX Market"), which turns an unattributed balance into an identified one
    without any manual research.

    Matching is on whole words, never substrings. Substring matching produced
    real false positives - the team "Initia" matched
    "InitializableImmutableAdminUpgradeabilityProxy", misattributing $38.5M of
    Aave's money. A wrong attribution is worse than an honest blank.
    """
    tokens = set(words(label))
    if not tokens:
        return None
    best = None
    for lowered, display, key in name_index:
        parts = words(lowered)
        if not parts or not all(p in tokens for p in parts):
            continue
        score = (len(parts), len(lowered))
        if best is None or score > best[0]:
            best = (score, display, key)
    return (best[1], best[2]) if best else None


def classify(entry: dict, known: dict, name_index: list,
             extra: str = "") -> tuple[str, str, str | None]:
    """Return (bucket, label, matched_team)."""
    addr = (entry.get("address") or {})
    hash_ = (addr.get("hash") or "").lower()

    name = addr.get("name") or ""
    tags = " ".join(t.get("display_name", "") if isinstance(t, dict) else str(t)
                    for t in (addr.get("public_tags") or []))
    ens = addr.get("ens_domain_name") or ""
    label = " / ".join(x for x in (name, tags, ens) if x)

    if hash_ in known:
        return "attributed", known[hash_], known[hash_]

    if not addr.get("is_contract"):
        return "wallet", label or "externally owned account", None

    blob = f"{name} {tags} {ens} {extra}".lower()
    if any(k in blob for k in SMART_ACCOUNT):
        return "wallet", (label or "smart-contract wallet"), None
    if any(k in blob for k in NOT_A_TEAM):
        return "infrastructure", label, None

    hit = match_label(f"{label} {extra}", name_index)
    if hit:
        return "matched_by_label", (f"{label} / {extra}".strip(" /")
                                    or "unnamed contract"), hit[0]

    return "unattributed_contract", label or "unnamed contract", None


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit how much stablecoin value we account for")
    ap.add_argument("--top", type=int, default=100,
                    help="holders to examine per token per chain (default 100)")
    ap.add_argument("--chain", action="append", help="limit to these chains (repeatable)")
    ap.add_argument("--enrich-min", type=float, default=3_000_000,
                    help="look up the implementation name for unnamed contracts above this")
    ap.add_argument("--enrich-max", type=int, default=25,
                    help="most contracts to look up per chain")
    ap.add_argument("--skip-holders", action="store_true",
                    help="only report fetch coverage, no on-chain holder walk")
    args = ap.parse_args()

    snap = read_json(DATA_DIR / "snapshot.json")
    payload = read_json(DOCS_DIR / "data.json")
    if not snap or not payload:
        print("Run fetch_llama.py and build.py first", file=sys.stderr)
        return 1

    cfg = load_all()

    # ---------------------------------------------------------------- part 1
    print("=" * 74)
    print("FETCH COVERAGE - teams we know about but have no token breakdown for")
    print("=" * 74)

    teams = snap["teams"]
    status = Counter(t.get("token_status") for t in teams)
    tvl_by_status: Counter = Counter()
    for t in teams:
        tvl_by_status[t.get("token_status")] += float(t.get("tvl") or 0)

    print(f"\n{'status':16} {'teams':>7} {'TVL':>14}   meaning")
    meaning = {
        "ok": "token split available",
        "partial": "some protocols missing a split",
        "not_fetched": "OUTSIDE the fetch scope - widen --min-tvl/--max-tvl",
        "unavailable": "adapter publishes no token data at all",
        "too_large": "document above the --max-mb cap",
        "error": "fetch failed - re-run",
    }
    for st, n in status.most_common():
        print(f"{str(st):16} {n:>7} {tvl_by_status[st]/1e9:>12,.2f}B   {meaning.get(st, '')}")

    blind = sum(v for k, v in tvl_by_status.items()
                if k in ("not_fetched", "unavailable", "too_large", "error"))
    total = sum(tvl_by_status.values())
    print(f"\nTVL with no token breakdown: ${blind/1e9:,.2f}B of ${total/1e9:,.2f}B "
          f"({blind/total*100:.1f}%)")

    worst = sorted((t for t in teams if t.get("token_status") != "ok"),
                   key=lambda t: -float(t.get("tvl") or 0))[:10]
    if worst:
        print("\nBiggest teams with no split (fix these first):")
        for t in worst:
            print(f"  {t['name'][:34]:35} ${float(t['tvl'])/1e9:>7.2f}B  {t.get('token_status')}")

    if args.skip_holders:
        return 0

    # ---------------------------------------------------------------- part 2
    print("\n" + "=" * 74)
    print("ATTRIBUTION COVERAGE - largest on-chain holders we cannot tie to a team")
    print("=" * 74)

    instances = (cfg["rpc"] or {}).get("blockscout") or {}
    chain_tokens = (cfg["rpc"] or {}).get("chains") or {}
    wanted = set(args.chain or [])

    # Address book we can already name.
    known: dict[str, str] = {}
    for team_key, spec in ((cfg["addresses"] or {}).get("teams") or {}).items():
        for _chain, addrs in ((spec or {}).get("addresses") or {}).items():
            for a in addrs or []:
                known[a.lower()] = (spec or {}).get("name") or team_key

    # What the dashboard currently attributes per chain.
    attributed: dict[str, float] = {}
    for r in payload["rows"]:
        for ch, v in (r["ch"] or {}).items():
            attributed[ch] = attributed.get(ch, 0.0) + v["usdc"] + v["usdt"]

    name_index = build_name_index(teams)
    findings: dict[str, list] = {}
    matches: dict[str, list] = {}
    for chain, base in instances.items():
        if wanted and chain not in wanted:
            continue
        tokens = (chain_tokens.get(chain) or {}).get("tokens") or {}
        if not tokens:
            continue

        rows = []
        failed = []
        for token_name, spec in tokens.items():
            holders = top_holders(base, spec["address"], args.top)
            if not holders:
                # Never silently report partial coverage as if it were complete -
                # a timed-out token makes the chain look far smaller than it is.
                failed.append(token_name)
                continue
            for h in holders:
                try:
                    raw = int(h.get("value", 0))
                except (TypeError, ValueError):
                    continue
                # Decimals come from the token metadata Blockscout returns.
                dec = int((h.get("token") or {}).get("decimals") or 6)
                value = raw / (10 ** dec)
                if value <= 0:
                    continue
                bucket, label, team = classify(h, known, name_index)
                rows.append({
                    "chain": chain, "token": token_name, "value": value,
                    "address": ((h.get("address") or {}).get("hash") or ""),
                    "bucket": bucket, "label": label, "team": team,
                    "is_contract": bool((h.get("address") or {}).get("is_contract")),
                })

        if not rows:
            print(f"\n{chain}: no holder data returned "
                  f"({', '.join(failed)} unreachable)" if failed else "")
            continue

        # Second pass: the largest unnamed contracts each earn one extra request
        # to find out what they really are. A bare ERC1967Proxy names its owner
        # through its implementation contract ("ATokenInstance Aave v3 USDC").
        big_unknown = sorted((r for r in rows
                              if r["bucket"] == "unattributed_contract"
                              and r["value"] >= args.enrich_min),
                             key=lambda r: -r["value"])[:args.enrich_max]
        if big_unknown:
            print(f"\n{chain}: identifying {len(big_unknown)} large unnamed "
                  f"contracts ...", flush=True)
            for r in big_unknown:
                extra = enrich(base, r["address"])
                time.sleep(0.3)
                if not extra:
                    continue
                bucket, label, team = classify(
                    {"address": {"hash": r["address"], "is_contract": True,
                                 "name": r["label"]}},
                    known, name_index, extra)
                r["bucket"] = bucket
                r["team"] = team
                r["label"] = f"{r['label']} / {extra}".strip(" /")

        by_bucket: Counter = Counter()
        for r in rows:
            by_bucket[r["bucket"]] += r["value"]
        scanned = sum(by_bucket.values())
        ours = attributed.get(chain, 0.0)

        warn = f"   [INCOMPLETE: {', '.join(failed)} unreachable]" if failed else ""
        print(f"\n{chain}  (top {args.top} holders per token, "
              f"${scanned/1e6:,.0f}M scanned){warn}")
        print(f"  dashboard attributes to teams        ${ours/1e6:>10,.0f}M")
        for b in ("wallet", "infrastructure", "attributed", "matched_by_label",
                  "unattributed_contract"):
            if by_bucket.get(b):
                print(f"  {b:36} ${by_bucket[b]/1e6:>10,.0f}M")

        named = sorted((r for r in rows if r["bucket"] == "matched_by_label"),
                       key=lambda r: -r["value"])
        matches[chain] = named
        if named:
            agg = {}
            for r in named:
                agg[r["team"]] = agg.get(r["team"], 0.0) + r["value"]
            print("\n  Reconciled to a team by its on-chain contract label:")
            for team, val in sorted(agg.items(), key=lambda kv: -kv[1])[:8]:
                print(f"    {team[:32]:33} ${val/1e6:>9,.1f}M")

        gaps = sorted((r for r in rows if r["bucket"] == "unattributed_contract"),
                      key=lambda r: -r["value"])
        findings[chain] = gaps
        if gaps:
            print(f"\n  Unnamed contracts holding stablecoins, largest first:")
            for g in gaps[:12]:
                print(f"    {g['address']}  {g['token']:>7} "
                      f"${g['value']/1e6:>9,.1f}M  {g['label'][:34]}")

    write_json(DATA_DIR / "coverage.json",
               {"generated_at": utcnow_iso(), "findings": findings,
                "matched_by_label": matches})

    total_gap = sum(g["value"] for gs in findings.values() for g in gs)
    count = sum(len(gs) for gs in findings.values())
    print("\n" + "=" * 74)
    print(f"{count} contracts holding ${total_gap/1e6:,.0f}M are not attributed to any team.")
    print("Written to data/coverage.json.")
    print("\nThese are candidates, not confirmed misses - many will be exchange or")
    print("bridge contracts whose labels we could not read. Identify the ones that")
    print("belong to a team and add them to config/addresses.yml, then run onchain.py")
    print("to fold their balances in as measured figures.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
