# Crypto Agents — project instructions and state

Read this fully before doing anything else in this repo. It exists so a session never
has to re-derive context that already exists — keep it current as things change rather
than letting it drift into a stale summary.

## What this is

A crypto futures signal/trading system with two parts:

1. **The skill** (`crypto-leverage-trade-plan`, lives at
   `~/.claude/skills/crypto-leverage-trade-plan/` on the operator's Mac, NOT in this
   repo) — decides whether a coin is worth trading and builds a risk-first plan:
   direction, entry, ATR-based stop, TP1/TP2, position size, leverage.
2. **This repo** (`agent/`) — a paper-trading ("demo") loop that takes the skill's
   TAKE signals and manages them as simulated Toobit positions: slots, portfolio heat,
   correlation/trend gates, circuit breaker, TP1 partials, trailing stops, time stops.

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

1. Commit and push to `origin/main` on GitHub.
2. Deploy to the server:
   - `agent/*.py` and other repo code → `/opt/crypto-screener/` (matches this repo's
     layout 1:1). Deploying code requires a service restart
     (`systemctl restart crypto-screener` as root) since it's a running Python process.
   - Skill files (`SKILL.md`, `references/*.md`, `scripts/*.py` under
     `~/.claude/skills/crypto-leverage-trade-plan/`) → `/opt/crypto-screener/skill/`
     on the server, same relative layout (`SKILL.md` at the root of that dir, not
     inside `references/`). Pure `.md` reference changes don't need a restart — they're
     read per-invocation. Script changes under `skill/scripts/` do, since `demo.py`
     imports `skill.compute_indicators` which shells out to them indirectly.
3. Verify: `systemctl is-active crypto-screener` and
   `curl -s http://94.74.166.123:8787/api/demo/report` (or `/api/health`) actually
   respond after the change.

**Exception — never sync these from local to server, ever:** `config/settings.json`
and `var/` are live server state (current tuning, the trade database, logs). They flow
the other way if at all — read them from the server to understand current live config,
never overwrite them with local defaults. `packaging/srv.sh sync` already knows this
and excludes them; if deploying by hand (scp), exclude them explicitly too.

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
  GETs, no credentials needed. Only reach for SSH when something needs to be *changed*
  on the server.
- Reaching the host from the Mac's browser/other tools may need a static route around
  a local VPN that otherwise intercepts the traffic:
  `sudo route -n add -host 94.74.166.123 192.168.3.1` (the user runs this, not Claude).
  Direct `ssh`/`curl`/`scp` from a Claude Code Bash session has worked without it.

### The GitHub repo is public

`seiwaz/crypto-project` is **not private**. Never commit credentials, API keys, or the
SSH password to any file in this repo, in any commit, ever — `.env`/`.env.local` are
gitignored for this reason and must stay that way. This applies even to files meant to
be temporary or "just for reference."

## Current state (updated 2026-08-19 — keep this section current, don't let it rot)

- **Demo account:** reset 2026-08-17, 1000 USDT starting capital, running fresh under
  the Round-1 fixes (see below). As of 2026-08-19: **1 of 30 minimum-sample trades
  closed** (win_rate 0%, expectancy −0.12R on n=1 — not remotely meaningful yet; check
  `/api/demo/report` for the live number before saying anything about performance).
  User wants a report at 10 closed trades.
- **Research:** Round 1 (retrospective — fixes already shipped in commits `459c994`
  through `a515b5d`) and Round 2 (public-source: correlation crash-asymmetry, trend
  quality vs. direction, RVOL threshold gap, event-risk/signal-freshness gaps) are done
  and written up in `docs/RESEARCH_LOG.md`, with the Round 2 doc changes already
  deployed to the live skill on the server. **Round 3 (expert/critical validation) has
  not been started.**
- **Held code changes** — researched, evidenced, NOT yet applied to `agent/demo.py`,
  deliberately, so they don't confound the in-flight 10-trade sample. See
  `docs/RESEARCH_LOG.md` → "Round 2 summary" table:
  - Market-stress correlation override (crash-tail correlation runs far above the
    calibrated 0.75 threshold)
  - Choppiness Index / Efficiency Ratio regime-quality gate alongside the existing
    EMA200 trend check
  - Automated RVOL ≥ 1.5 gate in `nobitex_api.py`'s scoring (currently computed but
    only advisory)
  - Signal max-age ceiling (low-risk, could ship independently — doesn't change which
    trades qualify)
- **Monitoring:** a cloud routine `crypto-demo-performance-check`
  (`trig_01D72wvtJHdgeYxyMGEcRPs7`) runs every 6 hours, checks `/api/demo/report` for
  20+ new closed trades since the last logged checkpoint, and appends to
  `docs/PERFORMANCE_LOG.md` when that threshold is hit. It only ever writes to that one
  file — it does not and must not edit strategy/code. `docs/PERFORMANCE_LOG.md` doesn't
  exist yet as of this writing; the routine creates it on first run.
- **Phase 4 (real-money connection) is explicitly gated**: only propose it after ≥100
  demo trades with stable positive expectancy across ≥3 consecutive evaluation periods,
  and never connect to a real exchange for automated execution without the user's
  explicit, separate approval — propose and wait, always.

## What needs to continue (pick this up without being re-asked)

1. Keep letting the demo account run; don't reset it without a clear reason (each
   reset restarts the sample and delays the checkpoint the user is waiting on).
2. When `docs/PERFORMANCE_LOG.md` gets its first real entry (20+ trades), read it
   against `docs/RESEARCH_LOG.md`'s held-changes table and decide with the user which
   held change to apply first — don't apply silently.
3. Round 3 research: quant/published backtests, exchange liquidation/margin-call
   documentation, and a critical re-check of whether Round 1/2's conclusions hold up —
   see `docs/RESEARCH_LOG.md`'s "Round 3 — Expert validation (pending)" stub for the
   specific open flag (Nobitex fee-schedule constants in `trade_plan.py` were never
   re-verified against the exchange's current published rates).
4. Every time SKILL.md, a reference doc, or `agent/*.py` changes: follow the sync rule
   above — commit+push to GitHub, deploy to the server, verify. Don't consider a change
   "done" until it's live on 94.74.166.123, since that's the only place it matters.

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
