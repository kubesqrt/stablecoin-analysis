# Stablecoin team dashboard

**Live: https://kubesqrt.github.io/stablecoin-analysis/**

Which teams hold USDC, USDT and USDe, on which chains, and are they already on Arbitrum?

Built for BD prospecting: find teams sitting on meaningful stablecoin balances that
Arbitrum hasn't captured yet. The default view is preset to teams in the **$500k–$10M**
band with **no Arbitrum signal**, sorted by stablecoin holdings.

## Quick start

```powershell
.\refresh.ps1
```

That collects the data, builds `docs/index.html`, runs the sanity checks, and opens
the dashboard. First run takes a few minutes; later runs are much faster because the
API honours `If-Modified-Since` and returns empty 304s for anything unchanged.

Nothing to install — `requests`, `PyYAML`, `Jinja2` and `eth_abi` are already present.

## What a row means

One row is a **team**, not a contract and not a protocol. Aave V2 and Aave V3 collapse
into a single `Aave` row.

The bundling comes from DefiLlama's `parentProtocol` mapping. Its adapters read each
team's pool and vault contracts directly — the contract-level work is already done, and
the parent mapping is the link that ties those contracts back to one team. Those
addresses aren't exposed through any API (they're unstructured JavaScript across ~1,500
adapter directories), which is why we consume the aggregation rather than rebuild it.
Where that isn't good enough, `onchain.py` reads contract balances directly.

## The Arbitrum marker

Four states, because DefiLlama alone isn't the whole picture:

| State | Meaning |
|---|---|
| **Deployed** | Real TVL on Arbitrum (amount shown) |
| **Listed only** | Contracts exist but hold almost nothing — several teams report ~$0.001 |
| **Mentioned** | Their own site or docs reference Arbitrum (`arbitrum_signal.py`) |
| **No signal** | Nothing found — the prospect list |

The dust threshold is `min_tvl_for_deployed` in `config/chains.yml` (default $1,000).
Without it, a team with a fraction of a cent on Arbitrum reads as "already deployed" and
drops off your prospect list.

To record something you know that the data doesn't — an announcement, a conversation —
edit `config/arbitrum_overrides.yml`. Manual entries always win.

### How much to trust "Mentioned"

`arbitrum_signal.py` fetches each team's own site and docs looking for the chain name.
Measured on this dataset:

- **Sensitivity ~40%** — of 10 teams DefiLlama confirms are on Arbitrum, 4 were detectable
  from their own site. Most crypto sites are JavaScript-rendered, so the HTML we can read
  says very little. It reads embedded page data (`__NEXT_DATA__`) as well as visible text,
  which is what catches the ones it does.
- **No false positives** in a 25-team prospect sample — all correctly came back clean.

So treat it as *additive only*: a "Mentioned" flag is real evidence, but the absence of one
proves nothing. It will never demote a team, and the matched phrase is stored on the row so
you can judge it yourself.

## What counts as a headline asset

Three assets are tracked as headline figures: **USDC**, **USDT** and **USDe**.

USDe earns its place on evidence, not reputation: it is 5th by circulating supply ($4.0B)
but **1st among non-USDC/USDT holdings across the teams tracked here ($7.8B)** — nearly 2×
DAI — because it is DeFi-native and concentrates in exactly the contracts this dashboard
follows. `sUSDe` rolls up into it as the same underlying claim.

Adapters report whatever symbol they like: Arbitrum USDT appears as `USDT0`, and lending
protocols report receipt tokens (`aArbUSDC`) rather than the underlying. A sample of 60
protocols produced **737 distinct symbols**, so `config/tokens.yml` sorts them by rule:

- **Core** — `USDC`, `USDT`, `USDT0`, `USDC.e`, `USDe`, `sUSDe`… The headline columns.
- **Wrapped** — receipt tokens representing exposure to a core asset. Off by default, toggle in the UI.
- **Other stables** — `DAI`, `USDS`, `USD1`, `PYUSD`… shown in their own column, never mixed in.

