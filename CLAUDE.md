# Crypto Agents — project instructions and state

Read this fully before doing anything else in this repo. It exists so a session never
has to re-derive context that already exists — keep it current as things change rather
than letting it drift into a stale summary.

## What this is

A crypto futures signal/trading system with two parts, both tracked in this repo:

1. **The skill** (`skill/` — mirrors `~/.claude/skills/crypto-leverage-trade-plan/` on
   the operator's Mac) — decides whether a coin is worth trading and builds a
   risk-first plan: direction, entry, ATR-based stop, TP1/TP2, position size, leverage.
   Keep both copies in sync when editing by hand (see Sync rule); the autonomous
   routine only ever edits the repo copy, which is the one that reaches the server.
2. **`agent/`** — a paper-trading ("demo") loop that takes the skill's TAKE signals and
   manages them as simulated Toobit positions: slots, portfolio heat, correlation/trend
   gates, circuit breaker, TP1 partials, trailing stops, time stops.

## Topology — three places, must not drift apart

| Where | What it is | Role |
|---|---|---|
| This local repo (`/Users/sadjad/Crypto agents`) | Working copy | Where changes are made |
| `github.com/seiwaz/crypto-project` (**public** repo) | `origin` | Version history; source for the cloud monitoring routine |
| `94.74.166.123` (SSH port 2266, systemd unit `crypto-screener`, user `screener`) | **The only place the code actually runs** | Live paper-trading service |

**The server is the deployment target, not a dev environment.** Never run the
scanner/demo loop locally — it only ever runs on 94.74.166.123. Local execution was
deliberately stopped.

### Sync rule — do this every time you change code or the skill, not just when asked

**As of 2026-08-19, deployment is pull-based and automatic; you don't need SSH to ship
a change.** Commit and push to `origin/main` on GitHub — that's it. A systemd timer on
the server, `crypto-screener-deploy.timer` (service `crypto-screener-deploy.service`,
script `/opt/crypto-screener-deploy/pull-deploy.sh`), fires every 5 minutes and:

1. `git fetch`/`reset --hard` a checkout at `/opt/crypto-screener-deploy/repo`.
2. If `agent/` **or `skill/scripts/*.py`** changed: compile-checks all of them first
   (`python3 -m py_compile`). **Both, not just `agent/`** — found the hard way
   2026-08-20: `skill/scripts/nobitex_api.py` is `import`ed and cached in the running
   process's memory (via `agent/skill.py: _load_api_module()`), so a change deployed
   to disk with no restart has *zero effect* until something else happens to restart
   the service. The Ichimoku addition (Round 7) sat live on disk but inactive for a
   while before this was caught and fixed. A syntax error in either tree aborts the
   deploy — the live code is left untouched and that commit is skipped, logged to
   `/opt/crypto-screener-deploy/deploy.log` — so a bad push costs a cycle, not an
   outage. Only on a clean compile does it rsync `agent/`, `skill/`, and `web/` into
   `/opt/crypto-screener/` and (if `agent/`/`skill/scripts/` changed) restart.
   `config/coins.txt` also syncs (and re-runs `./run.sh setup` discovery) when it
   changed, but the rest of `config/` never does — see the exception below.
3. Always applies `config/strategy-tuning.json` as a deep-merge patch onto the live
   `config/settings.json` via `config.save_settings()` — this needs no restart, so it's
   the preferred way to tune an existing parameter (see `agent/demo.py`'s `settings()`
   for the full key list: `atr_mult` top-level; `correlation_threshold`,
   `heat_cap_pct`, `time_stop_hours`, `max_correlated_same_side`, `counter_trend_gate`,
   `give_back_*`, `maker_*`, `entry_interval_seconds`, `trend_filter_interval`,
   `correlation_interval` all under `"demo"`). Leave the file `{}` when there's nothing
   to override — it's a patch, not a replacement. `demo.reset_password` (see Current
   state) is deliberately **not** set this way — it's configured directly on the
   server, never via this git-tracked file, since this repo is public.

Verify after any push that matters:
`ssh -p 2266 root@94.74.166.123 "tail -20 /opt/crypto-screener-deploy/deploy.log"`, or
just `curl -s http://94.74.166.123:8787/api/health` a few minutes later. Manual deploy
(scp + `systemctl restart` over SSH) still works and is fine for something urgent —
just also make sure it's committed, so the puller doesn't overwrite it with an older
`origin/main` five minutes later.

**Exception — never sync these from local/GitHub to server, ever:** `config/
settings.json` itself (only `strategy-tuning.json` patches onto it) and `var/` are live
server state (current full tuning, the trade database, logs). They flow the other way
if at all — read them from the server to understand current live config, never
overwrite them wholesale. `packaging/srv.sh sync` (the older, manual path) already
knows this and excludes them.

### Server access

- SSH: port 2266, user `root`. **Credentials are not stored in this repo, in Claude's
  memory, or in this file — ask the user each session.** They were shared once in chat
  on 2026-08-19 for that session only; treat that as non-recurring.
- A dedicated deploy key also exists at `~/.ssh/crypto-screener-deploy` on the
  operator's Mac (passphrase-free, for `packaging/srv.sh`) — the user's personal
  `id_ed25519` is passphrase-protected and unusable unattended.
