"""Sanity checks over the built snapshot. Run after fetch_llama.py + build.py.

These are the checks that catch the failure modes this data actually has:
double-counted pseudo-chains, broken parent bundling, and stablecoin totals that
exceed the TVL they are supposedly part of.
"""
from __future__ import annotations

import sys

from common import DATA_DIR, DOCS_DIR, Chains, load_all, read_json

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
results = []


def check(name: str, ok, detail: str = "") -> None:
    status = PASS if ok is True else (WARN if ok == "warn" else FAIL)
    results.append((status, name, detail))


def main() -> int:
    snap = read_json(DATA_DIR / "snapshot.json")
    payload = read_json(DOCS_DIR / "data.json")
    if not snap or not payload:
        print("Missing snapshot.json or docs/data.json - run fetch_llama.py and build.py")
        return 1

    cfg = load_all()
    chains = Chains(cfg["chains_cfg"])
    teams = snap["teams"]
    rows = payload["rows"]

    # 1. No pseudo-chain key survived into the output.
    bad_keys = sorted({c for t in teams for c in (t.get("chain_tokens") or {})
                       if chains.is_pseudo(c)}
                      | {c for t in teams for c in (t.get("chain_tvl") or {})
                         if chains.is_pseudo(c)})
    check("no pseudo-chain keys in output", not bad_keys,
          f"found {bad_keys[:6]}" if bad_keys else "borrowed/staking/pool2 all filtered")

    # 2. Stablecoin total must not exceed total TVL. Some overshoot is legitimate
    #    (TVL nets out borrows while token balances are gross), so flag only the
    #    egregious cases that indicate a counting bug.
    over = [(r["n"], r["core"], r["t"]) for r in rows
            if r["t"] > 0 and r["core"] > r["t"] * 1.5]
    check("stablecoins <= 1.5x total TVL", not over,
          f"{len(over)} teams over, worst: " +
          ", ".join(f"{n} ${c/1e6:.1f}M vs ${t/1e6:.1f}M TVL"
                    for n, c, t in sorted(over, key=lambda x: -x[1])[:3])
          if over else f"all {len(rows)} teams within bound")

    # 3. Parent bundling actually collapses children.
    multi = [t for t in teams if len(t.get("protocols") or []) > 1]
    aave = next((t for t in teams if t["key"] == "aave"), None)
    if aave:
        kids = [p["slug"] for p in aave["protocols"]]
        expected = sum(p["tvl"] for p in aave["protocols"] if p["counted"])
        drift = abs(expected - aave["tvl"])
        check("parent bundling (aave)", drift < max(1.0, expected * 1e-6),
              f"{len(kids)} protocols -> 1 team, total ${aave['tvl']/1e9:.2f}B")
    else:
        check("parent bundling (aave)", "warn", "aave not present in snapshot")
    check("multi-protocol teams exist", len(multi) > 50,
          f"{len(multi)} teams bundle more than one protocol")

    # 4. Token classification produced sane headline numbers.
    with_tokens = [r for r in rows if r["st"] in ("ok", "partial")]
    nonzero = [r for r in with_tokens if r["core"] > 0]
    check("teams with a token breakdown", len(with_tokens) > 500,
          f"{len(with_tokens)} teams, {len(nonzero)} holding USDC/USDT")

    # 5. USDT0 must be counted as USDT (it is the dominant USDT form on L2s).
    tc = cfg["tokens"]
    usdt0 = tc.classify("USDT0")
    check("USDT0 classified as core USDT", usdt0 == ("core", "USDT"), str(usdt0))
    # USDe is a headline asset; sUSDe is the same claim staked. Other stablecoins
    # must stay in their own column so the headline figures mean one thing.
    wrong = [f"{s}->{tc.classify(s)}" for s, want in
             [("USDe", ("core", "USDE")), ("sUSDe", ("core", "USDE")),
              ("DAI", ("other", "OTHER")), ("USDS", ("other", "OTHER")),
              ("PYUSD", ("other", "OTHER"))]
             if tc.classify(s) != want]
    check("stablecoin bucketing", not wrong,
          "; ".join(wrong) if wrong else f"assets: {', '.join(tc.assets)}")

    # 6. Arbitrum states are well-formed and the prospect list is non-trivial.
    states = {}
    for r in rows:
        states[r["arb"]] = states.get(r["arb"], 0) + 1
    unknown = set(states) - {"deployed", "listed", "mentioned", "none"}
    check("arbitrum states well-formed", not unknown, str(states))

    band = [r for r in rows if 500_000 <= r["t"] <= 10_000_000]
    prospects = [r for r in band if r["arb"] == "none"]
    check("prospect list populated", len(prospects) > 50,
          f"{len(prospects)} of {len(band)} band teams have no Arbitrum signal, "
          f"holding ${sum(p['core'] for p in prospects)/1e6:.0f}M")

    # 7. A team marked deployed must clear the meaningful-presence threshold;
    #    dust deployments belong in "listed" so they stay visible as prospects.
    min_deployed = float((cfg["chains_cfg"] or {}).get("min_tvl_for_deployed") or 0)
    inconsistent = [r["n"] for r in rows if r["arb"] == "deployed"
                    and r["arbTvl"] < min_deployed and r["arbSrc"] == "defillama"]
    check("deployed implies real Arbitrum TVL", not inconsistent,
          f"{len(inconsistent)} inconsistent: {inconsistent[:3]}" if inconsistent
          else f"all clear the ${min_deployed:,.0f} threshold")

    # 7b. Contract-label -> team matching. This had a real false positive: the
    #     team "Initia" matched "InitializableImmutableAdminUpgradeabilityProxy"
    #     and claimed $38.5M of Aave's money. Wrong attribution is worse than none.
    try:
        import coverage as cov
        idx = cov.build_name_index(teams)
        cases = [
            ("InitializableImmutableAdminUpgradeabilityProxy", None),
            ("TransparentUpgradeableProxy", None),
            ("ERC1967Proxy SingleOwnerMSCA", None),
            ("PoolManager", None),
            ("Aave v3 USDT", "Aave"),
            ("UniswapV3Pool", "Uniswap"),
            ("MarketToken GMX Market", "GMX"),
        ]
        wrong = []
        for label, expected in cases:
            hit = cov.match_label(label, idx)
            got = hit[0] if hit else None
            if got != expected:
                wrong.append(f"{label!r} -> {got} (want {expected})")
        check("contract-label matching", not wrong,
              "; ".join(wrong) if wrong else f"{len(cases)} cases correct, no false positives")
    except ImportError:
        check("contract-label matching", "warn", "coverage.py not importable")

    # 8. Chain market context loaded.
    markets = payload.get("markets") or []
    arb = next((m for m in markets if m["chain"] == "Arbitrum"), None)
    check("chain market context present", bool(arb) and arb["usdc"] > 0,
          f"{len(markets)} chains; Arbitrum USDC ${arb['usdc']/1e9:.2f}B, "
          f"USDT ${arb['usdt']/1e6:.0f}M" if arb else "missing")

    # 9. Dashboard actually built and embeds its data.
    index = DOCS_DIR / "index.html"
    html = index.read_text(encoding="utf8") if index.exists() else ""
    check("dashboard embeds payload", '<script id="payload"' in html and len(html) > 200_000,
          f"{len(html)/1e6:.1f} MB")
    check("no script breakout in embedded json",
          html.count("</script>") == html.count("<script"),
          f"{html.count('<script')} script tags open/close balanced")

    width = max(len(n) for _, n, _ in results)
    failed = 0
    for status, name, detail in results:
        if status == FAIL:
            failed += 1
        print(f"  [{status:4}] {name.ljust(width)}  {detail}")
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
