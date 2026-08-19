# Nobitex API reference

Base URL: `https://apiv2.nobitex.ir`
Official docs: https://apidocs.nobitex.ir/

Everything here is used by `scripts/nobitex_api.py`. Read this before live API work or
when debugging an auth failure. ⚠️ marks anything that can change without notice —
confirm against the official docs when precision matters.

---

## Credential handling

**Two auth mechanisms exist. Nobitex signs with Ed25519, not HMAC** — a detail worth
knowing because every other exchange's example code will lead you astray.

### API key (recommended)

Created via the panel or `POST /apikeys/create`. Returns a public `key` and a
`privateKey` **shown exactly once**. Three headers on every authenticated request:

| Header | Value |
|---|---|
| `Nobitex-Key` | the public key |
| `Nobitex-Timestamp` | current Unix time in seconds, UTC |
| `Nobitex-Signature` | `base64(Ed25519(timestamp + method + url + body))` |

The signed payload is a plain string concatenation. `url` is the request **path
including the query string** — e.g. `/market/orders/list?fromId=123`. Signing the path
without the query is the single most common cause of a 401 here.

Permissions are `READ`, `TRADE`, `WITHDRAW`, and keys support an IP whitelist and an
expiry date.

**Tell the user to create a READ-only key.** Everything this skill does lives under
READ. A key that cannot trade cannot be misused to trade, whatever happens downstream —
that's defence in depth and costs them nothing. Adding the IP whitelist is a further
free win.

### Legacy token

`Authorization: Token <token>` from the panel settings page. Simple, but it carries
**full account access** — it can trade and withdraw. Prefer the API key. If the user
only has a token, say plainly that it's broader access than the task needs.

### Storage rules

- Environment variables (`NOBITEX_API_KEY`, `NOBITEX_API_SECRET`, `NOBITEX_TOKEN`) or
  a `chmod 600` JSON file. The client warns on loose file permissions.
- **Never** as command-line arguments — argv is visible in shell history and to any
  process listing.
- Never echo credentials into the conversation, a file, a log, or a commit. The client
  redacts them from its own error messages; hold to the same standard in prose.
- If a secret is ever exposed, the fix is to delete the key
  (`POST /apikeys/delete/<public_key>`) and issue a new one — the user does this in
  the panel, not through this skill.

### Ed25519 backends

`scripts/nobitex_ed25519.py` prefers `cryptography`, then PyNaCl, then a pure-Python
RFC 8032 implementation so nothing has to be installed. All three are checked against
the official RFC 8032 test vectors — run the module directly to verify:

```bash
python3 scripts/nobitex_ed25519.py
```

The `privateKey` arrives base64url-encoded; standard base64, hex, and 64-byte
`seed||public` blobs are also accepted, because a credential that fails to parse is a
miserable thing to debug.

---

## The read-only guard

`nobitex_api.py` refuses to call anything that could place, modify, close, or cancel
an order, or move funds. Two independent mechanisms:

1. **Allowlist** — the path must appear in `PRIVATE_ALLOWLIST` or match a public
   prefix.
2. **Forbidden substrings** — `orders/add`, `cancel`, `withdraw`, `/close`,
   `edit-collateral`, `convert`, `update-status`, `apikeys`, `transfer`, `login`,
   `logout`. These raise even if a path somehow reached the allowlist.

This is structural rather than advisory on purpose. An analysis tool that can also
trade is one bad instruction away from being a trading bot nobody authorised. If a
genuinely-read endpoint is missing, add it to `PRIVATE_ALLOWLIST` — never loosen the
forbidden list.

---

## Endpoints used

### Public — no credentials

| Endpoint | Purpose | Rate limit ⚠️ |
|---|---|---|
| `GET /market/udf/history` | OHLCV candles | — |
| `GET /v3/orderbook/{SYMBOL}` | Order book (`all` for every market) | 300/min |
| `GET /v2/depth/{SYMBOL}` | Aggregated depth | 300/min |
| `GET /v2/trades/{SYMBOL}` | Recent trades | 60/min |
| `GET /market/stats` | Day open/high/low/close, volume, `isClosed` | 20/min |