- **Prefer the public API over SSH for anything read-only**: the dashboard at
  `http://94.74.166.123:8787` has no authentication and no firewall in front of it
  (`firewalld` inactive, `iptables` ACCEPT) — this is a known, accepted state, not a
  bug to fix reflexively. `/api/demo/report`, `/api/demo`, `/api/health` are all plain
  GETs, no credentials needed. The cloud optimization routine (below) uses only this —
  it has no SSH access and must never be given any. A human interactive session is the
  only place SSH is used, and only when something needs a one-off manual fix outside
  the normal push-and-let-the-timer-deploy-it flow.
- **The cloud routine must use `https://trade.ssptco.com` (added 2026-08-19), not the
  raw IP.** Anthropic's cloud agent sandbox egress proxy only tunnels outbound
  HTTPS/443 to arbitrary hosts — plain HTTP on a non-standard port (`:8787`) is
  rejected outright ("non-CONNECT request"), confirmed by a real failed test run
  (`curl`, an explicit proxy request, and `WebFetch` all failed the same way). `nginx`
  on the server now reverse-proxies `trade.ssptco.com` → `127.0.0.1:8787` with a
  Let's Encrypt cert (`certbot --nginx`, auto-renews, expires 2026-11-17 absent
  renewal). Config at `/etc/nginx/conf.d/trade.ssptco.com.conf` — do not repurpose
  `mail.ssptco.com` or `www.ssptco.com` on this box, both are real, live services
  (mail server and the actual company website on a *different* host respectively);
  this server hosts several unrelated domains/services (Odoo, Nextcloud, Zabbix, a
  mail server, a couple of client sites) — check for collisions before touching nginx
  config here. From the Mac or any normal network, both the raw IP:8787 and
  `https://trade.ssptco.com` work identically; only the cloud sandbox needs the HTTPS
  hostname specifically.
- Reaching the host from the Mac's browser/other tools may need a static route around
  a local VPN that otherwise intercepts the traffic:
  `sudo route -n add -host 94.74.166.123 192.168.3.1` (the user runs this, not Claude).
  Direct `ssh`/`curl`/`scp` from a Claude Code Bash session has worked without it.

### The GitHub repo is public

`seiwaz/crypto-project` is **not private**. Never commit credentials, API keys, or the
SSH password to any file in this repo, in any commit, ever — `.env`/`.env.local` are
gitignored for this reason and must stay that way. This applies even to files meant to
be temporary or "just for reference."

## Autonomous optimization — STOPPED (2026-08-20, supersedes the 2026-08-19 "full
autonomy" grant below; do not re-enable without the user explicitly asking)

**The cloud routine `crypto-demo-performance-check` (`trig_01D72wvtJHdgeYxyMGEcRPs7`)
is disabled (`enabled: false`) as of 2026-08-20.** The user's own words: "stop routine
and do not use it, I will continue the develop and verifies by claude code myself."
Development and verification now happen through interactive Claude Code sessions
(like the one that made Rounds 4-10) with the user directing changes directly, not
through the unattended cloud routine. Practically:
- Don't trigger the routine (`RemoteTrigger action: "run"`) for anything.
- Don't re-enable it (`enabled: true`) unless the user asks for that specifically —
  disabling was a deliberate policy reversal, not a temporary pause for a bug.
- It cannot be deleted from this session (`RemoteTrigger` has no delete action) — if
  the user wants it gone entirely, point them to `https://claude.ai/code/routines`.
- **Known unresolved issue, irrelevant now but worth knowing if this is ever
  revisited:** the routine's last three scheduled runs (2026-08-20, 00:43/06:43/12:43
  UTC) all failed identically with `403 Resource not accessible by integration` on
  every write attempt (`git push` and the GitHub MCP fallback both) — its GitHub App
  connector had read-only access, not write. It correctly detected this each time,
  pushed a notification, and made no fabricated changes. Grant "Contents: Read &
  write" at `https://claude.ai/admin-settings/claude-tag` (or reconnect the connector)
  before ever re-enabling it, or it will just fail the same way again.

### Prior policy, for the record (2026-08-19 — no longer in effect)

The user had explicitly asked for **full autonomy, running indefinitely**: the
routine could research, diagnose, and ship both parameter tuning
(`config/strategy-tuning.json`) and new code/skill logic (`agent/demo.py`, `skill/`)
on its own, without a human review gate per change. That was itself a deliberate
reversal after I (Claude) raised the overfitting risk explicitly and the user
confirmed they wanted full autonomy anyway. The full-autonomy grant is what's been
superseded now, not the original overfitting-risk conversation — see this project's
history around 2026-08-19 if that reasoning ever needs revisiting.

**The target is expectancy and real profit — never win rate as a goal in itself.**
This system's own data already shows >~43% win rate isn't reachable without destroying
expectancy (the TP1-lowering experiment, `docs/RESEARCH_LOG.md` commit `c6b2c5f`,
rejected every coin trying to buy a higher win rate this way). If a future cycle sees
win rate climb while expectancy or net R flattens or drops, that's a regression to
find and undo, not a result to keep.

**Discipline that keeps "unlimited autonomous cycles" from becoming "unlimited
thrashing":**
- One change per cycle, only after a 20-trade batch closes.
- Before proposing a new change, the routine checks whether its *own last* change
  regressed expectancy — if so it reverts that one thing this cycle instead of piling
  on a new untested change on top.
- Every change is researched first (WebSearch, cited) and logged in
  `docs/RESEARCH_LOG.md` as a new dated `Round N (auto, ...)` entry — issue, evidence,
  research, hypothesis, exact change, status. This *is* the audit trail; read it to see
  what the system has already tried before assuming something's untested.
- `config/strategy-tuning.json` changes are preferred over code changes (reversible,
  no restart). Code changes go through the compile-check-or-abort deploy safety net
  described in the Sync rule above, but that only catches syntax errors, not logic
  bugs — a genuinely broken (but valid Python) change can still reach the live service
  between one 6-hour check and the next.
