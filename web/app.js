/* Crypto Screener — vanilla JS, no build step.
 *
 * Two rules this file holds to:
 *
 * 1. It computes nothing. Every figure rendered comes from the API, which serves the
 *    skill's own output verbatim. Where a value is missing, the UI says "no data" —
 *    it never derives, interpolates, or rounds a gap away.
 * 2. Numbers are formatted with Latin digits and wrapped in dir="ltr" in both
 *    languages. Bidi reordering of a price is a safety problem, not a cosmetic one.
 */

'use strict';

const API = {
  state:      () => fetchJSON(`/api/state?lang=${encodeURIComponent(state.lang)}`),
  series:     (coin) => fetchJSON(`/api/coin/${encodeURIComponent(coin)}/series`),
  coin:       (c) => fetchJSON(`/api/coin/${encodeURIComponent(c)}?lang=${encodeURIComponent(state.lang)}`),
  settings:   (patch) => fetchJSON('/api/settings', patch),
  scan:       (coins) => fetchJSON('/api/scan', coins ? { coins } : {}),
  cancel:     () => fetchJSON('/api/scan/cancel', {}),
  manual:     (body) => fetchJSON('/api/manual-check', body),
  commentary: (body) => fetchJSON('/api/commentary', body),
  reassess:   () => fetchJSON('/api/llm/reassess', {}),
  live:        () => fetchJSON('/api/live'),
  liveHistory: () => fetchJSON('/api/live/history'),
};

