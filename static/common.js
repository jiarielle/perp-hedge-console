/* 共享工具:两个页面都用 */
const CUI = {
  async post(url, body) {
    const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                body: body ? JSON.stringify(body) : null});
    return r.json();
  },
  async get(url) { return (await fetch(url)).json(); },

  toast(msg, ok = true) {
    let box = document.getElementById('toast');
    if (!box) { box = document.createElement('div'); box.id = 'toast'; document.body.appendChild(box); }
    const t = document.createElement('div');
    t.className = 't ' + (ok ? 'good' : 'bad');
    t.textContent = msg;
    box.appendChild(t);
    setTimeout(() => t.remove(), 3500);
  },

  fmtAge(ts) {
    if (!ts) return '—';
    const s = Math.max(0, Math.round(Date.now()/1000 - ts));
    return s < 60 ? s + ' 秒前' : s < 3600 ? Math.round(s/60) + ' 分前' : (s/3600).toFixed(1) + ' 时前';
  },
  fmtUptime(ts) {
    if (!ts) return '—';
    const s = Math.max(0, Math.floor(Date.now()/1000 - ts));
    const h = Math.floor(s/3600), m = Math.floor(s%3600/60);
    return h ? `${h}时${m}分` : m ? `${m}分${s%60}秒` : s + '秒';
  },
  signedClass(v) { const n = parseFloat(v); return n > 0 ? 'up' : n < 0 ? 'down' : 'flat'; },
  sideWord(v) { const n = parseFloat(v); return n > 0 ? '多头' : n < 0 ? '空头' : '中性(空仓)'; },
  money(v, digits = 2) {
    const n = parseFloat(v);
    return isFinite(n) ? '$' + n.toLocaleString('en-US', {minimumFractionDigits: digits, maximumFractionDigits: digits}) : '—';
  },

  /* Popdex 风币种选择器:按钮(币种+价格+24h) + 可搜索列表面板 */
  symbolDropdown(root, opts) {
    root.classList.add('sym-wrap');
    const st = { open: false, filter: '', markets: opts.markets || [], current: opts.current || '' };

    const fmtPx = v => {
      const n = parseFloat(v);
      if (!isFinite(n)) return '—';
      return n.toLocaleString('en-US', {minimumFractionDigits: n < 100 ? 3 : 1, maximumFractionDigits: n < 100 ? 3 : 1});
    };
    const pct = v => { const n = parseFloat(v) * 100; return (n >= 0 ? '+' : '') + n.toFixed(2) + '%'; };
    const cur = () => st.markets.find(m => m.popdex_symbol === st.current) || null;

    function renderList() {
      const list = root.querySelector('.sym-list');
      if (!list) return;
      const q = st.filter.trim().toUpperCase();
      const rows = st.markets.filter(m => !q || m.base.includes(q));
      list.innerHTML = rows.map(m => {
        const n = parseFloat(m.change_pct) * 100;
        const cls = n >= 0 ? 'up' : 'down';
        const isCur = m.popdex_symbol === st.current;
        return `<div class="sym-row ${isCur ? 'cur' : ''}" data-base="${m.base}">
          <span class="b">${m.base}</span>
          <span class="p">${fmtPx(m.last_price)}</span>
          <span class="c ${cls}">${pct(m.change_pct)}</span></div>`;
      }).join('') || '<div class="none" style="padding:10px">无匹配</div>';
      list.querySelectorAll('.sym-row').forEach(r => {
        r.onclick = async () => {
          const m = st.markets.find(x => x.base === r.dataset.base);
          st.current = m.popdex_symbol;
          st.open = false;
          render();
          await opts.onPick(m);
        };
      });
    }
    function render() {
      const m = cur();
      const chg = m && m.change_pct ? parseFloat(m.change_pct) * 100 : null;
      root.innerHTML = `
        <button class="sym-btn" type="button">
          <span class="base">${m ? m.base : '—'}</span>
          <span class="pair">${m ? m.popdex_symbol : '加载中'}</span>
          <span class="px num">${m ? fmtPx(m.last_price) : ''}</span>
          <span class="chg num ${chg == null ? '' : chg >= 0 ? 'up' : 'down'}">${m && chg != null ? pct(m.change_pct) : ''}</span>
          <span class="caret">▾</span>
        </button>
        <div class="sym-panel" style="display:${st.open ? 'block' : 'none'}">
          <input class="sym-search" placeholder="搜索币种…" value="${st.filter.replace(/"/g, '&quot;')}">
          <div class="sym-list"></div>
        </div>`;
      root.querySelector('.sym-btn').onclick = (e) => {
        e.stopPropagation();
        st.open = !st.open;
        render();
        if (st.open) root.querySelector('.sym-search').focus();
      };
      root.querySelector('.sym-search').oninput = (ev) => { st.filter = ev.target.value; renderList(); };
      root.querySelector('.sym-search').onclick = (e) => e.stopPropagation();
      renderList();
    }
    document.addEventListener('click', () => { if (st.open) { st.open = false; render(); } });
    root.addEventListener('click', (e) => e.stopPropagation());
    render();
    return { update(markets, current) { st.markets = markets; st.current = current; render(); } };
  },
};
