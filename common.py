"""Shared helpers: config loading, chain normalisation, token classification."""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Token symbols and team names carry characters the Windows console's default
# cp1252 codepage cannot encode - Arbitrum's USDT reports its symbol as "USD₮0".
# Without this, printing a progress line raises UnicodeEncodeError mid-run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / ".cache"
HISTORY_DIR = DATA_DIR / "history"
DOCS_DIR = ROOT / "docs"

for _d in (DATA_DIR, CACHE_DIR, HISTORY_DIR, DOCS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def load_yaml(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf8")) or {}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# Chains
# --------------------------------------------------------------------------

class Chains:
    """Normalises DefiLlama's two chain-name namespaces into one."""

    def __init__(self, cfg: dict, key_to_label: dict | None = None):
        self.cfg = cfg or {}
        self.aliases = dict(self.cfg.get("aliases") or {})
        self.pseudo = {str(s).lower() for s in (self.cfg.get("pseudo_suffixes") or [])}
        # /config chainKeyToLabelMap maps internal keys ("bsc") to labels ("BSC").
        # Fold it in so we do not rely purely on hardcoded aliases.
        for key, label in (key_to_label or {}).items():
            pretty = str(key).strip()
            label = str(label).strip()
            if pretty and label and pretty.lower() != label.lower():
                self.aliases.setdefault(pretty.title(), label)
        self.target = self.cfg.get("target_chain", "Arbitrum")
        self.priority = list(self.cfg.get("priority") or [])

    def normalise(self, name: str) -> str:
        if not name:
            return name
        return self.aliases.get(name, name)

    def is_pseudo(self, key: str) -> bool:
        """Reject the non-chain buckets DefiLlama mixes into chainTvls.

        They come in two shapes and BOTH must go, or totals double-count:
          - per-chain suffixed: 'Arbitrum-borrowed', 'Ethereum-staking'
          - bare aggregates across every chain: 'borrowed', 'staking', 'pool2'

        The bare form is the dangerous one - it has no hyphen to give it away, and
        for a lending protocol 'borrowed' can dwarf real TVL (Wildcat: $173M
        borrowed against $9.3M of actual TVL).

        Real chain names containing a hyphen ('Avalanche C-Chain') are folded to
        their canonical form by the alias map before this is reached.
        """
        if not key:
            return True
        if key.lower() in self.pseudo:
            return True
        if "-" in key:
            return True
        return False

    def sort_key(self, name: str):
        try:
            return (0, self.priority.index(name))
        except ValueError:
            return (1, name)


# --------------------------------------------------------------------------
# Token classification
# --------------------------------------------------------------------------

class TokenClassifier:
    """Sorts adapter-reported token symbols into core / wrapped / other-stable.

    Adapters report whatever symbol they like: Arbitrum USDT shows up as 'USDT0',
    lending protocols report receipt tokens ('aArbUSDC') rather than the underlying.
    A 60-protocol sample of the target band produced 737 distinct symbols, so this
    is rule-based rather than an enumerated list.
    """

    def __init__(self, cfg: dict):
        cfg = cfg or {}
        # Assets are whatever config/tokens.yml declares under `core`, so adding a
        # fourth headline stablecoin is a config change, not a code change.
        # Keys keep their original casing in YAML ("USDe"), so index by that and
        # expose the uppercased name - looking the dict up by the uppercased key
        # silently yields an empty symbol set.
        core_cfg = cfg.get("core") or {}
        wrapped_cfg = cfg.get("wrapped") or {}
        self.assets = []
        self.core = {}
        self.wrapped = {}
        self.display = {}
        for key, symbols in core_cfg.items():
            asset = str(key).upper()
            self.assets.append(asset)
            self.display[asset] = str(key)
            self.core[asset] = {str(s).upper() for s in (symbols or [])}
            self.wrapped[asset] = [re.compile(p, re.I)
                                   for p in (wrapped_cfg.get(key) or [])]

        self.other = {s.upper() for s in (cfg.get("other_stables") or [])}
        self.ignore = [re.compile(p, re.I) for p in (cfg.get("ignore") or [])]

    def classify(self, symbol: str) -> tuple[str | None, str | None]:
        """Return (bucket, asset).

        bucket: 'core' | 'wrapped' | 'other' | None (not a stablecoin / ignored)
        asset:  one of self.assets, 'OTHER', or None
        """
        if not symbol:
            return None, None
        sym = symbol.strip().upper()

        for pat in self.ignore:
            if pat.search(sym):
                return None, None

        # Exact core matches win over every pattern.
        for asset in self.assets:
            if sym in self.core[asset]:
                return "core", asset

        # A distinct stablecoin is never a wrapped form of a core asset, even
        # though several (crvUSD, DAI) would match the loose patterns below.
        if sym in self.other:
            return "other", "OTHER"

        for asset in self.assets:
            for pat in self.wrapped[asset]:
                if pat.match(sym):
                    return "wrapped", asset

        # Anything else that still looks like a dollar stablecoin lands in 'other'
        # so a large holder is never silently dropped.
        if re.search(r"USD|DAI|EUR", sym):
            return "other", "OTHER"

        return None, None

    def split(self, tokens: dict) -> dict:
        """Aggregate {symbol: usd} into per-asset and wrapped totals."""
        out = {"other": 0.0}
        for asset in self.assets:
            out[asset.lower()] = 0.0
            out[f"{asset.lower()}_wrapped"] = 0.0

        for sym, usd in (tokens or {}).items():
            try:
                val = float(usd or 0)
            except (TypeError, ValueError):
                continue
            if val <= 0:
                continue
            bucket, asset = self.classify(sym)
            if bucket == "core":
                out[asset.lower()] += val
            elif bucket == "wrapped":
                out[f"{asset.lower()}_wrapped"] += val
            elif bucket == "other":
                out["other"] += val
        return out


def load_all():
    chains_cfg = load_yaml("chains.yml")
    return {
        "chains_cfg": chains_cfg,
        "tokens": TokenClassifier(load_yaml("tokens.yml")),
        "teams": load_yaml("teams.yml"),
        "arb_overrides": load_yaml("arbitrum_overrides.yml"),
        "addresses": load_yaml("addresses.yml"),
        "rpc": load_yaml("rpc.yml"),
    }
