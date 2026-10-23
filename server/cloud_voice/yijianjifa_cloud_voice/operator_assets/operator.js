'use strict';

async function startOperator({document: doc, fetch: request, navigator: browser, schedule = null, cancel = null, crypto: random = globalThis.crypto, storage = null}) {
  try {storage ??= globalThis.sessionStorage;} catch (_) {}
  const byId = id => doc.getElementById(id);
  const ui = {};
  for (const id of ['guideVisitors', 'guideMessage', 'listUpdated', 'downloadRefresh', 'downloadClicks', 'downloadMessage', 'balanceRefresh', 'supplierAvailable', 'supplierCash', 'supplierArrears', 'balanceMessage', 'balanceUpdated', 'workspace', 'loginSection', 'operatorPassword', 'loginForm', 'loginButton', 'logoutButton', 'testNotice', 'globalMessage', 'retrySession', 'issueForm', 'issueFields', 'orderRef', 'issueCount', 'issueTotal', 'newPurchase', 'issueButton', 'issueMessage', 'issuedSection', 'issuedTitle', 'issuedCodes', 'copyIssued', 'clearIssued', 'searchForm', 'searchInput', 'statusFilter', 'searchButton', 'listMessage', 'codeRows', 'emptyList', 'pageSummary', 'previousPage', 'nextPage', 'confirmDialog', 'confirmBody', 'confirmError', 'cancelRevoke', 'confirmRevoke']) ui[id] = byId(id);
  let session = {authenticated: false, csrf: '', test_mode: false};
  let balanceTimer = null;
  let codesTimer = null;
  let balanceBusy = false;
  let authVersion = 0;
  let busy = false;
  let purchase = null;
  const rememberPurchase = () => {try { if (purchase) storage?.setItem('p011.operator.purchase',JSON.stringify(purchase)); else storage?.removeItem('p011.operator.purchase'); } catch (_) {} };
  const restorePurchase = () => {
    try { const saved=JSON.parse(storage?.getItem('p011.operator.purchase') || 'null');
      if (saved && /^[0-9a-f]{32}$/.test(saved.purchase_id) && typeof saved.note==='string' && saved.note.trim() && saved.note.length<=128 && Number.isInteger(saved.count) && saved.count>=1 && saved.count<=500 && typeof saved.complete==='boolean') purchase=saved;
    } catch (_) {}
    if (purchase) {ui.orderRef.value=purchase.note;ui.issueCount.value=String(purchase.count);setBusy(false);updateTotal();
      message(ui.issueMessage,purchase.complete?'上笔已生成，可在记录中查看或复制；新购买请点击“下一笔购买”。':'上笔结果尚未确认，点击生成可安全重试。');}
  };
  let page = 0;
  let total = 0;
  let pageSize = 20;
  let listVersion = 0;
  let pendingRevoke = null;
  const rowButtons = [];
  const statusLabels = {unused: '未交付', delivered: '已交付，待兑换', redeemed: '已兑换', revoked: '已作废'};

  const message = (target, content = '', error = false) => {
    target.textContent = content;
    target.dataset.error = error ? 'true' : 'false';
  };
  const create = (tag, content, className) => {
    const node = doc.createElement(tag);
    if (content !== undefined) node.textContent = String(content);
    if (className) node.className = className;
    return node;
  };
  const money = fen => (Number(fen) / 100).toLocaleString('zh-CN', {maximumFractionDigits: 2});
  const words = value => Number(value).toLocaleString('zh-CN');
  const formatTime = value => {
    if (!value) return '';
    const date = new Date(typeof value === 'number' ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN', {hour12: false});
  };
  const clearCodes = () => {
    ui.issuedCodes.value = '';
    ui.issuedSection.hidden = true;
  };
  const clearRevoke = () => {
    pendingRevoke = null;
    if (ui.confirmDialog.open) ui.confirmDialog.close();
    message(ui.confirmError);
  };
  const showAuth = authenticated => {
    authVersion += 1;
    if (codesTimer !== null && cancel) cancel(codesTimer);
    codesTimer = null;
    if (balanceTimer !== null && cancel) cancel(balanceTimer);
    balanceTimer = null;
    ui.workspace.hidden = !authenticated;
    ui.loginSection.hidden = authenticated;
    ui.logoutButton.hidden = !authenticated;
    if (!authenticated) {
      ui.guideVisitors.textContent = '—';
      ui.guideMessage.textContent = '';
      if (ui.downloadClicks) ui.downloadClicks.textContent = '—';
      if (ui.downloadMessage) ui.downloadMessage.textContent = '';
      listVersion += 1;
      for (const id of ['supplierAvailable','supplierCash','supplierArrears']) ui[id].textContent = '—';
      message(ui.balanceMessage);
      ui.balanceUpdated.textContent = '';
      clearCodes();
      clearRevoke();
      rowButtons.length = 0;
      ui.codeRows.replaceChildren();
      ui.orderRef.value = '';
      purchase = null;
      ui.newPurchase.hidden = true;
      ui.issueFields.disabled = false;
      message(ui.issueMessage);
      message(ui.listMessage);
      ui.listUpdated.textContent = '';
    }
  };
  const updatePages = () => {
    const pages = Math.max(1, Math.ceil(total / pageSize));
    ui.pageSummary.textContent = `共 ${words(total)} 枚 · ${page + 1}/${pages} 页`;
    ui.previousPage.disabled = busy || page === 0;
    ui.nextPage.disabled = busy || (page + 1) * pageSize >= total;
  };
  const setBusy = value => {
    busy = value;
    ui.issueFields.disabled = value || !!purchase?.complete;
    ui.newPurchase.hidden = !purchase;
    ui.newPurchase.disabled = value;
    ui.logoutButton.disabled = value;
    ui.confirmRevoke.disabled = value;
    ui.cancelRevoke.disabled = value;
    for (const button of rowButtons) button.disabled = value;
    updatePages();
  };
  const api = async (operation, body) => {
    const options = {method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', mode: 'same-origin', redirect: 'error', cache: 'no-store', headers: {'Accept': 'application/json'}};
    if (body !== undefined) {
      if (!session.csrf) throw new Error('连接尚未就绪，请重新连接后操作。');
      options.headers['Content-Type'] = 'application/json';
      options.headers['X-CSRF-Token'] = session.csrf;
      options.body = JSON.stringify(body);
    }
    let response;
    try { response = await request(`/operator/api/${operation}`, options); }
    catch (_) { throw new Error('无法连接管理服务，请检查连接后重试。'); }
    let payload;
    try { payload = await response.json(); }
    catch (_) { throw new Error('服务返回异常，请稍后重试。'); }
    if (!response.ok || !payload || payload.ok !== true) {
      const error = new Error(payload && payload.error && payload.error.message || '操作未完成，请稍后重试。');
      error.code = payload && payload.error && payload.error.code;
      error.authExpired = response.status === 401 && (
        error.code === 'operator_login_required' || error.code === 'session_expired' ||
        (operation !== 'login' && error.code !== 'invalid_credentials')
      );
      throw error;
    }
    return payload.data;
  };
  const loadDownloads = async () => {
    if (!session.authenticated || !ui.downloadClicks) return;
    const version = authVersion;
    try {
      const data = await api('download-stats');
      if (!session.authenticated || version !== authVersion) return;
      ui.downloadClicks.textContent = `${words(data.clicks)} 次`;
      message(ui.downloadMessage, '页面打开时每分钟更新。');
      const guide = await api('guide-stats');
      if (!session.authenticated || version !== authVersion) return;
      ui.guideVisitors.textContent = typeof guide.visits === 'number' ? `${words(guide.visits)} 次` : '—';
      message(ui.guideMessage, guide.started_at ? `统计自：${formatTime(guide.started_at)}` : '教程统计暂不可用。');
    } catch (error) {
      if (version === authVersion) await reportError(ui.downloadMessage, error);
    }
  };
  if (ui.downloadRefresh) ui.downloadRefresh.onclick = loadDownloads;
  const loadBalance = async () => {
    if (!session.authenticated || balanceBusy) return;
    if (balanceTimer !== null && cancel) cancel(balanceTimer);
    balanceTimer = null;
    void loadDownloads();
    balanceBusy = true;
    ui.balanceRefresh.disabled = true;
    const version = authVersion;
    try {
      const data = await api('supplier-balance');
      if (!session.authenticated || version !== authVersion) return;
      for (const [id, key] of [['supplierAvailable','available_balance'],['supplierCash','cash_balance'],['supplierArrears','arrears_balance']]) {
        const value = data && data[key];
        ui[id].textContent = typeof value === 'string' && /^-?\d+(?:\.\d+)?$/.test(value) ? `${value} 元` : '—';
      }
      ui.balanceUpdated.textContent = data.checked_at ? `更新于：${formatTime(data.checked_at)}` : '';
      message(ui.balanceMessage, data.status === 'ok' ? '页面打开时每分钟更新。' : `${data.message || '暂时无法查询。'}${data.checked_at ? ' 当前显示旧余额，请以更新时间为准。' : ''}`, data.status === 'unavailable');
    } catch (error) {
      if (!session.authenticated || version !== authVersion) return;
      await reportError(ui.balanceMessage, error);
      if (!error.authExpired) message(ui.balanceMessage, '暂时无法查询；如有旧余额，请以其更新时间为准。', true);
    } finally {
      balanceBusy = false;
      ui.balanceRefresh.disabled = false;
      if (session.authenticated && schedule) balanceTimer = schedule(() => { balanceTimer = null; void loadBalance(); }, 60000);
    }
  };
  ui.balanceRefresh.onclick = loadBalance;
  const readSession = async () => {
    const data = await api('session');
    session = data;
    ui.testNotice.hidden = !data.test_mode;
    showAuth(data.authenticated === true);
    if (session.authenticated && !purchase) restorePurchase();
    if (session.authenticated) { void loadBalance(); scheduleCodes(); }
    ui.retrySession.hidden = true;
  };
  const reportError = async (target, error) => {
    if (error.authExpired) {
      session.authenticated = false;
      session.csrf = '';
      ui.loginButton.disabled = true;
      showAuth(false);
      try { await readSession(); } catch (_) { session.csrf = ''; ui.retrySession.hidden = false; }
      ui.loginButton.disabled = !session.csrf;
      message(ui.globalMessage, '管理登录已失效，请重新登录后操作。', true);
      return;
    }
    message(target, error.message || '操作未完成，请稍后重试。', true);
  };
  const showCodes = (items, title) => {
    ui.issuedCodes.value = items.map(item => item.code).join('\n');
    ui.issuedTitle.textContent = title;
    ui.issuedSection.hidden = false;
  };
  const copyCodes = async () => {
    if (!ui.issuedCodes.value) return;
    try {
      if (!browser.clipboard || !browser.clipboard.writeText) throw new Error('clipboard unavailable');
      await browser.clipboard.writeText(ui.issuedCodes.value);
      message(ui.issueMessage, '已复制。请通过原购买渠道发给买家，再手动标记“已交付”。');
    } catch (_) {
      ui.issuedCodes.focus();
      ui.issuedCodes.select();
      message(ui.issueMessage, '自动复制不可用，已选中兑换码，请手动复制（Ctrl+C 或 ⌘C）。');
    }
  };
  const mutateRow = async (operation, item) => {
    if (busy || !session.authenticated) return;
    setBusy(true);
    try {
      const result = await api(operation, {id: item.id});
      if (operation === 'reveal') {
        showCodes([result], `购买备注 ${item.order_ref} · 兑换码`);
        await copyCodes();
      } else {
        clearCodes();
        message(ui.listMessage, operation === 'delivered' ? '已标记交付，等待买家兑换。' : '已作废，该兑换码不能再充值。');
        await loadCodes();
      }
    } catch (error) { await reportError(ui.listMessage, error); }
    finally { setBusy(false); }
  };
  const rowButton = (title, action, className = '') => {
    const button = create('button', title, `secondary ${className}`.trim());
    button.type = 'button';
    button.disabled = busy;
    button.onclick = action;
    rowButtons.push(button);
    return button;
  };
  const renderRows = items => {
    rowButtons.length = 0;
    ui.codeRows.replaceChildren();
    ui.emptyList.hidden = items.length !== 0;
    for (const item of items) {
      const row = create('tr');
      const order = create('td');
      order.append(create('div', item.order_ref), create('div', formatTime(item.created_at), 'cell-secondary'));
      const suffix = create('td', `··· ${item.suffix}`, 'code-suffix');
      const status = create('td');
      status.append(create('span', statusLabels[item.status] || '状态未知', `status status-${item.status}`));
      if (item.delivered_at && !item.redeemed_at) status.append(create('div', formatTime(item.delivered_at), 'cell-secondary'));
      const account = create('td', item.redeemed_username || '—');
      if (item.redeemed_at) account.append(create('div', formatTime(item.redeemed_at), 'cell-secondary'));
      const controls = create('td');
      const actions = create('div', undefined, 'actions');
      if (item.can_copy === true) actions.append(rowButton('查看 / 复制', () => mutateRow('reveal', item)));
      if (item.status === 'unused') actions.append(rowButton('标记已交付', () => mutateRow('delivered', item)));
      if (item.status === 'unused' || item.status === 'delivered') {
        actions.append(rowButton('作废', () => {
          if (busy) return;
          pendingRevoke = item;
          ui.confirmBody.textContent = `购买备注 ${item.order_ref}，兑换码末5位 ${item.suffix}。作废后不能再兑换，且不会自动退款；如需退款，请在原交易平台另行处理。`;
          message(ui.confirmError);
          ui.confirmDialog.showModal();
        }, 'revoke'));
      }
      if (actions.children.length === 0) actions.append(create('span', '—', 'muted'));
      controls.append(actions);
      row.append(order, suffix, status, account, controls);
      ui.codeRows.append(row);
    }
  };
  const listInteraction = () => busy || Boolean(ui.confirmDialog.open) ||
    Boolean(doc.activeElement && ui.codeRows.contains && ui.codeRows.contains(doc.activeElement));
  function scheduleCodes() {
    if (!schedule || !session.authenticated || codesTimer !== null) return;
    codesTimer = schedule(async () => {
      codesTimer = null;
      const version = authVersion;
      try {
        if (session.authenticated && !doc.hidden && !listInteraction()) await loadCodes(true);
      } catch (error) {
        if (version === authVersion) await reportError(ui.listMessage, error);
      } finally {
        if (version === authVersion) scheduleCodes();
      }
    }, 15000);
  }
  async function loadCodes(automatic = false) {
    const version = ++listVersion;
    const query = new URLSearchParams({page: String(page), search: ui.searchInput.value.trim(), status: ui.statusFilter.value || 'all'});
    const result = await api(`codes?${query.toString()}`);
    if (version !== listVersion || !session.authenticated || (automatic && (doc.hidden || listInteraction()))) return;
    total = Number(result.total) || 0;
    page = Number(result.page) || 0;
    pageSize = Number(result.page_size) || 20;
    renderRows(result.items);
    ui.listUpdated.textContent = `最近更新：${new Date().toLocaleTimeString('zh-CN', {hour12:false})}`;
    updatePages();
  }
  const refresh = async () => {
    try { await loadCodes(); }
    catch (error) { await reportError(ui.listMessage, error); }
  };
  const updateTotal = () => {
    const count = Number(ui.issueCount.value);
    ui.issueTotal.textContent = Number.isInteger(count) && count >= 1 && count <= 500 ? `${money(count * 1990)} 元 · ${words(count)} 万字` : '请输入 1—500 枚';
  };
  ui.issueCount.value = '1';
  ui.statusFilter.value = 'all';
  ui.searchInput.value = '';
  ui.orderRef.value = '';
  ui.issueCount.oninput = updateTotal;
  ui.loginForm.onsubmit = async event => {
    event.preventDefault();
    if (ui.loginButton.disabled) return;
    const password = ui.operatorPassword.value;
    if (!password) { message(ui.globalMessage, '请填写管理密码。', true); return; }
    ui.loginButton.disabled = true;
    ui.operatorPassword.value = '';
    try {
      await api('login', {password});
      await readSession();
      message(ui.globalMessage);
      if (session.authenticated) await refresh();
    } catch (error) { await reportError(ui.globalMessage, error); }
    finally { ui.loginButton.disabled = !session.csrf; }
  };
  ui.logoutButton.onclick = async () => {
    if (busy) return;
    setBusy(true);
    clearCodes();
    try {
      await api('logout', {});
      purchase = null; rememberPurchase();
      session.authenticated = false;
      showAuth(false);
      await readSession();
      message(ui.globalMessage, '已退出兑换码管理。');
    } catch (error) { await reportError(ui.globalMessage, error); }
    finally { setBusy(false); }
  };
  ui.issueForm.onsubmit = async event => {
    event.preventDefault();
    if (busy || !session.authenticated || purchase?.complete) return;
    const order = ui.orderRef.value.trim();
    const count = Number(ui.issueCount.value);
    if (!order) { message(ui.issueMessage, '请填写购买备注，例如微信昵称或店铺订单号。', true); return; }
    if (!Number.isInteger(count) || count < 1 || count > 500) { message(ui.issueMessage, '兑换码数量须为 1—500 之间的整数。', true); return; }
    if (purchase && (purchase.note !== order || purchase.count !== count)) {
      message(ui.issueMessage, '上一笔结果尚未确认，请先使用原备注和数量重试。', true); return;
    }
    if (!purchase) purchase = {note:order, count, purchase_id:random.randomUUID().replaceAll('-',''), complete:false};
    rememberPurchase();
    setBusy(true);
    clearCodes();
    try {
      const result = await api('issue-purchase', {note:order, count, purchase_id:purchase.purchase_id});
      purchase.complete = true;
      rememberPurchase();
      showCodes(result.codes, `购买备注 ${order} · ${result.codes.length} 枚兑换码`);
      message(ui.issueMessage, result.replayed ? '已找回本笔购买的原兑换码，没有重复发放。' : `已生成 ${result.codes.length} 枚兑换码。请复制后通过原购买渠道交付。`);
      page = 0;
      await refresh();
    } catch (error) { await reportError(ui.issueMessage, error); }
    finally { setBusy(false); }
  };
  ui.newPurchase.onclick = () => { if (busy) return; purchase = null; rememberPurchase(); clearCodes(); message(ui.issueMessage); setBusy(false); ui.orderRef.focus(); };
  ui.copyIssued.onclick = copyCodes;
  ui.clearIssued.onclick = () => { clearCodes(); message(ui.issueMessage); };
  ui.searchForm.onsubmit = async event => { event.preventDefault(); page = 0; message(ui.listMessage); await refresh(); };
  ui.statusFilter.onchange = async () => { page = 0; message(ui.listMessage); await refresh(); };
  ui.previousPage.onclick = async () => { if (busy || page === 0) return; page -= 1; await refresh(); };
  ui.nextPage.onclick = async () => { if (busy || (page + 1) * pageSize >= total) return; page += 1; await refresh(); };
  ui.cancelRevoke.onclick = clearRevoke;
  ui.confirmDialog.oncancel = event => { if (busy) event.preventDefault(); else clearRevoke(); };
  ui.confirmRevoke.onclick = async () => {
    if (busy || !pendingRevoke || !session.authenticated) return;
    const item = pendingRevoke;
    setBusy(true);
    try {
      await api('revoke', {id: item.id});
      clearRevoke();
      clearCodes();
      message(ui.listMessage, '已作废，该兑换码不能再充值。');
      await refresh();
    } catch (error) { await reportError(ui.confirmError, error); }
    finally { setBusy(false); }
  };
  const connect = async () => {
    ui.retrySession.disabled = true;
    try {
      await readSession();
      message(ui.globalMessage);
      if (session.authenticated) await refresh();
    } catch (error) {
      showAuth(false);
      ui.retrySession.hidden = false;
      message(ui.globalMessage, error.message, true);
    } finally { ui.retrySession.disabled = false; ui.loginButton.disabled = !session.csrf; }
  };
  ui.retrySession.onclick = connect;
  await connect();
}

if (typeof module === 'object' && module.exports) module.exports = {startOperator};
if (typeof window !== 'undefined' && typeof document !== 'undefined') {
  startOperator({document, fetch: window.fetch.bind(window), navigator: window.navigator, schedule: window.setTimeout.bind(window), cancel: window.clearTimeout.bind(window)});
}