- The routine never has, and must never be given, exchange credentials of any kind.
  This stays paper-trading-only regardless of what looks promising.

## Current state (updated 2026-08-21 — keep this section current, don't let it rot)

- **Demo account:** running since the 2026-08-20 reset after the Round 3
  BTC-alignment fix; check `/api/demo/report` for the live closed-trade count before
  saying anything about performance — this line is kept current automatically by the
  cloud routine every ~6h, but the trading *configuration itself* has changed several
  times same-day since that reset (Rounds 4-10, below), so the accumulated sample
  mixes trades opened under different rules. A 213-trade review was done under the
  mixed sample (see "Round 10 findings" below) — treat its exact numbers as
  directional, not as a clean read on the *current* configuration specifically, until
  enough trades close under Round 10's fix to judge it on its own. No further reset
  has been done since Round 10; consider whether one is warranted before trusting
  aggregate stats too far.
- **Live trading configuration, as of Round 13 (all via `config/strategy-tuning.json`
  unless noted as a code change):**
  - `profile: "scalp"` (5-15m entries, 15m decision TF, 1H bias) — switched from
    `intraday` at the user's request for a 5-30 minute holding strategy (Round 4).
  - `atr_mult: 1.0` (was scalp's own default 1.5) — tightened after Round 4's initial
    30-min window produced 11/11 time-stops with zero real stop/TP hits; 1.0x was
    reused from Round 1's own multiplier sweep rather than picked arbitrarily
    (Round 5).
  - `scan_interval_minutes: 5`, `demo.entry_interval_seconds: 300` (both were 20/1200)
    — as close to "immediate entry on signal" as the batch-scan architecture supports.
  - `demo.cycle_seconds: 30` (was 60) — position management cadence.
  - `demo.time_stop_hours: 0.5` (30 min) — but **floating**, not a hard deadline (code
    change, `agent/demo.py`, Rounds 6 & 8): a *losing* position skips the clock
    entirely once underwater, governed only by its real stop, until it recovers to
    breakeven+ (Round 6). A *profitable* position is re-checked against the latest
    scan verdict every cycle (not once per 8h funding period) — still favoured, it
    floats past the deadline; no longer favoured, it closes immediately as
    `signal_exit` rather than waiting for the clock (Round 8). Missing scan data does
    neither — it falls through to the plain floor-vs-deadline test unchanged (a real
    bug in the first version of Round 8 treated missing data as "still favoured" and
    silently broke the ordinary time-stop for the common case; caught by the test
    suite before it ever deployed).
  - `demo.maker_timeout_minutes: 2` (was 30) — a 30-min resting limit order made no
    sense against a 30-min total hold budget (Round 4).
  - `_reduce_at_tp1` (code change, `agent/demo.py`, Round 10) now locks the runner's
    stop at the **TP1 price itself**, not breakeven-plus-costs — a reversal after TP1
    can only give back the runner's further upside, never the proven gain. Easier to
    stop the runner out on ordinary noise right after TP1 fires than a breakeven stop
    was — a real trade-off, not a free improvement.
  - Direction scoring (`skill/scripts/nobitex_api.py`) gained a 9th check, **Ichimoku
    Cloud** (Round 7) — price vs. the cloud (Tenkan/Kijun/Senkou A+B, correctly
    displaced 26 bars), skipped rather than forced when price sits inside the cloud.
    Threshold left unchanged (5/scalp, 6/intraday-swing) since it's one more vote in
    the same pool, not a harder bar.
  - Watchlist (`config/coins.txt`) expanded 47→69 coins (Round 4-era, same day):
    TON, LDO, FET, WIF, ORDI, AXS, JTO, COMP, CHZ, RUNE, DYDX, OP, SEI, GALA, EIGEN,
    GMX, IMX, AR, STX, STRK, ZRO, NOT added, each verified against live Toobit
    volume. `GRAM`, `MNT`, `PUMP`, `EOS`, `1000SATS` are listed but **not valid
    Toobit contracts** right now (discovery reports "no USDT perpetual contract") —
    sitting harmlessly unscanned rather than silently dropped; a human call whether
    to remove them, not an automated one.
  - `/api/demo/reset` is now password-gated (Round 9) — the password is configured
    only in the server's live `settings.json` (`demo.reset_password`), never in this
    public repo, never returned by `/api/settings`. Ask the user for it if a reset is
    ever needed from a fresh session; don't guess or search for it in files.
  - `agent/paper.py: exit_reason()` (Round 11, 2026-08-20, **critical, capital-
    affecting bug in the paper account**) used a `low <= level <= high` range-
    containment check for stop/tp1/tp2, when only a one-sided comparison is correct
    (`low <= stop` for a long, etc — `liq` right next to it was already written this
    way). The bug: once price moved cleanly past a stop and stayed there, the most
    recent candle's own high no longer reached back up to the old stop level, so the
    check silently stopped firing *forever*, even with the position sitting far past
    its stop. Found live (UNI and ARB both sitting ~170 minutes past their stop,
    never closing); both closed within seconds of the fix deploying, both net
    positive since TP1 had already banked before the drift. Fixed with direct
    one-sided comparisons; 4 new unit tests added directly against `exit_reason()`.
    Full writeup: `docs/RESEARCH_LOG.md` Round 11.
  - `qualifying_signals()`'s re-entry guard and `correlated_same_side()`'s cap check
    (Round 13, 2026-08-20) only read `store.paper_open_positions()`
    (`status='open'`), missing resting maker limit orders sitting at
    `status='pending'` until they fill — so a coin's own still-unfilled order didn't
    block a second entry into that same coin from a later scan, inside the same
    `maker_timeout_minutes` (2 min) window. Caught live: two simultaneous WIF
    positions from consecutive scans. Both guards now union open + pending
    positions. **Known, deliberately not fixed:** `state()`'s displayed
    `slots.filled`/`heat.used_pct` still count open-only, so capacity/heat can be
    briefly undercounted *across separate `try_fill_slots()` calls* while an order is
    pending — bounded to that same 2-minute window, and a narrower version of the
    same class of gap, not the specific bug that was demonstrated and fixed. Full
    writeup: `docs/RESEARCH_LOG.md` Round 13.
- **Round 10 findings (213-trade review, mixed pre/post-fix sample):** the headline
  finding was **zero of 213 trades were short** — traced to `skill.side_from_direction()`
  defaulting to long on any tie or near-tie instead of a symmetric margin-based
  choice; confirmed live the same day (a scan showed 66/69 coins scored long, 100% of
  TAKE/WATCH verdicts long). Fixed: a side must now lead by more than
  `DIRECTION_MARGIN` (1) to be chosen, and `demo.py: qualifying_signals()` now
  actually enforces the resulting `side_tied` flag (it existed and was shown in the
  UI before this, but nothing was reading it to block a trade). Secondary findings,
  still worth knowing even though the sample predates the fix: expectancy was thin
  (+0.054R) because real stop-losses averaged close to -0.85R while wins from the
  aggressive time-stop/signal-exit averaged only +0.27-0.29R; longs taken while BTC
  itself was *bearish* ran +0.29R expectancy / 71% win rate against **-0.05R / 54%**
  when BTC was bullish (backwards from naive expectation — likely means bullish-BTC
  entries were chasing already-extended moves); the 80-89 score band performed worse
  (42% win rate) than 70-79 (61%), i.e. score isn't well-calibrated as a quality
  signal above the pass bar; ZRO, WLD, UNI, MORPHO, KAS, INJ, PENGU, TIA, JTO, ASTER
  were the worst-performing coins, ENA, EIGEN, ONDO, SUI, IMX, JUP the best. Full
  writeup in `docs/RESEARCH_LOG.md` Round 10.
- **Round 12 (2026-08-20, docs-only):** researched the "risk-free trade" concept
  (breakeven-stop-after-partial-exit, cited sources in `docs/RESEARCH_LOG.md`) and
  confirmed Round 10's TP1-lock (`_reduce_at_tp1` locking the runner's stop at the
  TP1 fill price, not breakeven) is a correct, more conservative variant of the
  standard mechanism — no code change needed. It did find `skill/SKILL.md` had
  drifted: it still described the *old* pre-Round-10 "breakeven + accumulated costs"
  rule. Fixed `SKILL.md`'s management-rules line and added `skill/references/risk-
  math.md` §10 (the three stop-placement variants, their floors/trade-offs,
  citations). Synced to `~/.claude/skills/crypto-leverage-trade-plan/`. A reminder
  that skill docs can silently drift from what `agent/demo.py` actually does —
  worth a periodic side-by-side check, not just when directly asked.
- **Research:** Round 1 (retrospective — fixes shipped in commits `459c994` through
  `a515b5d`) and Round 2 (public-source: correlation crash-asymmetry, trend quality vs.
  direction, RVOL threshold gap, event-risk/signal-freshness gaps) are done, written up
  in `docs/RESEARCH_LOG.md`, and deployed live. **Round 3 (expert/critical validation)
  hasn't been started by a human** — the autonomous routine may end up covering pieces
  of it incidentally as it researches specific issues, but nobody has done a dedicated
  Round 3 pass; check `docs/RESEARCH_LOG.md` for the current highest Round number
  before assuming what's covered.
- **Candidate changes queue** — researched and evidenced in Round 2, available for the
  autonomous routine to pick up (or for a human to apply directly). See
  `docs/RESEARCH_LOG.md` → "Round 2 summary" table:
  - Market-stress correlation override (crash-tail correlation runs far above the
    calibrated 0.75 threshold)
  - Choppiness Index / Efficiency Ratio regime-quality gate alongside the existing
    EMA200 trend check
  - Automated RVOL ≥ 1.5 gate in `skill/scripts/nobitex_api.py`'s scoring (currently
    computed but only advisory)
  - Signal max-age ceiling (low-risk — doesn't change which trades qualify)
  Check whether an auto `Round N` entry has already applied (or reverted) one of these
  before assuming it's still untouched.
- **Monitoring + optimization: STOPPED (2026-08-20)** — `crypto-demo-performance-check`
  is disabled at the user's explicit request; nothing is polling, logging checkpoints,
  or auto-tuning right now. See "Autonomous optimization" above. Keeping this
  section's demo-account line current, watching `docs/PERFORMANCE_LOG.md` (frozen at
  whatever it last held), and any further tuning are all manual/interactive now —
  don't assume any of it is still happening in the background.
- **Phase 4 (real-money connection) is explicitly gated and NOT covered by the
  autonomy grant above**: only propose it after ≥100 demo trades with stable positive
  expectancy across ≥3 consecutive evaluation periods, and never connect to a real
  exchange for automated execution without the user's explicit, separate approval in
  an interactive session — propose and wait, always. The cloud routine has no
  exchange credentials and must never be given any.
- **Email notifications: wired up 2026-08-19.** Gmail MCP connector
  (`fd75ca4e-f9f5-4863-bf81-71cacb334024`) is attached to the routine, gated to only
  send on a real checkpoint or an applied/reverted change — not on plain no-op runs.
  The routine's report data source is also now push-based, not a direct fetch: the
  cloud sandbox's egress proxy only allows HTTPS to a fixed domain allowlist (confirmed
  via a real `EGRESS_BLOCKED` error, even after fixing an earlier separate HTTP-vs-HTTPS
  issue with an nginx+Let's Encrypt reverse proxy at `trade.ssptco.com` — that fix was
  necessary but not sufficient). The server now pushes a live report snapshot into
  `docs/live-report.json` in this repo every ~10 min via
  `crypto-report-push.timer`/`.service` (script `/opt/crypto-screener-deploy/push-
  report.sh`, using a separate write-scoped GitHub deploy key at `/opt/crypto-screener-
  deploy/report_push_key` — the routine itself still never gets SSH or push credentials,
  it only reads the file from its own checkout).
- **Known live bugs, found 2026-08-20, not yet both fixed:**
  1. **Fixed:** `agent/toobit.py: resolve_manual_checks()` resolved the "BTC / dominance
     alignment" direction check from BTC's own trend for *every* coin, regardless of
     that coin's own behavior — the same mistake `demo.counter_trend()` already proved
     backwards (commit `7356609`) and fixed, but never backported to the scoring path.
     Since this fed the score itself, not just an entry gate, it silently capped
     opportunities across the whole watchlist on every scan. See `docs/RESEARCH_LOG.md`
     Round 3 for the full writeup and live verification (two consecutive full-watchlist
     scans went from 0 TAKEs to 7 immediately after the fix; all 7 filled as real
     positions the same session).
  2. **Not fixed:** `POST /api/settings` fails with `OSError: Read-only file system` —
     the systemd unit's `ProtectSystem=strict` + `ReadWritePaths=/opt/crypto-screener/
     var` only allows writes to `var/`, but `config.save_settings()` writes to
     `config/settings.json`. Doesn't affect the autonomous tuning pipeline (the deploy
     timer's `strategy-tuning.json` merge runs outside the systemd sandbox), but the
     dashboard's own "change settings" UI/API is silently broken. Needs `config/` added
     to `ReadWritePaths` in the systemd unit, or the write moved elsewhere.
  3. **Worth watching, not fully root-caused:** after the 2026-08-20 restart to deploy
     the scoring fix, `demo.scheduler_loop`'s background thread appeared alive (thread
     count matched) but produced no activity — no log lines, no KV updates — for
     15+ minutes, despite `demo.enabled: true`. A manual `/api/demo/cycle` call worked
     instantly and correctly (opened all 7 pending positions), and a second clean
     service restart resolved it — the loop has run normally since. Possibly caused by
     unusual overlapping-scan load during that session's manual testing (three `/api/
     scan` calls in quick succession, one cancelled mid-flight) rather than a code bug;
     no exception or deadlock was found in logs. If it recurs without heavy manual
     testing, treat as a real bug and dig into `scanner.py`/`correlation.py`'s locking.
  4. **Fixed, later the same day:** the `exit_reason()` range-containment bug
     (Round 11) and the pending-order same-coin double-entry bug (Round 13) — both
     detailed as bullets under "Live trading configuration" above rather than
     repeated here. Both were found the same way: checking live positions/trades
     against what the code should be doing, not from a report or metric alone —
     worth remembering as the pattern that keeps finding real bugs in this system.

## Tabdeal integration — IN PROGRESS, real-money migration requested (2026-08-21)

**User's stated goal, verbatim intent: use Tabdeal as a real exchange to migrate off
demo/paper trading onto real trading.** This is a live, unresolved, high-stakes thread
— read this whole section before acting on it further, and do not silently start
wiring real execution without re-surfacing the open concerns below to the user first.

**How this started:** user asked to research Tabdeal (docs.tabdeal.org, `pip install
tabdeal-python`) and update the skill to support it. Investigation found the *official*
`tabdeal-python` package (v0.4.7, latest) only wraps **Spot** and **Isolated Margin**
(borrow-based leverage — interest, not funding; margin-call liquidation, not a
maintenance-tier formula) — no perpetual-futures class at all. User then named the
actual product they want: **"اهرم حرفه‌ای" (Professional Leverage)** — Tabdeal's
separate futures-style product, undocumented in the pip package but real, live, and
Binance-futures-shaped.

**Confirmed live, with the user's own real API key/secret (read-only GET calls only —
no order/transfer/leverage-change calls were ever made):**
- Base URL `https://api1.tabdeal.org`, futures paths under `/fapi/v1` (orders,
  leverage, exchangeInfo, depth) and `/fapi/v3` (account, balance, positionRisk). Same
  auth as spot: `X-MBX-APIKEY` header + HMAC-SHA256-signed query string
  (`timestamp`+`signature`), i.e. the same pattern `agent/toobit.py` already speaks.
