# Market Daily implementation contract

Python 3.9+ standard library only backend; vanilla HTML/CSS/JS frontend. US equities, UI Simplified Chinese. All timestamps ISO-8601 with offset; dates and daily-news filtering use America/New_York. No LLM or API key required. Source failures never substitute demo data.

`providers.py`: expose `collect_snapshot(now=None) -> dict`, `demo_snapshot() -> dict`; may expose pure helpers for tests. Live collection returns this snapshot; persistence owned by server:

```json
{
  "date": "2026-09-18", "generated_at": "2026-09-18T18:00:00-04:00",
  "market_date": "2026-09-18", "mode": "live", "status": "ok",
  "indices": [{"symbol":"SPY","name":"标普 500 ETF","price":600,"change_pct":0.5,"as_of":"2026-09-18T16:00:00-04:00","history":[580,590,600]}],
  "sectors": [{"symbol":"XLK","name":"信息技术","change_pct":2.5,"price":240,"relative_pct":2,"volume_ratio":1.6,"news_count":3,"heat_score":82,"as_of":"2026-09-18T16:00:00-04:00","history":[220,230,240]}],
  "news": [{"id":"abc","title":"Original headline","summary":"Short feed excerpt","url":"https://example.com/article","source":"CNBC","published_at":"2026-09-18T14:00:00-04:00","sectors":["XLK"],"category":"公司动态","importance":"high","score":8}],
  "alerts": [{"symbol":"XLK","name":"信息技术","kind":"price","severity":"high","title":"信息技术显著上涨","detail":"当日 +2.50%，超过 2% 阈值"}],
  "summary": ["Simple factual Chinese summary"],
  "sources": [{"name":"Yahoo Finance","status":"ok","detail":"15 / 15 行情","url":"https://finance.yahoo.com/"}],
  "errors": ["Readable partial failures"]
}
```

`status`: ok / partial / error; missing numeric data null, never fabricated. `market_date` null when no quotes. indices SPY, QQQ, DIA, IWM (explicit ETF proxies). Sectors XLK XLC XLY XLP XLE XLF XLV XLI XLB XLRE XLU; sector comparisons require same trading date as SPY. Daily volume ratio only once session completed; avoid claiming early-close coverage unless exchange calendar known. News for selected/current ET calendar day only, keep today separate from latest quote date on weekends. Keywords yield candidate importance; no causal claims. heat score documented and simple.

Server API:
- GET /api/dashboard?date=YYYY-MM-DD : {"snapshot": snapshot|null,"dates":["2026-09-18"],"collection":{"running":false,"last_error":null},"schedule":{"enabled":true,"hour":18,"minute":0,"timezone":"America/New_York","next_run":"..."}}. Omit date for latest saved live report. GET /api/demo same shape with fixture snapshot, no persistence.
- POST /api/refresh with JSON {} starts background collection and returns 202 {"running":true}; frontend polls /api/dashboard until done. A successful initial visit with no reports can offer/start refresh explicitly. Concurrent refreshes reuse current work. Frontend must NOT automatically demo-fallback.
- GET /api/export?date=YYYY-MM-DD&format=md|json returns saved snapshot as download.
- GET /api/health small health response.

Frontend: create static/index.html, static/styles.css, static/app.js. Elegant editorial light dashboard, violet accent, deliberate readable typography, sidebar, overview cards, sector heatmap, sorted sector rankings, major news and unusual moves. Responsive mobile. Filters news by keyword/category/sector, date archive, refresh, explicit live/demo switch, export. Demo fixture unmistakably labeled. Use safe DOM escaping for all untrusted strings; only http(s) news URLs. Modal/drawer sector detail can show price history and related headlines. Dates source statuses and errors visible. Green US gains/red losses. Never label data real-time.

Root handles server.py, README, run.sh, scheduler, SQLite persistence and integration tests. Agents own assigned files.

## Chinese news and coverage extension

- Keep `snapshot.news` as ET calendar-day news for daily summary and heat scoring. Add `snapshot.recent_news` holding deduplicated RSS entries published during the 72 hours ending at `generated_at`. No future items. Add `news_coverage: {today_count, hours24_count, hours72_count, retention_hours: 72}`. Source status detail explains counts. Defaults remain compatible for historical snapshots missing new fields.
- UI news defaults to 最近24小时, with 今日（美东）/最近24小时/最近72小时 selector. All windows are relative to saved report generated_at, not browser clock. Distinguish dates in news cards. Daily summary and heat remain today-only, clearly explained. Old snapshots may only offer their originally saved day; don't pretend old coverage is complete.
- Retain original `title`, `summary`; add `title_zh`, `summary_zh`, `translation_status` (translated/partial/unavailable/original), `translation_provider` optional. Display Chinese fields when available, expandable English originals, explicit unavailable state. Search both languages, include translations in sector detail and Markdown export. Demo Chinese titles require no translation network requests.
- Root creates `translations.py` with standard-library translation adapter and SQLite cache keyed by original text content; updates server collection/backfill/export. Providers agent owns providers.py and provider tests, frontend agent owns static files. Public title and feed snippet only are sent for translation, never full articles or credentials. Unofficial free translation endpoint best-effort; failure must preserve original news and be visible. Translation failure doesn't erase successful market data.

## Weekly earnings extension