**Adding a fourth asset is a config change, not a code change.** Declare it under `core:` in
`config/tokens.yml` and it flows through to the table columns, composition bars, legend,
per-chain detail and CSV automatically.

Teams whose adapter publishes no token split are labelled **"no token data"** rather than
being shown as zero — that distinction matters when you're reading the list.

## What are we missing?

```powershell
python coverage.py                      # full audit
python coverage.py --chain Arbitrum     # one chain
```

Two different gaps, measured separately rather than guessed at.

**1. Teams we know about but never fetched.** This was by far the biggest gap, and it
was purely a scoping choice. With the fetch limited to $250k–$50M, **3,042 teams holding
$485B had no token breakdown at all** — Binance CEX, Aave, Morpho, Lido among them.

Full scope is now the default, so this gap is closed:

| | before | after |
|---|---|---|
| Teams with a token split | 1,108 | **3,240** |
| TVL with no breakdown | $485B (98%) | **$2.47B (0.5%)** |
| USDC+USDT attributed to teams | $1.50B | **$112B** |

Binance CEX alone accounts for $48.8B of that. Incremental refreshes stay cheap because
unchanged protocols return an empty 304.

What remains is genuinely unavailable rather than unfetched: 784 teams whose adapters
publish no token split (`skipTokenBreakdownData`), worth $2.47B. Those are labelled
"no token data" in the UI, never shown as zero.

**2. Contracts we can't tie to any team.** DefiLlama only sees protocols it has an
adapter for, so `coverage.py` walks the largest on-chain holders of USDC/USDT (free
Blockscout API, no key, 9 chains) and sorts every holder into:

| Bucket | Meaning |
|---|---|
| `attributed` | In your `config/addresses.yml` |
| `matched_by_label` | Reconciled automatically from the contract's on-chain name |
| `wallet` | EOA or smart-contract wallet — a person, not a team |
| `infrastructure` | Exchange, bridge or issuer contract |
| `unattributed_contract` | **The real blind spot** |

Contracts whose own name is uninformative (a bare `ERC1967Proxy`) get a second lookup
against their *implementation* contract, which usually names the owner —
`ATokenInstance Aave v3 USDC`. On Arbitrum that pass cut the unattributed figure from
$447M to $143M, correctly reconciling GMX ($52M), Aave ($50M) and Ostium ($17M).

Matching is **whole-word only**. Substring matching produced a real false positive: the
team "Initia" matched `InitializableImmutableAdminUpgradeabilityProxy` and claimed $38.5M
of Aave's money. `verify.py` now regression-tests this — a wrong attribution is worse
than an honest blank.

Anything left in `unattributed_contract` (written to `data/coverage.json`) is a work
queue: identify the owner, add it to `config/addresses.yml`, and `onchain.py` will fold
in the measured balance.

### How much of the market this now accounts for

Against circulating supply on the tracked chains, **$119.6B of $249.5B — 47.9% — is now
attributed to a named team**, up from 0.6% before the full-scope fetch.

| Chain | Attributed | Circulating | Covered |
|---|---|---|---|
| Ethereum | $93.3B | $120.4B | 77.5% |
| Hyperliquid | $5.9B | $6.1B | 95.6% |
| Base | $4.0B | $4.2B | 94.6% |
| Solana | $4.1B | $9.6B | 42.2% |
| Arbitrum | $1.4B | $3.1B | 46.3% |
| BSC | $2.1B | $10.8B | 19.9% |
| Tron | $6.3B | $90.5B | 6.9% |

Tron is the outlier and it is not a defect: its supply is overwhelmingly held in exchange
and retail wallets rather than DeFi contracts, so there is little there to attribute. The
same logic caps every chain — **100% is not the target**, because a large share of every
stablecoin's supply legitimately sits with people and exchanges, not protocols.

Read coverage *within the holder set* (what `coverage.py` reports), not against total supply.

## Files