- `/r/fapi/v3/account` → `{"canTrade": true, "canDeposit": true, "canWithdraw": true,
  "assets": [], "positions": []}` — the futures account is real, active, currently
  flat/unfunded.
- `/r/fapi/v1/leverage?symbol=BTC_USDT` → `{"leverage": 10, "symbol": "BTC_USDT"}` —
  leverage is real, per-symbol, gettable and (per docs) settable via POST.
- 33 futures symbols exist. **Symbol format is strict underscore** (`BTC_USDT`) —
  `BTCUSDT` is rejected as invalid, unlike Toobit's no-separator convention.
- `exchangeInfo` only returns `pricePrecision`/`quantityPrecision`/`quotePrecision`
  per symbol — no `filters` (tickSize/stepSize/minNotional) and no leverage-bracket /
  maintenance-margin ladder the way Toobit's `riskLimits` provides. Usable as
  `tick = 10^-pricePrecision`, `step = 10^-quantityPrecision`, but there is nothing
  server-published to check a plan's liquidation buffer against ahead of placing an
  order — `positionRisk` should return a live `liquidationPrice` once a position
  actually exists, but that's *after the fact*, not a pre-trade check.

**Hard blocker, confirmed by direct testing, not just doc gaps — Tabdeal has
NO candle/kline data anywhere:**
- Every REST path tried 404'd live: `/fapi/v1/klines`, `/fapi/v3/klines`,
  `/api/v1/klines`, `/fapi/v1/premiumIndex`, `/fapi/v1/fundingRate`,
  `/fapi/v1/ticker/24hr`, `/fapi/v1/ticker/price` — tried with both symbol formats.
