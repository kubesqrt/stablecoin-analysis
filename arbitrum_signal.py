"""Tier 4: detect teams that have indicated the target chain outside DefiLlama.

DefiLlama only knows a team is on Arbitrum once its adapter reports TVL there.
A team that has announced a deployment, shipped contracts, or lists the chain in
its own docs is not a cold prospect - but DefiLlama shows nothing. This checks
each team's own website for a reference to the target chain.

It is OFF by default and not part of the normal refresh: it makes one request per
team to a third-party site. Run it deliberately:

    python arbitrum_signal.py --limit 50        # try it on 50 teams first
    python arbitrum_signal.py                   # all prospects

Results land in data/arbitrum_signals.json and are picked up by build.py. Nothing
here overrides a manual entry in config/arbitrum_overrides.yml.
"""
from __future__ import annotations

import argparse
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests

from common import DATA_DIR, load_all, read_json, utcnow_iso, write_json

UA = {"User-Agent": "Mozilla/5.0 (compatible; stablecoin-dashboard/1.0; BD research)"}
_local = threading.local()

# Paths worth checking beyond the landing page - chain support is usually
# documented rather than advertised on the homepage.
SUBPATHS = ["", "/docs", "/documentation", "/networks", "/chains", "/supported-networks"]


def session() -> requests.Session:
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update(UA)
        _local.s = s
    return s


def _context(haystack: str, target: str, window: int = 70) -> str | None:
    match = re.search(re.escape(target), haystack, re.I)
    if not match:
        return None
    start = max(0, match.start() - window)
    end = min(len(haystack), match.end() + window)
    return haystack[start:end].strip()


def looks_positive(text: str, target: str) -> tuple[bool, str]:
    """Find a reference to the target chain and return the surrounding phrase.

    Two passes, because most crypto sites are JavaScript-rendered and the visible
    HTML is nearly empty:

      1. Visible text - a real, human-readable mention. Strong evidence.
      2. Raw source including <script> blocks - Next.js and similar frameworks
         embed their chain list in __NEXT_DATA__ JSON, which pass 1 strips out.
         Weaker evidence, so it is labelled as such.

    Either way the matched phrase is stored for you to judge, rather than being
    presented as fact.
    """
    if not text:
        return False, ""

    plain = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.S | re.I)
    plain = re.sub(r"<style[^>]*>.*?</style>", " ", plain, flags=re.S | re.I)
    plain = re.sub(r"<[^>]+>", " ", plain)
    plain = re.sub(r"\s+", " ", plain)

    hit = _context(plain, target)
    if hit:
        return True, hit

    raw = re.sub(r"\s+", " ", text)
    hit = _context(raw, target, window=50)
    if hit:
        return True, "[in page data] " + hit
    return False, ""


def candidates(url: str) -> list:
    """Pages worth trying, cheapest and most likely first.

    Chain support is usually documented rather than advertised, and docs sites
    are typically statically generated - so they say more in raw HTML than the
    JavaScript-rendered landing page does.
    """
    base = url if url.endswith("/") else url + "/"
    out = [url] + [urljoin(base, p.lstrip("/")) for p in SUBPATHS if p]

    parsed = urlparse(url)
    host = parsed.netloc
    if host and not host.startswith("docs."):
        bare = host[4:] if host.startswith("www.") else host
        out.append(f"{parsed.scheme}://docs.{bare}")
    return out


def check_team(team: dict, target: str, timeout: int) -> dict | None:
    url = team.get("u") or team.get("url")
    if not url:
        return None
    if not urlparse(url).scheme:
        url = "https://" + url

    for candidate in candidates(url):
        try:
            r = session().get(candidate, timeout=timeout, allow_redirects=True)
        except Exception:  # noqa: BLE001 - third-party sites fail in every way
            continue
        if r.status_code != 200 or "html" not in (r.headers.get("content-type") or ""):
            continue
        hit, context = looks_positive(r.text[:400_000], target)
        if hit:
            return {
                "state": "mentioned",
                "note": f"{target} referenced on their site: “{context[:180]}”",
                "source": r.url,
                "checked_at": utcnow_iso(),
            }
        time.sleep(0.2)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Check team websites for references to the target chain")
    ap.add_argument("--limit", type=int, default=0, help="only check N teams")
    ap.add_argument("--workers", type=int, default=6,
                    help="keep this modest - these are other people's servers")
    ap.add_argument("--timeout", type=int, default=12)
    ap.add_argument("--min-tvl", type=float, default=500_000)
    ap.add_argument("--max-tvl", type=float, default=10_000_000)
    ap.add_argument("--recheck", action="store_true",
                    help="re-check teams already recorded")
    args = ap.parse_args()

    payload = read_json(DATA_DIR.parent / "docs" / "data.json")
    if not payload:
        print("docs/data.json missing - run build.py first", file=sys.stderr)
        return 1

    target = payload.get("target_chain", "Arbitrum")
    existing = read_json(DATA_DIR / "arbitrum_signals.json", {}) or {}

    # Only worth checking teams DefiLlama shows no signal for.
    todo = [r for r in payload["rows"]
            if r["arb"] == "none"
            and args.min_tvl <= r["t"] <= args.max_tvl
            and r.get("u")
            and (args.recheck or r["k"] not in existing)]
    if args.limit:
        todo = todo[:args.limit]

    if not todo:
        print("Nothing to check.")
        return 0

    print(f"Checking {len(todo)} team websites for '{target}' references "
          f"({args.workers} workers). This hits third-party sites - be patient.")

    found = 0
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(check_team, r, target, args.timeout): r for r in todo}
        for fut in as_completed(futures):
            row = futures[fut]
            done += 1
            try:
                result = fut.result()
            except Exception:  # noqa: BLE001
                result = None
            if result:
                existing[row["k"]] = result
                found += 1
                print(f"  + {row['n']}: {result['source']}")
            if done % 25 == 0:
                print(f"  {done}/{len(todo)} checked, {found} references found", flush=True)

    write_json(DATA_DIR / "arbitrum_signals.json", existing)
    print(f"\n{found} of {len(todo)} teams reference {target} on their own site.")
    print("Recorded in data/arbitrum_signals.json - re-run build.py to apply.")
    print("These are weak signals: the matched phrase is stored so you can judge each one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
