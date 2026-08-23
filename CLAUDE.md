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

## Current state (updated 2026-08-22 — keep this section current, don't let it rot)

- **Demo/paper account: STOPPED 2026-08-22** (`demo.enabled: false`) — the user's
  words: "now the demo trading is not important for me focus on taking signals and
  use them to open position in Tabdeal exchange". The *scanner* still runs and is
  what feeds the live engine; only the paper management loop is off. Its final state
  was ~1087 USDT from 1000 on Tabdeal data. Everything below about the paper account
  is history unless it is re-enabled — see "LIVE TRADING IS ON" above for what is
  actually trading now.
- **Demo account (historical):** ran since the 2026-08-20 reset after the Round 3
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
  2. **FIXED 2026-08-22.** `POST /api/settings` used to fail with `OSError:
     Read-only file system` — the systemd unit's `ProtectSystem=strict` +
     `ReadWritePaths=/opt/crypto-screener/var` allowed writes only to `var/`, while
     `config.save_settings()` writes `config/settings.json`. The UI change appeared
     to work and silently reverted. Fixed by adding `config/` to `ReadWritePaths`
     (both `/etc/systemd/system/crypto-screener.service` on the box and the template
     in `packaging/install-centos.sh`), and by catching `OSError` in the settings
     handler so a write failure now reports the real reason instead of a bare
     `{"error":"internal error"}` 500.
     **Separate gotcha, NOT a bug:** with `demo.auto_slots: true` (the default) the
     demo ignores the top-level `risk_pct` entirely and uses
     `derived_risk_pct() = heat_cap_pct / max_slots` — 6/20 = **0.3%**. So changing
     `risk_pct` in the UI saves correctly but has no effect on trading while
     auto_slots is on; the levers that actually move risk per trade are
     `demo.heat_cap_pct` and `demo.max_slots`. Set `demo.auto_slots: false` to make
     `risk_pct` authoritative. Needs `config/` added
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
  5. **Fixed 2026-08-22 — TP1 partials had NEVER fired, on either venue, for the
     life of the project.** `demo._touched()` used `low <= level <= high`, the
     identical range-containment bug Round 11 fixed in `paper.exit_reason()` and left
     behind in this second copy. Once price moves cleanly past TP1 the candle's *low*
     sits above it, so the check reads False forever. It cut both ways: winners ran to
     TP2 at FULL size (flattering every result ever reported), and anything that
     reached TP1 then reversed gave back the whole gain instead of banking half — so
     the "risk-free" mechanic requested in Round 10 had never once executed. Found by
     noticing 5 of 13 closed trades exited `tp2` with `tp1_filled=0`; ADA held 46
     minutes and peaked at 1.95R, which is geometrically impossible for a long that
     genuinely crossed TP1. **Why the tests missed it:** test 1 set price *exactly* at
     tp1, where `high=low=tp1` makes range containment coincidentally true. A level
     test that only samples the level itself cannot distinguish a one-sided check from
     a range check — the same blind spot as Round 11.
  6. **Fixed 2026-08-22 — a lost update silently discarded a real entry fee.**
     `cycle()` read the balance, accumulated deltas in Python and wrote an absolute
     total, while `_open()` did its own read-modify-write to debit a fee. Nine market
     entries inside 11 seconds and one debit was clobbered, leaving the balance
     permanently 0.1488 higher than the trades justified. Every credit/debit now goes
     through `store.paper_adjust_balance()`, a single
     `UPDATE ... SET balance = balance + ?`. `paper_set_balance` remains only for a
     genuine reset. `open` events also record their amount now — they carried only the
     slot and score, so the ledger could not explain the balance, which is exactly why
     this took a full reconciliation to find.
  4. **Fixed, later the same day:** the `exit_reason()` range-containment bug
     (Round 11) and the pending-order same-coin double-entry bug (Round 13) — both
     detailed as bullets under "Live trading configuration" above rather than
     repeated here. Both were found the same way: checking live positions/trades
     against what the code should be doing, not from a report or metric alone —
     worth remembering as the pattern that keeps finding real bugs in this system.

## LIVE TRADING IS ON — real money on Tabdeal since 2026-08-22

**This is no longer a paper project.** The account is small (~5.2 USDT) but the orders
are real, the fills are real, and the losses are real. Read this whole section before
touching `agent/live.py`, `agent/tabdeal_broker.py`, or anything they call.

**The paper demo is switched off** (`demo.enabled: false`) at the user's request —
"demo trading is not important for me, focus on taking signals and opening positions
on Tabdeal". The scanner still runs and is what feeds the live engine; only the paper
management loop is stopped. Do not re-enable it without being asked.

### How it is armed, and how to stop it

| | |
|---|---|
| Arm / disarm | `demo.live_trading` in the server's `settings.json`. Read fresh every cycle — no restart needed either way. |
| Send nothing but log intent | `demo.live_dry_run: true` |
| **Kill switch** | `POST /api/live/flatten` — closes every venue position, works even if the engine loop is wedged |
| Watch it | `GET /api/live`, or `grep -iE 'live' var/logs/server.log` |

Three independent things must all be true before any write reaches Tabdeal: the arm
flag, an exact path+verb match in `guard.TABDEAL_WRITE_ALLOWLIST`, and `dry_run` off
(the constructor default is **on**). Disarmed, every write *raises* — never a silent
no-op, because a caller must not be able to believe it traded when it did not.

### Live configuration in force

`live_max_slots: 4`, `live_max_total_notional: 25.0`, `live_leverage: 5`,
`live_cycle_seconds: 20`, `time_stop_hours: 0.5`, `max_entry_drift_r: 0.3`.