- The websocket (`wss://api1.tabdeal.org/stream/`, subscribe via a JSON
  `{"method":"SUBSCRIBE","params":[...],"id":N}` message, **not** the URL
  `?streams=` query form some Binance-style APIs also accept) works and pushes real
  depth data fine (`btcusdt@depth@2000ms` streamed live order-book updates), but a
  kline subscription (`btcusdt@kline_15m`, both symbol formats) got an explicit
  `{"error":"INVALID_FORMAT"}` rejection from the server — not silence, an active
  "this isn't a real stream" answer.
- **Without candles, nothing in `compute_indicators()` (EMA/ATR/RSI/Ichimoku/VWAP/
  swing structure — the entire signal engine) can be computed from Tabdeal data
  directly.** The user was mid-way through choosing how to handle this (options put
  to them: Toobit-candles-for-signals + Tabdeal-for-execution, vs. building candles
  locally from Tabdeal's own tick/depth stream going forward with no history, vs.
  pausing) when the conversation moved on to the reachability check below — **this
  is still an open, unresolved decision, not settled.**

**Server reachability, confirmed 2026-08-21 from 94.74.166.123:** DNS resolves
(`api1.tabdeal.org` → `185.143.233.238`/`185.143.234.238`), plain HTTPS REST works
(`/r/fapi/v1/ping` → `200 {}`), `iptables`/`firewalld` are not blocking outbound (same
permissive state noted elsewhere in this file). **Not yet confirmed:** clean websocket
reachability from the server specifically — `websocket-client` isn't installed there,
and a crude `curl` upgrade probe returned `HTTP 000` (inconclusive, not a real test).
Worth a proper Python-based test before relying on it, the same way it was verified
from the Mac.