async function fetchJSON(url, body) {
  const opts = body
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
    : {};
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

/* ------------------------------------------------------------------ i18n */

const state = {
  lang: localStorage.getItem('lang') || 'en',
  strings: {},
  data: null,
  filter: 'ALL',
  charts: new Map(),
  openCoins: new Set(JSON.parse(localStorage.getItem('openCoins') || '[]')),
  pollTimer: null,
  /* 'demo' is the retired tab's id and may still be in localStorage from a previous
   * visit; without this the saved value matches no panel and the page opens blank. */
  tab: (localStorage.getItem('tab') === 'demo' ? 'live'
        : localStorage.getItem('tab')) || 'screener',
  live: null,
  liveHistory: null,
  liveTimer: null,
  liveInFlight: false,
};

async function loadStrings(lang) {
  const res = await fetch(`./i18n/${lang}.json`);
  state.strings = await res.json();
}

function t(key, vars) {
  let s = state.strings[key];
  if (s === undefined) return key;
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.replaceAll(`{${k}}`, v);
  return s;
}

/* Translate a string that came from the skill rather than from our own UI.
 *
 * The skill emits English for a small fixed vocabulary — gate names, score factors,
 * manual-check labels, the five verdict actions. Those are looked up under a `skill.`
 * namespace and fall through to the original when no translation exists, which is the
 * right default for the free-form detail strings that carry live numbers. */
function ts(text) {
  if (!text) return text;
  const key = `skill.${text.trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '')}`;
  const value = state.strings[key];
  return value === undefined ? text : value;
}

function applyDirection() {
  const rtl = state.lang === 'fa';
  document.documentElement.lang = state.lang;
  document.documentElement.dir = rtl ? 'rtl' : 'ltr';
  document.querySelectorAll('[data-i18n]').forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
}

/* ------------------------------------------------------- number formatting */

/* Always Latin digits, never Persian ones. A Farsi reader comparing a price to the
 * Nobitex order book should see the same glyphs in both places. */
const NUM_LOCALE = 'en-US';

function fmtNum(value, digits) {
  if (value === null || value === undefined || Number.isNaN(value)) return null;
  const abs = Math.abs(value);
  let max = digits;
  if (max === undefined) {
    if (abs === 0) max = 2;
    else if (abs < 0.001) max = 8;
    else if (abs < 1) max = 6;
    else if (abs < 100) max = 4;
    else max = 2;
  }
  return value.toLocaleString(NUM_LOCALE, { maximumFractionDigits: max, minimumFractionDigits: 0 });
}

function fmtPct(value, digits = 2) {
  const n = fmtNum(value, digits);
  return n === null ? null : `${n}%`;
}

/** A numeric span that survives RTL untouched. */
function num(value, opts = {}) {
  const el = document.createElement('span');
  el.className = 'num';
  el.dir = 'ltr';
  const text = opts.raw !== undefined ? opts.raw : fmtNum(value, opts.digits);
  if (text === null || text === undefined) {
    el.textContent = t('card.noData');
    el.classList.add('stat__v--muted');
    el.dir = 'auto';
  } else {
    el.textContent = opts.suffix ? `${text}${opts.suffix}` : text;
  }
  return el;
}

/* Every absolute time in this UI is shown at UTC+03:30.
 *
 * A fixed offset rather than the viewer's own locale or the Asia/Tehran zone: the
 * operator reads these against the exchange and the server logs, so the clock has to
 * be the same number wherever the page is opened, including from a machine set to
 * another timezone. Iran has not observed DST since 2022, so +03:30 is the whole
 * rule; if that ever changes this is the one place to fix. */
const TZ_OFFSET_MIN = 3 * 60 + 30;
const TZ_LABEL = 'UTC+3:30';

function atLocal(iso, { withDate = true } = {}) {
  if (!iso) return '—';
  /* Server timestamps are UTC. Some carry an explicit offset, some are bare — treat
   * a bare one as UTC rather than letting the browser read it as local time. */
  const ms = Date.parse(iso.endsWith('Z') || /[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`);
  if (!Number.isFinite(ms)) return '—';
  const d = new Date(ms + TZ_OFFSET_MIN * 60_000);
  const p = (n) => String(n).padStart(2, '0');
  const clock = `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
  return withDate ? `${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${clock}` : clock;
}

function timeAgo(iso) {
  if (!iso) return t('card.noData');
  const then = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : `${iso}Z`);
  const secs = Math.round((Date.now() - then.getTime()) / 1000);
  if (!Number.isFinite(secs)) return t('card.noData');
  const rtf = new Intl.RelativeTimeFormat(state.lang === 'fa' ? 'fa' : 'en', { numeric: 'auto' });
  /* Seconds below a minute, and FLOOR rather than round for minutes.
   *
   * Rounding made a 31-second-old card read "1 minute ago" and a 90-second-old one
   * "2 minutes ago", so a single scan's cards — which span about a minute, because
   * that is how long a 33-coin pass takes — reported ages that disagreed with each
   * other and with the header. Floor never claims data is older than it is, and
   * seconds keep a fresh scan legible instead of collapsing to "this minute". */
  if (Math.abs(secs) < 60) return rtf.format(-secs, 'second');
  const mins = Math.floor(secs / 60);
  if (Math.abs(mins) < 60) return rtf.format(-mins, 'minute');
  return rtf.format(-Math.floor(mins / 60), 'hour');
}

/* ------------------------------------------------------------------- DOM */

function el(tag, opts = {}, children = []) {
  const node = document.createElement(tag);
  if (opts.class) node.className = opts.class;
  if (opts.text !== undefined) node.textContent = opts.text;
  /* `dir` is handled explicitly rather than left to the attrs loop. It was silently
   * dropped once, and the result was English detail strings inheriting RTL from
   * <html> — which moved a leading "0/6" to the end of the sentence. Reordering a
   * ratio is exactly the failure this app cannot afford, so it gets its own line. */
  if (opts.dir) node.dir = opts.dir;
  for (const [k, v] of Object.entries(opts.attrs || {})) {
    if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v);
  }
  for (const child of [].concat(children)) if (child) node.append(child);
  return node;
}

function stat(label, valueNode) {
  return el('div', { class: 'stat' }, [
    el('span', { class: 'stat__k', text: label }),
    el('span', { class: 'stat__v' }, [valueNode]),
  ]);
}

/* Icon + text, so the verdict never depends on colour. */
const VERDICT_ICON = { TAKE: '✓', WATCH: '◑', INCOMPLETE: '?', SKIP: '–', ERROR: '!' };
const VERDICT_CLASS = {
  TAKE: 'card--take', WATCH: 'card--watch', INCOMPLETE: 'card--incomplete',
  SKIP: 'card--skip', ERROR: 'card--error',
};

/* ------------------------------------------------------------------ cards */

/* A TAKE the live engine will not act on.
 *
 * The skill grades TAKE at score >= 70 with every gate passed. This deployment only
 * opens at >= min_score (75), so 70-74.9 renders as a green TAKE that never becomes
 * a position — which reads as "the signal fired and nothing happened". Say it on the
 * card instead of leaving it to be inferred. */
function belowEntryBar(card) {
  const bar = Number(((state.data || {}).settings || {}).min_score);
  const sc = Number(card.score);
  return card.verdict === 'TAKE' && Number.isFinite(bar) && Number.isFinite(sc)
         && sc < bar;
}

function verdictBadge(card) {
  const v = card.verdict || 'ERROR';
  const badge = el('span', { class: 'badge', attrs: { title: t(`verdict.${v}.meaning`) } }, [
    el('span', { class: 'badge__icon', text: VERDICT_ICON[v] || '!', attrs: { 'aria-hidden': 'true' } }),
    el('span', { text: t(`verdict.${v}`) }),
  ]);
  if (card.score !== null && card.score !== undefined) {
    const s = el('span', { class: 'badge__score num' });
    s.dir = 'ltr';
    s.textContent = fmtNum(card.score, 1);
    badge.append(s);
  }
  return badge;
}

function buildCard(card) {
  const open = state.openCoins.has(card.coin);
  const details = el('details', {
    class: `card ${VERDICT_CLASS[card.verdict] || 'card--error'}`,
    attrs: { 'data-coin': card.coin, open: open ? 'open' : null },
  });

  /* -- head -- */
  const idBlock = el('div', { class: 'card__id' }, [
    el('div', { class: 'card__coin' }, [
      el('strong', { text: card.coin }),
      el('span', { class: 'card__symbol', text: card.symbol || '' }),
    ]),
  ]);

  const line = el('div', { class: 'card__line' });
  if (card.error) {
    line.textContent = card.error;
  } else if (card.side) {
    const sidePill = el('span', {
      class: `pill ${card.side === 'long' ? 'pill--long' : 'pill--short'}`,
      text: t(`card.side.${card.side}`),
    });
    line.append(sidePill);
    if (card.side_tied) line.append(' ', el('span', { class: 'pill pill--muted', text: t('card.side.tied') }));
    if (card.levels && card.levels.entry !== undefined) {
      line.append(' ', document.createTextNode(`${t('pos.entry')} `), num(card.levels.entry));
    }
    /* Say why on the collapsed row. Sorting puts a SKIP scoring 87 above a TAKE
     * scoring 70 — correct, since a gate failure is decisive regardless of score —
     * but it reads as a contradiction unless the failing gate is right there. */
    const reason = (card.gates_failed || []).map(ts).join(' · ');
    if (reason) {
      line.append(' ', el('span', { class: 'card__reason', text: `— ${reason}` }));
    }
  }
  idBlock.append(line);

  if (card.lot_label) {
    idBlock.append(el('div', {
      class: 'card__symbol',
      text: t('card.perLot', { lot: fmtNum(card.lot_size, 0), coin: card.coin }),
    }));
  }

  const meta = el('div', { class: 'card__meta' }, [
    el('div', { text: `${t('card.updated')} ${timeAgo(card.fetched_at)}` }),
  ]);
  if (card.provisional) {
    meta.append(el('span', { class: 'pill pill--provisional', text: `⚠ ${t('card.provisional')}` }));
  }

  const bar = Number(((state.data || {}).settings || {}).min_score);
  const gate = belowEntryBar(card)
    ? el('span', {
        class: 'nogate',
        text: t('card.belowBar', { bar: fmtNum(bar) }),
        attrs: { title: t('card.belowBar.meaning', { bar: fmtNum(bar) }) },
      })
    : null;
  const head = el('summary', { class: 'card__head' },
                  [verdictBadge(card), idBlock, meta, gate]);
  details.append(head);

  /* -- body -- */
  const body = el('div', { class: 'card__body' });

  if (card.provisional) {
    body.append(el('div', { class: 'provisional-banner' }, [
      el('span', { text: '⚠', attrs: { 'aria-hidden': 'true' } }),
      el('span', { text: t('card.provisionalNote') }),
    ]));
  }
  if (card.action) body.append(el('p', { class: 'card__line', dir: 'auto', text: ts(card.action) }));

  if (!card.error) {
    const cols = el('div', { class: 'two-col' }, [chartBlock(card), positionBlock(card)]);
    body.append(cols);
    body.append(economicsBlock(card));
    const venue = venueBlock(card);
    if (venue) body.append(venue);
    body.append(manualBlock(card));
    body.append(qualificationBlock(card));
    body.append(commentaryBlock(card));
  }

  body.append(el('div', { style: 'display:flex;gap:.5rem;flex-wrap:wrap' }, [
    (() => {
      const b = el('button', { class: 'btn btn--ghost', text: t('bar.rescanCoin') });
      b.addEventListener('click', async (e) => {
        e.preventDefault();
        b.disabled = true;
        try { await API.scan([card.coin]); startPolling(); } finally { b.disabled = false; }
      });
      return b;
    })(),
  ]));

  details.append(body);

  details.addEventListener('toggle', () => {
    if (details.open) {
      state.openCoins.add(card.coin);
      renderChart(card);
    } else {
      state.openCoins.delete(card.coin);
    }
    localStorage.setItem('openCoins', JSON.stringify([...state.openCoins]));
  });
  if (open) queueMicrotask(() => renderChart(card));

  return details;
}

function positionBlock(card) {
  const L = card.levels || {};
  const S = card.sizing || {};
  const wrap = el('div', {}, [el('div', { class: 'section__title', text: t('pos.title') })]);
  const grid = el('div', { class: 'grid' }, [
    stat(t('pos.entry'), num(L.entry)),
    stat(t('pos.stop'), num(L.stop)),
    stat(t('pos.tp1'), num(L.tp1)),
    stat(t('pos.tp2'), num(L.tp2)),
    stat(t('pos.quantity'), num(S.quantity)),
    /* Toobit takes orders in contracts, not coins. Showing only the coin figure
       next to an order ticket is how you enter a position 1000x the intended size. */
    (card.venue && card.venue.contracts !== undefined && card.venue.contracts !== null)
      ? stat(t('pos.contracts'), num(card.venue.contracts))
      : null,
    stat(t('pos.notional'), num(S.notional)),
    stat(t('pos.leverage'), num(S.leverage, { suffix: '×' })),
    stat(t('pos.margin'), num(S.margin)),
    stat(t('pos.liquidation'), num(L.liquidation_price_estimate)),
    stat(t('pos.liqBuffer'), num(S.liq_buffer_x_stop, { suffix: `× ${''}` })),
  ]);
  wrap.append(grid);
  if (L.stop_source) {
    wrap.append(el('div', { class: 'check__meta', text: `${t('pos.stopSource')}: ${L.stop_source}` }));
  }
  return wrap;
}

function economicsBlock(card) {
  const E = card.economics || {};
  const S = card.sizing || {};
  return el('div', {}, [
    el('div', { class: 'section__title', text: t('econ.title') }),
    el('div', { class: 'grid' }, [
      stat(t('econ.r'), num(S.risk_amount_R)),
      stat(t('econ.fee'), num(E.round_trip_fee)),
      stat(t('econ.holding'), num(E.holding_cost)),
      stat(t('econ.total'), num(E.total_cost)),
      stat(t('econ.costOfR'), num(null, { raw: E.cost_in_R !== undefined ? fmtPct(E.cost_in_R * 100, 1) : null })),
      stat(t('econ.rr'), num(E.rr_tp2)),
      stat(t('econ.breakeven'), num(null, { raw: E.breakeven_win_rate !== undefined ? fmtPct(E.breakeven_win_rate * 100, 1) : null })),
      stat(t('econ.expectancy'), num(null, { raw: E.expectancy_net_R !== undefined ? `${fmtNum(E.expectancy_net_R, 3)}R` : null })),
    ]),
  ]);
}

/* Venue facts the planner does not own: contract size, funding, and where this
 * exchange's real maintenance margin differs from the profile's assumption. */
function venueBlock(card) {
  const v = card.venue;
  if (!v) return null;
  const wrap = el('div', {}, [el('div', { class: 'section__title', text: t('venue.title') })]);
  const cells = [];

  if (v.units_per_contract !== undefined && v.units_per_contract !== null) {
    cells.push(stat(t('pos.contractSize'),
      num(null, { raw: `${fmtNum(v.units_per_contract)} ${card.coin}` })));
  }
  const f = v.funding || card.funding;
  if (f && f.rate_pct !== undefined) {
    const label = `${f.rate_pct >= 0 ? '+' : ''}${fmtNum(f.rate_pct, 4)}%`;
    cells.push(stat(`${t('venue.funding')} ${t('venue.fundingPeriod', { period: f.period || '8H' })}`,
      num(null, { raw: label })));
  }
  if (v.max_leverage) cells.push(stat(t('venue.maxLeverage'), num(v.max_leverage, { suffix: '×' })));
  if (v.maint_margin_pct !== undefined && v.maint_margin_pct !== null) {
    cells.push(stat(t('venue.maintMargin'), num(null, { raw: `${fmtNum(v.maint_margin_pct, 3)}%` })));
  }
  if (!cells.length) return null;
  wrap.append(el('div', { class: 'grid' }, cells));

  /* The planner's profile assumes a fixed maintenance margin. Where the venue's real
     figure is higher, the liquidation estimate would otherwise be optimistic — say so. */
  const assumed = v.planner_maint_margin_pct;
  if (assumed !== undefined && v.maint_margin_pct > assumed) {
    wrap.append(el('div', { class: 'check__meta check__stale', dir: 'auto',
      text: t('venue.maintMarginNote', { assumed: fmtNum(assumed, 3), real: fmtNum(v.maint_margin_pct, 3) }) }));
  }
  if (v.leverage_correction) {
    wrap.append(el('div', { class: 'note note--warning' }, [
      el('span', { text: '⚠', attrs: { 'aria-hidden': 'true' } }),
      el('span', { dir: 'auto', text: v.leverage_correction.reason }),
    ]));
  }
  return wrap;
}

function manualBlock(card) {
  const checks = card.manual_checks || [];
  /* On a venue that publishes funding and a BTC perp, the skill's two MANUAL checks
     are settled from live data and none remain. Say that explicitly rather than
     silently omitting the block — "nothing here" and "nothing to do" look alike. */
  if (!checks.length) {
    return el('div', { class: 'manual' }, [
      el('div', { class: 'commentary__label' }, [
        el('span', { text: t('manual.title') }),
        el('span', { class: 'pill', text: `✓ ${t('manual.allAuto')}` }),
      ]),
    ]);
  }
  const openCount = checks.filter((c) => !c.resolved).length;
  const wrap = el('div', { class: `manual ${openCount ? 'manual--open' : ''}` });

  wrap.append(el('div', { class: 'commentary__label' }, [
    el('span', { text: t('manual.title') }),
    el('span', {
      class: 'pill',
      text: openCount ? t('manual.unresolved', { n: openCount }) : t('manual.allResolved'),
    }),
  ]));
  wrap.append(el('p', { class: 'manual__note', text: t('manual.note') }));

  for (const c of checks) {
    const id = `mc-${card.coin}-${btoa(unescape(encodeURIComponent(c.key))).replace(/=/g, '')}`;
    const input = el('input', { attrs: { type: 'checkbox', id } });
    input.checked = !!c.resolved;
    input.addEventListener('change', async () => {
      input.disabled = true;
      try {
        await API.manual({ coin: card.coin, key: c.key, resolved: input.checked });
        await refresh();
      } finally { input.disabled = false; }
    });

    const bodyEl = el('div', { class: 'check__body' }, [
      el('label', { text: ts(c.key), attrs: { for: id } }),
    ]);
    if (c.stale) {
      bodyEl.append(el('span', { class: 'check__meta check__stale', text: `⟳ ${t('manual.stale')}` }));
    } else if (c.resolved && c.resolved_at) {
      bodyEl.append(el('span', { class: 'check__meta', text: `${t('manual.confirmedAt')} ${timeAgo(c.resolved_at)}` }));
    } else if (c.observed) {
      bodyEl.append(el('span', { class: 'check__meta', dir: 'auto', text: ts(c.observed) }));
    }
    wrap.append(el('div', { class: 'check' }, [input, bodyEl]));
  }
  return wrap;
}

function qualificationBlock(card) {
  const d = el('details', { class: 'manual' });
  d.append(el('summary', { text: t('qual.title'), attrs: { style: 'cursor:pointer;font-size:.8rem' } }));

  const inner = el('div', { style: 'padding-block-start:.6rem;display:grid;gap:.9rem' });

  if ((card.gates || []).length) {
    inner.append(el('div', {}, [
      el('div', { class: 'section__title', text: t('qual.gates') }),
      el('div', { class: 'gates' }, card.gates.map((g) => el('div', {
        class: `gate ${g.passed ? 'gate--pass' : 'gate--fail'}`,
      }, [
        el('span', { class: 'gate__mark', text: g.passed ? '✓' : '✕', attrs: { 'aria-hidden': 'true' } }),
        el('span', { class: 'gate__name', text: ts(g.gate) }),
        el('span', { class: 'gate__detail', dir: 'auto', text: g.detail }),
      ]))),
    ]));
  }

  if ((card.score_breakdown || []).length) {
    inner.append(el('div', {}, [
      el('div', { class: 'section__title', text: t('qual.breakdown') }),
      el('div', { class: 'bars' }, card.score_breakdown.map((f) => {
        const pct = f.weight ? Math.max(0, Math.min(100, (f.points / f.weight) * 100)) : 0;
        const track = el('div', { class: 'bar__track' }, [
          el('div', { class: 'bar__fill', attrs: { style: `inline-size:${pct}%` } }),
        ]);
        const val = el('span', { class: 'bar__val num' });
        val.dir = 'ltr';
        val.textContent = `${fmtNum(f.points, 1)} / ${f.weight}`;
        return el('div', { class: 'bar', attrs: { title: f.detail } }, [
          el('span', { text: ts(f.factor) }), track, val,
        ]);
      })),
    ]));
  }

  const notes = [];
  for (const b of card.blockers || []) notes.push(el('div', { class: 'note note--blocker' }, [
    el('span', { text: '⛔', attrs: { 'aria-hidden': 'true' } }), el('span', { dir: 'auto', text: b }),
  ]));
  for (const w of card.warnings || []) notes.push(el('div', { class: 'note note--warning' }, [
    el('span', { text: '⚠', attrs: { 'aria-hidden': 'true' } }), el('span', { dir: 'auto', text: w }),
  ]));
  if (notes.length) inner.append(el('div', { class: 'notes' }, notes));

  if (card.direction_note) {
    inner.append(el('div', {}, [
      el('div', { class: 'section__title', text: t('qual.direction') }),
      el('p', { class: 'gate__detail', dir: 'auto', text: card.direction_note }),
    ]));
  }
  if (card.caveat) inner.append(el('p', { class: 'check__meta', dir: 'auto', text: card.caveat }));

  d.append(inner);
  return d;
}

/* The backend explains itself in English. Render the localised version when we have
 * a code for it, and only fall back to the raw English when we do not — an English
 * paragraph inside the Persian UI is exactly what this avoids. */
function reasonText(c) {
  if (c.reason_code) {
    const key = `comment.reason.${c.reason_code}`;
    if (state.strings[key] !== undefined) return t(key, c.reason_params || {});
  }
  return c.reason || '';
}

function commentaryBlock(card) {
  const c = card.commentary || {};
  const wrap = el('div', { class: 'commentary' });
  const label = el('div', { class: 'commentary__label' }, [
    el('span', { text: t('comment.label') }),
  ]);

  const btn = el('button', { class: 'btn btn--ghost', text: t('comment.generate'), attrs: { style: 'font-size:.72rem;padding:.12rem .5rem' } });
  btn.addEventListener('click', async (e) => {
    e.preventDefault();
    btn.disabled = true;
    btn.textContent = t('comment.generating');
    try {
      await API.commentary({ coin: card.coin, lang: state.lang });
      await refresh();
    } catch (err) {
      btn.textContent = String(err.message || err);
    } finally { btn.disabled = false; }
  });
  label.append(btn);
  wrap.append(label);

  if (c.status === 'ok' && c.text) {
    wrap.append(el('p', { class: 'commentary__text', dir: 'auto', text: c.text }));
    wrap.append(el('div', { class: 'commentary__meta', text: `${t('comment.model')}: ${c.model || '—'}` }));
  } else if (c.status) {
    const headline = c.status === 'rejected' ? t('comment.rejected') : t('comment.unavailable');
    wrap.append(el('p', { class: 'commentary__text', text: headline }));
    wrap.append(el('div', { class: 'commentary__meta', dir: 'auto', text: reasonText(c) }));
    /* A shell command must stay one unbreakable LTR run. Interpolated into a Persian
     * sentence it wrapped mid-command — "ollama" on one line, "pull qwen…" on the
     * next — which is not a command anyone can copy. */
    const cmd = (c.reason_params || {}).command;
    if (cmd) wrap.append(el('code', { class: 'cmd', dir: 'ltr', text: cmd }));
  } else {
    wrap.append(el('p', { class: 'commentary__text', text: t('comment.none') }));
  }
  return wrap;
}

/* ------------------------------------------------------------------ chart */

function chartBlock(card) {
  const wrap = el('div', { class: 'chart' }, [
    el('div', { class: 'section__title', text: `${t('chart.title')} · ${(card.timeframes || {}).decision || ''}` }),
  ]);
  const host = el('div', { class: 'chart__canvas', attrs: { 'data-chart': card.coin } });
  wrap.append(host);
  wrap.append(el('div', { class: 'chart__legend' }, [
    candleKey(t('chart.legend.up'), false),
    candleKey(t('chart.legend.down'), true),
    legendKey(t('chart.legend.ema20'), 'var(--series-1)'),
    legendKey(t('chart.legend.ema50'), 'var(--series-2)'),
    legendKey(t('chart.legend.ema200'), 'var(--series-3)'),
  ]));
  return wrap;
}

/* The hollow/solid candle encoding has to be stated, or it is a private joke. */
function candleKey(label, filled) {
  return el('span', { class: 'chart__key' }, [
    el('span', {
      class: 'chart__candle',
      attrs: { style: filled ? 'background:var(--candle)' : 'background:transparent', 'aria-hidden': 'true' },
    }),
    el('span', { text: label }),
  ]);
}

function legendKey(label, colour) {
  return el('span', { class: 'chart__key' }, [
    el('span', { class: 'chart__swatch', attrs: { style: `background:${colour}`, 'aria-hidden': 'true' } }),
    el('span', { text: label }),
  ]);
}

function cssVar(name) {
  return getComputedStyle(document.body).getPropertyValue(name).trim();
}

async function renderChart(card) {
  const host = document.querySelector(`[data-chart="${CSS.escape(card.coin)}"]`);
  if (!host || host.dataset.rendered === '1') return;
  host.dataset.rendered = '1';

  let payload;
  try {
    payload = await API.series(card.coin);
  } catch {
    host.replaceWith(el('div', { class: 'chart__placeholder', text: t('chart.none') }));
    return;
  }
  const series = payload.series || {};
  const candles = series.candles || [];
  if (!candles.length) {
    host.replaceWith(el('div', { class: 'chart__placeholder', text: t('chart.none') }));
    return;
  }

  const toTime = (iso) => Math.floor(new Date(`${iso}Z`).getTime() / 1000);

  /* Derive axis precision from the actual price magnitude. The default two decimals
   * renders a 0.0748 coin's entry and TP1 as the same "0.07", which is worse than
   * useless on a chart whose whole job is showing where the levels sit. */
  const ref = Math.abs(card.levels && card.levels.entry ? card.levels.entry
    : candles[candles.length - 1].close);
  let precision = 2;
  if (ref < 0.001) precision = 8;
  else if (ref < 1) precision = 6;
  else if (ref < 100) precision = 4;
  const priceFormat = { type: 'price', precision, minMove: 10 ** -precision };

  const chart = LightweightCharts.createChart(host, {
    height: 240,
    layout: { background: { color: 'transparent' }, textColor: cssVar('--ink-muted'), fontFamily: cssVar('--font-ui') },
    grid: { vertLines: { color: cssVar('--hairline') }, horzLines: { color: cssVar('--hairline') } },
    rightPriceScale: { borderColor: cssVar('--rule') },
    timeScale: { borderColor: cssVar('--rule'), timeVisible: true },
    crosshair: { mode: 0 },
    handleScale: { axisPressedMouseMove: false },
    /* The chart is a figure, not a document: it stays LTR in Persian so the time
       axis still runs oldest-to-newest and prices are not reordered. */
    localization: { locale: 'en-US' },
  });

  /* Candles are deliberately not green/red. Direction is encoded by fill — hollow
   * for up, solid for down — which is a shape channel, so it survives colour
   * blindness and greyscale. It also frees the colour channel entirely for the three
   * EMAs: with green candles, the aqua EMA 200 read as price data rather than as a
   * moving average. Quiet candles, coloured overlays. */
  const candleInk = cssVar('--candle');
  const candleSeries = chart.addCandlestickSeries({
    upColor: 'rgba(0,0,0,0)', downColor: candleInk,
    borderUpColor: candleInk, borderDownColor: candleInk,
    wickUpColor: candleInk, wickDownColor: candleInk,
    priceFormat,
    /* The entry level is the snapshot's last price, so the series' own last-value
     * label is the same number as the Entry line and the two collide on the axis. */
    lastValueVisible: false, priceLineVisible: false,
  });
  candleSeries.setData(candles.map((c) => ({
    time: toTime(c.timestamp), open: c.open, high: c.high, low: c.low, close: c.close,
  })));

  const emaColours = { ema20: '--series-1', ema50: '--series-2', ema200: '--series-3' };
  for (const [key, varName] of Object.entries(emaColours)) {
    const values = (series.ema || {})[key];
    if (!values) continue;
    const line = chart.addLineSeries({
      color: cssVar(varName), lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
      crosshairMarkerVisible: false, priceFormat,
    });
    /* Bars before the seed window have no EMA. Dropping them is honest; drawing a
       flat segment there would invent data. */
    line.setData(candles
      .map((c, i) => (values[i] === null || values[i] === undefined
        ? null : { time: toTime(c.timestamp), value: values[i] }))
      .filter(Boolean));
  }

  const L = card.levels || {};
  const levels = [
    { price: L.entry, title: t('pos.entry'), colour: cssVar('--ink-2'), style: 0 },
    { price: L.stop, title: t('pos.stop'), colour: cssVar('--critical'), style: 2 },
    { price: L.tp1, title: t('pos.tp1'), colour: cssVar('--good'), style: 2 },
    { price: L.tp2, title: t('pos.tp2'), colour: cssVar('--good'), style: 2 },
  ];
  for (const lv of levels) {
    if (lv.price === undefined || lv.price === null) continue;
    candleSeries.createPriceLine({
      price: lv.price, color: lv.colour, lineWidth: 1, lineStyle: lv.style,
      axisLabelVisible: true, title: lv.title,
    });
  }

  chart.timeScale().fitContent();
  state.charts.set(card.coin, chart);

  const ro = new ResizeObserver(() => chart.applyOptions({ width: host.clientWidth }));
  ro.observe(host);
  chart.applyOptions({ width: host.clientWidth });
}

/* ------------------------------------------------------------- rendering */

function renderFilters() {
  const box = document.getElementById('filters');
  box.replaceChildren();
  const counts = state.data.counts || {};
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const options = [['ALL', t('bar.filter.all'), total],
    ...['TAKE', 'WATCH', 'INCOMPLETE', 'SKIP', 'ERROR']
      .filter((v) => counts[v])
      .map((v) => [v, t(`verdict.${v}`), counts[v]])];

  for (const [value, label, count] of options) {
    const chip = el('button', {
      class: 'chip',
      attrs: { 'aria-pressed': String(state.filter === value), type: 'button' },
    }, [
      value !== 'ALL' ? el('span', { text: VERDICT_ICON[value], attrs: { 'aria-hidden': 'true' } }) : null,
      el('span', { text: label }),
      (() => { const c = el('span', { class: 'chip__count num' }); c.dir = 'ltr'; c.textContent = count; return c; })(),
    ]);
    chip.addEventListener('click', () => { state.filter = value; render(); });
    box.append(chip);
  }
}

function renderBoard() {
  const board = document.getElementById('board');
  board.replaceChildren();
  const coins = (state.data.coins || [])
    .filter((c) => state.filter === 'ALL' || (c.verdict || 'ERROR') === state.filter);

  if (!coins.length) {
    board.append(el('div', { class: 'empty' }, [
      el('h2', { text: t('empty.title'), attrs: { style: 'font-size:1rem' } }),
      el('p', { text: t('empty.body') }),
    ]));
    return;
  }
  for (const card of coins) board.append(buildCard(card));
}

function renderUnavailable() {
  const box = document.getElementById('unavailable');
  const grid = document.getElementById('unavailGrid');
  const rows = [
    ...(state.data.unavailable || []),
    ...(state.data.pending || []).map((p) => ({ ...p, status: 'pending' })),
  ];
  if (!rows.length) { box.hidden = true; return; }
  box.hidden = false;

  const total = (state.data.watchlist || {}).requested || rows.length;
  document.getElementById('unavailCount').textContent =
    t('unavail.subtitle', { n: rows.length, total });

  grid.replaceChildren();
  for (const r of rows) {
    grid.append(el('div', { class: 'unavailable__row' }, [
      el('span', { class: 'unavailable__coin', text: r.coin }),
      el('span', { class: 'unavailable__why', dir: 'auto', text: r.reason || t(`unavail.${r.status}`) }),
    ]));
  }
}

function renderScan() {
  const scan = state.data.scan || {};
  const box = document.getElementById('scanStatus');
  const text = document.getElementById('scanText');
  const prog = document.getElementById('scanProgress');
  const bar = document.getElementById('scanBar');
  const running = !!scan.running;

  box.classList.toggle('scan--running', running);
  prog.hidden = !running;

  if (running) {
    const done = scan.completed || 0;
    const total = scan.total || 0;
    text.textContent = `${t('bar.scanning')} ${done}/${total}${scan.current_coin ? ` · ${scan.current_coin}` : ''}`;
    bar.style.inlineSize = total ? `${(done / total) * 100}%` : '0';
  } else {
    /* started_at, not finished_at.
     *
     * A 33-coin pass takes about a minute, so the first coin's data is already that
     * old by the time the scan finishes. Reporting the finish made the header look
     * FRESHER than every card beneath it — "last scan 1 minute ago" above a board of
     * "updated 2 minutes ago". The start is when the oldest card on screen was
     * gathered, so the header is now the upper bound on the board's age rather than
     * the lower one. */
    text.textContent = `${t('bar.lastScan')} ${timeAgo(scan.started_at || scan.finished_at)}`;
  }
  document.getElementById('rescan').textContent = running ? t('bar.cancel') : t('bar.rescan');
}

function renderSettingsForm() {
  /* Only the controls that actually change what this deployment does. The exchange
   * selector, capital and risk % were removed from the header: the venue is fixed to
   * Tabdeal, `capital` is the planner's sizing number and setting it to the real
   * balance breaks signal generation outright, and `risk_pct` is ignored entirely
   * while demo.auto_slots is on. Showing a control that silently does nothing is
   * worse than not showing it. */
  const s = state.data.settings || {};
  document.getElementById('profile').value = s.profile;
  document.getElementById('interval').value = s.scan_interval_minutes;
}

function renderDrawer() {
  const body = document.getElementById('drawerBody');
  body.replaceChildren();
  const llm = state.data.llm || {};
  const hw = llm.hardware || {};
  const wl = state.data.watchlist || {};

  body.append(el('h3', { text: t('settings.llm') }));
  const dl = el('dl', { class: 'kv' });
  dl.append(el('dt', { text: t('settings.llm.model') }),
    el('dd', { dir: 'auto', text: llm.model || t('settings.llm.none') }));
  body.append(dl);

  if ((llm.reasoning || []).length) {
    body.append(el('h3', { text: t('settings.llm.reasoning') }));
    body.append(el('ul', {}, llm.reasoning.map((r) => el('li', { dir: 'auto', text: r }))));
  }

  body.append(el('h3', { text: t('settings.hardware') }));
  const hwl = el('dl', { class: 'kv' });
  for (const [k, v] of [['CPU', hw.cpu_model], ['Cores', hw.cpu_cores],
    ['RAM', hw.total_ram_gb ? `${hw.total_ram_gb} GB` : null], ['GPU', hw.gpu],
    ['Platform', hw.platform]]) {
    if (!v) continue;
    hwl.append(el('dt', { text: k }));
    const dd = el('dd', { class: 'num' }); dd.dir = 'ltr'; dd.textContent = v;
    hwl.append(dd);
  }
  body.append(hwl);

  const btn = el('button', { class: 'btn', text: t('settings.reassess'), attrs: { style: 'margin-block-start:.8rem' } });
  btn.addEventListener('click', async () => { btn.disabled = true; await API.reassess(); await refresh(); btn.disabled = false; });
  body.append(btn);

  body.append(el('h3', { text: t('settings.watchlist') }));
  const wll = el('dl', { class: 'kv' });
  wll.append(el('dt', { text: t('settings.generated') }), el('dd', { text: timeAgo(wl.generated_at) }));
  wll.append(el('dt', { text: t('bar.filter.all') }));
  const dd2 = el('dd', { class: 'num' }); dd2.dir = 'ltr';
  dd2.textContent = `${wl.scannable} / ${wl.requested}`;
  wll.append(dd2);
  body.append(wll);
  if (wl.margin_detection) body.append(el('p', { class: 'check__meta', dir: 'auto', text: wl.margin_detection }));
}

/* --------------------------------------------------------------- formatting */

/* Sign carries the meaning, not colour: "+0.42" and "−0.42" are already distinct
 * without it, so the class only reinforces what the glyph already says. */
function signed(value, opts = {}) {
  const node = num(value, opts);
  if (typeof value !== 'number' || !Number.isFinite(value)) return node;
  /* A value that rounds away to nothing is neither a gain nor a loss. Without this,
   * an MFE of -0.003R printed as "−0R" — a minus sign on a zero, which reads as a
   * loss that is not there. */
  const shown = parseFloat(node.textContent);
  if (shown === 0) {
    node.textContent = node.textContent.replace(/^[-−+]/, '');
    return node;
  }
  node.classList.add(value > 0 ? 'pnl--up' : 'pnl--down');
  if (value > 0) node.textContent = `+${node.textContent}`;
  return node;
}

/* Build a sentence that contains a literal code fragment — a CLI flag, a filename —
 * with that fragment in its own LTR run.
 *
 * Bidi control characters embedded in the translation file were tried first and are
 * not reliable: inside a Persian paragraph "--slots" still rendered as "slots--",
 * because the leading hyphens are neutral and get reordered. An element with an
 * explicit dir is unambiguous, and unlike an invisible U+2066 it is visible to
 * whoever edits the string next. */
function sentenceWithCode(key, code) {
  const parts = t(key).split('{code}');
  const nodes = [document.createTextNode(parts[0] || '')];
  if (parts.length > 1) {
    nodes.push(el('code', { class: 'code', text: code, dir: 'ltr' }));
    nodes.push(document.createTextNode(parts[1] || ''));
  }
  return nodes;
}

/* An R-multiple back into cash. R is fixed at entry, so this is exact, not a
 * re-derivation from the current price. */
function inUsdt(rMultiple, riskAmount) {
  if (typeof rMultiple !== 'number' || typeof riskAmount !== 'number') return null;
  return rMultiple * riskAmount;
}

/* ------------------------------------------------------- live positions

 * Replaces the demo-trading panel. The paper account is switched off; what matters
 * now is the four real positions on Tabdeal, so this shows those and their price
 * history rather than a simulated ledger.
 */

function renderBtc(price) {
  const node = document.getElementById('btcPriceValue');
  if (!node) return;
  /* Keep the last good price rather than blanking on a dropped poll: a header
   * number that flickers to "—" every few seconds is worse than one a few seconds
   * stale, and this is a reference reading, not a trading input. */
  if (price === null || price === undefined || !Number.isFinite(Number(price))) return;
  node.textContent = fmtNum(price);
}

function pnlClass(v) {
  if (!Number.isFinite(v) || v === 0) return 'num';
  return v > 0 ? 'num num--up' : 'num num--down';
}

function pctFromEntry(p, mark) {
  const e = Number(p.entry);
  if (!Number.isFinite(e) || !e || !Number.isFinite(mark)) return null;
  return ((mark - e) / e) * 100 * (p.side === 'short' ? -1 : 1);
}

function heldLabel(openedTs, closedTs) {
  if (!Number.isFinite(openedTs)) return '—';
  const end = Number.isFinite(closedTs) ? closedTs : Date.now() / 1000;
  const mins = Math.max(0, Math.floor((end - openedTs) / 60));
  if (mins < 60) return `${mins}m`;
  return `${Math.floor(mins / 60)}h ${mins % 60}m`;
}

/* One line per position, y = % away from ITS OWN entry.
 *
 * Plotting raw price would need one axis per coin — FLOKI trades near 0.000027 and
 * BTC near 77,000, so on a shared axis every line but the largest collapses onto the
 * baseline. Percent-from-entry is also the quantity the exit rules actually act on,
 * and it puts the 0.2% round-trip cost on the chart as a fixed reference band. */
const LINE_COLOURS = ['#3b82f6', '#f59e0b', '#10b981', '#a855f7', '#ef4444', '#06b6d4'];

function positionChart(positions) {
  const W = 720, H = 210, PADL = 44, PADR = 12, PADT = 12, PADB = 22;
  const live = positions.filter((p) => (p.points || []).length > 1);
  if (!live.length) {
    return el('div', { class: 'empty', text: t('live.chart.empty') });
  }

  let tMax = 0, yMin = 0, yMax = 0;
  const series = live.map((p, i) => {
    const t0 = Number(p.opened_ts) || (p.points[0] || {}).ts;
    const pts = p.points.map((s) => {
      const pct = pctFromEntry(p, Number(s.mark));
      return { x: Math.max(0, (Number(s.ts) - t0) / 60), y: pct };
    }).filter((d) => Number.isFinite(d.x) && Number.isFinite(d.y));
    for (const d of pts) {
      if (d.x > tMax) tMax = d.x;
      if (d.y < yMin) yMin = d.y;
      if (d.y > yMax) yMax = d.y;
    }
    return { p, pts, colour: LINE_COLOURS[i % LINE_COLOURS.length] };
  }).filter((s) => s.pts.length > 1);

  if (!series.length) return el('div', { class: 'empty', text: t('live.chart.empty') });

  /* Always show at least the cost band, so a flat line is visibly flat against the
   * fee rather than filling the plot with noise. */
  yMin = Math.min(yMin, -0.25);
  yMax = Math.max(yMax, 0.25);
  const pad = (yMax - yMin) * 0.1 || 0.1;
  yMin -= pad; yMax += pad;
  tMax = Math.max(tMax, 1);

  const sx = (x) => PADL + (x / tMax) * (W - PADL - PADR);
  const sy = (y) => PADT + (1 - (y - yMin) / (yMax - yMin)) * (H - PADT - PADB);
  const svgEl = (tag, attrs) => {
    const n = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    return n;
  };

  const svg = svgEl('svg', {
    viewBox: `0 0 ${W} ${H}`, class: 'poschart',
    preserveAspectRatio: 'none', role: 'img',
    'aria-label': t('live.chart.title'),
  });

  // the 0.2% round-trip band: inside it, a position has not paid for itself
  svg.append(svgEl('rect', {
    x: PADL, y: sy(0.2), width: W - PADL - PADR,
    height: Math.max(1, sy(-0.2) - sy(0.2)),
    fill: 'currentColor', opacity: '0.06',
  }));
  svg.append(svgEl('line', {
    x1: PADL, x2: W - PADR, y1: sy(0), y2: sy(0),
    stroke: 'currentColor', 'stroke-opacity': '0.35', 'stroke-dasharray': '3 3',
  }));

  for (const gy of [yMax, 0, yMin]) {
    const lab = svgEl('text', {
      x: PADL - 6, y: sy(gy) + 3, 'text-anchor': 'end',
      class: 'poschart__axis',
    });
    lab.textContent = `${gy > 0 ? '+' : ''}${gy.toFixed(2)}%`;
    svg.append(lab);
  }
  const xlab = svgEl('text', {
    x: W - PADR, y: H - 6, 'text-anchor': 'end', class: 'poschart__axis',
  });
  xlab.textContent = `${Math.round(tMax)}m`;
  svg.append(xlab);

  for (const s of series) {
    svg.append(svgEl('polyline', {
      points: s.pts.map((d) => `${sx(d.x).toFixed(1)},${sy(d.y).toFixed(1)}`).join(' '),
      fill: 'none', stroke: s.colour, 'stroke-width': '1.8',
      'stroke-linejoin': 'round', 'stroke-opacity': s.p.status === 'open' ? '1' : '0.4',
    }));
    const last = s.pts[s.pts.length - 1];
    svg.append(svgEl('circle', {
      cx: sx(last.x), cy: sy(last.y), r: '2.8', fill: s.colour,
    }));
  }

  const legend = el('div', { class: 'poschart__legend' },
    series.map((s) => el('span', { class: 'poschart__key' }, [
      el('i', { attrs: { style: `background:${s.colour}` } }),
      el('span', { text: s.p.coin, dir: 'ltr' }),
    ])));

  return el('div', { class: 'poschart__wrap' }, [svg, legend]);
}

/* `pnl` is the server's own figure for this symbol: gross, cost and net, all against
 * a mark read at request time. This file does not recompute any of it — deriving P&L
 * in the browser from a 15s-old sampled mark is what put this column out of step with
 * the venue's «سود و زیان ناخالص» in the first place. */
function livePositionRow(p, pnl) {
  const mark = pnl && Number.isFinite(Number(pnl.mark)) ? Number(pnl.mark) : null;
  const pct = pnl && pnl.pct !== undefined ? Number(pnl.pct) : null;
  const gross = pnl && pnl.gross !== undefined ? Number(pnl.gross) : null;
  const net = pnl && pnl.net !== undefined ? Number(pnl.net) : null;
  const money = (v) => (v === null || !Number.isFinite(v))
    ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(5)}`;
  const cells = [
    el('td', {}, [el('strong', { text: p.coin, dir: 'ltr' })]),
    el('td', { class: `side side--${p.side}`, text: t(`side.${p.side}`) }),
    el('td', { class: 'num', text: fmtNum(p.entry), dir: 'ltr' }),
    el('td', { class: 'num', text: mark === null ? '—' : fmtNum(mark), dir: 'ltr' }),
    el('td', {
      class: pnlClass(pct), dir: 'ltr',
      text: pct === null || !Number.isFinite(pct) ? '—'
            : `${pct > 0 ? '+' : ''}${pct.toFixed(3)}%`,
    }),
    /* Gross first, because that is the figure the Tabdeal app shows and the one
     * people reconcile against; net beside it is the same trade after the round
     * trip, which is what the engine's exit rule actually tests. */
    el('td', { class: pnlClass(gross), text: money(gross), dir: 'ltr' }),
    el('td', { class: pnlClass(net), text: money(net), dir: 'ltr' }),
    el('td', { class: 'num', text: heldLabel(Number(p.opened_ts)), dir: 'ltr' }),
    /* One column: the pair is read together — "where does this get out" — and
     * splitting it cost a column of width on a table that already scrolls. */
    el('td', { class: 'num sltp', dir: 'ltr' }, [
      el('span', { class: 'sltp__sl', text: fmtNum(p.stop) }),
      el('span', { class: 'sltp__sep', text: ' / ' }),
      el('span', { class: 'sltp__tp', text: fmtNum(p.tp1) }),
    ]),
    el('td', { class: 'num', text: p.score != null ? Number(p.score).toFixed(1) : '—', dir: 'ltr' }),
  ];
  return el('tr', {}, cells);
}

function liveTable(positions, pnlBySymbol) {
  const open = positions.filter((p) => p.status === 'open');
  if (!open.length) return el('div', { class: 'empty', text: t('live.none') });
  const head = el('tr', {},
    ['coin', 'side', 'entry', 'mark', 'pct', 'gross', 'pnl', 'held', 'sltp', 'score']
      .map((k) => el('th', { text: t(`live.col.${k}`) })));
  return el('table', { class: 'ltable' }, [
    el('thead', {}, [head]),
    el('tbody', {}, open.map((p) => livePositionRow(p, pnlBySymbol.get(p.symbol)))),
  ]);
}

/* Totals above the list, because the list is long and the question people actually
 * bring to it - am I up or down, and on what - should not need scrolling. Net is the
 * venue's own realised figure, so it is already after commission. */
function historySummary(closed) {
  let net = 0, wins = 0, losses = 0;
  for (const r of closed) {
    const v = Number(r.realised_pnl);
    if (!Number.isFinite(v)) continue;
    net += v;
    if (v > 0) wins += 1; else if (v < 0) losses += 1;
  }
  const n = wins + losses;
  return el('div', { class: 'stats stats--hist' }, [
    stat(t('live.hist.trades'), el('span', { class: 'num', text: String(closed.length), dir: 'ltr' })),
    stat(t('live.hist.net'), el('span', {
      class: pnlClass(net), dir: 'ltr',
      text: `${net > 0 ? '+' : ''}${net.toFixed(5)}`,
    })),
    stat(t('live.hist.wins'), el('span', {
      class: 'num', dir: 'ltr', text: `${wins} / ${losses}`,
    })),
    stat(t('live.hist.winrate'), el('span', {
      class: 'num', dir: 'ltr', text: n ? `${Math.round((wins / n) * 100)}%` : '—',
    })),
  ]);
}

function historyTable(closed) {
  if (!closed.length) return el('div', { class: 'empty', text: t('live.hist.none') });
  const cols = ['coin', 'side', 'entry', 'exit', 'pct', 'pnl', 'held', 'reason', 'closed'];
  const head = el('tr', {}, cols.map((k) => el('th', {
    /* Label the clock column with its offset — an unlabelled time invites the reader
     * to assume their own zone and misread every row by three and a half hours. */
    text: k === 'closed' ? `${t('live.col.closed')} (${TZ_LABEL})` : t(`live.col.${k}`),
  })));
  const rows = closed.map((r) => {
    const e = Number(r.entry_price), x = Number(r.exit_price);
    const sgn = r.side === 'short' ? -1 : 1;
    const pct = (Number.isFinite(e) && Number.isFinite(x) && e)
      ? ((x - e) / e) * 100 * sgn : null;
    const net = Number(r.realised_pnl);
    return el('tr', {}, [
      el('td', {}, [el('strong', { text: r.coin, dir: 'ltr' })]),
      el('td', { class: `side side--${r.side}`, text: t(`side.${r.side}`) }),
      el('td', { class: 'num', text: fmtNum(r.entry_price), dir: 'ltr' }),
      el('td', { class: 'num', text: fmtNum(r.exit_price), dir: 'ltr' }),
      el('td', {
        class: pnlClass(pct), dir: 'ltr',
        text: pct === null ? '—' : `${pct > 0 ? '+' : ''}${pct.toFixed(3)}%`,
      }),
      el('td', {
        class: pnlClass(net), dir: 'ltr',
        text: Number.isFinite(net) ? `${net > 0 ? '+' : ''}${net.toFixed(5)}` : '—',
      }),
      el('td', {
        class: 'num', dir: 'ltr',
        text: heldLabel(Number(r.opened_ts),
                        r.closed_at ? Date.parse(r.closed_at) / 1000 : undefined),
      }),
      el('td', { text: t(`demo.exit.${r.exit_reason}`) || r.exit_reason || '—' }),
      el('td', { class: 'num', dir: 'ltr', text: atLocal(r.closed_at) }),
    ]);
  });
  return el('table', { class: 'ltable' }, [el('thead', {}, [head]), el('tbody', {}, rows)]);
}

function renderLive() {
  const host = document.getElementById('liveBody');
  const st = state.live;
  const hist = state.liveHistory;
  if (!st) {
    host.replaceChildren(el('div', { class: 'empty', text: t('live.loading') }));
    return;
  }
  if (st.error) {
    host.replaceChildren(el('div', { class: 'empty', text: `${t('error.load')} ${st.error}` }));
    return;
  }

  /* `st.live` is the server's per-symbol mark and P&L, read at request time. The
   * browser used to assemble this itself from the venue snapshot and, when that came
   * back as "0", from the last sampled history point - which is thinned to 15s while
   * this tab refreshes every 3s, so the P&L lagged the venue by up to fifteen
   * seconds and never agreed with it. */
  const pnlBySymbol = new Map();
  for (const row of st.live || []) pnlBySymbol.set(row.symbol, row);

  const bal = (st.balance || [])[0] || {};
  const summary = el('div', { class: 'stats' }, [
    stat(t('live.wallet'), el('span', { class: 'num', text: fmtNum(bal.walletBalance), dir: 'ltr' })),
    stat(t('live.slots'), el('span', {
      class: 'num', dir: 'ltr',
      text: `${(st.slots || {}).used ?? '—'} / ${(st.slots || {}).max ?? '—'}`,
    })),
    stat(t('live.notional'), el('span', {
      class: 'num', dir: 'ltr',
      text: `${fmtNum((st.notional || {}).used)} / ${fmtNum((st.notional || {}).cap)}`,
    })),
    stat(t('live.armed'), el('span', {
      class: st.enabled ? 'pill pill--on' : 'pill',
      text: st.enabled ? (st.dry_run ? t('live.dryrun') : t('live.on')) : t('live.off'),
    })),
  ]);

  const positions = (hist && hist.positions) || [];
  host.replaceChildren(
    summary,
    el('h3', { class: 'sect', text: t('live.open') }),
    liveTable(positions, pnlBySymbol),
    el('h3', { class: 'sect', text: t('live.history') }),
    historySummary(st.closed || []),
    historyTable(st.closed || []),
    el('h3', { class: 'sect', text: t('live.chart.title') }),
    positionChart(positions),
  );
}

async function refreshLive() {
  /* One request at a time: the venue call can take over a second, so a 5s timer
   * would otherwise stack requests and render an older mark after a newer one. */
  if (state.liveInFlight) return;
  state.liveInFlight = true;
  try {
    const [s, h] = await Promise.all([API.live(), API.liveHistory()]);
    state.live = s;
    state.liveHistory = h;
    renderBtc(s.btc);
  } catch (err) {
    /* Keep the last good board rather than blanking it on a dropped poll. */
    if (!state.live) {
      document.getElementById('liveBody').replaceChildren(
        el('div', { class: 'empty', text: `${t('error.load')} ${err.message || err}` }));
      return;
    }
  } finally {
    state.liveInFlight = false;
  }
  renderLive();
}

const LIVE_POLL_MS = 3000;

function startLivePolling() {
  clearInterval(state.liveTimer);
  state.liveTimer = setInterval(() => {
    if (state.tab !== 'live' || document.hidden) return;
    refreshLive();
  }, LIVE_POLL_MS);
}


function renderTabs() {
  for (const btn of document.querySelectorAll('.tab')) {
    const active = btn.dataset.panel === state.tab;
    btn.setAttribute('aria-selected', active ? 'true' : 'false');
    btn.classList.toggle('tab--active', active);
  }
  document.getElementById('panelScreener').hidden = state.tab !== 'screener';
  document.getElementById('panelLive').hidden = state.tab !== 'live';
}

function render() {
  applyDirection();
  renderTabs();
  renderScan();
  renderFilters();
  renderBoard();
  renderUnavailable();
  renderSettingsForm();
  renderDrawer();
}

/* ------------------------------------------------------------------- app */

async function refresh() {
  try {
    state.data = await API.state();
    render();
  } catch (err) {
    document.getElementById('board').replaceChildren(
      el('div', { class: 'empty', text: `${t('error.load')} ${err.message || ''}` }));
  }
}

const SCREENER_POLL_MS = 3000;

function startPolling() {
  clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    await refresh();
    if (!(state.data && state.data.scan && state.data.scan.running)) {
      clearInterval(state.pollTimer);
      state.pollTimer = setInterval(refresh, SCREENER_POLL_MS);
    }
  }, 2500);
}

function wireControls() {
  const push = async (patch) => { await API.settings(patch); await refresh(); };

  document.getElementById('profile').addEventListener('change', (e) => push({ profile: e.target.value }));
  document.getElementById('interval').addEventListener('change', (e) => {
    const v = parseFloat(e.target.value);
    if (Number.isFinite(v) && v > 0) push({ scan_interval_minutes: v });
  });

  document.getElementById('rescan').addEventListener('click', async () => {
    const running = state.data && state.data.scan && state.data.scan.running;
    if (running) await API.cancel(); else await API.scan();
    startPolling();
  });

  document.getElementById('langToggle').addEventListener('click', async () => {
    state.lang = state.lang === 'en' ? 'fa' : 'en';
    localStorage.setItem('lang', state.lang);
    await loadStrings(state.lang);
    await API.settings({ language: state.lang });
    /* Charts cache colours and locale at construction, so rebuild them. */
    for (const chart of state.charts.values()) chart.remove();
    state.charts.clear();
    await refresh();
  });

  for (const btn of document.querySelectorAll('.tab')) {
    btn.addEventListener('click', async () => {
      state.tab = btn.dataset.panel;
      localStorage.setItem('tab', state.tab);
      renderTabs();
      /* Fetched when opened rather than kept warm behind a hidden panel — polling
         prices nobody is looking at is wasted work. */
      if (state.tab === 'live') await refreshLive();
    });
  }

  const drawer = document.getElementById('drawer');
  document.getElementById('settingsToggle').addEventListener('click', () => { drawer.hidden = !drawer.hidden; });
  document.getElementById('drawerClose').addEventListener('click', () => { drawer.hidden = true; });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') drawer.hidden = true; });
}

(async function main() {
  await loadStrings(state.lang);
  wireControls();
  await refresh();
  startLivePolling();
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && state.tab === 'live') refreshLive();
  });
  if (state.tab === 'live') await refreshLive();
  if (state.data && state.data.scan && state.data.scan.running) startPolling();
  else state.pollTimer = setInterval(refresh, SCREENER_POLL_MS);
})();

/* Last line on purpose: index.html watches for this to tell a truncated download
 * apart from a server outage. Anything added below it weakens that signal. */
window.__screenerLoaded = true;