**Sizing is anchored to total notional, not to capital × risk_pct.** Under cross
margin the binding constraint is the whole book: liquidation is assessed on total
notional, so that is the thing to control. $25 across 4 slots is ~$6.25 each, ~4.7x
the balance, keeping liquidation roughly 20% away. Setting the top-level `capital` to
the real 5.27 to size positions instead **broke signal generation outright** — the
planner could no longer fund any plan (84x leverage required against a 17.5x cap) and
all 33 coins went SKIP. Leave `capital` at 1000: it is the *planner's* number, and
plans only need it to be fundable — the levels they emit are what matter downstream.

### Who owns what — the safety split

    the EXCHANGE owns both levels  — stop loss AND TP1 go onto the position itself via
                                     positionSlTp, so a stop-out or a target is
                                     honoured even if this process dies
    the ENGINE owns the judgement  — which signal to take, signal_exit, time_stop

A monitoring loop is a fine place for "should I still be in this trade" and a terrible
place for "am I about to lose more than I planned". **The stop must never depend on
`live.py` running.** Always verify a stop landed by reading `stopLossPrice` back from
the venue — the first two live positions opened *unprotected* because the attach
silently failed and only the log line showed it.

### Closing conditions (final, after the 2026-08-22 revisions)

Exchange-enforced: **stop loss**, **TP1** (a full close — there is no TP2 and no
partial), **liquidation**. Engine-enforced: **`signal_exit`** (in profit, setup no
longer TAKE), **`time_stop`** (≥30 min with PnL between 0 and +0.5R; a loser is never
time-stopped, it rides its exchange stop), **`exchange_exit`** (reconcile found it
gone; the real fill is read back from the venue).

**TP1 closes outright.** An earlier revision had it moving the stop up to TP1 and
letting the trade run — that was a misreading and was corrected. The TP1 *partial* is
also gone: Tabdeal supports neither `reduceOnly` nor a partial close, so a half-exit
had to be an opposing MARKET order that could **flip** the position rather than trim
it. Removing it removed that entire failure class.

### Venue quirks that cost real time to find

- **`positionRisk` is close to useless here.** It carries no position id at all
  (`positionSlTp` requires one, so no stop can ever be attached from it) and returns
  `markPrice`, `unRealizedProfit` and `liquidationPrice` as the string `"0"` on a live
  position. **Use `/r/fapi/v1/position`**: it has `id`, the real `entryPrice`, and
  `stopLossPrice`/`takeProfitPrice` so stop attachment is verifiable. `positionAmt`
  there is unsigned with a separate LONG/SHORT `side`. Compute mark and PnL yourself.
- **Leverage must be POSTed per symbol before that symbol can trade.** An
  unconfigured market answers `500 {"code":1300,"msg":"TraderMarketConfig matching
  query does not exist"}`. `POST /fapi/v1/leverage` creates it.
- **`availableBalance` is always `"0.00000000"`** even with a funded wallet and no
  positions. It is simply not populated — it does **not** block trading. Proven by
  placing real orders against it.
- **There is no meaningful minimum notional** — a $0.48 order was accepted.
- **A price band is enforced.** Orders ~30% from market are rejected with
  `code 1209 "قیمت سفارش نامعتبر است"`. Within ~2% is fine.
- **`orderbook` requires `limit >= 5`** (`code 1201`).
- **`userTrades` has no `realizedPnl` field** — only price/qty/commission/time/
  buyer/maker. Gross comes from the *position* record; fees from the fills.
- **`/r/fapi/v3/account` currently 500s** with `name 'market' is not defined` — a
  Python NameError leaking out of their server. Not ours; use `/balance`.
- Writes are **never retried**. A timeout may mean the order landed, and a blind retry
  turns one position into two. Reconcile against the venue instead.

### Bugs found by running it live — and the pattern behind them

Every one of these looked healthy from the outside. Six in one session:

1. **No exchange stop attached** — `positionRisk` has no `positionId`, so the first two
   real positions ran unprotected. Fixed by switching endpoints; *verify by reading the
   stop back*.
2. **Dry runs wrote phantom `live_positions` rows** with no order id. `reconcile()`
   then correctly saw them missing and logged "closed by the exchange", churning the
   record with trades that never happened.
3. **The planner emitted an inverted plan** and scored it 71.2: BNB long, entry
   700.281, stop 712.834 *above* it, tp1 equal to the stop — from "structural (behind
   swing + 0.25 ATR)" when price had fallen through the swing low. `valid_geometry()`
   now requires `stop < entry < tp1 <= tp2` (mirror for short), enforced in
   `qualifying_signals` and again in `_enter`.
4. **`_profit_signal_check` raised `KeyError: 'exchange'`** on live rows (that column
   exists only on `paper_positions`). The exception aborted `_manage_one` *before* the
   time stop, so any live position **in profit got no management at all**.
5. **`UnboundLocalError` on `side`** in `_enter` — every entry threw, and `try_open`
   reported it as `"no_signal"`. The engine was structurally incapable of opening a
   position while looking merely idle.
6. **A concurrency race double-sized a position.** A manual `try_open` ran alongside
   the scheduler; both passed the "not held" check and both ordered, one second apart.
   Under cross margin they netted into one venue position of double size tracked by
   two DB rows, each recording the same close — the account was down 0.050144 while
   the DB claimed 0.075243. Entry is now behind a non-blocking lock, the book is
   re-read inside it, and `manage()` handles one row per symbol.
7. **Entry timer drifted against the scan cycle.** The engine attempted entry seconds
   before the scan that produced the signal completed, so a valid TAKE sat unacted on
   for five minutes. Entry now fires on a **new completed scan**, with
   `entry_interval_seconds` kept only as a floor.