**Two safety concerns raised with the user, not yet resolved either way:**
1. **The API key/secret the user pasted (now in the gitignored local `.env` as
   `TABDEAL_API_KEY`/`TABDEAL_API_SECRET`, base URL left blank — never in this public
   repo, never printed again after the first time) are FULL TRADE-PERMISSION keys on
   a real, funded account** (`canTrade: true` on both spot and futures; the spot
   wallet holds real SHIB) — not read-only, unlike every other exchange key this
   project has used so far (Nobitex, Toobit both explicitly recommend read-only).
   Recommended the user rotate this key once done exploring. Also recommended they
   stop pasting secrets directly into chat (paste into `.env` instead) since this
   session's transcript now contains them regardless of where they end up stored.
2. **This directly runs into the Phase 4 gate already established in this file**
   (see below): real-money connection was explicitly gated on ≥100 demo trades with
   *stable* positive expectancy across ≥3 consecutive evaluation periods, plus a
   separate explicit approval in an interactive session — "propose and wait, always."
   The demo account does have 309 closed trades (>100), but per this file's own
   "Current state" notes the sample mixes trades opened under materially different
   bug-fix states (Rounds 4-13, same day) and has NOT yet been judged stable across
   ≥3 consecutive periods under one settled configuration. **The user has now stated
   the real-trading intent directly and interactively — that satisfies the "explicit
   approval" half of the gate, but the trade-count/stability precondition has not
   been re-verified as met.** This was raised but not yet resolved when the session
   paused to save state for a model switch — surface it again before writing any
   order-placement code, don't silently proceed past it.

**Separate, not-yet-raised-in-full risk worth surfacing early in the next session:**
the futures order types confirmed documented so far are only `LIMIT`/`MARKET` — no
`STOP_MARKET`/`STOP_LOSS`/conditional order type has been confirmed to exist on
Tabdeal's futures API (spot has `STOP_LOSS_LIMIT`; futures docs explicitly said
"other advanced types not currently supported" without confirming which, if any,
stop-type exists). The paper-trading system's whole safety model relies on *its own*
monitoring loop to detect a stop/TP hit (`agent/paper.py: exit_reason()`) — that's
fine for a simulation, but for **real money**, if Tabdeal has no server-side/exchange-
native stop order, a monitoring-loop outage (this project has already had one real,
if brief, unexplained scheduler-loop stall — see "Known live bugs" item 3 above)
would leave a real leveraged position with **zero downside protection** for however
long the loop is down. This must be confirmed one way or the other — and designed
around if there's no native stop — before any real order is ever placed. Not yet
investigated at all as of this save point.

**Nothing has been written to `agent/` or `skill/` for Tabdeal yet** — this section
exists entirely to preserve research findings and open decisions, not completed work.
No code changes, no commits, no deploys related to Tabdeal have happened.

