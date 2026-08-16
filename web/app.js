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
  demo:       () => fetchJSON('/api/demo'),
  demoReport: () => fetchJSON('/api/demo/report'),
  demoCycle:  () => fetchJSON('/api/demo/cycle', {}),
  demoReset:  () => fetchJSON('/api/demo/reset', {}),
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
  tab: localStorage.getItem('tab') || 'screener',
  demo: null,
  report: null,
  demoTimer: null,
  demoInFlight: false,
  demoError: null,
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

function timeAgo(iso) {
  if (!iso) return t('card.noData');
  const then = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : `${iso}Z`);
  const mins = Math.round((Date.now() - then.getTime()) / 60000);
  if (!Number.isFinite(mins)) return t('card.noData');
  const rtf = new Intl.RelativeTimeFormat(state.lang === 'fa' ? 'fa' : 'en', { numeric: 'auto' });
  if (Math.abs(mins) < 60) return rtf.format(-mins, 'minute');
  return rtf.format(-Math.round(mins / 60), 'hour');
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

  const head = el('summary', { class: 'card__head' }, [verdictBadge(card), idBlock, meta]);
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
    text.textContent = `${t('bar.lastScan')} ${timeAgo(scan.finished_at || scan.started_at)}`;
  }
  document.getElementById('rescan').textContent = running ? t('bar.cancel') : t('bar.rescan');
}