**The pattern: `"no_signal"` must never be the answer to "something went wrong."**
Four of these hid behind a healthy-looking idle state. Log every attempt and its
outcome, not just successes, and distinguish "nothing to take" from "everything I
tried threw" (`all_entries_failed`).

### Accounting on real money

Record what the **venue** says, never our own estimate. `settle()` closes, then reads
the fill back and stores price and **net of commission**. Two ways this was wrong:
`userTrades` has no `realizedPnl` (so gross must come from the position record), and
filtering fills by our own `opened_ts` **excluded the entry fill** — it is written
after the order returns and lost a race with the fill's own timestamp, halving every
trade's recorded cost. Filter by the venue's `createdTime`.

At 0.1% a side on ~$6.25 notional, **a round trip costs ~$0.0124 whatever happens**.
Both of the first two closes were signal-exited before price moved enough to cover it,
so the fee *was* the entire result. The live record now reconciles to the account to
the cent (DB −0.050143 vs account −0.050144).

## 2026-08-23 — why the account was losing, and what changed (Rounds 15-17)

The account was down 0.544 USDT on 23 closed trades (−10%). Root-caused with the
live record and two replays, not from reports. **Read `docs/RESEARCH_LOG.md`
Rounds 15-17 before changing anything here.**

**The loss was an exit asymmetry, not the signals and not the ATR.**
Six `exchange_exit`s (the venue stop firing) were **89% of all loss**, at a median
hold of **548 minutes** on a 5-20 minute strategy. `_manage_one` re-checked a
*winner* every cycle and closed it once the setup lapsed; a *loser* was checked
against nothing (`_profit_signal_check` ran only in the in-profit branch, and the
time stop is guarded by `0 <= upnl`). Winners banked **+0.11R**, losers realised
**−1.0R** — about 1:9 against us. Fixed with `adverse_exit`.

**Tested and REJECTED, so it is not retried:** tightening the ATR timeframe. The
intuitive fix makes it worse — 1.5×ATR15m gives a 0.788% stop and 0.254R of cost,
1.5×ATR5m gives 0.403%/0.496R, 1.0×ATR5m gives 0.269%/0.744R, needing a 75-87%
win rate at 1:1. The scalp profile's 15m ATR choice is correct.

**Holding is now judged on the thesis, not the entry gates.** The exit test *was*
the entry test, so a position closed the moment it wouldn't be worth opening
again — AAVE left at 76.9→74.0 with the verdict still TAKE. Worse, `verdict` goes
SKIP on cost/spread/liquidity, which are questions about *opening*. A held
position now closes only on a direction flip or conviction below a hold floor
(`exit_score_margin`, default 10 points under the entry bar). Without this a
multi-hour hold is impossible.

**Signal scoring double-counted.** Measured pairwise agreement between the 9
direction checks: `price vs EMA200 (bias)` ↔ `EMA50 vs EMA200 (bias)` **90.7%**,
`price vs EMA50 (decision)` ↔ `price vs VWAP` **87.7%**. Checks now carry a
`family` and share one vote's weight via `skill.weigh_votes()`.
**Three calibration traps, all caught before they did damage** — worth knowing
because each would have silently stopped trading:
  1. Collapsing a family to its *majority* makes it abstain on internal
     disagreement; typical counts fell to 3-2 of 6 against a threshold of 4.
     Fractional weighting instead.
  2. `DIRECTION_MARGIN` was calibrated for integers out of 9; a weighted
     4.00-3.00 split is a margin of exactly 1.00, read as TIED. Both the vote
     threshold and the tie margin now rescale with the denominator.
  3. **`tabdeal.build_snapshot` re-counted the votes with plain integer sums after
     resolving the manual checks, throwing the weighting away.** Production scored
     unchanged while the unit tests passed, because they tested `score_direction`
     and the discarding happened one function later. Found only by reading a live
     stored snapshot. *Any change to scoring must be verified in a live snapshot,
     not in a test harness* — see also the 22L/7S figure below.

**Hold extended to 8 hours; shorts switched off.** From a replay of **21,315
signals** (33 coins, ~25 days, the real scoring code). Net of the 0.200% round
trip: longs go −0.172% at 30m → +0.086% at 4h → **+0.348% at 8h** → +1.392% at
24h, while **shorts are negative at every horizon and worsen with time** (−0.209%
→ −0.706%, n=9,741). `time_stop_hours: 8.0`, `allow_shorts: false` (a setting, not
a deletion — the window is one rising regime).

**A smaller replay misled on two of three conclusions.** An earlier 1,331-signal
run (8 coins) reported that ≥8-of-9 votes underperform (n=134, 41% win). At
n=1,461 score≥8 is the *best* bucket at every horizon. It also put 4h at +0.28%
when the large sample says −0.079%. Sample size is part of the evidence.

**Corrected claim, for the record:** a "22 long / 7 short" watchlist result quoted
during this work came from a test harness feeding **15m** decision candles; the
scalp profile uses **5m**. Production still scores broadly long because the market
is broadly long.

**Also fixed:** entry filled only one slot per scan (`_try_open_locked` returned on
its first fill) and the scheduler required both a fresh scan *and*
`entry_interval_seconds`, so it skipped whole scans. That is why an XRP at 80.8
sat unacted on with three slots free.

**Still open, and bigger than any of these fixes.** The edge is close to the fee.
At 0.1% a side the round trip is 0.2% of notional, and the filter only clears it
at an 8h+ hold. Nothing here manufactures edge; it removes unforced losses. Judge
the 8h long-only configuration on its own sample before adding size.

