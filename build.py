"""Turn data/snapshot.json into the dashboard: docs/index.html + docs/data.json.

Classification happens here rather than in the collector, so the token rules in
config/tokens.yml can be changed and re-applied without refetching anything.
"""
from __future__ import annotations

import argparse
import json
import sys
import webbrowser

from jinja2 import Environment, FileSystemLoader, select_autoescape

from common import (DATA_DIR, DOCS_DIR, ROOT, Chains, load_all, read_json,
                    utcnow_iso, write_json)

TEMPLATE_DIR = ROOT / "templates"

# Ordered worst-to-best: a team's state is the strongest signal found.
ARB_STATES = ("none", "mentioned", "listed", "deployed")


def arbitrum_state(team: dict, target: str, signals: dict, overrides: dict,
                   min_deployed: float = 1000.0) -> dict:
    """Decide whether a team is already on the target chain.

    DefiLlama is not the only source: a team can be flagged by its own site or
    docs (signals, gathered by arbitrum_signal.py) or by hand in
    config/arbitrum_overrides.yml, which always wins.
    """
    tvl = float((team.get("chain_tvl") or {}).get(target) or 0)
    listed = target in (team.get("chains") or [])

    if tvl >= min_deployed:
        state, why, source = "deployed", f"${tvl:,.0f} TVL on {target}", "defillama"
    elif tvl > 0:
        # Dust: contracts exist but hold effectively nothing, so this stays a
        # live prospect rather than being written off as already deployed.
        state = "listed"
        why = f"only ${tvl:,.2f} TVL on {target} - deployed but dormant"
        source = "defillama"
    elif listed:
        state, why, source = "listed", f"listed on {target}, no TVL reported", "defillama"
    else:
        state, why, source = "none", "no signal found", None

    signal = signals.get(team["key"])
    if signal and ARB_STATES.index(signal.get("state", "none")) > ARB_STATES.index(state):
        state = signal["state"]
        why = signal.get("note") or "referenced on their own site"
        source = signal.get("source") or "website"

    override = overrides.get(team["key"])
    if override:
        state = override.get("state", state)
        why = override.get("note") or "manual override"
        source = override.get("source") or "manual"

    return {"state": state, "tvl": tvl, "why": why, "source": source}


