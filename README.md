# Crypto Screener

A local, read-only screening dashboard for leveraged crypto markets. It scans a
watchlist on a schedule and tells you, per coin, whether it is worth trading right
now — and when it isn't, why not.

Two venues are supported, switchable from the header:

| | Toobit (default) | Nobitex |
|---|---|---|
| Instrument | USDT perpetuals | معاملات تعهدی (pool-backed margin) |
| Coverage of the shipped list | 47 of 50 | 36 of 50 |
| Credentials | **none needed** — all public | READ-only key required |
| Funding rate | published, resolved automatically | not available |
| Scan duration | ~5 min for 47 coins | ~7–8 min for 36 |

**It places no orders.** Not now, and not by accident: see [Read-only](#read-only).

All analysis is delegated to the
[`crypto-leverage-trade-plan`](https://github.com/) skill. This repository contains no
trading maths — no ATR, no position sizing, no scoring. It runs the skill's scripts as
subprocesses and stores their JSON verbatim, so every number on screen traces back to
one tested implementation.

---

## Setup

Requires Python 3.10+ and nothing else. There are no third-party Python packages.

```bash
./run.sh setup     # venv, .env, credential check, symbol discovery, Ollama check
./run.sh start     # http://127.0.0.1:8787
```

`setup` is safe to re-run; it will not overwrite an existing `.env`.

### Nobitex API key

Create the key at **nobitex.ir → Settings → API keys**, then put it in `.env`:

```
NOBITEX_API_KEY=<the public key>
NOBITEX_API_SECRET=<the privateKey, shown exactly once at creation>
```

Two things to get right:

- **READ permission only.** Everything here lives under READ. A key that cannot trade
  cannot be misused to trade, whatever happens downstream. It costs you nothing.
- **Add an IP whitelist** restricted to this machine's public address.

`.env` is gitignored and `chmod 600`. Its values are read by the backend only and are
never serialised into an API response, a log line, or anything the browser receives.
If a secret is ever exposed, delete the key in the Nobitex panel and issue a new one.

Public market data needs no credentials. Without them you lose margin fee rates and
account state; the rest still works.

### Commands

| Command | What it does |
|---|---|
| `./run.sh setup` | venv, `.env` from example, credential check, symbol discovery, Ollama check |
| `./run.sh start` | Start the dashboard. Idempotent — starting twice will not spawn two servers, and a stale PID file is cleaned up rather than treated as fatal |
| `./run.sh stop` | Graceful shutdown, SIGKILL only as a fallback |
| `./run.sh restart` | |
| `./run.sh status` | Running?, PID, coin counts, last scan, Ollama state, credentials present |
| `./run.sh logs [-f]` | |
| `./run.sh scan-once [COIN…]` | One scan in the foreground, for debugging |

The scan scheduler runs as a thread inside the server process rather than as a second
daemon: one process to supervise, and no way for two of them to disagree about the
database. `scan-once` still runs standalone.

---

## What the verdicts mean

The skill answers two questions in order: *is this instrument worth risking anything
on?*, and only then *how?*. The verdict answers the first.

| Verdict | Meaning | What to do |
|---|---|---|
| **TAKE** | Score ≥ 70 and every gate passed | Execute the plan as written — after resolving the manual checks |
| **WATCH** | Score 50–69, gates passed | Write down the *one* condition that would upgrade it, and wait for that specific thing |
| **INCOMPLETE** | Under 80% factor coverage | Not enough live data to judge. Usually means a snapshot failed |
| **SKIP** | Any gate failed, or score < 50 | No position |

**SKIP is a complete answer, not a failure.** Most of a screening system's value is the
trades it prevents. Days where all 36 coins come back SKIP are normal — that is what
filtering looks like. The UI renders SKIP in neutral grey rather than red for exactly
this reason: 33 red cards would read as 33 alarms when they are 33 correct decisions.

A gate failure is decisive regardless of score. A coin can score 87 and still be SKIP
because the spread gate failed — no entry price rescues an illiquid book.

The score is a **heuristic ranking device**, not a fitted model and not a probability of
profit. A 78 does not mean a 78% chance of winning. Use it to compare candidates.

### Provisional TAKEs

Two direction checks — BTC/dominance alignment and funding-rate positioning — cannot be
derived from OHLCV. The skill returns them as `null` with a `MANUAL` marker rather
than quietly passing them.

**On Toobit both are settled from live data**: funding comes from the public funding
endpoint, and BTC alignment from `BTC-SWAP-USDT` candles using the skill's own EMA
logic. Each shows the figure it was resolved from, and the label says plainly that
dominance is *not* covered — only the BTC leg. On Nobitex neither can be settled and
both stay manual.

A TAKE with unresolved manual checks is labelled **Provisional** and carries a banner
saying it is not confirmed. Tick the checks yourself once you have looked them up.
Ticks are timestamped, and a tick made *before* the latest scan is shown as stale — so
yesterday's confirmation cannot silently prop up today's TAKE.

---

## Read-only

The guarantee is structural, not a promise in a comment.

1. The skill's bundled client refuses any path that could place, modify, close or cancel
   an order, or move funds — via a path allowlist plus a forbidden-substring list.
2. `agent/guard.py` repeats that check at this application's own layer, so the property
   does not depend on one file in someone else's directory staying as it is.
3. `guard.self_test()` runs at server startup against 15 known-bad and 8 known-good
   paths. **If the guard ever stops rejecting a write path, the server refuses to
   start.** Run it yourself with `.venv/bin/python -m agent.guard`.
4. The browser can never ask the server to call an arbitrary exchange path. There is no
   proxy endpoint. The only outbound calls are made by the scanner, through the skill.

There is no trade button anywhere, not even a disabled one.

---

## How it works

```
agent/
  config.py     paths, settings, .env loading, credential presence (never values)
  skill.py      the only module that talks to the analysis engine, via subprocess
  discover.py   resolves the 50 requested coins to real Nobitex symbols
  scanner.py    the scan loop
  store.py      SQLite: scans, results, chart series, manual checks, commentary
  server.py     stdlib HTTP on 127.0.0.1 — JSON API + static UI
  guard.py      read-only allowlist, mirrored from the skill, self-tested at boot
  llm.py        optional Ollama commentary
web/            vanilla JS, no build step, all assets vendored
config/
  watchlist.json   generated by discovery — what each coin actually resolved to
  settings.json    profile, capital, risk %, interval, language, model choice
```

Per coin, per scan:

```bash
nobitex_api.py snapshot --symbol <SYM> --profile <profile> --out <tmp> --save-csv <tmp>
trade_plan.py  plan --snapshot <tmp> --side <derived> --capital <cap> --json
```

`--side` comes from whichever direction the snapshot's own `direction_score` favours.
`--save-csv` captures the raw OHLCV for the chart at no extra API cost — the snapshot
JSON stores only scalar indicators.

A full 36-coin pass takes **7–8 minutes** (measured: 423s, 460s). The skill serialises
requests with a ~1.1s floor and each coin costs roughly five calls. That gap is
re-enforced *across* process boundaries here, because the skill's own limiter lives
inside a single process and we spawn one per coin. Scans run in a background thread;
page loads never wait on them.

Steady-state resource use, measured over a 30-minute watch: ~27 MB RSS, 42 open file
descriptors, both flat across scans. If you see either climbing, that is a bug — one
such leak is described in the commit history.

### Choosing which coins to screen

`config/coins.txt` is the list — one ticker per line, `#` for comments, blanks
ignored. Write the plain ticker (`BTC`, not `BTCUSDT`); discovery works out the real
market for each. Edit the file and re-run `./run.sh setup` to pick up the change.

There is no coin list in the code, so the file cannot drift out of sync with what the
scanner actually screens.

### Symbol discovery

Do not assume `<COIN>USDT`. Each venue is enumerated live and everything that fails to
resolve is shown with its reason rather than dropped.

**Toobit** — 47 of the 50 resolve to perpetual contracts. `GRAM` and `MNT` have no
contract; `PUMP` exists only as `PUMPBTC` and `PUMP2`, neither unambiguously the same
asset, so it is reported rather than guessed at.

**Nobitex** — 36 are margin-tradeable, 7 are spot-only (TAO, PUMP, MORPHO, ALGO, JUP,
INJ, PENGU) and 7 are not listed (CRO, WLFI, MNT, ICP, KAS, VET, TIA).

### Contracts are not coins

Toobit trades contracts, and one contract is rarely one coin: `BTC-SWAP-USDT` is
0.001 BTC, `CRO-SWAP-USDT` is 10 CRO, and `1000SHIB` carries its scale in the name.
The skill sizes positions in coins, so every card shows **both** the coin quantity and
the contract count. Entering the coin figure on a Toobit ticket would be wrong by up
to three orders of magnitude.

Order-book depth is quoted in contracts too, and is multiplied out before the
liquidity gate sees it — without that, BTC's book reads 1000x thinner than it is.

### Maintenance margin

The planner's `generic-perp` profile assumes a 0.5% maintenance margin, and the skill
exposes no flag to override it. Toobit publishes the real per-tier figure: 0.25% for
BTC and ETH, but 0.67% for ADA and 2.5% for CRO.

Where the real figure is **higher** than the assumption, the profile would place
liquidation further away than it actually is — the one direction that cannot be left
alone. Two things happen: the card shows both numbers with a warning, and if the
planner's chosen leverage would not survive the real figure, the plan is re-run with a
corrected leverage cap. Sizing still happens entirely inside `trade_plan.py`.

Both groups are listed in a collapsed section at the bottom of the dashboard with the
reason, rather than silently dropped.

Two corrections the naive mapping gets wrong, both handled:

- Nobitex quotes low-value coins in **scaled lots**. SHIB trades as `1K_SHIB` and PEPE
  as `1M_PEPE`; mapping them to `SHIBUSDT` reports them as unlisted when they trade
  fine. Prices on those markets are per-lot, which the card labels.
- `GET /margin/v2/delegation-limit` rejects every parameter spelling tried
  (`InvalidSymbol`, symbol `""`), so it cannot enumerate margin availability.
  `GET /margin/fee-rates` returns the same universe in one allowlisted call and is used
  instead.

Re-run discovery with `./run.sh setup` when Nobitex lists something new.

### Capital and currency

Position size is quantity × price, so capital must be denominated in the currency the
market quotes in. All 36 scannable coins resolved to USDT markets, so this is currently
a no-op — but if a coin ever falls back to an IRT market, capital is converted at the
live USDT/IRT rate recorded with that scan. A missing rate is an error on that coin, never
an assumed conversion.

The default capital is **1,000 USDT**. At 10,000 even BTC fails the liquidity-depth gate
against Nobitex's pool depth, which would report your position size as a market defect.

---

## Bilingual EN / FA

One toggle in the header, persisted to `localStorage` and `config/settings.json`.
Persian sets `dir="rtl"` on `<html>`; one stylesheet serves both directions because every
directional property is logical (`margin-inline-start`, never `margin-left`).

**Numbers never flip.** Every figure is rendered in Latin digits inside a `dir="ltr"`
span, and charts stay LTR so the time axis still runs oldest-to-newest. This is a safety
property, not a cosmetic one — bidi reordering turned a leading `0/6` into a trailing one
during development, which is exactly how `3,140` becomes something dangerous.

Strings live in `web/i18n/en.json` and `web/i18n/fa.json`. Trading terms follow the
glossary in the skill's `references/indicators.md`, so the vocabulary matches Nobitex's
own: حد ضرر، حد سود، نقطه ورود، اهرم، وجه تضمین، ارزش اسمی، نسبت ریوارد به ریسک، امید ریاضی.
The skill's own fixed English vocabulary (gate names, score factors, verdict actions) is
translated under a `skill.` key namespace; free-form detail strings that carry live
numbers fall through to English deliberately.

---

## Local commentary (optional)

Ollama is **never a hard dependency**. If it is absent, commentary is disabled with a
clear log line and everything else works — the analysis is deterministic and does not
need a model.

On startup `llm.py` detects hardware, lists installed models with parameter counts and
quantisation, decides whether any is suitable, and writes the choice *and the reasoning*
to `config/settings.json` so it is not re-derived every start. The reasoning is visible
in the Settings panel.

The model's role is strictly narrative: it receives the finished analysis and writes two
to four sentences. It never produces, adjusts, or second-guesses a price, level, score or
verdict. Two defences enforce that:

1. The prompt carries a curated fact sheet — mostly non-numeric — and instructs the model
   to write no numerals at all.
2. `validate_numbers()` re-reads the output and **discards the whole commentary** if it
   contains any number absent from the input. Persian and Arabic-Indic digits are folded
   to ASCII first, so a Farsi answer cannot smuggle a fabricated figure past the check.
   A discarded commentary says so; it is never silently swallowed.

**Persian commentary requires a model of ~7B or larger.** This is from measurement, not
theory: `qwen2.5:3b` produced Farsi that misdescribed the analysis — calling gates
"فروشگاه‌ها" (shops) and inventing phrases like "ترک بانک Bitcoin" — while its English was
fine. The number guard held throughout; the problem was prose that misrepresents the
verdict, which is its own kind of false confidence. Set `llm.allow_weak_persian` to
`true` in `config/settings.json` to override.

Commentary is generated **on demand**, per card, rather than for every coin in a scan.
On a machine without GPU acceleration a per-scan pass would take longer than the scan.

---

## Phase 2 — what execution would need

Nothing here is wired for it, deliberately. The seams left for it:

**Seams that already exist**

- `agent/skill.py` is the single choke point for engine calls. An execution client would
  be a sibling module, not an edit to this one.
- `store.py` already keys results by `(scan_id, coin)` and keeps plan JSON verbatim, so
  an `orders` table referencing a `result_id` would record exactly which analysis a fill
  came from.
- The plan JSON already contains everything an order needs: side, entry, stop, TP1, TP2,
  quantity, leverage, margin.
- `server.py` separates read routes from local POSTs, and settings changes already go
  through a validated allowlist.
- Manual-check state is already timestamped and staleness-aware — an execution path can
  refuse to act on a stale confirmation.

**What phase 2 must add, and must not skip**

1. **A second credential with TRADE permission**, kept separate from the READ key. Do not
   widen the existing key. The read path should keep using the read-only key so the
   guarantee above survives.
2. **A deliberate relaxation of `guard.py`** — a new, explicitly named write allowlist,
   not a loosening of `FORBIDDEN`. `self_test()` should then assert that the *read* client
   still rejects those paths.
3. **Idempotency keys** on every order submission, so a retry after a timeout cannot
   double-fill.
4. **A confirmation step that is not a button.** The provisional/manual-check machinery
   exists precisely so an unconfirmed TAKE cannot become an order; phase 2 should refuse
   to submit while any manual check is unresolved or stale.
5. **Reconciliation** — poll `/positions/list` and reconcile against what was submitted.
   An order that succeeded but whose response was lost is the normal failure mode.
6. **A kill switch** and the circuit breakers the skill already specifies (2 losses or
   −3% equity for scalp; 3 losses or −5% for intraday).
7. **Re-validating the plan against fresh data before submitting.** A plan computed six
   minutes ago at the start of a scan is stale by the time you click.

---

## Notes and caveats

- Charts render entirely from stored data. No network call, no CDN. Every JS, CSS and
  font asset is vendored into the repo.
- Candles are neutral — hollow for up, solid for down — rather than green/red. Direction
  is encoded by fill, which is a shape channel, so it survives colour blindness and
  greyscale. It also frees the colour channel for the three EMAs; with green candles the
  aqua EMA 200 read as price data. Say the word if you'd rather have conventional
  green/red candles back.
- Verdict badges pair colour with an icon and text, so no meaning rests on hue alone.
  The categorical palette is validated with the `dataviz` skill's checker in both light
  and dark modes.
- Timestamps and chart axes are Gregorian/UTC, matching the skill's output. There is no
  Jalali conversion.
- `config/watchlist.json` is gitignored — it is generated per machine and per moment.

**This is analysis tooling, not advice.** It is the mechanical output of a stated method.
It does not predict anything, and leveraged positions can lose the entire margin.