| Path | Purpose |
|---|---|
| `fetch_llama.py` | Collect from DefiLlama → `data/snapshot.json` |
| `build.py` | Classify tokens, apply the Arbitrum marker → `docs/` |
| `verify.py` | 14 sanity checks (double-counting, bundling, bounds, label matching) |
| `coverage.py` | Audit what we are missing, and why |
| `arbitrum_signal.py` | Optional: check team websites for Arbitrum references |
| `onchain.py` | Optional: exact balances via Multicall3 |
| `config/` | Token rules, chain aliases, team merges, address book, overrides |
| `docs/` | The dashboard — exactly what GitHub Pages serves |
| `.github/workflows/` | Daily rebuild and publish |

## Exact on-chain balances

For anything DefiLlama covers poorly — a new chain, a treasury wallet, a figure you need
measured rather than estimated — put the addresses in `config/addresses.yml` and run:

```powershell
python onchain.py --probe
python onchain.py
```

`--probe` verifies every token contract and reads its `decimals()` on-chain. Decimals are
never assumed: **BSC USDC and USDT are 18 decimals**, and treating them as 6 would
overstate a balance by a factor of a trillion.

All 13 configured chains are verified working, including **Monad, Hyperliquid and Plasma**.
Multicall3 sits at the same address on every one of them, so a few hundred
(token, holder) pairs cost one request, pinned to a single block. Balances read this way
are tagged `rpc` in the dashboard so measured is visibly distinct from estimated.

Validation: the USDC held by Aave's Arbitrum aToken read on-chain came to $38,376,297
against DefiLlama's $38,836,138 — a 1.2% difference, explained by snapshot timing.

Plasma USDC is deliberately absent: no primary source confirms an address, so it reads as
unavailable rather than guessed. Add it to `config/rpc.yml` once verified.

## Updating itself

`.github/workflows/refresh.yml` rebuilds the site every day at **06:15 UTC** and commits
`docs/`, which Pages serves. It caches `data/.cache` between runs, so unchanged protocols
return empty 304s instead of re-downloading ~5,200 documents. `verify.py` runs *before* the
commit step, so a failed sanity check blocks publication rather than shipping bad data.
It can also be triggered by hand from the Actions tab.

**One-time setup.** Pushing a workflow file needs a scope the CLI token doesn't carry by
default, so the file is committed locally but not yet on GitHub. To enable it:

```powershell
gh auth refresh -s workflow
```
```powershell
git add .github; git commit -m "Add daily refresh workflow"; git push
```

### Local schedule instead

Does the same thing without involving GitHub:

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PWD\refresh.ps1`" -NoOpen"
$trigger = New-ScheduledTaskTrigger -Daily -At 7am
Register-ScheduledTask -TaskName "Stablecoin dashboard" -Action $action -Trigger $trigger
```

## Publishing updates

Pages is already serving `docs/` from `main`. To publish a refresh:

```powershell
.\refresh.ps1 -NoOpen
git add docs data/history
git commit -m "Refresh data"
git push
```

The site rebuilds within about a minute.

To automate it with a scheduled GitHub Action, add `.github/workflows/refresh.yml`
running `fetch_llama.py` then `build.py` and committing `docs/`. Note the `gh` token in
use here only has `gist`, `read:org` and `repo` scopes — pushing a workflow file needs
the `workflow` scope, so either run `gh auth refresh -s workflow` or add the file through
the GitHub web UI.

`data/.cache/` is gitignored — it's regenerated on demand.

## Caveats worth knowing

- Figures are DefiLlama's latest published snapshot, typically hours old, not real time.
- Per-token detail is only fetched for teams between **$250k and $50M** TVL
  (`--min-tvl` / `--max-tvl`). Larger teams appear with USD TVL but no token split.
- Ten protocols publish documents too large to parse under the 25MB cap; raise it with
  `--max-mb` if you need them.
- "No signal" means nothing was found, not that the team is definitely absent from
  Arbitrum. Treat it as a lead, not a fact.