## 2026-08-23 (later) — exits restricted, history recorded, UI rebuilt

**The live engine now takes exactly ONE exit, at the operator's instruction:**
held **>= 1 hour** AND **net profitable after the round trip**
(`profit_close_after_h: 1.0`). Everything else belongs to the exchange's own stop
and take-profit, which sit on the position at the venue and survive this process
dying. Three exits were removed to make that true:
- `signal_exit` banked a winner as soon as profit cleared the fee — median hold
  **11 minutes** for +0.230% gross (~+0.11R) while losers ran the full −1.0R.
- `time_stop` fired on `0 <= upnl < floor`, a range that includes a position in
  profit but *below* the round trip, so it booked a certain loss.
- `adverse_exit` is **off** (`adverse_exit_enabled: false`) — "do not touch
  positions in loss". Kept behind a flag, not deleted, because the measurement
  behind it stands (six stop-outs = 89% of all loss, median hold 548 min) and
  turning it off restores that exposure. **The case for switching it off is
  FLOKI:** entry 0.00002693 → exit 0.00002692, a one-tick −0.0353% move that
  realised −0.01297 because the 0.01094 round trip was **85% of the loss**.

**Position history is now recorded** — `live_samples` table, written by the 3s
monitoring loop, thinned to one point per 15s, pruned after 14 days. Each row:
mark, gross and net unrealised, R, hold, and *the verdict and score the engine
was judging it against at that instant*. Closed trades store only endpoints,
which is why every post-mortem so far had to reconstruct the middle from exchange
candles — that shows what the market did, not what the engine saw.
Read it at `GET /api/live/history`.

**Two bugs in that work, both found on the server, not locally:**
1. `_record_sample` read `live.settings()["exchange"]` — that key is on
   `demo.settings()`. KeyError on *every* call, and the `except` was `log.debug`,
   so the table stayed empty and nothing said why. Identical to the earlier
   `_profit_signal_check` KeyError. It now warns once per process.
2. `/api/live` and `/api/live/history` were under `_api_post`, so the dashboard's
   GET got "unknown endpoint". Both are read-only and now sit in `_api_get`.

**A real position ran unprotected for 40 minutes.** SUI opened 17:07 local with
no exchange stop: `_attach_stop` reads the position back for its `positionId`,
the venue had not registered it yet, and the failure was logged once and
**abandoned** — even though reconcile learned the id seconds later. FLOKI, opened
one second earlier in the same batch, was fine. Filling several slots per scan
makes that race *more* likely. `reconcile()` now checks every open position for a
venue stop each cycle and re-attaches a missing one, reading it back before
believing it. `_venue_has_stop()` handles `None`, `""` and the string `"0"` —
Tabdeal reports an absent level all three ways depending on endpoint.

**Web UI:** the demo tab is replaced by a live Tabdeal positions tab (5s refresh)
with one line per position on a shared chart. The chart plots **percent from each
position's own entry**, not raw price — FLOKI trades near 0.000027 and BTC near
77,000, so on a shared price axis every line but the largest collapses onto the
baseline — and shades the 0.2% round-trip band. Header stripped to controls that
do something here: the exchange selector (venue is fixed to Tabdeal and it only
listed Toobit/Nobitex), Capital (planner-only; setting it to the real balance
breaks signal generation outright) and Risk % (ignored while `demo.auto_slots` is
on) are gone. The footer claimed "Read-only. This tool never places orders",
which stopped being true when real money went live.

## Why the balance fell — the decomposition, and the fix (2026-08-23)

**Reconciled 41 closed trades against the venue: realised −0.662, of which
−0.467 is round-trip FEES and only −0.195 is price.** Fees were **71% of the
entire loss** — ~9% of a 5 USDT account paid in commission, because the engine
took 41 trades in two days at 0.2% each. Trade *frequency* was the problem, not
direction. By reason: `exchange_exit` −0.412 (n=7), `adverse_exit` −0.261 (n=7),
`time_stop` −0.040 (n=4), `signal_exit` **+0.050 across 21 trades**.

**An engine close must never reduce the balance.** PEPE closed as `profit_close`
for **−0.00039**: the rule required gross > the round trip but measured gross at
the **mark**, while the close is a **MARKET** order — the fill crossed the spread
and landed under the bar it had just cleared (+0.01052 gross vs a 0.01091 round
trip). The bar is now `round_trip × profit_close_fee_multiple` (**1.5**), leaving
~0.1% of notional as slippage headroom against measured spreads of 0.05–0.13%.
A cushion is not a proof, so a close that still settles ≤ 0 logs an **ERROR**
naming the setting to raise — PEPE's −0.00039 vanished into the ledger and was
only found by reconciling 41 trades by hand.

**First result under the new rules (verified live):** 3 closes, net **+0.08487**,
wallet 4.61662 → 4.70182. FLOKI **+1.288%** and SUI **+0.703%** were both closed
by the *exchange take-profit*, not by the engine — the same FLOKI the old
`adverse_exit` had closed at −0.013R earlier the same day, and roughly 10× what
the old `signal_exit` banked (~+0.11R). Letting the venue's TP run the winner is
where the profit came from. SHIB, held exactly 1.00h and in loss, was correctly
left untouched.

## Live engine rules as they now stand (2026-08-23, authoritative)

**Exits — the engine takes exactly ONE, and never touches a loser:**