### Full prerequisite audit (2026-08-21) — verdict: NOT READY, 6 blockers

Run at the user's request ("check all prerequisites") before any migration work.
Everything below was verified by live authenticated GET calls or direct inspection,
not from docs alone. **Corrections to the section above, found during the audit:**

**CLEARED — these are genuinely fine:**
1. **Exchange-native SL/TP exists** — `POST /fapi/v1/positionSlTp` is real (GET
   returns `405 {"code":1217}`, an API-level "wrong method", not a 404 page).
   This resolves the biggest safety worry flagged above: a real position *can*
   carry an exchange-side stop that survives our monitoring loop dying. **This
   changes the earlier "no confirmed stop type" concern from blocker to cleared** —
   the `/fapi/v1/order` endpoint really does only accept LIMIT/MARKET, but SL/TP is
   a separate position-level endpoint, not an order type.
2. **Reachable from the server** — DNS + HTTPS confirmed from 94.74.166.123.
3. **Auth is Binance-shaped HMAC-SHA256**, same pattern `agent/toobit.py` speaks.
4. **Leverage + positionRisk + liquidationPrice** all work.
5. **Track record partly transfers**: of 919 closed demo trades, 374 (40.7%) are on
   coins Tabdeal actually lists, and that subset performs *better* than the whole
   (+0.1387R expectancy, 63.1% win, vs +0.1053R / 61.4% overall). 374 > the ≥100
   Phase 4 minimum.

**BLOCKERS — all six must be resolved before real money:**
1. **No candle data anywhere on Tabdeal** (detailed above). Signal engine cannot run
   on Tabdeal data. Unresolved: use Toobit candles + Tabdeal execution, or build bars
   locally from Tabdeal's depth stream with no history.
2. **The codebase is architecturally read-only, on purpose.** `agent/guard.py` holds
   two independent allowlists (Nobitex + Toobit), forbids the substrings `order`,
   `leverage`, `transfer`, `cancel`, `close`, refuses any non-GET verb, and
   `self_test()` runs at server startup — **the server refuses to boot if the guard
   ever stops rejecting a known-bad path.** The skill's own client has a second copy
   of the same guard. Real trading means deliberately dismantling a safety system
   that was built deliberately, in two places, with a startup tripwire. This is a
   major architectural decision, not a config flag.
