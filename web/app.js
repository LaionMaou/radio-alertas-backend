(() => {
  'use strict';

  const HISTORY_LIMIT = 10;
  const POLL_INTERVAL_MS = 10000;
  const SSE_RETRY_MS = 60000;
  const MAX_SSE_ERRORS = 3;

  const KIND_LABELS = {
    eew: '緊急地震速報',
    earthquake: '地震情報',
    tsunami: '津波情報',
    typhoon: '台風情報',
  };

  const STATUS_LABELS = {
    active: '発生',
    updated: '更新',
    cancelled: '取消',
    stale: '古い情報',
    expired: '期限切れ',
    test: 'テスト',
  };

  const TTS_LABELS = {
    none: '放送なし',
    queued: '放送待ち',
    running: '放送中',
    ok: '放送済み',
    partial: '一部失敗',
    failed: '放送失敗',
    'dry-run': 'ドライラン',
    duplicate: '放送済み',
    unknown: '確認中',
  };

  const TSUNAMI_LABELS = {
    MajorWarning: '大津波警報',
    Warning: '津波警報',
    Watch: '津波注意報',
    Unknown: '津波予報',
    Checking: '津波確認中',
    NonEffective: '若干の海面変動',
    None: '津波の心配なし',
  };

  const SOURCE_LABELS = {
    p2p: 'P2PQuake',
    jma: '気象庁',
    simulate: 'シミュレーション',
    manual: '手動',
  };

  const jstFormat = new Intl.DateTimeFormat('ja-JP', {
    timeZone: 'Asia/Tokyo',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  });

  const el = {
    link: document.getElementById('link-status'),
    p2p: document.getElementById('p2p-status'),
    mode: document.getElementById('mode-pill'),
    clock: document.getElementById('clock'),
    empty: document.getElementById('alert-empty'),
    card: document.getElementById('alert-card'),
    kind: document.getElementById('alert-kind'),
    status: document.getElementById('alert-status'),
    tts: document.getElementById('alert-tts'),
    time: document.getElementById('alert-time'),
    title: document.getElementById('alert-title'),
    summary: document.getElementById('alert-summary'),
    details: document.getElementById('alert-details'),
    meta: document.getElementById('alert-meta'),
    unpin: document.getElementById('unpin'),
    history: document.getElementById('history-list'),
    historyEmpty: document.getElementById('history-empty'),
    stopbar: document.getElementById('stopbar'),
    stopButton: document.getElementById('stop-button'),
    stopCancel: document.getElementById('stop-cancel'),
    stopStatus: document.getElementById('stop-status'),
  };

  const state = {
    alerts: new Map(),
    selected: null,
    status: null,
    source: null,
    errors: 0,
    pollTimer: null,
    retryTimer: null,
    stop: { key: null, phase: 'idle', message: '', retryTimer: null },
  };

  function formatJst(value, options) {
    const seconds = Boolean(options && options.seconds);
    if (!value) {
      return '';
    }
    const date = value instanceof Date ? value : new Date(value);
    if (Number.isNaN(date.getTime())) {
      return '';
    }
    const parts = {};
    for (const part of jstFormat.formatToParts(date)) {
      if (part.type !== 'literal') {
        parts[part.type] = part.value;
      }
    }
    const time = parts.hour + ':' + parts.minute + (seconds ? ':' + parts.second : '');
    return parts.year + '/' + parts.month + '/' + parts.day + ' ' + time;
  }

  function text(value) {
    if (typeof value === 'string') {
      return value.trim();
    }
    if (typeof value === 'number' && Number.isFinite(value)) {
      return String(value);
    }
    return '';
  }

  function readJson(event) {
    try {
      const data = JSON.parse(event.data);
      return data && typeof data === 'object' ? data : null;
    } catch {
      return null;
    }
  }

  function sortKey(alert) {
    const issued = Date.parse(alert.issued_at || '');
    const received = Date.parse(alert.received_at || '');
    return [
      Number.isNaN(issued) ? 0 : issued,
      Number.isNaN(received) ? 0 : received,
    ];
  }

  function sortedAlerts() {
    return [...state.alerts.values()].sort((a, b) => {
      const left = sortKey(a);
      const right = sortKey(b);
      return right[0] - left[0] || right[1] - left[1];
    });
  }

  function newestKey() {
    const list = sortedAlerts();
    return list.length > 0 ? list[0].event_key : null;
  }

  function currentKey() {
    if (state.selected && state.alerts.has(state.selected)) {
      return state.selected;
    }
    return newestKey();
  }

  function currentAlert() {
    const key = currentKey();
    return key ? state.alerts.get(key) || null : null;
  }

  function addRow(rows, label, value) {
    const content = text(value);
    if (content) {
      rows.push([label, content]);
    }
  }

  function joinNames(items, limit) {
    const names = [];
    for (const item of items) {
      const name = text(item && (item.pref || item.name || item.prefecture));
      if (name && !names.includes(name)) {
        names.push(name);
      }
    }
    if (names.length <= limit) {
      return names.join('、');
    }
    return names.slice(0, limit).join('、') + ' ほか' + (names.length - limit) + '地域';
  }

  function detailRows(alert) {
    const rows = [];
    const epicenter = alert.epicenter || {};
    const intensity = alert.max_intensity || {};
    const tsunami = alert.tsunami || {};

    addRow(rows, '発生時刻', formatJst(alert.origin_at || alert.issued_at, { seconds: true }));
    addRow(rows, '発表時刻', formatJst(alert.issued_at, { seconds: true }));

    addRow(rows, alert.kind === 'eew' ? '震源（推定）' : '震源', epicenter.name);
    if (epicenter.magnitude !== null && epicenter.magnitude !== undefined) {
      addRow(rows, 'マグニチュード', 'M' + epicenter.magnitude);
    }
    if (epicenter.depth_km !== null && epicenter.depth_km !== undefined) {
      addRow(rows, '深さ', epicenter.depth_km + 'km');
    }
    if (text(intensity.label) && intensity.label !== '不明') {
      addRow(rows, alert.kind === 'eew' ? '予想最大震度' : '最大震度', intensity.label);
    }

    if (alert.kind === 'earthquake' && text(tsunami.domestic)) {
      addRow(rows, '津波', TSUNAMI_LABELS[tsunami.domestic] || tsunami.domestic);
    }

    if (alert.kind === 'tsunami') {
      if (!tsunami.cancelled && text(tsunami.grade)) {
        addRow(rows, '区分', TSUNAMI_LABELS[tsunami.grade] || tsunami.grade);
      }
      const areas = Array.isArray(tsunami.areas) ? tsunami.areas : [];
      if (areas.length > 0) {
        addRow(rows, '対象地域', joinNames(areas, 8));
      }
      if (tsunami.immediate) {
        addRow(rows, '対応', '直ちに避難してください');
      }
      const heights = areas
        .map((area) => text(area && area.max_height && area.max_height.description))
        .filter(Boolean);
      if (heights.length > 0) {
        addRow(rows, '予想される高さ', heights.slice(0, 3).join('、'));
      }
    }

    if (alert.kind === 'eew') {
      const eew = alert.eew || {};
      const areas = Array.isArray(eew.areas) ? eew.areas : [];
      if (areas.length > 0) {
        addRow(rows, '強い揺れの地域', joinNames(areas, 8));
      }
      addRow(rows, '状況', eew.condition);
      if (text(eew.serial)) {
        addRow(rows, '報数', '第' + eew.serial + '報');
      }
    }

    if (alert.kind === 'typhoon') {
      const typhoon = alert.typhoon || {};
      const label = text(typhoon.number_label) || (text(typhoon.number) ? '台風' + typhoon.number + '号' : '');
      addRow(rows, '台風番号', label);
      addRow(rows, '名称', typhoon.name);
      addRow(rows, '階級', typhoon.class);
      addRow(rows, '中心位置', typhoon.location);
      addRow(rows, '進行方向', typhoon.direction);
      if (typhoon.pressure_hpa !== null && typhoon.pressure_hpa !== undefined) {
        addRow(rows, '中心気圧', typhoon.pressure_hpa + 'hPa');
      }
      if (typhoon.wind_mps !== null && typhoon.wind_mps !== undefined) {
        addRow(rows, '最大風速', typhoon.wind_mps + 'm/s');
      }
      if (typhoon.gust_mps !== null && typhoon.gust_mps !== undefined) {
        addRow(rows, '最大瞬間風速', typhoon.gust_mps + 'm/s');
      }
      const areas = Array.isArray(typhoon.areas) ? typhoon.areas : [];
      const ranked = areas
        .filter((area) => area && typeof area.probability === 'number')
        .sort((a, b) => b.probability - a.probability)
        .slice(0, 4)
        .map((area) => {
          const name = text(area.name) || text(area.prefecture) || text(area.code);
          return name ? name + ' ' + area.probability + '%' : '';
        })
        .filter(Boolean);
      if (ranked.length > 0) {
        addRow(rows, '暴風域に入る確率', ranked.join('、'));
      }
    }

    return rows;
  }

  function renderDetails(alert) {
    const fragment = document.createDocumentFragment();
    for (const [label, value] of detailRows(alert)) {
      const cell = document.createElement('div');
      cell.className = 'detail';
      const term = document.createElement('dt');
      term.textContent = label;
      const description = document.createElement('dd');
      description.textContent = value;
      cell.append(term, description);
      fragment.append(cell);
    }
    el.details.replaceChildren(fragment);
  }

  function renderPanel() {
    const alert = currentAlert();
    if (!alert) {
      el.card.hidden = true;
      el.empty.hidden = false;
      return;
    }

    el.empty.hidden = true;
    el.card.hidden = false;

    const grade = alert.tsunami && alert.tsunami.grade ? alert.tsunami.grade : '';
    el.kind.textContent = KIND_LABELS[alert.kind] || text(alert.kind) || '警報';
    el.kind.dataset.kind = alert.kind || '';
    el.kind.dataset.grade = grade;

    el.status.textContent = STATUS_LABELS[alert.status] || text(alert.status) || '不明';
    el.status.dataset.status = alert.status || '';

    const tts = alert.tts || {};
    el.tts.textContent = TTS_LABELS[tts.status] || text(tts.status) || '放送なし';
    el.tts.dataset.tts = tts.status || 'none';

    el.time.textContent = formatJst(alert.issued_at, { seconds: true });
    el.time.dateTime = alert.issued_at || '';

    el.title.textContent = text(alert.title) || KIND_LABELS[alert.kind] || '警報';
    el.summary.textContent = text(alert.summary);

    renderDetails(alert);

    const meta = [];
    if (alert.version > 1) {
      meta.push('第' + alert.version + '版');
    }
    const announced = Array.isArray(alert.announcements) ? alert.announcements.length : 0;
    if (announced > 0) {
      meta.push('放送' + announced + '回');
    }
    const source = SOURCE_LABELS[alert.source] || text(alert.source);
    if (source) {
      meta.push('情報源: ' + source);
    }
    const received = formatJst(alert.received_at, { seconds: true });
    if (received) {
      meta.push('受信 ' + received);
    }
    el.meta.textContent = meta.join(' · ');

    const pinned = Boolean(state.selected && state.alerts.has(state.selected));
    el.unpin.hidden = !pinned;
  }

  const STOP_RETRY_MS = 15000;

  // La barra depende solo de `tts.loop_active`: el store marca `expired` los
  // eventos viejos aunque el bucle siga sonando, y el locutor puede estar
  // leyendo un evento anterior mientras el audio repite.
  function loopAlert() {
    for (const alert of sortedAlerts()) {
      if (alert.tts && alert.tts.loop_active === true) {
        return alert;
      }
    }
    return null;
  }

  function setStopState(phase, message, tone) {
    state.stop.phase = phase;
    state.stop.message = message || '';
    state.stop.tone = tone || '';
    renderStopbar();
  }

  function clearStopRetry() {
    if (state.stop.retryTimer !== null) {
      clearTimeout(state.stop.retryTimer);
      state.stop.retryTimer = null;
    }
  }

  function scheduleStopRetry() {
    clearStopRetry();
    state.stop.retryTimer = setTimeout(() => {
      state.stop.retryTimer = null;
      if (state.stop.phase !== 'done') {
        return;
      }
      setStopState('confirm', '停止の確認が取れませんでした。もう一度お試しください。', 'warn');
    }, STOP_RETRY_MS);
  }

  function renderStopbar() {
    const alert = loopAlert();
    if (!alert) {
      clearStopRetry();
      state.stop.key = null;
      state.stop.phase = 'idle';
      state.stop.message = '';
      state.stop.tone = '';
      el.stopbar.hidden = true;
      el.stopButton.textContent = '放送を停止';
      el.stopButton.disabled = false;
      el.stopButton.dataset.phase = 'idle';
      el.stopCancel.hidden = true;
      el.stopCancel.disabled = false;
      el.stopStatus.hidden = true;
      el.stopStatus.textContent = '';
      return;
    }

    if (state.stop.key !== alert.event_key) {
      clearStopRetry();
      state.stop.key = alert.event_key;
      state.stop.phase = 'idle';
      state.stop.message = '';
      state.stop.tone = '';
    }

    const phase = state.stop.phase;
    el.stopbar.hidden = false;
    el.stopButton.dataset.phase = phase;
    el.stopButton.textContent = phase === 'confirm' ? '本当に停止しますか？' : '放送を停止';
    el.stopButton.disabled = phase === 'busy' || phase === 'done';
    el.stopCancel.hidden = phase !== 'confirm';
    el.stopCancel.disabled = phase === 'busy' || phase === 'done';
    el.stopStatus.hidden = !state.stop.message;
    el.stopStatus.textContent = state.stop.message;
    el.stopStatus.dataset.tone = state.stop.tone || 'warn';
  }

  async function requestStop() {
    if (state.stop.phase === 'busy' || state.stop.phase === 'done') {
      return;
    }
    setStopState('busy', '停止を要求しています…', 'warn');

    let response = null;
    try {
      response = await fetch('/api/stop', {
        method: 'POST',
        headers: { Accept: 'application/json' },
      });
    } catch (error) {
      response = null;
    }

    if (response === null) {
      setStopState('confirm', 'サーバーに接続できませんでした。もう一度お試しください。', 'bad');
      return;
    }
    if (response.ok) {
      setStopState('done', '停止しました。自動放送に戻ります。', 'ok');
      scheduleStopRetry();
      return;
    }
    if (response.status === 401 || response.status === 403) {
      setStopState('confirm', '認証に失敗しました。ページを再読み込みしてください。', 'bad');
      return;
    }
    setStopState('confirm', '停止できませんでした。もう一度お試しください。', 'bad');
  }

  function cancelStop() {
    if (state.stop.phase !== 'confirm') {
      return;
    }
    setStopState('idle', '', '');
  }

  function renderHistory() {
    const list = sortedAlerts().slice(0, HISTORY_LIMIT);
    const activeKey = currentKey();
    const fragment = document.createDocumentFragment();

    for (const alert of list) {
      const item = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'history-item';
      button.dataset.kind = alert.kind || '';
      button.setAttribute('aria-current', alert.event_key === activeKey ? 'true' : 'false');

      const time = document.createElement('time');
      time.className = 'history-time';
      time.dateTime = alert.issued_at || '';
      time.textContent = formatJst(alert.issued_at);
      if (!time.textContent) {
        time.hidden = true;
      }

      const kind = document.createElement('span');
      kind.className = 'history-kind';
      kind.textContent = KIND_LABELS[alert.kind] || text(alert.kind);

      const title = document.createElement('span');
      title.className = 'history-title';
      title.textContent = text(alert.title) || KIND_LABELS[alert.kind] || '';

      const summary = document.createElement('span');
      summary.className = 'history-summary';
      summary.textContent = text(alert.summary);

      button.append(time, kind, title, summary);
      button.addEventListener('click', () => {
        state.selected = alert.event_key === newestKey() ? null : alert.event_key;
        render();
      });

      item.append(button);
      fragment.append(item);
    }

    el.history.replaceChildren(fragment);
    el.historyEmpty.hidden = list.length > 0;
  }

  function setLink(tone, label) {
    el.link.textContent = label;
    el.link.dataset.tone = tone;
  }

  function renderStatus() {
    const status = state.status && typeof state.status === 'object' ? state.status : {};
    const p2p = status.p2p && typeof status.p2p === 'object' ? status.p2p : {};

    let tone = 'unknown';
    let label = 'P2P 確認中';
    if (p2p.enabled === false) {
      tone = 'unknown';
      label = 'P2P 停止';
    } else if (p2p.connected) {
      tone = 'ok';
      label = 'P2P 接続中';
    } else if (typeof p2p.attempts === 'number' && p2p.attempts > 0) {
      tone = 'warn';
      label = 'P2P 再接続中';
    } else {
      tone = 'bad';
      label = 'P2P オフライン';
    }
    el.p2p.textContent = label;
    el.p2p.dataset.tone = tone;

    if (typeof status.dry_run === 'boolean') {
      el.mode.hidden = false;
      el.mode.textContent = status.dry_run ? 'DRY-RUN（無音）' : '本番モード';
      el.mode.dataset.tone = status.dry_run ? 'warn' : 'live';
    }
  }

  function render() {
    renderStatus();
    renderStopbar();
    renderPanel();
    renderHistory();
  }

  function applySnapshot(data) {
    const alerts = Array.isArray(data.alerts) ? data.alerts : [];
    const next = new Map();
    for (const alert of alerts) {
      if (alert && text(alert.event_key)) {
        next.set(alert.event_key, alert);
      }
    }
    if (data.alert && text(data.alert.event_key)) {
      next.set(data.alert.event_key, data.alert);
    }
    state.alerts = next;
    if (state.selected && !next.has(state.selected)) {
      state.selected = null;
    }
    if (data.status && typeof data.status === 'object') {
      state.status = data.status;
    }
  }

  function applyAlert(alert) {
    if (!alert || !text(alert.event_key)) {
      return;
    }
    state.alerts.set(alert.event_key, alert);
  }

  function applyStatus(data) {
    const current = state.status && typeof state.status === 'object' ? state.status : {};
    const next = { ...current };
    if (data.p2p && typeof data.p2p === 'object') {
      next.p2p = { ...(current.p2p || {}), ...data.p2p };
    }
    state.status = next;
  }

  async function poll() {
    try {
      const [latestResponse, historyResponse, healthResponse] = await Promise.all([
        fetch('api/alert/latest', { cache: 'no-store' }),
        fetch('api/alert/history?limit=' + HISTORY_LIMIT, { cache: 'no-store' }),
        fetch('health', { cache: 'no-store' }),
      ]);
      if (!latestResponse.ok || !historyResponse.ok) {
        throw new Error('poll failed');
      }
      const latest = await latestResponse.json();
      const history = await historyResponse.json();
      const alerts = Array.isArray(history.alerts) ? history.alerts : [];
      const next = new Map();
      for (const alert of alerts) {
        if (alert && text(alert.event_key)) {
          next.set(alert.event_key, alert);
        }
      }
      if (latest.alert && text(latest.alert.event_key)) {
        next.set(latest.alert.event_key, latest.alert);
      }
      state.alerts = next;
      if (state.selected && !next.has(state.selected)) {
        state.selected = null;
      }
      if (healthResponse.ok) {
        state.status = await healthResponse.json();
      }
      render();
    } catch {
      setLink('warn', '更新できません');
    }
  }

  function stopPolling() {
    if (state.pollTimer !== null) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function stopRetry() {
    if (state.retryTimer !== null) {
      clearInterval(state.retryTimer);
      state.retryTimer = null;
    }
  }

  function startPolling() {
    stopRetry();
    if (state.pollTimer !== null) {
      return;
    }
    setLink('warn', 'ポーリング');
    void poll();
    state.pollTimer = setInterval(() => {
      void poll();
    }, POLL_INTERVAL_MS);
    state.retryTimer = setInterval(() => {
      connect();
    }, SSE_RETRY_MS);
  }

  function closeSource() {
    if (state.source) {
      state.source.close();
      state.source = null;
    }
  }

  function connect() {
    stopRetry();
    stopPolling();
    closeSource();

    let source;
    try {
      source = new EventSource('api/alert/stream');
    } catch {
      startPolling();
      return;
    }
    state.source = source;
    state.errors = 0;
    setLink('unknown', '接続中');

    source.addEventListener('open', () => {
      state.errors = 0;
      setLink('live', 'ライブ');
    });

    source.addEventListener('snapshot', (event) => {
      const data = readJson(event);
      if (!data) {
        return;
      }
      state.errors = 0;
      applySnapshot(data);
      setLink('live', 'ライブ');
      render();
    });

    source.addEventListener('alert', (event) => {
      const alert = readJson(event);
      if (!alert) {
        return;
      }
      applyAlert(alert);
      render();
    });

    source.addEventListener('status', (event) => {
      const data = readJson(event);
      if (!data) {
        return;
      }
      applyStatus(data);
      renderStatus();
    });

    source.addEventListener('error', () => {
      state.errors += 1;
      if (state.errors < MAX_SSE_ERRORS) {
        setLink('warn', '再接続中');
        return;
      }
      closeSource();
      startPolling();
    });
  }

  function tick() {
    el.clock.textContent = formatJst(new Date(), { seconds: true });
  }

  el.unpin.addEventListener('click', () => {
    state.selected = null;
    render();
  });

  el.stopButton.addEventListener('click', () => {
    if (state.stop.phase === 'confirm') {
      requestStop();
      return;
    }
    if (state.stop.phase !== 'idle') {
      return;
    }
    setStopState('confirm', '', '');
  });

  el.stopCancel.addEventListener('click', cancelStop);

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      cancelStop();
    }
  });

  tick();
  setInterval(tick, 1000);
  render();
  connect();
})();
