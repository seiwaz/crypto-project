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
2. If `agent/` changed: compile-checks every `agent/*.py` first
   (`python3 -m py_compile`). A syntax error aborts the deploy — the live code is left
   untouched and that commit is skipped, logged to
   `/opt/crypto-screener-deploy/deploy.log` — so a bad push costs a cycle, not an
   outage. Only on a clean compile does it rsync `agent/` and `skill/` into
   `/opt/crypto-screener/` and `systemctl restart crypto-screener`.
3. Always applies `config/strategy-tuning.json` as a deep-merge patch onto the live
   `config/settings.json` via `config.save_settings()` — this needs no restart, so it's
   the preferred way to tune an existing parameter (see `agent/demo.py`'s `settings()`
   for the full key list: `atr_mult` top-level; `correlation_threshold`,
   `heat_cap_pct`, `time_stop_hours`, `max_correlated_same_side`, `counter_trend_gate`,
   `give_back_*`, `maker_*`, `entry_interval_seconds`, `trend_filter_interval`,
   `correlation_interval` all under `"demo"`). Leave the file `{}` when there's nothing
   to override — it's a patch, not a replacement.

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

## Current state (updated 2026-08-19 — keep this section current, don't let it rot)

- **Demo account:** reset 2026-08-17, 1000 USDT starting capital. As of 2026-08-19:
  **1 of 30 minimum-sample trades closed** (win_rate 0%, expectancy −0.12R on n=1 — not
  remotely meaningful yet; check `/api/demo/report` for the live number before saying
  anything about performance). This line is kept current automatically by the cloud
  routine every ~6h.
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
- **Email notifications (requested 2026-08-19, not yet wired up):** the user wants an
  email to seiwaz@gmail.com whenever a run produces something real — a checkpoint
  logged, a change applied, or a change reverted — not on plain no-op runs. This needs
  a Gmail MCP connector attached to the routine's `mcp_connections`
  (`https://claude.ai/customize/connectors`); as of this writing no such connector was
  available, so it's pending the user connecting one. Once attached, add a final step
  to the routine's prompt that sends the run's summary by email, gated on the same
  "something happened" condition already used for git commits.

## What needs to continue (pick this up without being re-asked)

1. Keep letting the demo account run; the autonomous routine may reset gates/params
   via `strategy-tuning.json` or code, but nothing should reset the *account itself*
   (capital, trade history) without a clear reason — that restarts the sample.
2. Wire up the Gmail notification once the connector is confirmed connected (see
   "Email notifications" above) — attach it to `trig_01D72wvtJHdgeYxyMGEcRPs7` via
   `mcp_connections` and add the send-email step to its prompt.
3. Round 3 research (human- or routine-driven): quant/published backtests, exchange
   liquidation/margin-call documentation, and a critical re-check of whether Round 1/2's
   conclusions hold up — see `docs/RESEARCH_LOG.md`'s "Round 3 — Expert validation
   (pending)" stub for the specific open flag (Nobitex fee-schedule constants in
   `trade_plan.py` were never re-verified against the exchange's current published
   rates).
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
  chase a higher win rate at the expense of expectancy.
- `agent/correlation.py` and `agent/reachability.py` are local stand-ins for the
  skill's `market_context.py` (not installed anywhere). If that file ever actually
  ships, delete the stand-ins rather than keeping both.