**Candles** — `GET /market/udf/history?symbol=BTCIRT&resolution=240&to=<unix>&countback=300`

Response is TradingView UDF format, parallel arrays rather than objects:

```json
{"s":"ok","t":[...],"o":[...],"h":[...],"l":[...],"c":[...],"v":[...]}
```

`"s":"no_data"` means the range is empty; `"s":"error"` carries `errmsg`. Maximum
**500 candles per request** — use `page` for more. Minute candles exist only from
Farvardin 1401 onward.

Resolutions: `1`, `5`, `15`, `30`, `60`, `180`, `240`, `360`, `720`, `D`, `2D`, `3D`.
**There is no weekly resolution** — the client aggregates `D` into weeks for the swing
profile's bias timeframe.

**Order book** — `bids` and `asks` are `[price, amount]` string pairs, plus
`lastTradePrice` and `lastUpdate`. Note that sub-second polling returns cached data, so
there's no point going faster than ~1s.

### Private — READ permission

| Endpoint | Purpose |
|---|---|
| `GET /users/profile` | Account level (which caps leverage) |
| `GET /users/limitations` | Account limits |
| `POST /users/wallets/list` | Balances |
| `GET /margin/fee-rates` | **Actual** margin fee tier — use instead of guessing |
| `GET /margin/v2/delegation-limit` | Which multipliers are available for a coin |
| `GET /positions/list` | Open margin positions |
| `GET /positions/active-count` | Position count |
| `GET /positions/{id}/status` | Single position detail |
| `GET /market/orders/list` | Open orders |

Two of these materially improve a plan: `margin/fee-rates` replaces an assumed fee with
the user's real one, and `positions/list` reveals correlated exposure the user may have
forgotten — portfolio heat is the sum of open risk, not the largest single position.

---

## Symbols and conventions

Candle and order-book symbols are concatenated: `BTCIRT`, `USDTIRT`, `ETHUSDT`.
`market/stats` instead takes separate `srcCurrency` / `dstCurrency`, and uses **`rls`**
where the symbol says `IRT`. The client handles the mapping.

**Toman markets quote in rials on some endpoints** ⚠️ — verify the magnitude against a
known price before trusting a level. An order of magnitude error in an entry price is
the kind of mistake that survives every other check in this skill.

Set `User-Agent: TraderBot/<name>` — Nobitex asks bots to identify themselves, and it
makes support conversations far easier.

---

## Rate limits and etiquette

The client serialises requests with a ~1.1s minimum gap, which sits comfortably inside
every documented limit. Don't remove it: sub-second polling returns cached data anyway,
so it buys nothing and risks a ban.

For continuous order-book tracking Nobitex recommends the WebSocket feed. This skill
does point-in-time analysis, so REST polling is the right tool.

---

## Command reference

```bash
python3 scripts/nobitex_api.py auth-check
python3 scripts/nobitex_api.py candles   --symbol BTCIRT --resolution 240 --count 300
python3 scripts/nobitex_api.py candles   --symbol BTCIRT --resolution 15 --csv btc15.csv
python3 scripts/nobitex_api.py orderbook --symbol ETHUSDT
python3 scripts/nobitex_api.py screen    --symbols BTCIRT,ETHIRT,SOLUSDT \
                                          --profile intraday --capital 10000
python3 scripts/nobitex_api.py snapshot  --symbol ETHUSDT --profile intraday \
                                          --out snap.json
python3 scripts/nobitex_api.py positions
python3 scripts/nobitex_api.py account   --symbol ETHUSDT
```

`snapshot` is the one that feeds the planner:

```bash
python3 scripts/trade_plan.py plan --snapshot snap.json --side long --capital 10000
```

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| 401 with an API key | Query string omitted from the signed URL, or clock skew — the timestamp must be current UTC seconds |
| 401 with a token | Token expired (30 days, or on logout); fetch a fresh one from the panel |
| 403 from an IP-whitelisted key | Request came from an address not on the list |
| `"s":"no_data"` | Range too old, wrong symbol, or minute candles before 1401 |
| Fewer candles than requested | 500-candle cap per request |
| `BLOCKED: ...` | The read-only guard did its job. Do not work around it |
| Prices look 10× off | Rial vs Toman on a Toman market |
