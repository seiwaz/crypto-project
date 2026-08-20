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

## Autonomous optimization (policy set 2026-08-19 — supersedes the earlier "hold for
review" stance; that stance is intentionally overridden, not forgotten)

The user explicitly asked for **full autonomy, running indefinitely**: the cloud
routine `crypto-demo-performance-check` (`trig_01D72wvtJHdgeYxyMGEcRPs7`, every 6h) may
research, diagnose, and ship both parameter tuning (`config/strategy-tuning.json`) and
new code/skill logic (`agent/demo.py`, `skill/`) on its own, without a human review gate
per change. This was a deliberate reversal after I (Claude) raised the overfitting risk
explicitly and the user confirmed they wanted full autonomy anyway — see this
project's conversation history around 2026-08-19 if the reasoning ever needs
revisiting, but treat the policy itself as settled, not open to re-litigating each
session.

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

## Current state (updated 2026-08-20 — keep this section current, don't let it rot)

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
- **Live trading configuration, as of Round 10 (all via `config/strategy-tuning.json`
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
- **Monitoring + optimization:** `crypto-demo-performance-check` polls
  `/api/demo/report` every 6h, keeps this section's demo-account line current every
  run, and — once 20+ new trades have closed since the last checkpoint — logs to
  `docs/PERFORMANCE_LOG.md` and runs a full diagnose → research → apply-or-revert cycle
  (see "Autonomous optimization" above). `docs/PERFORMANCE_LOG.md` and `docs/
  RESEARCH_LOG.md`'s auto `Round N` entries are the actual current-state source of
  truth for anything performance-related — this file summarizes, it doesn't replace them.
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

## What needs to continue (pick this up without being re-asked)

1. Keep letting the demo account run; the autonomous routine may reset gates/params
   via `strategy-tuning.json` or code, but nothing should reset the *account itself*
   (capital, trade history) without a clear reason — that restarts the sample.
2. Watch how the Round 10 fixes actually play out with a real sample: does short-side
   trading actually appear now (check `by side` in `/api/demo/report`'s trades — it
   should no longer be 100% long), and does the tighter TP1-lock stop cause more
   runner stop-outs right after TP1 than the old breakeven stop did (a real trade-off
   the round's log flagged, not yet measured). Worth a deliberate account reset once
   there's confidence the current configuration is stable, so the next sample isn't
   mixing pre/post-Round-10 trades the way the 213-trade review had to.
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