| | |
|---|---|
| Engine closes | held **≥ 1h** AND gross **> 1.5 × round trip** AND the signal is no longer green (`profit_close`) |
| Engine **keeps** a winner | past the hour and above the bar, but the scan still says **TAKE at ≥ `hold_take_score` (70)** on our side — logged as `riding_signal` |
| Everything else | the exchange's own **stop** and **TP1**, attached to the position |
| A losing position | never touched by the engine, at any hold |

`signal_exit`, `time_stop` and `adverse_exit` are all gone from the live path
(`adverse_exit` survives behind `adverse_exit_enabled: false`). Settings:
`profit_close_after_h: 1.0`, `profit_close_fee_multiple: 1.5`, `hold_take_score: 70`,
`live_cycle_seconds: 3`, `allow_shorts: false`, `min_score: 75`.

**The 1.5× cushion is not decoration.** The profit test reads the *mark*; the close
is a **MARKET** order that crosses the spread. At 1.0× a close settled **negative**
(PEPE: +0.01052 gross vs a 0.01091 round trip → −0.00039). A close that still
settles ≤ 0 now logs an ERROR naming the setting to raise.

**The hold bar (70) is deliberately above the abandon floor (65).** They answer
different questions: the floor asks "is the thesis dead" and is right for *leaving*
a trade; banking one needs a positive, current reason, because a position can sit
well above the floor and still be a fading signal. Missing scan data does **not**
hold — with profit already clear of the round trip, banking is the safe side of
that uncertainty. Evidence: the only profitable closes this account has had were
FLOKI **+1.288%** and SUI **+0.703%**, both reaching the exchange TP precisely
because nothing cut them at the hour.

**Position history is recorded** in `live_samples` (mark, gross/net unrealised, R,
hold, and the verdict+score being judged against), written by the 3s loop, thinned
to 15s, pruned after 14 days. `GET /api/live/history`.

**`reconcile()` repairs a missing exchange stop every cycle.** SUI ran unprotected
for 40 minutes because `_attach_stop` read the position back before the venue had
registered it, logged the failure once, and never retried. `_venue_has_stop()`
treats `None`, `""` and the string `"0"` as absent — Tabdeal reports all three.

**Web UI** (`live` tab, 3s refresh): BTCUSDT in the header (cached 5s, served from
both `/api/state` and `/api/live`); open positions with a **net** P/L column and a
combined SL/TP column; closed positions with totals above the list; the chart last.
The P/L column is net of the round trip on purpose — nine of the first 23 trades
moved in our favour and still lost money, and a gross column would call them wins.

**Skill sync:** `skill/SKILL.md` Step 8 now carries a "What the live Tabdeal engine
actually does" table, because the generic rules above it (TP1 50% partial, stop
locked at TP1, time stop at 6 candles) are **not** what runs. Tabdeal supports
neither `reduceOnly` nor a partial close, so TP1 is a full close and there is no
TP2. Keep that table in step with `agent/live.py`.

## Websocket price feed, and what Tabdeal actually publishes (2026-08-23)

**Marks now come from Tabdeal's pushed order book.** `agent/tabdeal_ws.py` keeps one
socket to `wss://api1.tabdeal.org/stream/`, subscribes to the *open* symbols only,
and `tabdeal.mark_price()` uses the pushed mid when it is fresher than 8s, falling
back to REST otherwise. That removes ~80 REST calls a minute from the 3s loop.
Verified live: 4 symbols subscribed, prices 0.1-1.6s old.

**Exactly one stream exists.** Probed across both hosts and both sockets:
`<sym>@depth@2000ms` on `api1` works and pushes a **full snapshot** (100 bids + 100
asks, best first). Everything else is refused with an explicit `INVALID_FORMAT`:
`trade`, `aggTrade`, `trades`, `deal`, `matches`, `kline`, `candle`, `ticker`,
`miniTicker`, `bookTicker`, `markPrice`, `openInterest`.
`wss://ws.tabdeal.org/special_margin/stream/` connects and accepts **nothing**.
REST `trades`/`aggTrades`/`historicalTrades`/`ticker/24hr`/`klines`/`openInterest`
all 404 on both hosts.

**Consequences, both of which close old questions:**
- **There is no "websocket only" configuration.** Candles — and therefore every
  indicator — stay on REST, and account/position/order calls stay on signed REST.
- **Futures volume is confirmed unobtainable.** No trade feed, no futures klines, no
  ticker. The "volume bias" direction check (1 of 9) will keep measuring the *spot*
  book. This is now verified across two hosts, two sockets and ten REST paths, not
  inferred from one probe.

The socket's symbol convention is the **inverse** of REST's: REST needs the
underscore (`BTC_USDT`), the socket rejects it (`btcusdt@depth@2000ms`).

The feed computes the bid/ask **mid** deliberately — the same quantity REST
`mark_price()` returned — so changing transport did not shift every P&L figure. The
frames also carry `p` (tracks a mark) and `f`/`f_bid`/`f_ask` (a fair-price family);
adopting one would change what "mark" *means*, which is a separate decision.

### Deployment gotcha: the service runs from a virtualenv

**`websocket-client` must be installed into `/opt/crypto-screener/.venv`, not system
python.** `run.sh` starts `/opt/crypto-screener/.venv/bin/python -m agent.server`.
A system-wide `pip3 install` is invisible to it, and the failure is confusing:
`import websocket` succeeds as the `screener` user, from the service's cwd, and
inside a `systemd-run` sandbox with the same properties — while the live process
still says `ModuleNotFoundError`. `/proc/<pid>/exe` says `/usr/bin/python3.12`
because the venv python is a symlink, so that check *actively misleads*. Check
`/proc/<pid>/cmdline` instead.

The app otherwise stays **stdlib-only**; this is the one optional accelerator, and
without it the feed reports itself unavailable and every mark falls back to REST.

