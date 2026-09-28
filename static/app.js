'use strict';

(() => {
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const ET = 'America/New_York';
  const SECTORS = [
    { symbol: 'XLK', name: '信息技术' }, { symbol: 'XLC', name: '通信服务' },
    { symbol: 'XLF', name: '金融' }, { symbol: 'XLY', name: '可选消费' },
    { symbol: 'XLI', name: '工业' }, { symbol: 'XLV', name: '医疗保健' },
    { symbol: 'XLE', name: '能源' }, { symbol: 'XLP', name: '必需消费' },
    { symbol: 'XLU', name: '公用事业' }, { symbol: 'XLB', name: '原材料' },
    { symbol: 'XLRE', name: '房地产' },
  ];
  const INDICES = [
    { symbol: 'SPY', name: '标普 500' }, { symbol: 'QQQ', name: '纳斯达克 100' },
    { symbol: 'DIA', name: '道琼斯工业' }, { symbol: 'IWM', name: '罗素 2000' },
  ];
  const state = {
    mode: 'live', snapshot: null, dates: [], collection: {}, schedule: {},
    selectedDate: '', category: 'all', sector: 'all', keyword: '', newsLimit: 6, newsRange: '24h',
    sort: 'heat_score', earningsDay: '', leaderStrategy: 'pullback', leaderSector: 'all', leaderView: 'candidate', requestVersion: 0, busy: false, refreshPending: false,
    pollTimer: null, toastTimer: null, fetchError: '', staticMode: false,
  };
  let chartId = 0;

  function escapeHTML(value) {
    return String(value ?? '').replace(/[&<>"']/g, (char) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[char]);
  }
  const esc = escapeHTML;
  const isNumber = (value) => typeof value === 'number' && Number.isFinite(value);
  const asArray = (value) => Array.isArray(value) ? value : [];
  const dateIsValid = (value) => /^\d{4}-\d{2}-\d{2}$/.test(value || '');
  const formatNumber = (value, digits = 2) => isNumber(value)
    ? new Intl.NumberFormat('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value)
    : '—';
  const percent = (value, suffix = '%') => isNumber(value)
    ? `${value > 0 ? '+' : ''}${formatNumber(value)}${suffix}` : '—';
  const direction = (value) => isNumber(value) ? value > 0 ? 'positive' : value < 0 ? 'negative' : 'neutral' : 'neutral';
  const sectorName = (symbol) => symbol === 'OTHER' ? '市值前 100' : SECTORS.find((sector) => sector.symbol === symbol)?.name || symbol;

  function safeURL(value) {
    try {
      const url = new URL(String(value));
      return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
    } catch (_) { return ''; }
  }

  function formatTime(value, options = {}) {
    if (!value) return '时间未知';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '时间未知';
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: ET, hour: '2-digit', minute: '2-digit', hour12: false, ...options,
    }).format(date);
  }

  function formatDate(value, weekday = false) {
    if (!dateIsValid(value)) return '日期未知';
    const date = new Date(`${value}T12:00:00Z`);
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: ET, year: 'numeric', month: '2-digit', day: '2-digit',
      ...(weekday ? { weekday: 'short' } : {}),
    }).format(date);
  }

  function historyValues(history) {
    return asArray(history).map((item) => typeof item === 'object' && item !== null ? item.close : item).filter(isNumber);
  }

  function chart(history, change, className = 'sparkline', label = '近期收盘价走势') {
    const values = historyValues(history);
    if (values.length < 2) return '';
    const width = 150, height = 48, padding = 3;
    const low = Math.min(...values), high = Math.max(...values), range = high - low || Math.max(Math.abs(high) * 0.005, 1);
    const points = values.map((value, index) => [
      padding + index / (values.length - 1) * (width - padding * 2),
      high === low ? height / 2 : padding + (high - value) / range * (height - padding * 2),
    ]);
    const path = points.map((point, index) => `${index ? 'L' : 'M'}${point[0].toFixed(2)},${point[1].toFixed(2)}`).join(' ');
    const fill = `${path} L${width - padding},${height} L${padding},${height} Z`;
    const id = `chart-fill-${++chartId}`;
    const color = direction(change) === 'negative' ? '#cc898b' : direction(change) === 'positive' ? '#73aa90' : '#b2a3c6';
    return `<svg class="${esc(className)}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="${esc(label)}"><defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${color}" stop-opacity=".2"/><stop offset="100%" stop-color="${color}" stop-opacity="0"/></linearGradient></defs><path d="${fill}" fill="url(#${id})" stroke="none"/><path d="${path}" stroke="${color}" fill="none" vector-effect="non-scaling-stroke"/></svg>`;
  }

  function emptyContent(title, detail = '') {
    return `<div class="empty-content"><svg aria-hidden="true" viewBox="0 0 32 32"><rect x="6" y="5" width="20" height="23" rx="3"/><path d="M11 11h10M11 16h10m-10 5h6"/></svg>${esc(title)}${detail ? `<small>${esc(detail)}</small>` : ''}</div>`;
  }

  function mergedSectors() {
    const available = asArray(state.snapshot?.sectors);
    return SECTORS.map((sector) => ({ ...sector, ...available.find((item) => item.symbol === sector.symbol) }));
  }

  function renderIndices() {
    const available = asArray(state.snapshot?.indices);
    $('#indices-grid').innerHTML = INDICES.map((index) => {
      const quote = available.find((item) => item.symbol === index.symbol) || {};
      const hasPrice = isNumber(quote.price);
      const trend = direction(quote.change_pct);
      const arrow = isNumber(quote.change_pct) && quote.change_pct !== 0
        ? `<svg aria-hidden="true" viewBox="0 0 16 16"><path d="${quote.change_pct > 0 ? 'm4 10 4-4 4 4M8 6v7' : 'm4 6 4 4 4-4M8 3v7'}"/></svg>` : '';
      return `<article class="index-card ${hasPrice ? '' : 'index-card-empty'}"><div class="index-top"><h2 class="index-name">${index.name}</h2><span class="ticker-tag">${index.symbol}</span></div><div class="index-value">${formatNumber(quote.price)}</div><div class="index-bottom"><span class="index-change ${trend}">${arrow}${percent(quote.change_pct)}<small>当日</small></span></div><div class="index-chart">${chart(quote.history, quote.change_pct, 'sparkline', `${index.symbol} 近期收盘价走势`)}</div><div class="index-asof">${hasPrice ? `ETF · USD · ${esc(formatTime(quote.as_of, { month: '2-digit', day: '2-digit' }))} ET` : 'ETF 代理 · 等待行情'}</div></article>`;
    }).join('');
  }

  function heatClass(value) {
    if (!isNumber(value)) return 'heatmap-missing';
    if (Math.abs(value) < 0.005) return 'heatmap-flat';
    const intensity = Math.abs(value) >= 1.5 ? 'strong' : Math.abs(value) >= 0.6 ? 'medium' : 'light';
    return `heatmap-${value > 0 ? 'up' : 'down'}-${intensity}`;
  }

  function renderSectors() {
    const sectors = mergedSectors();
    $('#sector-heatmap').innerHTML = sectors.map((sector) => {
      const stale = sector.comparable === false && sector.trading_date;
      const change = stale ? null : sector.change_pct;
      const label = stale ? `旧报价 ${sector.trading_date}` : isNumber(change) ? `当日 ${percent(change)}` : '暂无行情';
      return `<button type="button" class="heatmap-cell ${heatClass(change)}" data-sector="${esc(sector.symbol)}" aria-label="${esc(sector.name)}，${esc(label)}，查看详情"><span class="heatmap-name">${esc(sector.name)}</span><strong class="heatmap-value">${percent(change)}</strong><span class="heatmap-symbol">${esc(sector.symbol)}${stale ? ` · ${esc(sector.trading_date)}` : ''}</span>${stale ? '' : chart(sector.history, change, 'tile-chart', `${sector.symbol} 走势`)}</button>`;
    }).join('');
    const sortKey = state.sort;
    sectors.sort((a, b) => {
      const left = a.comparable !== false && isNumber(a[sortKey]) ? a[sortKey] : -Infinity;
      const right = b.comparable !== false && isNumber(b[sortKey]) ? b[sortKey] : -Infinity;
      return left === right ? 0 : right - left;
    });
    $('#sector-ranking').innerHTML = sectors.map((sector, index) => {
      const score = isNumber(sector.heat_score) ? Math.max(0, Math.min(100, sector.heat_score)) : 0;
      const change = sector.comparable === false ? null : sector.change_pct;
      const incomplete = sector.heat_complete === false && isNumber(sector.heat_score);
      return `<tr><td><button type="button" class="ranking-sector-button" data-sector="${esc(sector.symbol)}" aria-label="查看${esc(sector.name)}详情"><span class="ranking-number">${String(index + 1).padStart(2, '0')}</span><span><span class="ranking-name">${esc(sector.name)}</span><span class="ranking-symbol">${esc(sector.symbol)}</span></span></button></td><td class="ranking-change ${direction(change)}">${percent(change)}</td><td><span class="heat-score" title="${incomplete ? '分项数据不完整，缺失项未计分' : '规则关注度得分'}"><span class="heat-score-track" aria-hidden="true"><span class="heat-score-fill" style="width:${score}%"></span></span><span class="heat-score-value">${formatNumber(sector.heat_score, 0)}${incomplete ? '*' : ''}</span></span></td></tr>`;
    }).join('');
    const marketDate = state.snapshot?.market_date;
    $('#quote-date-note').textContent = marketDate ? `· ${marketDate.slice(5).replace('-', '/')}` : '· 暂无报价';
    $('#sector-heatmap').setAttribute('aria-label', marketDate ? `${marketDate} 最新交易日板块涨跌幅` : '暂无板块报价');
  }

  function etDateKey(value) {
    const date = value ? new Date(value) : new Date();
    if (Number.isNaN(date.getTime())) return '';
    const parts = new Intl.DateTimeFormat('en-US', { timeZone: ET, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(date);
    const field = (name) => parts.find((part) => part.type === name)?.value || '';
    return `${field('year')}-${field('month')}-${field('day')}`;
  }

  function formatMarketCap(value) {
    if (!isNumber(value)) return '—';
    return value >= 1e12 ? `${formatNumber(value / 1e12)} 万亿` : `${formatNumber(value / 1e8, 1)} 亿`;
  }

  function earningsCompanies(earnings) {
    const threshold = Math.max(1e10, isNumber(earnings?.threshold_usd) ? earnings.threshold_usd : 1e10);
    return asArray(earnings?.companies).filter((company) => isNumber(company.market_cap) && company.market_cap >= threshold)
      .sort((a, b) => b.market_cap - a.market_cap).slice(0, 15);
  }

  function earningsAnalyst(company, earnings) {
    const analyst = company.analyst;
    if (!analyst?.status || ['unavailable', 'error'].includes(analyst.status) || !isNumber(analyst.target_mean) || analyst.target_mean <= 0) {
      return '<span class="earnings-unavailable">暂无预测</span><small>未取得可核对的分析师目标价</small>';
    }
    const currency = typeof analyst.currency === 'string' && analyst.currency ? analyst.currency : '币种未提供';
    const hasUpside = currency !== '币种未提供' && isNumber(analyst.current_price) && analyst.current_price > 0 && isNumber(analyst.upside_pct);
    const source = safeURL(analyst.source_url);
    const oldQuote = analyst.as_of && earnings.as_of && etDateKey(analyst.as_of) < etDateKey(earnings.as_of);
    const horizon = analyst.horizon ? `目标期限：${analyst.horizon}` : '期限未提供';
    const ratingLabels = { strong_buy: '强烈买入', buy: '买入', hold: '持有', underperform: '表现逊于大盘', sell: '卖出', strong_sell: '强烈卖出' };
    const rating = analyst.rating ? ratingLabels[analyst.rating] || analyst.rating : '';
    return `<div class="earnings-target"><strong>${esc(currency)} ${formatNumber(analyst.target_mean)}</strong>${hasUpside ? `<span class="earnings-upside ${direction(analyst.upside_pct)}">${percent(analyst.upside_pct)}</span>` : '<span class="earnings-unavailable">空间待补</span>'}</div><small class="earnings-horizon">${esc(horizon)}${oldQuote ? ' · 旧报价' : ''}</small><details class="earnings-analyst-details"><summary>查看依据 <span aria-hidden="true">⌄</span></summary><div><p>参考股价：${isNumber(analyst.current_price) ? `${esc(currency)} ${formatNumber(analyst.current_price)}` : '未提供'}</p><p>目标价覆盖：${isNumber(analyst.count) ? `${formatNumber(analyst.count, 0)} 位分析师` : '人数未提供'}${rating ? ` · ${esc(rating)}` : ''}</p><p>数据时间：${analyst.as_of ? `${esc(formatTime(analyst.as_of, { year: 'numeric', month: '2-digit', day: '2-digit' }))} ET${oldQuote ? '（早于本次采集日）' : ''}` : '未提供'}</p>${source ? `<a href="${esc(source)}" target="_blank" rel="noopener noreferrer">查看目标价来源 ↗</a>` : '<p>目标价来源链接未提供</p>'}<p>目标价隐含空间非财报当天涨跌预测。</p></div></details>`;
  }

  function renderLeaderScreen() {
    const screen = state.snapshot?.leader_screen;
    const stocks = asArray(screen?.stocks);
    const strategies = asArray(screen?.strategies).length ? screen.strategies : [{ id: 'pullback', name: '龙头回调', candidate_count: screen?.candidate_count || 0 }];
    if (!strategies.some((strategy) => strategy.id === state.leaderStrategy)) state.leaderStrategy = strategies[0].id;
    const activeStrategy = strategies.find((strategy) => strategy.id === state.leaderStrategy) || strategies[0];
    const strategyResult = (stock) => stock.strategy_results?.[state.leaderStrategy] || (state.leaderStrategy === 'pullback'
      ? { status: stock.status, checks: stock.checks, reason: stock.reason } : { status: 'insufficient', checks: {}, reason: '该日报尚无此策略数据' });
    const candidates = stocks.filter((stock) => strategyResult(stock).status === 'candidate');
    $('#leaders-strategy-tabs').innerHTML = strategies.map((strategy) => `<button type="button" data-leader-strategy="${esc(strategy.id)}" class="${state.leaderStrategy === strategy.id ? 'selected' : ''}" aria-pressed="${state.leaderStrategy === strategy.id}"><span>${esc(strategy.name)}</span><strong>${formatNumber(strategy.candidate_count, 0)}</strong></button>`).join('');
    const sectorCounts = Object.fromEntries(SECTORS.map((sector) => [sector.symbol,
      candidates.filter((stock) => stock.sector === sector.symbol).length]));
    const top100Candidates = candidates.filter((stock) => asArray(stock.universe_tags).includes('market_cap_top_100')).length;
    $('#leaders-sector').innerHTML = `<option value="all">全部股票</option><option value="TOP100">市值前 100 · ${top100Candidates} 个候选</option>` + SECTORS.map((sector) => `<option value="${sector.symbol}">${esc(sector.name)} · ${sectorCounts[sector.symbol] || 0} 个候选</option>`).join('');
    if (state.leaderSector !== 'all' && state.leaderSector !== 'TOP100' && !SECTORS.some((sector) => sector.symbol === state.leaderSector)) state.leaderSector = 'all';
    $('#leaders-sector').value = state.leaderSector;
    $('#leaders-view').value = state.leaderView;
    $('#leaders-sector-tabs').innerHTML = `<button type="button" data-leader-sector="TOP100" class="${state.leaderSector === 'TOP100' ? 'selected' : ''}" aria-pressed="${state.leaderSector === 'TOP100'}"><span>市值前 100</span><strong>${top100Candidates}</strong></button>` + SECTORS.map((sector) => `<button type="button" data-leader-sector="${sector.symbol}" class="${state.leaderSector === sector.symbol ? 'selected' : ''}" aria-pressed="${state.leaderSector === sector.symbol}"><span>${esc(sector.name)}</span><strong>${sectorCounts[sector.symbol] || 0}</strong></button>`).join('');
    const rules = screen?.rules || {};
    const ruleItems = state.leaderStrategy === 'value_momentum' ? [
      ['正市盈率', '仅比较盈利为正且取得可核对市盈率的公司'],
      ['相对低估', `市盈率位于股票池较低 30%${isNumber(activeStrategy?.pe_cutoff) ? `，本期上限 ${formatNumber(activeStrategy.pe_cutoff)} 倍` : ''}`],
      ['近期上涨', `最近 ${activeStrategy?.momentum_days || 20} 个交易日涨幅 ≥ ${activeStrategy?.momentum_min_pct || 3}%`],
      ['趋势确认', '股价站上 50 日均线'],
    ] : [
      ['前期上涨', `高点较 60 个交易日前 ≥ ${isNumber(rules.gain_min_pct) ? rules.gain_min_pct : 20}%`],
      ['高点够近', `20 日高点出现在最近 ${isNumber(rules.high_recency) ? rules.high_recency : 10} 个交易日`],
      ['开始回调', `距高点回落 ${isNumber(rules.pullback_min_pct) ? rules.pullback_min_pct : 5}%–${isNumber(rules.pullback_max_pct) ? rules.pullback_max_pct : 12}%`],
      ['趋势向上', '股价 > 50 日均线 > 200 日均线'],
    ];
    $('#leaders-rules').innerHTML = ruleItems.map(([label, detail]) => `<div><strong>${label}</strong><span>${detail}</span></div>`).join('');
    let visible = stocks.filter((stock) => state.leaderSector === 'all' || (state.leaderSector === 'TOP100'
      ? asArray(stock.universe_tags).includes('market_cap_top_100') : stock.sector === state.leaderSector));
    if (state.leaderView === 'candidate') visible = visible.filter((stock) => strategyResult(stock).status === 'candidate');
    visible.sort((a, b) => (strategyResult(a).status !== 'candidate') - (strategyResult(b).status !== 'candidate') || (state.leaderStrategy === 'value_momentum' ? (b.return_20d_pct || -Infinity) - (a.return_20d_pct || -Infinity) : (b.gain_pct || -Infinity) - (a.gain_pct || -Infinity)));
    const checkLabels = state.leaderStrategy === 'value_momentum'
      ? { positive_pe: '正市盈率', low_pe: '相对低估', recent_momentum: '近期上涨', above_sma50: '站上 50 日线' }
      : { prior_gain: '前期上涨', recent_high: '高点够近', pullback: '回调幅度', uptrend: '上升趋势' };
    $('#leaders-list').innerHTML = visible.map((stock) => {
      const history = asArray(stock.history).map((item) => item?.close).filter(isNumber);
      const result = strategyResult(stock);
      const insufficient = result.status === 'insufficient';
      const quoteURL = `https://finance.yahoo.com/quote/${encodeURIComponent(stock.symbol || '')}/`;
      const checks = Object.entries(checkLabels).map(([key, label]) => `<span class="leader-check ${result.checks?.[key] ? 'passed' : 'failed'}"><i aria-hidden="true">${result.checks?.[key] ? '✓' : '–'}</i>${label}</span>`).join('');
      const tags = asArray(stock.universe_tags);
      const origins = `${tags.includes('sector_leader') ? '<span>板块龙头</span>' : ''}${tags.includes('market_cap_top_100') ? `<span>市值第 ${formatNumber(stock.market_cap_rank, 0)} 名</span>` : ''}`;
      const metrics = state.leaderStrategy === 'value_momentum'
        ? `<div><span>滚动市盈率</span><strong>${isNumber(stock.pe_ratio) ? `${formatNumber(stock.pe_ratio)}×` : '—'}</strong><small>公开行情字段</small></div><div><span>近 20 日涨幅</span><strong class="${direction(stock.return_20d_pct)}">${percent(stock.return_20d_pct)}</strong><small>复权收盘价</small></div><div><span>相对 50 日线</span><strong class="${direction(isNumber(stock.price) && isNumber(stock.sma50) ? stock.price - stock.sma50 : null)}">${isNumber(stock.price) && isNumber(stock.sma50) ? percent((stock.price / stock.sma50 - 1) * 100) : '—'}</strong><small>${esc(stock.trading_date || '日期未知')}</small></div>`
        : `<div><span>最新复权收盘</span><strong>$${formatNumber(stock.price)}</strong><small>${esc(stock.trading_date || '日期未知')}</small></div><div><span>前期上涨</span><strong class="${direction(stock.gain_pct)}">${percent(stock.gain_pct)}</strong><small>高点相对 60 日前</small></div><div><span>距近期高点</span><strong class="negative">${percent(stock.pullback_pct)}</strong><small>${esc(stock.high_date || '')} 高点</small></div>`;
      return `<article class="leader-stock ${result.status === 'candidate' ? 'leader-candidate' : ''}" data-leader-symbol="${esc(stock.symbol || '')}"><header><div><a href="${esc(quoteURL)}" target="_blank" rel="noopener noreferrer"><strong>${esc(stock.symbol || '—')}</strong><span>${esc(stock.name || '')}</span></a><small>${esc(sectorName(stock.sector))} · ${esc(stock.sector || '')}</small><div class="leader-origins">${origins}</div></div><span class="leader-state ${result.status}">${result.status === 'candidate' ? '符合观察条件' : insufficient ? '数据不足' : '暂未满足'}</span></header>${insufficient ? `<div class="leader-insufficient">${esc(result.reason || '策略所需数据不足')}</div>` : `<div class="leader-metrics">${metrics}</div><div class="leader-chart">${history.length > 1 ? chart(history, state.leaderStrategy === 'value_momentum' ? stock.return_20d_pct : stock.pullback_pct, 'leader-sparkline', `${stock.symbol} 近 ${history.length} 个交易日复权收盘走势`) : ''}</div><div class="leader-checks">${checks}</div>`}</article>`;
    }).join('');
    const scopeCount = state.leaderSector === 'all' ? screen?.universe_size : state.leaderSector === 'TOP100'
      ? stocks.filter((stock) => asArray(stock.universe_tags).includes('market_cap_top_100')).length
      : stocks.filter((stock) => stock.sector === state.leaderSector).length;
    $('#leaders-count').textContent = screen ? String(candidates.length) : '—';
    const statusLabel = { ok: `${screen?.universe_size || 0} 家已更新`, partial: '部分行情可用', error: '行情暂不可用' };
    $('#leaders-status').textContent = state.mode === 'demo' && screen ? '演示筛选' : screen ? statusLabel[screen.status] || '状态待确认' : '尚未收集';
    $('#leaders-status').className = `subtle-tag ${screen?.status !== 'ok' ? 'earnings-status-warning' : ''}`;
    $('#leaders-asof').textContent = screen?.as_of ? `筛选采集于 ${formatTime(screen.as_of, { year: 'numeric', month: '2-digit', day: '2-digit' })} ET · 信号只使用完整交易日` : '使用最近一个完整交易日的复权收盘价';
    $('#leaders-empty').classList.toggle('hidden', !!visible.length);
    $('#leaders-empty').innerHTML = visible.length ? '' : emptyContent(screen ? '当前筛选下暂无候选' : '尚未收集个股筛选数据', screen ? '可以切换板块，或选择“查看全部股票”检查每项条件。' : '点击「收集最新数据」后，系统会检查板块龙头与市值前 100 股票。');
    $('#leaders-list').classList.toggle('hidden', !visible.length);
    $('#leaders-coverage').textContent = screen ? `合并股票池 ${scopeCount || 0} 家 · 板块龙头 ${screen.sector_leader_count || 110} 家 · 市值榜 ${screen.market_cap_top_100_count || 0} / 100 家 · 数据可用 ${screen.available_count || 0} / ${screen.universe_size || 0} 家 · 当前显示 ${visible.length} 家` : '';
    $('#leaders-note').textContent = screen?.note || '规则筛选的研究候选，不是买入建议。历史走势不能保证未来表现。';
    const errors = asArray(screen?.errors);
    $('#leaders-errors').classList.toggle('hidden', !errors.length);
    $('#leaders-errors').innerHTML = errors.slice(0, 12).map((error) => `<p>${esc(error)}</p>`).join('') + (errors.length > 12 ? `<p>另有 ${errors.length - 12} 项读取错误。</p>` : '');
  }

  function renderEarnings() {
    const earnings = state.snapshot?.earnings;
    const companies = earningsCompanies(earnings);
    const hasWeek = dateIsValid(earnings?.week_start) && dateIsValid(earnings?.week_end);
    const anchor = hasWeek ? earnings.week_start : dateIsValid(state.snapshot?.date) ? state.snapshot.date : etDateKey();
    const monday = new Date(`${anchor}T12:00:00Z`);
    monday.setUTCDate(monday.getUTCDate() - (monday.getUTCDay() + 6) % 7);
    const dates = Array.from({ length: 5 }, (_, index) => {
      const day = new Date(monday);
      day.setUTCDate(monday.getUTCDate() + index);
      return day.toISOString().slice(0, 10);
    });
    if (!dates.includes(state.earningsDay)) state.earningsDay = dates.includes(etDateKey()) ? etDateKey() : dates[0];
    const focusedTab = $('#earnings-tabs').contains(document.activeElement);
    const coveredDates = [...new Set(asArray(earnings?.coverage_dates).filter(dateIsValid))].sort();
    const dayCovered = hasWeek && (earnings.status === 'ok' || coveredDates.includes(state.earningsDay));
    const weekdayLabels = ['周一', '周二', '周三', '周四', '周五'];
    $('#earnings-tabs').innerHTML = dates.map((date, index) => {
      const selected = date === state.earningsDay;
      const count = companies.filter((company) => company.report_date === date).length;
      const countLabel = count || earnings?.status === 'ok' || coveredDates.includes(date) ? `${count} 家` : '待采集';
      return `<button type="button" role="tab" id="earnings-tab-${date}" class="earnings-day-tab" data-earnings-day="${date}" aria-selected="${selected}" aria-controls="earnings-panel" tabindex="${selected ? 0 : -1}" aria-label="${weekdayLabels[index]} ${date}，${countLabel}"><span class="earnings-day-label">${weekdayLabels[index]}</span><span class="earnings-day-date">${date.slice(5).replace('-', '/')}</span><span class="earnings-day-count">${countLabel}</span></button>`;
    }).join('');
    $('#earnings-panel').setAttribute('aria-labelledby', `earnings-tab-${state.earningsDay}`);
    if (focusedTab) $('#earnings-tabs [aria-selected="true"]').focus({ preventScroll: true });
    const visible = companies.filter((company) => company.report_date === state.earningsDay);
    const historical = state.mode !== 'demo' && dateIsValid(state.snapshot?.date) && state.snapshot.date < etDateKey();
    const oldWeek = state.mode !== 'demo' && hasWeek && (earnings.week_end < etDateKey() || earnings.week_start > etDateKey());
    $('#earnings-week').textContent = `${dates[0]} — ${dates[4]} · 美东周一至周五${historical || oldWeek ? ' · 历史日报' : ''}${hasWeek ? '' : ' · 尚未收集日历'}`;
    const threshold = Math.max(1e10, isNumber(earnings?.threshold_usd) ? earnings.threshold_usd : 1e10);
    $('#earnings-scope').textContent = `市值 ≥ ${formatNumber(threshold / 1e8, 0)} 亿美元 · 全周按市值选取最多 15 家 · 每日按市值排列${state.mode === 'demo' ? ' · 虚构公司示例' : ''}`;
    $('#earnings-count').textContent = earnings ? String(companies.length) : '—';
    const statusLabels = { ok: '日历已收集', partial: '日历覆盖不完整', error: '日历暂不可用' };
    $('#earnings-status').textContent = state.mode === 'demo' && earnings ? '演示财报' : earnings ? statusLabels[earnings.status] || '状态待确认' : '尚未收集';
    $('#earnings-status').className = `subtle-tag ${earnings?.status === 'partial' || earnings?.status === 'error' ? 'earnings-status-warning' : ''}`;
    const timing = { before_open: '盘前', after_close: '盘后', during_market: '盘中', unknown: '时段待定' };
    $('#earnings-list').innerHTML = visible.map((company, index) => {
      const source = safeURL(company.source_url);
      const companyName = company.name_zh || company.name || company.symbol || '公司名称未提供';
      const companyLabel = `<span class="earnings-symbol">${esc(company.symbol || '—')}</span><span class="earnings-name">${esc(companyName)}</span>`;
      const eps = isNumber(company.eps_estimate) ? `<strong class="earnings-eps">${formatNumber(company.eps_estimate)}</strong> <span class="earnings-currency">${esc(company.eps_currency || '币种未提供')}</span>` : '<span class="earnings-unavailable">暂无预期</span>';
      return `<tr class="earnings-row" data-symbol="${esc(company.symbol || '')}"><td class="earnings-company" data-label="公司"><span class="earnings-rank">${String(index + 1).padStart(2, '0')}</span><div>${source ? `<a class="earnings-company-link" href="${esc(source)}" target="_blank" rel="noopener noreferrer" aria-label="查看 ${esc(company.symbol || companyName)} 财报日历来源">${companyLabel}<span class="earnings-source-arrow" aria-hidden="true">↗</span></a>` : companyLabel}</div></td><td data-label="市值 · 美元"><strong class="earnings-cap">${formatMarketCap(company.market_cap)}</strong></td><td data-label="预计发布 · 美东"><span class="earnings-report-date">${esc(formatDate(company.report_date, true))}</span><span class="earnings-time earnings-time-${Object.keys(timing).includes(company.report_time) ? company.report_time : 'unknown'}">${timing[company.report_time] || timing.unknown}</span></td><td data-label="预期 EPS"><div>${eps}</div><small>${isNumber(company.analyst_count) ? `${formatNumber(company.analyst_count, 0)} 位分析师的盈利预期` : '盈利预期 · 覆盖人数未提供'}</small></td><td class="earnings-forecast" data-label="分析师目标价 / 隐含空间">${earningsAnalyst(company, earnings)}</td></tr>`;
    }).join('');
    $('#earnings-table-wrap').classList.toggle('hidden', !visible.length);
    $('#earnings-empty').classList.toggle('hidden', !!visible.length);
    $('#earnings-empty').innerHTML = visible.length ? '' : emptyContent(
      !earnings ? '尚未收集本周财报' : !dayCovered ? '当天财报日历尚未取得' : '当天暂无入选榜单的公司',
      !earnings ? '这份日报尚未保存财报日历。点击「收集最新数据」后查看。' : !dayCovered ? `${formatDate(state.earningsDay, true)} 的数据尚未取得，请查看下方来源状态。` : `${formatDate(state.earningsDay, true)} 暂无公司入选本周大公司榜单，可切换其他工作日查看。`,
    );
    $('#earnings-asof').textContent = earnings?.as_of ? `日历采集于 ${formatTime(earnings.as_of, { year: 'numeric', month: '2-digit', day: '2-digit' })} ET${historical ? ' · 保留所选历史日报时点' : ''}` : '尚未收集本周财报';
    $('#earnings-coverage').textContent = earnings ? `源日历 ${isNumber(earnings.total_scheduled) ? formatNumber(earnings.total_scheduled, 0) : '未知'} 条 · 符合市值条件 ${isNumber(earnings.total_large) ? formatNumber(earnings.total_large, 0) : '未知'} 家 · 当前日期 ${visible.length} 家 / 全周入选 ${companies.length} 家。${coveredDates.length ? `成功覆盖日期：${coveredDates.join('、')}` : '成功覆盖日期未提供'}` : '';
    $('#earnings-note').textContent = earnings?.note || '财报日历为预计日期，请以公司公告为准。目标价空间不等于财报当天涨跌预测。';
    $('#earnings-sources').innerHTML = asArray(earnings?.sources).map((source) => {
      const url = safeURL(source.url);
      const status = source.status === 'ok' ? '可用' : source.status === 'partial' ? '部分可用' : '不可用';
      return `<span class="earnings-source" title="${esc(source.detail || status)}"><span class="source-dot ${source.status === 'ok' ? '' : source.status === 'partial' ? 'source-partial' : 'source-error'}" aria-hidden="true"></span>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(source.name || '日历来源')} ↗</a>` : esc(source.name || '日历来源')}<span>${status}</span>${source.detail ? `<small>${esc(source.detail)}</small>` : ''}</span>`;
    }).join('');
    const errors = asArray(earnings?.errors);
    $('#earnings-errors').classList.toggle('hidden', !errors.length);
    $('#earnings-errors').innerHTML = errors.map((error) => `<p>${esc(error)}</p>`).join('');
  }

  function renderSummary() {
    const summary = asArray(state.snapshot?.summary).filter((item) => typeof item === 'string');
    $('#daily-summary').innerHTML = summary.length
      ? summary.map((item) => `<p>${esc(item)}</p>`).join('')
      : `<p class="muted">${state.snapshot ? '当前数据不足，暂无可生成的市场摘要。请查看来源状态。' : '采集完成后，这里将呈现有行情依据的每日市场摘要。'}</p>`;
  }

  function renderSentiment() {
    const mood = state.snapshot?.sentiment;
    const score = isNumber(mood?.score) ? mood.score : null;
    $('#sentiment-score').textContent = score === null ? '—' : formatNumber(score, 0);
    $('#sentiment-label').textContent = mood?.label || '等待数据';
    $('#sentiment-explanation').textContent = mood?.explanation || '综合大盘趋势、市场广度、动量、波动率与已收录新闻。';
    $('#sentiment-meta').textContent = mood ? `置信度 ${mood.confidence || '未知'} · 可用权重 ${isNumber(mood.coverage_weight) ? formatNumber(mood.coverage_weight, 0) : 0}% · ${mood.market_date || '行情日期未知'}` : '缺失分项不会按零分处理';
    $('#sentiment-gauge').style.setProperty('--sentiment-score', score === null ? 0 : score);
    $('#sentiment-gauge').className = `sentiment-gauge sentiment-${score === null ? 'missing' : score <= 20 ? 'fear' : score <= 40 ? 'cautious' : score < 60 ? 'neutral' : score < 80 ? 'optimistic' : 'greedy'}`;
    const components = asArray(mood?.components);
    $('#sentiment-components').innerHTML = components.length ? components.map((item) => `<div class="sentiment-component ${item.available ? '' : 'unavailable'}"><div><span>${esc(item.label || item.key)}</span><strong>${isNumber(item.score) ? formatNumber(item.score, 0) : '—'}</strong></div><div class="sentiment-bar"><i style="width:${isNumber(item.score) ? clampNumber(item.score, 0, 100) : 0}%"></i></div><small>${esc(item.detail || '该分项数据不足')} · 权重 ${formatNumber(item.weight, 0)}%</small></div>`).join('') : '<div class="sentiment-component unavailable"><div><span>等待情绪数据</span><strong>—</strong></div><small>完成采集后显示六项指标</small></div>';
    const history = asArray(state.snapshot?.sentiment_history).filter((item) => dateIsValid(item.date) && isNumber(item.score));
    $('#sentiment-history').innerHTML = history.length > 1
      ? `${chart(history.map((item) => item.score), history.at(-1).score - history[0].score, 'sentiment-chart', `最近 ${history.length} 份日报情绪走势`)}<small>${history.length} 份日报</small>`
      : '<span>历史曲线会随每日采集逐步形成</span>';
    $('#sentiment-note').textContent = mood?.note || '情绪分数是规则指标，不预测未来涨跌。';
  }

  function clampNumber(value, low, high) {
    return Math.max(low, Math.min(high, value));
  }

  function renderCategories() {
    const categories = [...new Set(newsInWindow().map((item) => item.category).filter(Boolean))];
    const labels = categories.length ? categories : ['宏观政策', '财报业绩', '公司动态', '市场动态'];
    if (state.category !== 'all' && !labels.includes(state.category)) state.category = 'all';
    $('.news-tabs').innerHTML = ['all', ...labels].map((category) => `<button type="button" class="${state.category === category ? 'selected' : ''}" data-category="${esc(category)}" aria-pressed="${state.category === category}">${category === 'all' ? '全部' : esc(category)}</button>`).join('');
  }

  function hasRecentCoverage() {
    return Array.isArray(state.snapshot?.recent_news)
      && Number.isFinite(new Date(state.snapshot?.generated_at).getTime());
  }

  function effectiveNewsRange() {
    return hasRecentCoverage() ? state.newsRange : 'today';
  }

  function newsInWindow(range = effectiveNewsRange()) {
    if (range === 'today' || !hasRecentCoverage()) return asArray(state.snapshot?.news);
    const end = new Date(state.snapshot.generated_at).getTime();
    const start = end - (range === '72h' ? 72 : 24) * 60 * 60 * 1000;
    return state.snapshot.recent_news.filter((item) => {
      const published = new Date(item.published_at).getTime();
      return Number.isFinite(published) && published >= start && published <= end;
    });
  }

  function newsTranslation(item) {
    const titleZh = typeof item.title_zh === 'string' ? item.title_zh.trim() : '';
    const summaryZh = typeof item.summary_zh === 'string' ? item.summary_zh.trim() : '';
    const originalTitle = typeof item.title === 'string' ? item.title : '';
    const originalSummary = typeof item.summary === 'string' ? item.summary : '';
    const hasChinese = (text) => /[\u3400-\u9fff]/.test(text);
    const missing = [];
    if (!titleZh && originalTitle && !hasChinese(originalTitle)) missing.push('标题未译');
    if (!summaryZh && originalSummary && !hasChinese(originalSummary)) missing.push('摘要未译');
    const translated = (titleZh && titleZh !== originalTitle) || (summaryZh && summaryZh !== originalSummary);
    const title = titleZh || originalTitle || '未提供标题';
    const summary = summaryZh || originalSummary;
    let note;
    if (translated) note = `机器翻译，可展开核对原文${missing.length ? ` · ${missing.join('、')}，保留英文` : ''}`;
    else if (missing.length) note = `翻译暂不可用 · ${missing.join('、')}，保留英文`;
    else note = '中文原文';
    const showOriginal = (titleZh && titleZh !== originalTitle) || (summaryZh && summaryZh !== originalSummary);
    const original = showOriginal
      ? `<details class="news-original"><summary>查看英文原文 <span aria-hidden="true">⌄</span></summary><div>${originalTitle ? `<p class="original-title" lang="en">${esc(originalTitle)}</p>` : ''}${originalSummary ? `<p lang="en">${esc(originalSummary)}</p>` : ''}</div></details>` : '';
    const noteMarkup = `<div class="translation-note ${missing.length ? 'translation-incomplete' : ''}"${item.translation_provider ? ` title="${esc(item.translation_provider)}"` : ''}>${esc(note)}</div>`;
    return { title, summary, noteMarkup, original };
  }

  function renderNewsCoverage() {
    const recent = hasRecentCoverage();
    const snapshot = state.snapshot;
    const activeRange = effectiveNewsRange();
    const select = $('#news-range');
    select.value = snapshot ? activeRange : state.newsRange;
    select.disabled = !snapshot;
    [...select.options].forEach((option) => {
      option.disabled = !!snapshot && !recent && option.value !== 'today';
    });
    const counts = snapshot ? [asArray(snapshot.news).length, recent ? newsInWindow('24h').length : '未留存', recent ? newsInWindow('72h').length : '未留存'] : ['—', '—', '—'];
    $('#news-coverage-counts').innerHTML = ['当日', '24 小时', '72 小时'].map((label, index) => `<span class="coverage-count ${activeRange === ['today', '24h', '72h'][index] && snapshot ? 'active' : ''}">${label} <strong>${esc(counts[index])}</strong></span>`).join('');
    $('.day-only-note').textContent = snapshot ? `${activeRange === 'today' ? '日报当日' : activeRange === '72h' ? '最近 72 小时' : '最近 24 小时'} · 美东` : '公开报道 · 美东';
    const cutoff = snapshot?.generated_at ? `${formatTime(snapshot.generated_at, { year: 'numeric', month: '2-digit', day: '2-digit' })} ET` : '';
    $('#news-coverage-note').textContent = !snapshot
      ? '采集后默认展示最近 24 小时的公开报道，保留原文与中文译文。'
      : recent
        ? `截至本份日报采集时 ${cutoff}。仅统计已取得的 RSS 报道；今日速读与板块热度仍按 ${snapshot.date} 美东自然日统计。`
        : `历史日报仅留存 ${snapshot.date} 美东自然日的报道，未保存完整 24 / 72 小时窗口。今日速读与板块热度仍按当日统计。`;
    const translation = snapshot?.translation;
    $('#news-translation-note').textContent = translation?.detail
      ? `${translation.provider ? `${translation.provider} · ` : ''}${translation.detail}` : '';
    $('#news-translation-note').classList.toggle('hidden', !translation?.detail);
  }

  function filteredNews() {
    const query = state.keyword.trim().toLocaleLowerCase();
    return newsInWindow().filter((item) => {
      return (state.category === 'all' || item.category === state.category)
        && (state.sector === 'all' || asArray(item.sectors).includes(state.sector))
        && (!query || `${item.title || ''} ${item.summary || ''} ${item.title_zh || ''} ${item.summary_zh || ''} ${item.source || ''} ${asArray(item.sectors).join(' ')}`.toLocaleLowerCase().includes(query));
    }).sort((a, b) => {
      const score = (isNumber(b.score) ? b.score : 0) - (isNumber(a.score) ? a.score : 0);
      return score || (new Date(b.published_at).getTime() || 0) - (new Date(a.published_at).getTime() || 0);
    });
  }

  function renderNews() {
    const allNews = newsInWindow();
    const news = filteredNews();
    renderNewsCoverage();
    $('#news-total').textContent = String(allNews.length);
    $('#nav-news-count').textContent = state.snapshot ? String(allNews.length) : '—';
    $('#news-list').innerHTML = news.length ? news.slice(0, state.newsLimit).map((item) => {
      const url = safeURL(item.url);
      const copy = newsTranslation(item);
      const title = esc(copy.title);
      const important = item.importance === 'high';
      return `<article class="news-item ${important ? 'is-important' : ''}"><span class="news-dot" aria-hidden="true"></span><div><div class="news-meta"><span class="news-source">${esc(item.source || '来源未知')}</span><span class="news-meta-divider"></span><time datetime="${esc(item.published_at || '')}">${esc(formatTime(item.published_at, { year: 'numeric', month: '2-digit', day: '2-digit' }))} ET</time>${important ? '<span class="importance-tag" title="由关键词规则筛选，待进一步核实">重点候选</span>' : ''}</div><h3>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${title}<span class="external-arrow" aria-label="在新窗口打开">↗</span></a>` : title}</h3>${copy.summary ? `<p class="news-excerpt">${esc(copy.summary)}</p>` : ''}${copy.noteMarkup}${copy.original}<div class="news-tags">${asArray(item.sectors).map((symbol) => `<button class="news-sector-tag" type="button" data-filter-sector="${esc(symbol)}">${esc(sectorName(symbol))}</button>`).join('')}${item.category ? `<span class="news-category-tag">${esc(item.category)}</span>` : ''}</div></div></article>`;
    }).join('') : emptyContent(
      allNews.length ? '没有符合条件的报道' : state.snapshot ? '所选时间范围暂无报道' : '重要的新闻，准备在这里相遇',
      allNews.length ? '试试其他关键词、类别或板块。' : state.snapshot ? '可切换时间范围；报道数量取决于发布时间与数据源覆盖。' : '采集后会按纽约时间整理公开报道，并保留原文链接。',
    );
    $('#news-result-count').textContent = allNews.length
      ? `已显示 ${Math.min(state.newsLimit, news.length)} 条 · 筛选匹配 ${news.length} / ${allNews.length} 条 · 按重要性排序`
      : '暂无报道';
    $('#show-more-news').classList.toggle('hidden', news.length <= state.newsLimit);
    $$('.news-tabs button').forEach((button) => {
      const selected = button.dataset.category === state.category;
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
    $('#news-sector').value = state.sector;
  }

  function renderAlerts() {
    const alerts = asArray(state.snapshot?.alerts);
    const kindLabels = { price: '价格异动', volume: '成交放量', relative: '相对强弱', news: '新闻密集' };
    $('#alerts-list').innerHTML = alerts.length ? alerts.map((alert) => `<article class="alert-item"><div class="alert-item-header"><span class="alert-kind kind-${['price', 'volume'].includes(alert.kind) ? alert.kind : 'other'}">${esc(kindLabels[alert.kind] || '异动信号')}</span><h3 class="alert-title">${esc(alert.title || '板块异动')}</h3></div><p class="alert-detail">${esc(alert.detail || '')}</p>${alert.symbol ? `<button type="button" class="alert-sector-link" data-sector="${esc(alert.symbol)}">${esc(alert.symbol)} · ${esc(alert.name || sectorName(alert.symbol))} <span aria-hidden="true">↗</span></button>` : ''}</article>`).join('') : emptyContent(state.snapshot ? '暂未发现符合规则的异动' : '留意趋势之外的变化', state.snapshot ? '信号基于可用行情筛选，不代表没有市场风险。' : '价格、相对表现与成交量异常将在这里归集。');
  }

  function renderSources() {
    const sources = asArray(state.snapshot?.sources);
    $('#source-list').innerHTML = sources.length ? sources.map((source) => {
      const url = safeURL(source.url);
      const label = esc(source.name || '未命名来源');
      const sourceStatus = source.status === 'ok' ? '可用' : source.status === 'partial' ? '部分可用' : '不可用';
      return `<div class="source-item" title="${esc(`${sourceStatus}：${source.detail || ''}`)}"><span class="source-dot ${source.status === 'ok' ? '' : source.status === 'partial' ? 'source-partial' : 'source-error'}" aria-label="${sourceStatus}"></span><strong>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${label} ↗</a>` : label}</strong><span class="source-detail">${esc(source.detail || sourceStatus)}</span></div>`;
    }).join('') : '<p class="muted">采集后显示各数据源的状态、更新时间与异常信息。</p>';
    const errors = asArray(state.snapshot?.errors);
    $('#source-errors').classList.toggle('hidden', !errors.length);
    $('#source-errors').innerHTML = errors.map((error) => `<p>${esc(error)}</p>`).join('');
    $('#source-overall').textContent = state.mode === 'demo' ? '示例数据源' : sources.length ? `${sources.filter((source) => source.status === 'ok').length} / ${sources.length} 来源可用` : '等待数据';
  }

  function renderSchedule() {
    const schedule = state.schedule || {};
    const pad = (value) => String(value).padStart(2, '0');
    if (state.staticMode) {
      $('#schedule-short').textContent = '云端收盘后自动更新';
      $('#schedule-detail').textContent = '云端定时任务在美股交易日收盘后运行；网页直接读取生成的日报文件，不需要本机保持开机。';
      return;
    }
    if (schedule.enabled === false) {
      $('#schedule-short').textContent = '自动采集已关闭';
      $('#schedule-detail').textContent = '自动采集已关闭。你可以随时手动采集，再回看已保存的每日手记。';
      return;
    }
    const time = isNumber(schedule.hour) && isNumber(schedule.minute) ? `${pad(schedule.hour)}:${pad(schedule.minute)}` : '18:00';
    $('#schedule-short').textContent = `每日 ${time} ET 自动采集`;
    $('#schedule-detail').textContent = schedule.next_run
      ? `服务运行期间，每日 ${time} ET 自动整理。下次定时：${formatTime(schedule.next_run, { month: '2-digit', day: '2-digit' })} ET。`
      : `服务运行期间，每日 ${time} ET 自动采集行情与公开新闻，保存为可回看的每日手记。`;
  }

  function renderControls() {
    const snapshot = state.snapshot;
    const demo = state.mode === 'demo';
    const running = state.refreshPending || (!demo && !!state.collection.running);
    const status = $('#report-status');
    const statusLabels = { ok: '日报已归档', partial: '部分数据可用', error: '采集未完整' };
    status.className = `status-chip ${demo ? 'status-demo' : running || !snapshot ? 'status-pending' : snapshot.status === 'partial' ? 'status-partial' : snapshot.status === 'error' ? 'status-error' : ''}`;
    status.textContent = demo ? '演示数据' : running ? '正在收集' : snapshot ? statusLabels[snapshot.status] || '日报已归档' : '暂无日报';
    const dates = state.dates.filter(dateIsValid);
    $('#report-date').innerHTML = demo
      ? `<option value="${esc(snapshot?.date || '')}">${snapshot ? esc(formatDate(snapshot.date, true)) : '演示日报'}</option>`
      : dates.length ? dates.map((date) => `<option value="${date}">${esc(formatDate(date, true))}</option>`).join('') : '<option value="">暂无已保存日报</option>';
    if (!demo && snapshot?.date && dates.includes(snapshot.date)) $('#report-date').value = snapshot.date;
    else if (!demo && state.selectedDate && dates.includes(state.selectedDate)) $('#report-date').value = state.selectedDate;
    $('#report-date').disabled = demo || !dates.length || state.busy || running;
    $('#last-updated').textContent = snapshot?.generated_at
      ? `${demo ? '示例采集' : '采集于'} ${formatTime(snapshot.generated_at, { month: '2-digit', day: '2-digit' })} ET${snapshot.market_date && snapshot.market_date !== snapshot.date ? ` · 报价日 ${snapshot.market_date.slice(5).replace('-', '/')}` : ''}`
      : running ? '正在采集 ETF、110 家龙头与新闻，可能需要约 1 分钟…' : '尚未采集 · 点击右上角开始';
    $('#live-mode').classList.toggle('selected', !demo);
    $('#demo-mode').classList.toggle('selected', demo);
    $('#live-mode').setAttribute('aria-pressed', String(!demo));
    $('#demo-mode').setAttribute('aria-pressed', String(demo));
    $('#refresh-button').disabled = running || state.busy;
    $('#refresh-button').classList.toggle('spinning', running);
    $('#refresh-button span').textContent = state.staticMode ? '读取云端日报' : running ? '正在收集…' : demo ? '采集真实数据' : '收集最新数据';
    $('#export-button').disabled = demo || !snapshot || !dateIsValid(snapshot.date) || state.busy;
    $('#demo-banner').classList.toggle('hidden', !demo);
    $('#empty-state').classList.toggle('hidden', !!snapshot || demo);
    $('#main').setAttribute('aria-busy', String(state.busy || running));
    const messages = [];
    if (state.fetchError) messages.push(state.fetchError);
    else if (running) messages.push('正在收集公开行情与当日新闻，完成后会自动更新页面。已有日报仍可继续查看。');
    else if (!demo && state.collection.last_error) messages.push(`最近一次采集出现问题：${state.collection.last_error}`);
    else if (!demo && snapshot?.status === 'partial') messages.push('部分数据源暂不可用，页面仅展示本次成功取得的信息。详情见页面底部的数据来源。');
    else if (!demo && snapshot?.status === 'error') messages.push('本次采集未取得完整数据。请查看数据源错误信息，稍后重新收集。');
    else if (!demo && snapshot && snapshot.market_date && snapshot.market_date !== snapshot.date) messages.push(`新闻按 ${snapshot.date} 纽约自然日整理；行情为最近取得的 ${snapshot.market_date} 交易日报价。`);
    $('#notice').classList.toggle('hidden', !messages.length);
    $('#notice').classList.toggle('notice-info', running && !state.fetchError);
    $('#notice').textContent = messages.join(' ');
  }

  function render() {
    renderControls(); renderIndices(); renderSentiment(); renderSummary(); renderSectors();
    renderCategories(); renderNews(); renderAlerts(); renderSources(); renderSchedule(); renderLeaderScreen(); renderEarnings();
  }

  async function request(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(path, { ...options, signal: controller.signal, cache: 'no-store', headers: { Accept: 'application/json', ...options.headers } });
      if (!response.ok) {
        let detail = '';
        try { const body = await response.json(); detail = body.error || body.detail || ''; } catch (_) { /* Status remains useful for non-JSON failures. */ }
        const error = new Error(detail || `服务返回 HTTP ${response.status}`);
        error.status = response.status;
        throw error;
      }
      const body = await response.json();
      if (!body || typeof body !== 'object') throw new Error('服务返回了无效数据');
      return body;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('连接超时，请检查本地服务并重试');
      if (error instanceof TypeError) throw new Error('无法连接本地服务，请确认服务正在运行');
      throw error;
    } finally { clearTimeout(timeout); }
  }

  async function staticDashboard(mode, selectedDate) {
    const target = mode === 'demo' ? 'data/demo.json' : selectedDate ? `data/reports/${encodeURIComponent(selectedDate)}.json` : 'data/latest.json';
    const response = await fetch(new URL(target, document.baseURI), { cache: 'no-store', headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error(`静态日报返回 HTTP ${response.status}`);
    const payload = await response.json();
    if (!payload || typeof payload !== 'object') throw new Error('静态日报格式无效');
    return payload;
  }

  function stopPolling() {
    if (state.pollTimer) clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }

  function queuePoll() {
    stopPolling();
    if (state.mode === 'live' && document.visibilityState === 'visible') {
      state.pollTimer = setTimeout(() => loadDashboard({ quiet: true }), state.collection.running || state.refreshPending ? 2500 : 60000);
    }
  }

  async function loadDashboard({ quiet = false, resetDate = false } = {}) {
    if (resetDate) state.selectedDate = '';
    const version = ++state.requestVersion;
    const mode = state.mode;
    if (!quiet) state.busy = true;
    renderControls();
    try {
      let payload;
      if (state.staticMode) {
        payload = await staticDashboard(mode, state.selectedDate);
      } else {
        const path = mode === 'demo' ? '/api/demo' : `/api/dashboard${state.selectedDate ? `?date=${encodeURIComponent(state.selectedDate)}` : ''}`;
        try {
          payload = await request(path);
        } catch (apiError) {
          try {
            payload = await staticDashboard(mode, state.selectedDate);
            state.staticMode = true;
          } catch (_) {
            throw apiError;
          }
        }
      }
      if (version !== state.requestVersion || mode !== state.mode) return;
      const wasRunning = state.collection.running || state.refreshPending;
      state.snapshot = payload.snapshot && typeof payload.snapshot === 'object' ? payload.snapshot : null;
      if (mode === 'live') state.dates = asArray(payload.dates);
      state.collection = payload.collection || {};
      state.schedule = payload.schedule || {};
      state.fetchError = '';
      state.busy = false;
      state.refreshPending = false;
      render();
      if (mode === 'live' && wasRunning && !state.collection.running) {
        showToast(state.collection.last_status === 'error' ? '采集失败，已保留上次日报。' : state.collection.last_status === 'partial' || state.collection.last_error ? '采集已完成，部分数据可能不可用。' : '最新市场日报已保存。');
      }
      queuePoll();
    } catch (error) {
      if (version !== state.requestVersion || mode !== state.mode) return;
      state.busy = false;
      state.fetchError = `${error.message}。${state.snapshot ? '当前保留上次成功载入的日报。' : '尚未取得日报；可重试，或显式切换到演示模式查看示例。'}`;
      renderControls();
      if (state.collection.running || state.refreshPending) {
        state.pollTimer = setTimeout(() => loadDashboard({ quiet: true }), 6000);
      } else queuePoll();
    }
  }

  async function refresh() {
    if (state.busy || state.refreshPending || (state.mode === 'live' && state.collection.running)) return;
    if (state.staticMode) {
      showToast('正在读取云端最近一次定时任务生成的日报。');
      await loadDashboard({ resetDate: true });
      return;
    }
    stopPolling();
    const refreshVersion = ++state.requestVersion;
    if (state.mode === 'demo') {
      state.mode = 'live';
      state.snapshot = null;
    }
    state.selectedDate = '';
    state.refreshPending = true;
    state.fetchError = '';
    render();
    try {
      await request('/api/refresh', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      if (refreshVersion !== state.requestVersion || state.mode !== 'live') return;
      state.collection.running = true;
      await loadDashboard({ quiet: true, resetDate: true });
    } catch (error) {
      if (refreshVersion !== state.requestVersion || state.mode !== 'live') return;
      state.refreshPending = false;
      state.collection.running = false;
      state.fetchError = `无法启动采集：${error.message}。请稍后重试。`;
      renderControls();
    }
  }

  async function switchMode(mode) {
    if (mode === state.mode && !state.fetchError) return;
    stopPolling();
    state.mode = mode;
    state.snapshot = null;
    state.fetchError = '';
    state.refreshPending = false;
    state.collection = {};
    state.category = 'all';
    state.newsLimit = 6;
    state.busy = false;
    render();
    await loadDashboard();
  }

  function showToast(message) {
    clearTimeout(state.toastTimer);
    $('#toast').textContent = message;
    $('#toast').classList.remove('hidden');
    state.toastTimer = setTimeout(() => $('#toast').classList.add('hidden'), 4500);
  }

  function openSector(symbol) {
    const sector = mergedSectors().find((item) => item.symbol === symbol);
    if (!sector) return;
    $('#sector-dialog-symbol').textContent = `${symbol} · SECTOR DETAIL${state.mode === 'demo' ? ' · 演示' : ''}`;
    $('#sector-dialog-title').textContent = sector.name;
    const related = asArray(state.snapshot?.news).filter((item) => asArray(item.sectors).includes(symbol));
    const history = historyValues(sector.history);
    $('#sector-dialog-content').innerHTML = `<div class="dialog-price"><strong>${formatNumber(sector.price)}</strong><span class="${direction(sector.change_pct)}">${percent(sector.change_pct)}</span></div><p class="muted">${esc(symbol)} 板块 ETF · USD${sector.as_of ? ` · ${esc(formatTime(sector.as_of, { year: 'numeric', month: '2-digit', day: '2-digit' }))} ET` : ' · 暂无报价'}</p><div class="dialog-chart-wrap">${history.length > 1 ? chart(history, sector.change_pct, 'dialog-chart', `${sector.name} ${history.length} 个观测日收盘价走势`) : '<span class="muted">暂无足够的历史行情</span>'}</div><div class="dialog-chart-note"><span>近期收盘价走势 · 非日内分时</span><span>${history.length ? `${history.length} 个观测日` : '等待数据'}</span></div><div class="dialog-stats"><div class="dialog-stat"><span>相对 SPY / 百分点</span><strong>${percent(sector.relative_pct, '')}</strong></div><div class="dialog-stat"><span>日成交量比</span><strong>${isNumber(sector.volume_ratio) ? `${formatNumber(sector.volume_ratio)}×` : '—'}</strong></div><div class="dialog-stat"><span>板块热度 / 100</span><strong>${formatNumber(sector.heat_score, 0)}</strong></div></div><h3 class="dialog-news-title">日报当日相关报道 <span class="heading-count">${related.length}</span></h3>${related.length ? `<ul class="dialog-news-list">${related.map((item) => {
      const url = safeURL(item.url);
      const copy = newsTranslation(item);
      return `<li>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(copy.title)} ↗</a>` : `<span>${esc(copy.title)}</span>`}<small>${esc(item.source || '来源未知')} · ${esc(formatTime(item.published_at, { year: 'numeric', month: '2-digit', day: '2-digit' }))} ET</small>${copy.summary ? `<p class="news-excerpt">${esc(copy.summary)}</p>` : ''}${copy.noteMarkup}${copy.original}</li>`;
    }).join('')}</ul>` : '<p class="muted" style="margin-top:12px">所选日报暂无该板块相关报道。</p>'}<p class="dialog-note">${isNumber(sector.volume_ratio) ? '成交量比为该交易日成交量与此前交易日均量之比。' : '成交量比仅在完整常规交易时段结束且历史数据充分时提供。'} 相对表现仅比较同一交易日的 ETF 报价。${state.mode === 'demo' ? ' 当前为演示数据，不反映实际市场。' : ''}</p>`;
    $('#sector-dialog').showModal();
  }

  function openExport() {
    if (state.mode !== 'live' || !dateIsValid(state.snapshot?.date)) return;
    const date = encodeURIComponent(state.snapshot.date);
    $('#export-markdown').href = state.staticMode ? `data/reports/${date}.md` : `/api/export?date=${date}&format=md`;
    $('#export-json').href = state.staticMode ? `data/reports/${date}.json` : `/api/export?date=${date}&format=json`;
    $('#export-dialog-title').textContent = `导出 ${state.snapshot.date} 日报`;
    $('#export-dialog').showModal();
  }

  function attachEvents() {
    $('#refresh-button').addEventListener('click', refresh);
    $('#live-mode').addEventListener('click', () => switchMode('live'));
    $('#demo-mode').addEventListener('click', () => switchMode('demo'));
    $('#exit-demo').addEventListener('click', () => switchMode('live'));
    $('#report-date').addEventListener('change', (event) => {
      state.selectedDate = event.target.value;
      state.newsLimit = 6;
      state.category = 'all';
      loadDashboard();
    });
    $('#sector-sort').addEventListener('change', (event) => { state.sort = event.target.value; renderSectors(); });
    $('#leaders-sector').addEventListener('change', (event) => { state.leaderSector = event.target.value; renderLeaderScreen(); });
    $('#leaders-view').addEventListener('change', (event) => { state.leaderView = event.target.value; renderLeaderScreen(); });
    $('#leaders-strategy-tabs').addEventListener('click', (event) => {
      const button = event.target.closest('[data-leader-strategy]');
      if (!button) return;
      state.leaderStrategy = button.dataset.leaderStrategy;
      renderLeaderScreen();
    });
    $('#leaders-sector-tabs').addEventListener('click', (event) => {
      const button = event.target.closest('[data-leader-sector]');
      if (!button) return;
      state.leaderSector = state.leaderSector === button.dataset.leaderSector ? 'all' : button.dataset.leaderSector;
      renderLeaderScreen();
    });
    $('#sentiment-method-button').addEventListener('click', () => {
      $('#methodology').scrollIntoView({ behavior: 'smooth', block: 'start' });
      const details = $('#methodology details');
      if (details) details.open = true;
    });
    $('#earnings-tabs').addEventListener('click', (event) => {
      const tab = event.target.closest('[data-earnings-day]');
      if (!tab) return;
      state.earningsDay = tab.dataset.earningsDay;
      renderEarnings();
      $('#earnings-tabs [aria-selected="true"]').focus({ preventScroll: true });
    });
    $('#earnings-tabs').addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      const tabs = $$('#earnings-tabs [role="tab"]');
      const current = tabs.indexOf(event.target);
      if (current < 0) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (current + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
      state.earningsDay = tabs[next].dataset.earningsDay;
      renderEarnings();
    });
    $('#news-search').addEventListener('input', (event) => { state.keyword = event.target.value; state.newsLimit = 6; renderNews(); });
    $('#news-sector').addEventListener('change', (event) => { state.sector = event.target.value; state.newsLimit = 6; renderNews(); });
    $('#news-range').addEventListener('change', (event) => {
      state.newsRange = event.target.value;
      state.newsLimit = 6;
      state.category = 'all';
      renderCategories();
      renderNews();
    });
    $('.news-tabs').addEventListener('click', (event) => {
      const button = event.target.closest('[data-category]');
      if (!button) return;
      state.category = button.dataset.category;
      state.newsLimit = 6;
      renderNews();
    });
    $('#show-more-news').addEventListener('click', () => { state.newsLimit += 6; renderNews(); });
    document.addEventListener('click', (event) => {
      const sectorButton = event.target.closest('[data-sector]');
      if (sectorButton) openSector(sectorButton.dataset.sector);
      const filterButton = event.target.closest('[data-filter-sector]');
      if (filterButton) {
        const symbol = filterButton.dataset.filterSector;
        if (!SECTORS.some((sector) => sector.symbol === symbol)) return;
        state.sector = symbol;
        state.newsLimit = 6;
        renderNews();
        $('#news-sector').focus({ preventScroll: true });
      }
    });
    $('#close-dialog').addEventListener('click', () => $('#sector-dialog').close());
    $('#export-button').addEventListener('click', openExport);
    $('#close-export').addEventListener('click', () => $('#export-dialog').close());
    $$('.export-option').forEach((link) => link.addEventListener('click', () => {
      $('#export-dialog').close();
      showToast('已请求下载日报。');
    }));
    $$('dialog').forEach((dialog) => dialog.addEventListener('click', (event) => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
    }));
    $$('.nav-item').forEach((link) => link.addEventListener('click', () => {
      $$('.nav-item').forEach((item) => item.classList.toggle('active', item === link));
      $('.breadcrumb strong').textContent = ({ overview: '市场概览', sectors: '板块雷达', leaders: '股票观察', earnings: '本周财报', news: '新闻聚焦', alerts: '异动观察' })[link.dataset.nav] || '市场概览';
    }));
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible' && state.mode === 'live' && !state.busy && !state.refreshPending) loadDashboard({ quiet: true });
      else if (document.visibilityState !== 'visible') stopPolling();
    });
  }

  $('#news-sector').innerHTML = '<option value="all">全部板块</option>' + SECTORS.map((sector) => `<option value="${sector.symbol}">${sector.name}</option>`).join('');
  attachEvents();
  render();
  loadDashboard();
})();
