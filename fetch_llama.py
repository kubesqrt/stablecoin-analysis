"""Tier 1 + Tier 2 collector: DefiLlama -> data/snapshot.json.

Tier 1 is one 8.6MB request covering every protocol (USD TVL per chain, parent
mapping, categories). Tier 2 fetches the per-token breakdown, which only exists in
the per-protocol document - there is no free bulk token endpoint (/tokenProtocols
returns HTTP 402). Those documents run from 10KB to 45MB, so Tier 2 is restricted
to a TVL scope, capped by size, and cached with If-Modified-Since (the API honours
it and returns a 0-byte 304, making repeat runs nearly free).
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from common import (CACHE_DIR, DATA_DIR, HISTORY_DIR, Chains, load_all,
                    read_json, utcnow_iso, write_json)

API = "https://api.llama.fi"
STABLES_API = "https://stablecoins.llama.fi"
UA = {"User-Agent": "stablecoin-team-dashboard/1.0 (internal BD tooling)"}

_local = threading.local()


def session() -> requests.Session:
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update(UA)
        _local.s = s
    return s


def get_json(url: str, timeout: int = 120, retries: int = 3):
    last = None
    for attempt in range(retries):
        try:
            r = session().get(url, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001 - network layer, retry everything
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed after {retries} tries: {last}")


# ---------------------------------------------------------------------------
# Tier 2 helpers
# ---------------------------------------------------------------------------

def _nearest(series: list, target_ts: float, tolerance: int = 3 * 86400):
    """Pick the series entry closest to target_ts, or None if nothing is near it.

    tokensInUsd is a daily series, so "value 7 days ago" comes straight out of the
    document we already downloaded - no need to wait for local history to build up.
    """
    best, best_gap = None, None
    for entry in series:
        try:
            ts = float(entry.get("date", 0))
        except (TypeError, ValueError):
            continue
        gap = abs(ts - target_ts)
        if best_gap is None or gap < best_gap:
            best, best_gap = entry, gap
    if best is None or best_gap > tolerance:
        return None
    return best


def _usd_tokens(entry: dict) -> dict:
    """Keep only dollar-ish symbols; a large protocol reports 200+ tokens."""
    out = {}
    for sym, usd in (entry.get("tokens") or {}).items():
        s = str(sym).upper()
        if ("USD" in s or "DAI" in s or "EUR" in s or "FRAX" in s
                or "GHO" in s or "MIM" in s):
            try:
                val = float(usd or 0)
            except (TypeError, ValueError):
                continue
            if val > 0:
                out[sym] = round(val, 2)
    return out


def extract_tokens(doc: dict, chains: Chains) -> dict:
    """Reduce a multi-MB protocol document to the few numbers we keep.

    Everything else - years of daily history across every chain - is discarded
    immediately so the snapshot stays small.
    """
    result = {
        "status": "ok",
        "chains": {},
        "skip_token_breakdown": bool(doc.get("skipTokenBreakdownData")),
        "misrepresented": bool(doc.get("misrepresentedTokens")),
    }

    chain_tvls = doc.get("chainTvls") or {}
    any_tokens = False

    for raw_chain, payload in chain_tvls.items():
        if chains.is_pseudo(raw_chain):
            continue
        if not isinstance(payload, dict):
            continue
        chain = chains.normalise(raw_chain)

        series = payload.get("tokensInUsd") or []
        tvl_series = payload.get("tvl") or []

        entry = {"now": {}, "d7": {}, "d30": {}, "tvl": 0.0}

        if tvl_series:
            try:
                entry["tvl"] = float(tvl_series[-1].get("totalLiquidityUSD") or 0)
            except (TypeError, ValueError, AttributeError):
                entry["tvl"] = 0.0

        if series:
            any_tokens = True
            last = series[-1]
            last_ts = float(last.get("date") or 0)
            entry["now"] = _usd_tokens(last)
            entry["as_of"] = last_ts
            for label, days in (("d7", 7), ("d30", 30)):
                past = _nearest(series, last_ts - days * 86400)
                if past is not None and past is not last:
                    entry[label] = _usd_tokens(past)

        # A single document lists the same chain under BOTH its internal key and
        # its display label ("Sxr" and "SX Rollup", identical values). Those are
        # aliases, not two chains, so collapse with max - summing doubles them.
        if chain in result["chains"]:
            prev = result["chains"][chain]
            for key in ("now", "d7", "d30"):
                for sym, val in entry[key].items():
                    prev[key][sym] = max(prev[key].get(sym, 0.0), val)
            prev["tvl"] = max(prev["tvl"], entry["tvl"])
        else:
            result["chains"][chain] = entry

    if not any_tokens:
        # Distinguish "no token data published" from "holds no stablecoins".
        result["status"] = "unavailable"
    return result


def fetch_protocol(slug: str, chains: Chains, max_bytes: int, use_cache: bool,
                   retries: int = 3) -> dict:
    """Fetch one protocol document, honouring the disk cache and the size cap.

    Sustained concurrent traffic draws intermittent connection resets from the
    CDN, so transport failures are retried with backoff. A retry is cheap: the
    conditional request usually comes back as an empty 304.
    """
    cache_path = CACHE_DIR / f"{slug.replace('/', '_')}.json"
    cached = read_json(cache_path) if use_cache else None

    headers = {}
    if cached and cached.get("last_modified"):
        headers["If-Modified-Since"] = cached["last_modified"]

    url = f"{API}/protocol/{slug}"
    last_error = "unknown"

    for attempt in range(retries):
        if attempt:
            time.sleep(1.5 * attempt + random.random())
        try:
            r = session().get(url, headers=headers, timeout=180, stream=True)
        except Exception as exc:  # noqa: BLE001 - transport layer, retry
            last_error = str(exc)[:120]
            continue

        try:
            if r.status_code == 304 and cached:
                return cached["data"]

            if r.status_code in (429, 500, 502, 503, 504):
                last_error = f"HTTP {r.status_code}"
                time.sleep(2.0 * (attempt + 1))
                continue

            if r.status_code != 200:
                last_error = f"HTTP {r.status_code}"
                break

            # Stream so an oversized document is abandoned, not buffered whole.
            buf = bytearray()
            oversized = False
            try:
                for chunk in r.raw.stream(1 << 16, decode_content=True):
                    buf += chunk
                    if len(buf) > max_bytes:
                        oversized = True
                        break
            except Exception as exc:  # noqa: BLE001
                last_error = f"stream: {str(exc)[:100]}"
                continue
        finally:
            r.close()

        if oversized:
            if cached:
                return dict(cached["data"], stale=True)
            return {"status": "too_large", "chains": {},
                    "error": f"document exceeds {max_bytes // 10 ** 6}MB cap"}

        try:
            doc = json.loads(buf)
        except ValueError as exc:
            last_error = f"json: {str(exc)[:100]}"
            break

        data = extract_tokens(doc, chains)
        del doc
        del buf

        write_json(cache_path, {
            "last_modified": r.headers.get("last-modified"),
            "fetched_at": utcnow_iso(),
            "data": data,
        })
        return data

    # Every attempt failed: a stale cached figure beats no figure at all.
    if cached:
        return dict(cached["data"], stale=True)
    return {"status": "error", "chains": {}, "error": last_error}


# ---------------------------------------------------------------------------
# Team assembly
# ---------------------------------------------------------------------------

def is_live(p: dict) -> bool:
    return not (p.get("deadUrl") or p.get("rugged") or p.get("deprecated"))


def build_teams(protocols: list, parents: dict, chains: Chains, teams_cfg: dict) -> dict:
    """Group protocols into teams.

    The default key is DefiLlama's own parent-protocol mapping (aave-v2 + aave-v3
    -> "Aave"), which is exactly the contract-bundling link we want. teams.yml can
    override it.
    """
    split = set(teams_cfg.get("split") or [])
    rename = teams_cfg.get("rename") or {}
    exclude = set(teams_cfg.get("exclude") or [])
    forced = {}
    for team_key, spec in (teams_cfg.get("merge") or {}).items():
        for slug in (spec or {}).get("slugs") or []:
            forced[slug] = team_key

    teams: dict[str, dict] = {}
    for p in protocols:
        slug = p.get("slug")
        if not slug:
            continue
        if slug in forced:
            key = forced[slug]
        elif slug in split:
            key = slug
        else:
            key = p.get("parentProtocolSlug") or slug
        if key in exclude:
            continue

        parent = parents.get(key) or {}
        team = teams.setdefault(key, {
            "key": key,
            "name": rename.get(key) or parent.get("name") or p.get("name") or key,
            "url": parent.get("url") or p.get("url"),
            "twitter": parent.get("twitter") or p.get("twitter"),
            "logo": parent.get("logo") or p.get("logo"),
            "category": p.get("category"),
            "protocols": [],
            "tvl": 0.0,
            "chain_tvl": {},
            "chains": set(),
            "_max_child_tvl": -1.0,
        })

        tvl = float(p.get("tvl") or 0)

        # A handful of protocols are explicitly excluded from their parent's total.
        counted = not (p.get("excludeTvlFromParent") and key != slug)

        team["protocols"].append({
            "slug": slug,
            "name": p.get("name"),
            "tvl": tvl,
            "counted": counted,
        })

        # Chains the protocol lists count as presence even with no TVL reported.
        for raw_chain in (p.get("chains") or []):
            team["chains"].add(chains.normalise(raw_chain))

        # Category follows the largest child protocol.
        if tvl > team["_max_child_tvl"]:
            team["_max_child_tvl"] = tvl
            team["category"] = p.get("category") or team["category"]

        if not counted:
            continue

        team["tvl"] += tvl

        # Collapse alias keys within THIS protocol first (max, since the same
        # chain is listed under both its internal key and its display label),
        # then add across protocols (sum, since those are genuinely additive).
        own: dict[str, float] = {}
        for raw_chain, value in (p.get("chainTvls") or {}).items():
            if chains.is_pseudo(raw_chain):
                continue
            try:
                val = float(value or 0)
            except (TypeError, ValueError):
                continue
            chain = chains.normalise(raw_chain)
            own[chain] = max(own.get(chain, 0.0), val)

        for chain, val in own.items():
            team["chain_tvl"][chain] = team["chain_tvl"].get(chain, 0.0) + val
            if val > 0:
                team["chains"].add(chain)

    for team in teams.values():
        team["chains"] = sorted(team["chains"], key=chains.sort_key)
        team.pop("_max_child_tvl", None)
    return teams


def chain_markets(chains: Chains) -> dict:
    """Chain-level USDC/USDT supply, so a team's balance can be read against the
    size of that chain's stablecoin market."""
    out: dict[str, dict] = {}
    try:
        data = get_json(f"{STABLES_API}/stablecoins?includePrices=false", timeout=90)
    except RuntimeError as exc:
        print(f"  ! stablecoin market data unavailable: {exc}", file=sys.stderr)
        return out
    for asset in data.get("peggedAssets") or []:
        symbol = (asset.get("symbol") or "").upper()
        if symbol not in ("USDT", "USDC"):
            continue
        for raw_chain, payload in (asset.get("chainCirculating") or {}).items():
            chain = chains.normalise(raw_chain)
            try:
                val = float(((payload or {}).get("current") or {}).get("peggedUSD") or 0)
            except (TypeError, ValueError):
                continue
            if val > 0:
                slot = out.setdefault(chain, {})
                slot[symbol] = slot.get(symbol, 0.0) + val
    return out


