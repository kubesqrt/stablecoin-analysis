# Stablecoin team dashboard

Which teams hold USDC and USDT, on which chains, and are they already on Arbitrum?

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

## What counts as USDC / USDT

Adapters report whatever symbol they like: Arbitrum USDT appears as `USDT0`, and lending
protocols report receipt tokens (`aArbUSDC`) rather than the underlying. A sample of 60
protocols produced **737 distinct symbols**, so `config/tokens.yml` sorts them by rule:

- **Core** — `USDC`, `USDT`, `USDT0`, `USDC.e`, `USDT.e`, `USDBC`, `axlUSDC`. The headline numbers.
- **Wrapped** — receipt tokens representing USDC/USDT exposure. Off by default, toggle in the UI.
- **Other stables** — `USDe`, `DAI`, `USD1`, `PYUSD`… shown in their own column, never mixed in.

Teams whose adapter publishes no token split are labelled **"no token data"** rather than
being shown as zero — that distinction matters when you're reading the list.

## Files

| Path | Purpose |
|---|---|
| `fetch_llama.py` | Collect from DefiLlama → `data/snapshot.json` |
| `build.py` | Classify tokens, apply the Arbitrum marker → `docs/` |
| `verify.py` | 13 sanity checks (double-counting, bundling, bounds) |
| `arbitrum_signal.py` | Optional: check team websites for Arbitrum references |
| `onchain.py` | Optional: exact balances via Multicall3 |
| `config/` | Token rules, chain aliases, team merges, address book, overrides |
| `docs/` | The dashboard — exactly what GitHub Pages would serve |

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

## Scheduled refresh

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PWD\refresh.ps1`" -NoOpen"
$trigger = New-ScheduledTaskTrigger -Daily -At 7am
Register-ScheduledTask -TaskName "Stablecoin dashboard" -Action $action -Trigger $trigger
```

## Publishing to GitHub Pages

`docs/` is already self-contained and needs no build step. Push the repo, enable Pages on
the `docs/` folder, and add a scheduled Action running `fetch_llama.py` then `build.py`
and committing `docs/`. `data/.cache/` is gitignored — it's regenerated on demand.

## Caveats worth knowing

- Figures are DefiLlama's latest published snapshot, typically hours old, not real time.
- Per-token detail is only fetched for teams between **$250k and $50M** TVL
  (`--min-tvl` / `--max-tvl`). Larger teams appear with USD TVL but no token split.
- Ten protocols publish documents too large to parse under the 25MB cap; raise it with
  `--max-mb` if you need them.
- "No signal" means nothing was found, not that the team is definitely absent from
  Arbitrum. Treat it as a lead, not a fact.