def build_rows(snapshot: dict, cfg: dict, min_tvl: float) -> list:
    classifier = cfg["tokens"]
    assets = [a.lower() for a in classifier.assets]
    chains = Chains(cfg["chains_cfg"])
    target = snapshot.get("target_chain") or chains.target
    overrides = (cfg["arb_overrides"] or {}).get("overrides") or {}
    min_deployed = float((cfg["chains_cfg"] or {}).get("min_tvl_for_deployed") or 0)
    signals = read_json(DATA_DIR / "arbitrum_signals.json", {}) or {}
    onchain = read_json(DATA_DIR / "onchain.json", {}) or {}

    rows = []
    for team in snapshot.get("teams") or []:
        if float(team.get("tvl") or 0) < min_tvl:
            continue

        per_chain = {}
        totals = {"other": 0.0}
        for a in assets:
            totals[a] = 0.0
            totals[f"{a}_wrapped"] = 0.0
        past = {"d7": 0.0, "d30": 0.0}
        has_past = {"d7": False, "d30": False}

        for chain, entry in (team.get("chain_tokens") or {}).items():
            # Past windows are accumulated first and unconditionally: a chain a
            # team has fully exited holds nothing now but held plenty 7d ago, and
            # skipping it would hide the outflow.
            for window in ("d7", "d30"):
                raw = entry.get(window) or {}
                if raw:
                    split = classifier.split(raw)
                    past[window] += sum(split[a] for a in assets)
                    has_past[window] = True

            now = classifier.split(entry.get("now") or {})
            if not any(now.values()):
                continue
            slot = {a: round(now[a], 2) for a in assets}
            slot["wrapped"] = round(sum(now[f"{a}_wrapped"] for a in assets), 2)
            slot["other"] = round(now["other"], 2)
            slot["src"] = "llama"
            per_chain[chain] = slot
            for key in totals:
                totals[key] += now[key]

        # Exact on-chain reads override the estimate for that chain.
        for chain, measured in (onchain.get(team["key"]) or {}).items():
            blank = {a: 0.0 for a in assets}
            blank.update({"wrapped": 0.0, "other": 0.0})
            slot = per_chain.setdefault(chain, blank)
            for key in assets:
                if key not in measured:
                    continue
                totals[key] += float(measured.get(key) or 0) - float(slot.get(key) or 0)
                slot[key] = round(float(measured.get(key) or 0), 2)
            slot["src"] = "rpc"

        core = sum(totals[a] for a in assets)
        arb = arbitrum_state(team, target, signals, overrides, min_deployed)

        top_chain = max(per_chain.items(),
                        key=lambda kv: sum(kv[1].get(a, 0) for a in assets),
                        default=(None, None))[0]

        rows.append({
            "k": team["key"],
            "n": team.get("name") or team["key"],
            "c": team.get("category") or "Uncategorised",
            "u": team.get("url"),
            "t": round(float(team.get("tvl") or 0), 2),
            **{a: round(totals[a], 2) for a in assets},
            "core": round(core, 2),
            "w": round(sum(totals[f"{a}_wrapped"] for a in assets), 2),
            "o": round(totals["other"], 2),
            "st": team.get("token_status") or "not_fetched",
            "arb": arb["state"],
            "arbTvl": round(arb["tvl"], 2),
            "arbWhy": arb["why"],
            "arbSrc": arb["source"],
            "ch": per_chain,
            "chains": team.get("chains") or [],
            "top": top_chain,
            "d7": round(core - past["d7"], 2) if has_past["d7"] else None,
            "d30": round(core - past["d30"], 2) if has_past["d30"] else None,
            "np": len(team.get("protocols") or []),
        })

    rows.sort(key=lambda r: r["core"], reverse=True)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Render the dashboard from snapshot.json")
    ap.add_argument("--min-tvl", type=float, default=100_000,
                    help="omit teams below this total TVL (default 100k)")
    ap.add_argument("--open", action="store_true", help="open the dashboard when done")
    args = ap.parse_args()

    snapshot = read_json(DATA_DIR / "snapshot.json")
    if not snapshot:
        print("data/snapshot.json missing - run: python fetch_llama.py", file=sys.stderr)
        return 1

    cfg = load_all()
    classifier = cfg["tokens"]
    rows = build_rows(snapshot, cfg, args.min_tvl)

    markets = snapshot.get("chain_markets") or {}
    priority = snapshot.get("priority_chains") or []
    market_rows = [dict({"chain": c},
                        **{a.lower(): round(markets.get(c, {}).get(a, 0), 2)
                           for a in classifier.assets})
                   for c in priority if c in markets]

    categories = sorted({r["c"] for r in rows})
    chain_names = sorted({c for r in rows for c in r["ch"]},
                         key=Chains(cfg["chains_cfg"]).sort_key)

    payload = {
        "assets": [{"key": a.lower(), "label": classifier.display[a.upper()]}
                   for a in classifier.assets],
        "generated_at": snapshot.get("generated_at") or utcnow_iso(),
        "built_at": utcnow_iso(),
        "target_chain": snapshot.get("target_chain", "Arbitrum"),
        "scope": snapshot.get("scope") or {},
        "markets": market_rows,
        "categories": categories,
        "chains": chain_names,
        "rows": rows,
    }

    write_json(DOCS_DIR / "data.json", payload)

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)),
                      autoescape=select_autoescape(["html"]))
    template = env.get_template("dashboard.html.j2")
    # Embedded rather than fetched, so the file works over file:// as well as
    # from GitHub Pages (a local fetch() of data.json is blocked by CORS).
    # Escaping "<" keeps a team name containing "</script>" from breaking out of
    # the JSON block; the sequence stays valid JSON either way.
    embedded = json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c")
    html = template.render(
        payload_json=embedded,
        generated_at=payload["generated_at"],
        target_chain=payload["target_chain"],
    )
    out = DOCS_DIR / "index.html"
    out.write_text(html, encoding="utf8")

    size_mb = out.stat().st_size / 1e6
    prospects = [r for r in rows if r["arb"] == "none" and 500_000 <= r["t"] <= 10_000_000]
    print(f"docs/index.html  {size_mb:.1f} MB  {len(rows)} teams")
    print(f"  {len(prospects)} teams in the $500k-$10M band with no {payload['target_chain']} signal")
    labels = " + ".join(a["label"] for a in payload["assets"])
    print(f"  holding ${sum(r['core'] for r in prospects):,.0f} in {labels}")

    if args.open:
        webbrowser.open(out.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