3. **Phase 4 stability gate FAILS.** Split into 5 consecutive blocks, expectancy is
   *not* stable — it swings hard and **the most recent block is negative in both
   cuts**: all-trades blocks run +0.012/+0.114/+0.181/+0.263/**−0.041**R;
   Tabdeal-only coins run +0.261/**−0.049**/+0.245/+0.297/**−0.051**R. The gate
   requires stable positive expectancy across ≥3 consecutive periods. Also note the
   entire 919-trade sample spans only **36.5 hours** (~25 trades/hour) — one market
   regime, and it straddles the Round 11/13 bug fixes.
4. **Futures fee rate is unknown and potentially fatal.** Not in the API, not in the
   docs, not findable by search. Spot reports `makerCommission: 40 / takerCommission:
   40` — on the Binance convention that reads as **0.40% per side**. If futures fees
   are anywhere near that, the strategy dies: at ~$270 notional against a $3 R, a
   0.8% round trip is **~0.72R of cost** versus the ~0.11R the plans currently assume
   on Toobit's 0.06% taker. Expectancy is +0.14R; +0.6R of extra drag makes it
   deeply negative. **Verify the real futures fee before anything else — this single
   number can invalidate the whole migration.**
5. **`reduceOnly` is documented as "فعلا پشتیبانی نمی شود" (not currently
   supported).** Without it there is no guaranteed-safe way to close: an oversized
   close can flip into an opposite position rather than flatten.
6. **No close-position endpoint found.** Docs reference `POST /fapi/v1/positionClose`
   but that path 404s, as do five other candidates tried. Either it's undocumented
   under another name or closing happens only via an opposing order (which, with no
   `reduceOnly`, is exactly the risk in blocker 5). Must be resolved.

**Remaining gaps (not blockers, but real):**
- **45 of 74 watchlist coins don't exist on Tabdeal** (61%). Only 33 futures symbols
  total. Symbol format is strict underscore (`BTC_USDT`); `BTCUSDT` is rejected.
- `exchangeInfo` gives only `pricePrecision`/`quantityPrecision` — no tickSize,
  stepSize, or minNotional filters, and no maintenance-margin/leverage-bracket
  ladder, so there is no pre-trade liquidation-buffer check the way Toobit's
  `riskLimits` allows. `positionRisk` gives `liquidationPrice` only after the fact.
- **The futures account is unfunded** (`assets: []`, `balance: []`).
- The whole execution layer does not exist: `agent/paper.py` is a simulator, and a
  real broker needs order-state reconciliation, partial fills, real slippage,
  rejects, and a kill switch — none of which a paper account ever had to handle.
- Websocket reachability *from the server* still unverified (`websocket-client` isn't
  installed there; a curl upgrade probe was inconclusive).
- API key is full-permission (`canTrade: true`, and futures reports
  `canWithdraw: true`) on a real funded spot wallet. Should be rotated to the
  narrowest permission set that works, ideally IP-whitelisted to the server.

## What needs to continue (pick this up without being re-asked)

1. Keep letting the demo account run; the autonomous routine may reset gates/params
   via `strategy-tuning.json` or code, but nothing should reset the *account itself*
   (capital, trade history) without a clear reason — that restarts the sample.
2. Watch how the Round 10 fixes actually play out with a real sample. **Partially
   confirmed 2026-08-20/21:** shorts are being generated again at the scan level (a
   live watchlist scan showed 33 long / 5 short across 38 coins, one short reaching
   WATCH) — the market itself has just been broadly bullish, which is why closed and
   open demo trades have stayed long-heavy; this is not the Round 10 bug recurring.
   If short trades ever go conspicuously quiet again, re-check `side_from_direction`
   and `DIRECTION_MARGIN` before assuming it's just the market. Still unmeasured:
   whether the tighter TP1-lock stop causes more runner stop-outs right after TP1
   than the old breakeven stop did (the round's log flagged this trade-off, not yet
   measured with real data). Given Rounds 11 and 13 also changed live behavior since
   the account was last reset (2026-08-20, post-Round-3), worth a deliberate account
   reset once there's confidence the current configuration (through Round 13) is
   stable, so the next sample isn't mixing trades opened under materially different
   bug-fix states the way the 213-trade review had to.
3. The expert-validation research pass originally planned as "Round 3" got superseded
   same-day by the urgent BTC-alignment scoring bug (which ended up *being* Round 3 —
   see `docs/RESEARCH_LOG.md`). That validation pass — quant/published backtests,
   exchange liquidation/margin-call documentation, a critical re-check of Round 1/2's
   conclusions, and the specific open flag that Nobitex fee-schedule constants in
   `trade_plan.py` were never re-verified against the exchange's current published
   rates — still hasn't happened under any round number. Pick it up as a fresh round
   whenever there's space for it, human- or routine-driven.
4. Every time `skill/`, or `agent/*.py` changes by hand (not through the autonomous
   routine): follow the sync rule above — commit+push to GitHub, let the deploy timer
   pick it up (or deploy manually + verify). Don't consider a change "done" until it's
   live on 94.74.166.123, since that's the only place it matters. If you edit the
   *Mac-local* skill copy at `~/.claude/skills/crypto-leverage-trade-plan/` directly
   (e.g. because you're using the skill interactively), remember to also copy those
   edits into this repo's `skill/` — they are two directories, not a symlink, and only
   the repo copy reaches the server.
5. Periodically skim `docs/RESEARCH_LOG.md`'s auto `Round N` entries and
   `docs/PERFORMANCE_LOG.md` — the routine self-corrects regressions, but a human
   read-through catches things a single-window revert rule can't (e.g. a change that's
   neutral for several windows then breaks on a regime shift).

## Established facts — don't re-litigate these from scratch

- The fee/cost model does **not** subtract from stop distance — `cost_in_R` is compared
  against `R`, gated in `trade_plan.py`. The actual fee/stop interaction is TP1
  reachability: tighter, more-reachable targets fail the cost-efficiency gate; the
  current 1.5R/48h stop-and-hold combo was chosen by replaying this account's own
  history, not assumed.
- Counter-trend gating uses the **instrument's own** trend (EMA200/1H), not BTC's —
  gating on BTC alone was measured to be exactly backwards.
- A win rate materially above ~43% is not supported by any data collected on this
  system so far; published trend-following systems in this style run 30–45%. Don't
  chase a higher win rate at the expense of expectancy. **Caveat added 2026-08-20:**
  this ceiling was established under the old `intraday` profile (4H decision TF,
  hours-long holds). The current `scalp` configuration (Rounds 4-10) is a different
  system on a different timeframe and hasn't run long enough post-fix to say whether
  the same ceiling applies — don't assume it transfers, but don't assume it doesn't
  either without evidence.
- `agent/correlation.py` and `agent/reachability.py` are local stand-ins for the
  skill's `market_context.py` (not installed anywhere). If that file ever actually
  ships, delete the stand-ins rather than keeping both.
- A tie or near-tie in `long_score` vs `short_score` used to default to long — a real
  bug (`skill.side_from_direction`, fixed Round 10), not a market artifact, and it
  produced literally zero short trades across the account's first 213 closed trades.
  If short trades ever go conspicuously quiet again, check this isn't recurring
  before assuming the market is simply one-directional.
- `skill/scripts/*.py` changes need the live service to **restart** to take effect,
  the same as `agent/*.py` — they're `import`ed and cached in the running process's
  memory, unlike `SKILL.md`/`references/*.md` which are read fresh from disk every
  time. The deploy pipeline handles this automatically now (see the Sync rule), but
  if ever deploying by hand, don't assume a skill script change is "just docs."
- Level checks in `agent/paper.py: exit_reason()` (stop/tp1/tp2/liq) must be
  **one-sided** comparisons (`low <= stop` for a long), never `low <= level <= high`
  range containment — a real bug (fixed Round 11), not a rare edge case: once price
  moves cleanly past a level and the most recent candle's own high/low no longer
  reaches back to it, a range check silently stops firing forever, even with the
  position sitting far past the level. If a stop/TP is ever reported as "not
  working" again, check this pattern hasn't crept back in before looking elsewhere.
- A resting maker limit order (`status='pending'` in `paper_positions`) carries the
  same committed risk as a filled one (`margin`/`risk_amount`/stop/targets are all
  set at placement, not at fill) but is easy to leave out of a guard that only reads
  `store.paper_open_positions()` (`status='open'` only) — that gap let the same coin
  be entered twice at once before Round 13 fixed `qualifying_signals()`'s
  `open_coins` and `correlated_same_side()`'s cap check to union open + pending.
  Any *new* guard or capacity check written against open positions should ask
  whether a still-pending order needs to count too.