def merge_token_data(teams: dict, token_data: dict, chains: Chains) -> None:
    """Roll per-protocol token breakdowns up to the team.

    Pseudo-chain keys are re-filtered here as well as at extraction time, so a
    cache written by an older version cannot reintroduce a double count.
    """
    for key, team in teams.items():
        per_protocol = token_data.get(key) or {}
        merged: dict[str, dict] = {}
        statuses = []
        for data in per_protocol.values():
            statuses.append(data.get("status", "error"))
            for chain, entry in (data.get("chains") or {}).items():
                if chains.is_pseudo(chain):
                    continue
                slot = merged.setdefault(chain, {"now": {}, "d7": {}, "d30": {}, "tvl": 0.0})
                for window in ("now", "d7", "d30"):
                    for sym, val in (entry.get(window) or {}).items():
                        slot[window][sym] = slot[window].get(sym, 0.0) + val
                slot["tvl"] += entry.get("tvl") or 0.0
        team["chain_tokens"] = merged
        if not statuses:
            team["token_status"] = "not_fetched"
        elif "ok" in statuses:
            team["token_status"] = "ok" if all(s == "ok" for s in statuses) else "partial"
        else:
            team["token_status"] = statuses[0]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Collect per-team stablecoin balances from DefiLlama")
    ap.add_argument("--min-tvl", type=float, default=250_000,
                    help="lower bound of the per-token fetch scope (default 250k)")
    ap.add_argument("--max-tvl", type=float, default=50_000_000,
                    help="upper bound of the per-token fetch scope (default 50M)")
    ap.add_argument("--limit", type=int, default=0,
                    help="only fetch N protocol documents (smoke test)")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--max-mb", type=float, default=25.0,
                    help="skip documents larger than this (falls back to USD-only)")
    ap.add_argument("--no-cache", action="store_true",
                    help="ignore the If-Modified-Since cache")
    ap.add_argument("--skip-tokens", action="store_true",
                    help="Tier 1 only, no per-token fetch")
    args = ap.parse_args()

    cfg = load_all()
    started = time.time()

    print("Tier 1: fetching protocol list ...")
    protocols_raw = get_json(f"{API}/protocols")
    print(f"  {len(protocols_raw)} protocols")

    print("Fetching parent registry and chain labels ...")
    try:
        conf = get_json(f"{API}/config", timeout=180)
    except RuntimeError as exc:
        print(f"  ! /config unavailable ({exc}); using hardcoded chain aliases")
        conf = {}
    key_to_label = conf.get("chainKeyToLabelMap") or {}
    parents = {}
    for parent in conf.get("parentProtocols") or []:
        pid = (parent.get("id") or "").replace("parent#", "")
        if pid:
            parents[pid] = parent
    print(f"  {len(parents)} parent protocols, {len(key_to_label)} chain labels")

    chains = Chains(cfg["chains_cfg"], key_to_label)

    live = [p for p in protocols_raw if is_live(p)]
    teams = build_teams(live, parents, chains, cfg["teams"])
    print(f"  {len(live)} live protocols -> {len(teams)} teams")

    print("Fetching chain-level stablecoin market sizes ...")
    markets = chain_markets(chains)
    print(f"  {len(markets)} chains")

    # Scope by TEAM tvl, so a team is either fully covered or not covered at all -
    # never half its chains.
    in_scope = {k: t for k, t in teams.items()
                if args.min_tvl <= t["tvl"] <= args.max_tvl}
    slugs = [(k, p["slug"]) for k, t in in_scope.items()
             for p in t["protocols"] if p["counted"]]
    if args.limit:
        slugs = slugs[:args.limit]

    token_data: dict[str, dict] = {}
    if not args.skip_tokens and slugs:
        print(f"Tier 2: token breakdown for {len(in_scope)} teams "
              f"({len(slugs)} protocols), {args.workers} workers ...")
        max_bytes = int(args.max_mb * 10 ** 6)
        done = 0
        counters: dict[str, int] = {}
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(fetch_protocol, slug, chains, max_bytes,
                                   not args.no_cache): (key, slug)
                       for key, slug in slugs}
            for fut in as_completed(futures):
                key, slug = futures[fut]
                try:
                    data = fut.result()
                except Exception as exc:  # noqa: BLE001
                    data = {"status": "error", "chains": {}, "error": str(exc)[:120]}
                token_data.setdefault(key, {})[slug] = data
                status = data.get("status", "error")
                counters[status] = counters.get(status, 0) + 1
                done += 1
                if done % 100 == 0 or done == len(slugs):
                    detail = " ".join(f"{k}={v}" for k, v in sorted(counters.items()))
                    print(f"  {done}/{len(slugs)}  {detail}", flush=True)

    merge_token_data(teams, token_data, chains)

    snapshot = {
        "generated_at": utcnow_iso(),
        "scope": {"min_tvl": args.min_tvl, "max_tvl": args.max_tvl,
                  "teams_total": len(teams), "teams_in_scope": len(in_scope)},
        "target_chain": chains.target,
        "priority_chains": chains.priority,
        "chain_markets": markets,
        "teams": list(teams.values()),
    }
    write_json(DATA_DIR / "snapshot.json", snapshot)

    # Daily history, for trend beyond the 30 days the protocol documents give us.
    day = snapshot["generated_at"][:10]
    write_json(HISTORY_DIR / f"{day}.json", {
        "generated_at": snapshot["generated_at"],
        "teams": {t["key"]: {"tvl": t["tvl"],
                             "chains": {c: v for c, v in t["chain_tvl"].items() if v}}
                  for t in teams.values()},
    })

    size_mb = (DATA_DIR / "snapshot.json").stat().st_size / 1e6
    print(f"\nWrote data/snapshot.json ({size_mb:.1f} MB) in {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