`earnings.py` exposes `collect_earnings(now=None) -> dict`, `demo_earnings() -> dict`. Standard library only. Current week = Monday through Sunday in America/New_York, based on collection time, never silently next week. Universe = companies listed in fetched US-market earnings calendar, market capitalization >= USD10 billion, ranked by market cap descending, max15. Include a count of all source rows, large matches, actual coverage dates and errors. Dates from calendar are anticipated, not company-confirmed unless explicit evidence.

Snapshot `earnings` shape:
```json
{"status":"ok", "week_start":"2026-09-28", "week_end":"2026-10-04", "as_of":"2026-09-28T08:00:00-04:00", "timezone":"America/New_York", "threshold_usd":10000000000, "limit":15, "total_scheduled":42, "total_large":6, "companies":[{"symbol":"EXAMPLE", "name":"Example Company", "name_zh":null, "report_date":"2026-09-29", "report_time":"after_close", "market_cap":120000000000, "eps_estimate":1.23, "eps_currency":"USD", "analyst_count":12, "source_url":"https://www.nasdaq.com/market-activity/earnings?date=2026-09-29", "analyst":{"status":"unavailable", "target_mean":null, "current_price":null, "upside_pct":null, "rating":null, "count":null, "currency":"USD", "horizon":null, "as_of":null, "source_url":null}}], "sources":[{"name":"Nasdaq earnings calendar", "url":"https://www.nasdaq.com/market-activity/earnings", "status":"ok", "detail":"..."}], "errors":[], "note":"财报日历为预计日期，请以公司公告为准。目标价空间不等于财报当天涨跌预测。"}
```

report_time = before_open / after_close / during_market / unknown. Missing values null (EPS0 valid, negative EPS valid). Do not conflate consensus EPS analyst_count with analysts covering price targets. If analyst consensus target available, fetch matching currency current price and calculate `(target_mean/current_price-1)*100`; preserve source and as_of. State published horizon if known, otherwise explicitly unknown. No asserted earnings-day prediction. If unavailable render 暂无预测; don't infer from EPS. Failed calendar queries must yield partial/error, never success with fabricated empty list. Demo clearly fictional names and mode inherited snapshot.

Provider agent owns earnings.py; frontend agent owns static changes; root integrates into server collection, snapshot demo, export, README and current app. Independent tests own tests/test_earnings.py. Earnings errors must not discard market/news success, and old archive without this field must show 尚未收集本周财报, not no firms. Do not relabel archived weeks as current: display explicit week dates and 历史日报 when relevant.

## Leader pullback screen extension

Snapshot `leader_screen` is an additive object. The universe combines exactly ten manually reviewed leaders for each XLK/XLC/XLY/XLP/XLE/XLF/XLV/XLI/XLB/XLRE/XLU sector with the current 100 largest US-listed stocks by Yahoo Finance-reported market capitalization on major US exchanges. Companies are deduplicated by symbol. If the dynamic market-cap source fails, the stable 110-stock leader universe remains available and status is partial. Signals use the latest completed adjusted daily close; unfinished current-session bars cannot create a signal.

`status` is `ok|partial|error`; counts include `universe_size`, `available_count`, `candidate_count`, `sector_leader_count`, and `market_cap_top_100_count`. Each stock has `symbol`, display `name`, `sector`, `universe_tags`, optional `market_cap` and `market_cap_rank`, `status` (`candidate|not_matched|insufficient`), actual `trading_date`, price/high/gain/pullback/SMA values when available, four boolean `checks`, and up to 90 dated adjusted closes. A market-cap-only stock uses sector `OTHER` when the compact ranking source does not provide an ETF-sector classification. Rules and source status are included in the payload. A candidate must pass every rule. Missing history or provider failure is `insufficient`, never a negative signal. This is a research-priority screen, never a buy recommendation or forecast. Demo values are explicitly fictional.

`strategies` describes every selectable strategy and its candidate count. Each stock's `strategy_results` contains independent `status`, `checks`, and `reason` values for `pullback`, `value_momentum`, and `three_week_rise`. The value-momentum strategy requires a positive trailing P/E at or below the lower-30-percentile cutoff (capped at 25), a 20-session adjusted return of at least 3%, and price above SMA50. Missing P/E is insufficient, never treated as cheap.

`three_week_rise` uses the last close from each completed ISO calendar week and requires three consecutive positive week-over-week returns. An in-progress Monday-through-Thursday week is omitted. `weekly_returns_pct` contains the three individual returns and `three_week_return_pct` their compounded total. Nasdaq's stock directory maps market-cap additions back to the eleven dashboard sectors; a missing source classification remains explicit as `OTHER`.

## Market sentiment and static publishing

Snapshot `sentiment` contains a 0–100 score, one of `极度恐慌|偏谨慎|中性|偏乐观|极度乐观`, coverage weight, confidence, explanation and six components. Component weights are trend25, sector breadth20, leader breadth15, SPY momentum15, VIX15 and captured-news keywords10. Missing components are unavailable/null and the aggregate is renormalized over available weights; they are never converted to zero. `sentiment_history` contains at most 30 actually archived daily scores and is never backfilled with fictional live history.

`batch.py` publishes schema version1 API-compatible envelopes to `web-data/latest.json`, `index.json`, `demo.json`, and dated JSON/Markdown files. Writes use a temporary file followed by atomic replace. A failed/error collection cannot replace latest. The frontend first uses the local API and falls back to relative `data/*.json`, allowing one codebase to run under GitHub Pages. Static refresh rereads generated data; it cannot start a collection in the browser.