function renderSettingsForm() {
  const s = state.data.settings || {};
  const ex = document.getElementById('exchange');
  if (ex && s.exchange) ex.value = s.exchange;
  document.getElementById('profile').value = s.profile;
  document.getElementById('capital').value = s.capital;
  document.getElementById('risk').value = s.risk_pct;
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

/* ------------------------------------------------------------------ demo */

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

function demoAccount(acc) {
  return el('div', { class: 'demo__account' }, [
    stat(t('demo.equity'), num(acc.equity, { digits: 2, suffix: ' USDT' })),
    stat(t('demo.balance'), num(acc.balance, { digits: 2 })),
    stat(t('demo.openPnl'), signed(acc.open_pnl, { digits: 4 })),
    stat(t('demo.usedMargin'), num(acc.used_margin, { digits: 2 })),
    stat(t('demo.available'), num(acc.available_margin, { digits: 2 })),
    stat(t('demo.return'), signed(acc.return_pct, { digits: 3, suffix: '%' })),
  ]);
}

/* An empty slot is a legitimate state, so it is labelled with its cause rather than
 * left blank. "No qualifying signal" points at the market; "insufficient margin"
 * points at the sizing. Reading one as the other wastes a lot of time. */
function slotReason(reason) {
  if (!reason) return null;
  const detail = reason.detail || {};
  const body = [el('strong', { text: t(`demo.slot.${reason.code}`) })];
  if (reason.code === 'insufficient_margin') {
    body.push(el('span', { class: 'demo__hint' }, [
      document.createTextNode(`${t('demo.slot.needs')} `),
      num(detail.needs, { digits: 2 }),
      document.createTextNode(` · ${t('demo.available')} `),
      num(detail.available, { digits: 2 }),
    ]));
    body.push(el('p', { class: 'demo__note' },
                  sentenceWithCode('demo.slot.slotsHint', '--slots')));
  } else if (reason.code === 'heat_cap') {
    body.push(el('span', { class: 'demo__hint' }, [
      num(reason.heat, { digits: 2, suffix: '%' }),
      document.createTextNode(` / `),
      num(reason.cap, { digits: 1, suffix: '%' }),
    ]));
  } else if (reason.candidates) {
    body.push(el('span', { class: 'demo__hint' }, [
      num(reason.candidates), document.createTextNode(` ${t('demo.slot.candidates')}`),
    ]));
  }
  return el('div', { class: 'demo__slotreason' }, body);
}

function demoSlots(d) {
  const { slots, heat } = d;
  const pips = el('div', { class: 'slots__pips' });
  for (let i = 0; i < slots.total; i += 1) {
    pips.append(el('span', {
      class: `slots__pip${i < slots.filled ? ' slots__pip--filled' : ''}`,
      attrs: { 'aria-hidden': 'true' },
    }));
  }
  const heatPct = Math.min(100, (heat.used_pct / heat.cap_pct) * 100);
  return el('section', { class: 'demo__slots' }, [
    el('div', { class: 'slots__head' }, [
      el('h3', { text: t('demo.slots') }),
      el('span', { class: 'slots__count' }, [
        num(slots.filled), document.createTextNode(` ${t('bar.of')} `), num(slots.total),
      ]),
    ]),
    pips,
    slotReason(slots.reason),
    el('div', { class: 'heat' }, [
      el('div', { class: 'heat__head' }, [
        el('span', { text: t('demo.heat') }),
        /* Heat is an amount of risk, not a gain — showing it with a "+" and the
           profit colour made 3.88% of equity at risk read as money made. */
        el('span', {}, [
          num(heat.used_pct, { digits: 2, suffix: '%' }),
          document.createTextNode(` / `),
          num(heat.cap_pct, { digits: 1, suffix: '%' }),
        ]),
      ]),
      el('div', { class: 'heat__track' }, [
        el('div', { class: 'heat__fill', attrs: { style: `inline-size:${heatPct}%` } }),
      ]),
    ]),
    d.correlation_filter && !d.correlation_filter.available
      ? el('p', { class: 'demo__warn' },
           sentenceWithCode('demo.corrUnavailable', 'market_context.py'))
      : null,
  ]);
}

function positionRow(p) {
  const s = p.state;
  const cells = [
    el('td', {}, [el('div', { class: 'pos__coin', text: p.coin }),
                  el('div', { class: 'pos__sym', text: p.symbol, dir: 'ltr' })]),
    el('td', { class: `pos__side pos__side--${p.side}`,
               text: t(`card.side.${p.side}`) }),
    /* Toobit sizes an order in USDT, so the notional leads. Contracts and coins stay
     * underneath: the contract count is what a venue ticket actually takes, and the
     * two differ by the contract multiplier — 10x on FIL, 1000x on some others. */
    el('td', {}, [
      num(s ? s.notional : null, { digits: 2 }),
      el('div', { class: 'pos__sub' }, [
        num(p.contracts, { digits: 1 }),
        el('span', { text: ` ${t('demo.contracts')} · ` }),
        num(s ? s.coins : null, { digits: 4 }),
      ]),
    ]),
    el('td', {}, [num(p.entry_price, { digits: 6 })]),
    el('td', {}, [num(s ? s.mark : null, { digits: 6 }),
                  el('div', { class: 'pos__sub', text: p.mark_source || '' })]),
    /* Targets sit under the stop rather than in columns of their own: the row already
     * carries twelve figures, and what a reader needs at a glance is the three levels
     * that end the trade, together. A filled TP1 is marked, because a position whose
     * stop now sits at breakeven is a different trade from the one that was opened. */
    el('td', {}, [
      num(p.stop, { digits: 6 }),
      el('div', { class: 'pos__sub' }, [
        el('span', { text: `${t('demo.tp1')} ` }), num(p.tp1, { digits: 6 }),
        p.tp1_filled ? el('span', { class: 'pos__filled', text: ` ${t('demo.filled')}` }) : null,
      ]),
      el('div', { class: 'pos__sub' }, [
        el('span', { text: `${t('demo.tp2')} ` }), num(p.tp2, { digits: 6 }),
      ]),
      p.stop_moved_to_be
        ? el('div', { class: 'pos__sub pos__be', text: t('demo.stopAtBe') }) : null,
    ]),
    el('td', {}, [num(s ? s.liquidation_price : null, { digits: 6 })]),
    el('td', {}, [num(p.leverage, { digits: 2, suffix: '×' })]),
    /* Funding rides under margin rather than taking a column of its own: at 13
       columns the last one fell outside the viewport, and an accrued cost nobody can
       see is the same as not showing it. Both are collateral-side figures. */
    el('td', {}, [num(p.margin, { digits: 2 }),
                  el('div', { class: 'pos__sub' }, [
                    signed(p.funding_paid, { digits: 5 })])]),
    el('td', {}, [signed(s ? s.unrealised_pnl : null, { digits: 4 }),
                  el('div', { class: 'pos__sub' }, [
                    signed(s ? s.unrealised_r : null, { digits: 3, suffix: 'R' })])]),
    /* MFE gets its own column rather than hiding in a detail panel: a trade that ran
     * to +1.4R and came back is a management failure, one that never moved is a
     * thesis failure, and they are indistinguishable from P&L alone.
     *
     * Shown in USDT with R underneath. R is the comparable unit across coins, but
     * cash is the one the number is actually felt in, so it leads. */
    el('td', {}, [signed(inUsdt(p.mfe_r, p.risk_amount), { digits: 4 }),
                  el('div', { class: 'pos__sub' }, [
                    signed(p.mfe_r, { digits: 2, suffix: 'R' })]),
                  el('div', { class: 'pos__sub pos__mae' }, [
                    signed(inUsdt(p.mae_r, p.risk_amount), { digits: 4 })]),
                  el('div', { class: 'pos__sub' }, [
                    signed(p.mae_r, { digits: 2, suffix: 'R' })])]),
    el('td', {}, [num(s ? s.margin_ratio_pct : null, { digits: 2, suffix: '%' })]),
  ];
  const row = el('tr', {}, cells);
  if (!s) row.classList.add('pos--stale');
  return row;
}

function demoPositions(d) {
  if (!d.positions.length) {
    return el('div', { class: 'empty', text: t('demo.noPositions') });
  }
  const headers = ['demo.col.coin', 'demo.col.side', 'demo.col.sizeUsdt', 'demo.col.entry',
                   'demo.col.mark', 'demo.col.levels', 'demo.col.liq', 'demo.col.lev',
                   'demo.col.marginFunding', 'demo.col.upnl', 'demo.col.mfe',
                   'demo.col.marginRatio'];
  /* The unit is stated once, above the table, rather than repeated in every header.
     Putting "(USDT)" on four headers pushed the table 91px past its container, and a
     column that scrolls out of sight tells the reader nothing. */
  return el('div', {}, [
    el('p', { class: 'demo__unit', text: t('demo.unitNote') }),
    el('div', { class: 'table-wrap' }, [
    el('table', { class: 'postable' }, [
      el('thead', {}, [el('tr', {}, headers.map((k) => el('th', { text: t(k) })))]),
      el('tbody', {}, d.positions.map(positionRow)),
    ]),
    ]),
  ]);
}

function reportBlock(r) {
  if (!r) return null;
  const a = r.aggregate;
  const kids = [el('h3', { text: t('demo.report') })];

  if (!r.sample.sufficient) {
    kids.push(el('p', { class: 'demo__warn' }, [
      document.createTextNode(`${t('demo.sampleShort')} `),
      num(r.sample.closed), document.createTextNode(' / '), num(r.sample.minimum),
    ]));
  }

  kids.push(el('div', { class: 'demo__account' }, [
    stat(t('demo.closed'), num(a.closed)),
    stat(t('demo.winRate'), num(a.win_rate, { digits: 1, suffix: '%' })),
    stat(t('demo.expectancy'), signed(a.expectancy_r, { digits: 3, suffix: 'R' })),
    stat(t('demo.totalR'), signed(a.total_r, { digits: 2, suffix: 'R' })),
    stat(t('demo.maxDd'), signed(a.max_drawdown_r, { digits: 2, suffix: 'R' })),
    stat(t('demo.costs'), num(a.costs_paid, { digits: 4 })),
  ]));

  if (r.by_exit_reason.length) {
    kids.push(el('h4', { text: t('demo.byExit') }));
    kids.push(el('div', { class: 'table-wrap' }, [
      el('table', { class: 'postable' }, [
        el('thead', {}, [el('tr', {}, ['demo.col.reason', 'demo.col.count',
          'demo.col.share', 'demo.col.avgR', 'demo.col.totalR', 'demo.col.avgMfe']
          .map((k) => el('th', { text: t(k) })))]),
        el('tbody', {}, r.by_exit_reason.map((b) => el('tr', {}, [
          el('td', { text: t(`demo.exit.${b.reason}`) }),
          el('td', {}, [num(b.count)]),
          el('td', {}, [num(b.share_pct, { digits: 1, suffix: '%' })]),
          el('td', {}, [signed(b.avg_r, { digits: 3, suffix: 'R' })]),
          el('td', {}, [signed(b.total_r, { digits: 2, suffix: 'R' })]),
          el('td', {}, [signed(b.avg_mfe_r, { digits: 2, suffix: 'R' })]),
        ]))),
      ]),
    ]));
  }
  return el('section', { class: 'demo__report' }, kids);
}

function renderDemo() {
  const host = document.getElementById('demoBody');
  const d = state.demo;
  if (!d) {
    host.replaceChildren(el('div', { class: 'empty', text: t('demo.loading') }));
    return;
  }
  host.replaceChildren(
    el('section', { class: 'demo__head' }, [
      el('div', {}, [
        el('h2', { text: t('demo.title') }),
        el('p', { class: 'demo__note', text: t('demo.subtitle') }),
      ]),
      el('div', { class: 'demo__actions' }, [
        el('button', { class: 'btn', attrs: { id: 'demoCycle' }, text: t('demo.runCycle') }),
        el('button', { class: 'btn btn--ghost', attrs: { id: 'demoReset' }, text: t('demo.reset') }),
      ]),
    ]),
    demoAccount(d.account),
    demoSlots(d),
    demoPositions(d),
    reportBlock(state.report),
  );

  document.getElementById('demoCycle').addEventListener('click', async (e) => {
    e.target.disabled = true;
    e.target.textContent = t('demo.running');
    try { await API.demoCycle(); } finally { await refreshDemo(); }
  });
  document.getElementById('demoReset').addEventListener('click', async () => {
    if (!window.confirm(t('demo.resetConfirm'))) return;
    await API.demoReset();
    await refreshDemo();
  });
}

async function refreshDemo() {
  /* One request at a time. Marks come from a live endpoint that can take over a
   * second, so a 3s timer would otherwise stack requests it never waits for, and the
   * page would render them out of order — an older mark landing after a newer one. */
  if (state.demoInFlight) return;
  state.demoInFlight = true;
  try {
    const [d, r] = await Promise.all([API.demo(), API.demoReport()]);
    state.demo = d;
    state.report = r;
    state.demoError = null;
  } catch (err) {
    /* A dropped poll keeps the last good board rather than blanking it. Losing one
     * refresh should not throw away every figure on screen. */
    state.demoError = err.message || String(err);
    if (!state.demo) {
      document.getElementById('demoBody').replaceChildren(
        el('div', { class: 'empty', text: `${t('error.load')} ${state.demoError}` }));
      return;
    }
  } finally {
    state.demoInFlight = false;
  }
  renderDemo();
}

const DEMO_POLL_MS = 3000;

function startDemoPolling() {
  clearInterval(state.demoTimer);
  state.demoTimer = setInterval(() => {
    /* Only while the tab is visible and in front. Polling a hidden tab every 3s
     * spends requests on a board nobody is reading. */
    if (state.tab !== 'demo' || document.hidden) return;
    refreshDemo();
  }, DEMO_POLL_MS);
}

function renderTabs() {
  for (const btn of document.querySelectorAll('.tab')) {
    const active = btn.dataset.panel === state.tab;
    btn.setAttribute('aria-selected', active ? 'true' : 'false');
    btn.classList.toggle('tab--active', active);
  }
  document.getElementById('panelScreener').hidden = state.tab !== 'screener';
  document.getElementById('panelDemo').hidden = state.tab !== 'demo';
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

function startPolling() {
  clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    await refresh();
    if (!(state.data && state.data.scan && state.data.scan.running)) {
      clearInterval(state.pollTimer);
      state.pollTimer = setInterval(refresh, 60000);
    }
  }, 2500);
}