## Real-money migration — the dossier that preceded going live

The user has asked for everything to be prepared so a "migrate to real trading"
instruction can be acted on. **That dossier is the collected result** — the complete
verified execution API (order/close/SL-TP/leverage/transfer, with exact parameters,
from Tabdeal's official Postman collection), the two mechanic gaps with no clean
native solution, what code must be built, a pre-flight checklist, and the gate.
**Nothing has been executed and nothing is enabled**; the codebase is still
structurally read-only. Do not start building or opening `guard.py` without the user
explicitly asking — and when they do, surface §2 (TP1 partial has no safe primitive;
cross-margin liquidation unmodelled) and §5 (the gate is not met) before writing code.

## Tabdeal — LIVE as the demo's sole venue since 2026-08-22

**The cutover is done.** The screener and demo now run entirely on Tabdeal data:
`exchange: "tabdeal"` (in `config/strategy-tuning.json` and the server's
`settings.json`), watchlist = all 33 Tabdeal futures symbols, demo account reset to
1000 USDT / 0 positions and tagged `tabdeal`. Verified end to end: scan 597 read 33
Tabdeal symbols, and the demo placed and then correctly cancelled a real maker order
(`LTC_USDT`, limit 52.08, unfilled after 2m).

**Trap found during the cutover, worth remembering:** `demo.settings()` resolves
`demo.get("exchange") or s.get("exchange") or "toobit"` — there was a **`demo.exchange`
override** in `settings.json` still set to `"toobit"` after the top-level was switched.
Left alone it would have had the demo reading Toobit scan results while the scanner
wrote Tabdeal ones: **zero signals, no error, nothing in the logs.** Fixed by deleting
`demo.exchange` entirely so it inherits the top-level key — one venue key is harder to
leave half-switched than two that must agree. Check for this pattern before assuming a
venue switch is complete.

**The headline result: TAKEs collapsed from ~15/32 on Toobit to 1/33 on Tabdeal.**
This is the gates working, not a bug — verified by reading the failure reasons rather
than assuming. Across scan 597's 33 coins: **16 fail on fees** (7 "cost efficiency,
costs are 0.36R vs max 0.25R" + 9 "plan blocker: cost filter failed"), 4 fail
**spread** (Tabdeal's books are thinner — ATOM 0.128% vs the 0.1% max; WIF scored 73.1
with +0.151R expectancy and was still correctly SKIPped on spread alone), 4 fail
**volatility fit** (PAXG/XAUT are gold-backed and too quiet — ATR 0.16% vs the 0.3%
floor, exactly as flagged in `coins.txt`), 2 **liquidity depth**, 2 **liquidation
buffer**. Direction scoring is fully healthy on this venue: "9/9 automated checks
favour long, all checks resolved from live data" — the missing funding rate does not
reduce coverage, because scalp skips that check anyway.

**What this means:** the current scalp configuration barely functions on Tabdeal. That
is the honest answer the cost gate was built to give, and it matches the repricing
analysis below — a 0.2% round trip needs **fewer, larger-R trades**, i.e. something
closer to the `intraday` profile, not 25 scalps an hour. Changing the profile is the
obvious next experiment and has **not** been done yet.

**Open question raised by the first order:** `maker_entry` is now of dubious value
here. Tabdeal charges 0.1% maker *and* taker, so resting a limit saves nothing on
fees; all it buys is the 0.1% price improvement, paid for with a 2-minute fill window
that the very first order missed. Worth measuring before keeping or dropping it.

### Round 14 (2026-08-22) — stale signals silently rewrote trade geometry

Found by auditing the first Tabdeal closed trades against real 1m candles rather than
trusting the recorded exits. **A plan's `stop`/`tp1`/`tp2` are anchored to the entry
price at *scan* time, but `_proposal()` fills at the *current* mark and never
re-anchors them.** When the market moves in between, the trade's R:R is rewritten
without any error or warning.

FLOKI: filled 3.32% above a plan entry whose stop was 1.75% away — **1.83R of drift
before the position opened**. TP1 then sat *below* the entry (already passed) and TP2
0.19R away against a stop 2.83R below: risking 2.83R for 0.19R on what the plan
called a 2R target. LINK opened at 0.48R of drift (1.48R risk, 0.52R target).

Fixed with a `stale_signal` decline in `try_fill_slots`, threshold
`demo.max_entry_drift_r` (default **0.3R**), computed by `_entry_drift_r()`. Drift is
signed against the position, so a fill *better* than the plan entry is negative and
never blocked. Against the first 16 real entries it rejects exactly the two broken
ones and allows every well-formed one (all ≤0.25R). Rejecting beats re-anchoring: the
stop came from structure and ATR observed at the old price, so sliding it to the new
one would invent a level the planner never validated. Tests 9e; 53/53 passing.

This is the "signal max-age ceiling" that has sat in the Round 2 candidate queue
unimplemented — it turned out to matter far more than "low-risk" suggested, because
the damage is to geometry, not freshness.

**Verification done at the same time, all clean:** exit reasons reconcile against
real 1m candles (FLOKI's tp2 genuinely traded; the three `signal_exit`s touched
neither stop nor tp2, and every exit price sits inside its candle range); per-trade
P&L is exact (`gross − exit_fee + realised_partial == realised_pnl` on all six);
fees are 0.1% both sides as expected; and the account reconciles **to the cent** —
`1000 + 8.9559 realised − 2.4201 entry fees = 1006.5358`, matching the stored
balance exactly. **Still unverified live on Tabdeal:** the TP1 partial + stop-lock
path (Round 10), because no trade has reached TP1 yet — it is covered by tests 1/2
but has no live Tabdeal evidence.

### Original investigation (2026-08-21) — kept for the reasoning trail

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
1. ~~No candle data anywhere on Tabdeal.~~ **WRONG — RESOLVED 2026-08-22. Tabdeal
   has full OHLCV history; it is just on a different host.** The earlier conclusion
   was drawn from probing `api1.tabdeal.org` (`/fapi/*`, `/api/*`) and the
   `wss://api1…/stream/` websocket, all of which genuinely lack klines. The web
   charts are fed by a **separate host** found in the Nuxt config on
   `tabdeal.org/special-margin` (`BROWSER_BASE_URL: "https://api-web.tabdeal.org"`):

       GET https://api-web.tabdeal.org/plots/history
           ?symbol=BTC_USDT&resolution=15&from=<unix_s>&to=<unix_s>
       -> {"data":[{"time":1787344200,"low":…,"high":…,"open":…,"close":…,
                    "volume":…}, …]}          # empty is {"data":[],"no_data":true}

   Verified live: **symbol must be underscore form** (`BTC_USDT`; `BTCUSDT` returns
   `no_data`), `time` is unix **seconds**, and `/r/plots/history` is the identical
   read replica. Resolutions that work: `1, 5, 15, 30, 60, 120, 240, 360, 720, 1D`
   (`3`, `D`, `W`, `1W`, `1M` return empty). History depth is at least **90 days on
   15m (8640 bars)** — far beyond the ~200 bars EMA200 needs. **All 33 futures
   symbols return full candles**, GRAM included.
   Prices track the futures book closely: BTC 15m close 77,489.6 vs `fapi` depth mid
   ~77,432 at the same moment (**0.07%**), so these candles are a fair basis for
   signals on the perp. Other useful config from that same Nuxt block:
   `wss://ws.tabdeal.org/special_margin/{broadcast,stream}/`, `…/prices/`,
   `…/broadcast/`, `…/stream/`, plus `apollo.tabdeal.org` (GraphQL) and
   `cms.tabdeal.org`.

   **Data provenance, verified 2026-08-22 — read this before trusting a volume
   signal.** `plots/history` is Tabdeal's *general* chart API, not a futures one: it
   serves spot-only pairs (`BTC_IRT`, `USDT_IRT`, `ETH_IRT` all return bars and none
   have a futures market), and no futures-specific chart path exists —
   `market_type=special_margin`, `type=futures` and `/special_margin/plots/...` are
   all ignored or 404. So **the candles are the SPOT series**. Measured basis against
   both books: chart close sits within **0.02-0.17%** of spot mid *and* futures mid
   (BTC -0.016%/+0.020%, SUI -0.018%/-0.030%, DOGE +0.126%/+0.094%), so every
   price-based indicator — EMA, ATR, RSI, Ichimoku, VWAP, structure — is effectively
   identical either way, and so are the ATR-derived stops. **The one input that is
   genuinely wrong is volume**: `plots/history` returns spot volume, so the "volume
   bias (last 10)" direction check (1 of 9) measures the wrong book. Unfixable —
   the venue publishes no futures volume anywhere. Everything else the screener uses
   *is* real اهرم حرفه‌ای data: symbol universe and precision from
   `/r/fapi/v1/exchangeInfo`, order book / spread / liquidity-depth gate from
   `/r/fapi/v1/depth`, and all execution from `/fapi/v1/*`.

   **Lesson worth keeping:** "the endpoint doesn't exist" was wrong because only one
   host was probed. When an exchange's web UI visibly renders a chart, the data
   exists somewhere — read the front-end's own config for its base URLs before
   concluding a capability is missing.
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
4. **RESOLVED 2026-08-22, and it is the decisive finding: Tabdeal's fee is 0.1% on
   BOTH maker and taker, and it breaks the current strategy.** Source:
   `https://tabdeal.org/commissions` (fetchable from the server, not from the Mac —
   see note below), section «کارمزد اهرم‌ حرفه‌ای»: all four VIP levels show taker
   `0.001` / maker `0.001`, with «کارمزد فعلی اهرم حرفه‌ای: 0.001». Confirmed as
   0.1% (not 0.001%) by the worked example on `/special-margin`, which says «با فرض
   کارمزد 0.1%». **There is no maker discount at all** — the demo's maker-entry
   optimisation buys nothing here.
   - Toobit effective round trip today: 0.02% maker in + 0.06% taker out = 0.08%.
     Tabdeal: 0.1% + 0.1% = **0.2%, i.e. 2.5x**.
   - Repriced the 374 Tabdeal-tradeable closed trades by reconstructing each trade's
     notional from the fee actually charged, then re-charging at 0.1%/0.1%:
     **expectancy +0.1387R → +0.0455R (67% of the edge consumed), win rate
     63.1% → 50.3%**, cost per trade 0.062R → 0.155R ($0.187 → $0.466 on a $3 R).
   - Per-block after repricing: `+0.166 / −0.141 / +0.142 / +0.209 / −0.139` — **two
     of five blocks negative, including the most recent.** That is not a thin edge,
     it is noise around breakeven.
   - **The 0.1% is explicitly a temporary promotional rate** («به مناسبت معرفی محصول
     اهرم حرفه‌ای، موقتا…»), so the real long-run rate is unknown and can only go up.
   - **Implication:** the scalp profile (Round 4+, ~25 trades/hour, 1R/2R targets,
     ~30-min holds) is precisely the wrong shape for a 0.2% round trip. This is the
     cost-drag mechanism already documented in `skill/references/risk-math.md` §6 —
     fees are charged on notional, so a high-frequency small-R strategy pays them
     over and over. Making Tabdeal viable means **fewer, larger-R trades** (back
     toward the `intraday` profile the system used before Round 4), not a config
     tweak. Do not migrate the scalp configuration as-is.
5. **`reduceOnly` is documented as "فعلا پشتیبانی نمی شود" (not currently
   supported).** Without it there is no guaranteed-safe way to close: an oversized
   close can flip into an opposite position rather than flatten.
6. **No close-position endpoint found.** Docs reference `POST /fapi/v1/positionClose`
   but that path 404s, as do five other candidates tried. Either it's undocumented
   under another name or closing happens only via an opposing order (which, with no
   `reduceOnly`, is exactly the risk in blocker 5). Must be resolved.

**Product mechanics, from `https://tabdeal.org/special-margin` (2026-08-22):**
- **Margin is CROSS, not isolated** — «معامله با اهرم حرفه‌ای صرافی تبدیل به‌صورت
  کراس انجام می‌شود». The whole اهرم-حرفه‌ای wallet backs every position. Tabdeal
  states the consequence plainly: «ممکن است یک پوزیشن، تمام اکانت را لیکوئید کند»
  — one position can liquidate the entire account, and positions *in profit* get
  closed too. **`agent/paper.py` explicitly assumes isolated margin** ("Assumes
  isolated margin" in its exchange notes, and `liquidation_price()` solves the
  isolated formula per position). The demo runs 10-20 concurrent positions sharing
  one pool under cross — the 6% portfolio-heat cap was designed for isolated, where
  each position's loss is bounded by its own margin. **The entire liquidation and
  heat model has to be rewritten for cross margin**, not merely re-parameterised.
- **Maintenance margin is a flat 0.5% of position value** — no tier ladder. Simpler
  than Toobit's 9-tier `riskLimits`, and it means liquidation *is* computable
  locally after all, which partly offsets the missing bracket data noted below.
  Tabdeal's own worked example: $100 balance, 10x, $1000 position → maintenance
  $5 → liquidated once loss reaches $95.
- **Max leverage 100x** (selectable 1–100).
- **Funding rate: not mentioned anywhere on the product page** — unknown whether
  this product charges funding/interest at all. Must be confirmed; it is a direct
  input to the cost model.

**Remaining gaps (not blockers, but real):**
- **Network access asymmetry:** `tabdeal.org` (bare) does not resolve from the
  operator's Mac; `www.tabdeal.org` resolves but redirects to the bare host and
  fails. The **server reaches both fine** — fetch Tabdeal web pages from
  94.74.166.123, not locally. `api1.tabdeal.org` resolves and works from both.
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

1. **Live trading is the focus now, not the demo.** Watch `var/logs/server.log` for
   `live entry attempt` / `live: OPENED` / `live reconcile` lines, and reconcile the
   `live_positions` table against the venue periodically — the DB and the account
   agreed to the cent as of 2026-08-22 and should stay that way. The unresolved
   economic question is unchanged and is the one that matters: at 0.1% a side a round
   trip costs ~0.2% of notional, so the direction filter has to clear **0.2% per
   30-minute hold** for any of this to be profitable. Both of the first two closes were
   signal-exited before price moved enough to cover the fee. Measure that edge before
   adding size.
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
- **Real money is live on Tabdeal.** `agent/tabdeal_broker.py` is the only module that
  can move it, and `agent/live.py` is the only thing that drives it. Both are gated by
  `demo.live_trading` + `guard.TABDEAL_WRITE_ALLOWLIST` + `dry_run`. Kill switch:
  `POST /api/live/flatten`. Treat any change near these with the care that implies.
- **A level check must be one-sided, never `low <= level <= high`.** This exact bug has
  now been found and fixed **three separate times** in three places —
  `paper.exit_reason()` (Round 11), `demo._touched()` (2026-08-22, which silently
  disabled every TP1 partial the project ever had), and it is the shape to check first
  whenever a stop or target is reported as "not working". A test that samples the level
  *exactly* cannot tell a one-sided check from a range check, so always drive price
  clearly past it.
- **Never do read-modify-write on money in Python.** `UPDATE ... SET x = x + ?` in one
  statement. A lost update cost a real entry fee on 2026-08-20 and was invisible until
  a full reconciliation, because the event ledger did not record amounts either.
- **"no signal" must never be how a failure surfaces.** Four separate live bugs hid
  behind an idle-looking engine that only spoke when it succeeded. Log every attempt
  and its outcome, and distinguish "nothing qualified" from "everything I tried threw".
- **The venue is the source of truth, always.** `reconcile()` reads `positionRisk`
  every cycle and believes it over our own records — that is the only reason the phantom
  dry-run rows and the double-sized position were visible at all. Never let a local
  record override what the exchange says is open.
- **Verify a stop landed by reading it back.** `positionSlTp` returning
  `{"msg":"success"}` is not proof; `stopLossPrice` on `/fapi/v1/position` is. The first
  two live positions ran unprotected because the attach failed silently.
- A resting maker limit order (`status='pending'` in `paper_positions`) carries the
  same committed risk as a filled one (`margin`/`risk_amount`/stop/targets are all
  set at placement, not at fill) but is easy to leave out of a guard that only reads
  `store.paper_open_positions()` (`status='open'` only) — that gap let the same coin
  be entered twice at once before Round 13 fixed `qualifying_signals()`'s
  `open_coins` and `correlated_same_side()`'s cap check to union open + pending.
  Any *new* guard or capacity check written against open positions should ask
  whether a still-pending order needs to count too.