function wireControls() {
  const push = async (patch) => { await API.settings(patch); await refresh(); };

  document.getElementById('profile').addEventListener('change', (e) => push({ profile: e.target.value }));
  /* Switching venue changes which results are in scope, so the board is rebuilt
     from scratch rather than reconciled — the two venues share no symbols. */
  document.getElementById('exchange').addEventListener('change', async (e) => {
    for (const chart of state.charts.values()) chart.remove();
    state.charts.clear();
    await push({ exchange: e.target.value });
  });
  for (const [id, key] of [['capital', 'capital'], ['risk', 'risk_pct'], ['interval', 'scan_interval_minutes']]) {
    document.getElementById(id).addEventListener('change', (e) => {
      const v = parseFloat(e.target.value);
      if (Number.isFinite(v) && v > 0) push({ [key]: v });
    });
  }

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
      /* The demo reads live marks, so it is fetched when opened rather than kept warm
         behind a hidden panel — polling prices nobody is looking at is wasted work. */
      if (state.tab === 'demo') await refreshDemo();
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
  startDemoPolling();
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && state.tab === 'demo') refreshDemo();
  });
  if (state.tab === 'demo') await refreshDemo();
  if (state.data && state.data.scan && state.data.scan.running) startPolling();
  else state.pollTimer = setInterval(refresh, 60000);
})();
