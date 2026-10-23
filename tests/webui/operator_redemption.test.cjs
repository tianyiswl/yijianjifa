const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {Node} = require('./support/commerce_view.cjs');
const assets = path.resolve(__dirname, '../../server/cloud_voice/yijianjifa_cloud_voice/operator_assets');
const flush = () => new Promise(resolve => setImmediate(resolve));
function text(node) { return [node.textContent, ...node.children.map(text)].join(' '); }
async function mount(handler, clipboard, timers = {}) {
  const ids = new Map();
  const html = fs.existsSync(path.join(assets, 'index.html')) ? fs.readFileSync(path.join(assets, 'index.html'), 'utf8') : '';
  for (const match of html.matchAll(/<(\w+)\b[^>]*\bid="([^"]+)"[^>]*>/g)) ids.set(match[2], new Node(match[1]));
  for (const node of ids.values()) {
    node.setSelectionRange = () => { node.selected = true; };
    node.select = () => { node.selected = true; };
    node.showModal = () => { node.open = true; };
    node.close = () => { node.open = false; };
  }
  const document = {getElementById: id => ids.get(id), createElement: tag => new Node(tag)};
  const calls = [];
  const fetch = async (url, options = {}) => {
    const parsed = new URL(url, 'http://localhost');
    const body = options.body ? JSON.parse(options.body) : undefined;
    calls.push({path: parsed.pathname, params: parsed.searchParams, body, options});
    const result = await handler(parsed.pathname.split('/').at(-1), body, parsed.searchParams, calls);
    if (result && result.error) return {ok: false, status: result.status || 400, json: async () => ({ok: false, error: result.error})};
    return {ok: true, status: 200, json: async () => ({ok: true, data: result})};
  };
  const filename = path.join(assets, 'operator.js');
  const start = fs.existsSync(filename) ? require(filename).startOperator : undefined;
  assert.equal(typeof start, 'function', '管理页需要能够启动真实表单流程');
  await start({document, fetch, navigator: {clipboard}, crypto:require("node:crypto").webcrypto, ...timers});
  return {ids, calls, document, get: id => ids.get(id), flush};
}
const session = (authenticated = true, testMode = true, csrf = 'csrf-test') => ({authenticated, test_mode: testMode, csrf, pack: {price_fen: 1990, words: 10000}});
const code = extra => ({id: 'c-1', suffix: 'ABCDE', order_ref: 'ORDER-1', batch_id: 'b-1', words: 10000, price_fen: 1990, created_at: '2026-10-18T10:00:00+08:00', delivered_at: null, redeemed_at: null, redeemed_username: '', status: 'unused', can_copy: true, ...extra});
const base = async op => op === 'session' ? session() : op === 'codes' ? {items: [code()], total: 1, page: 0, page_size: 20} : {};
async function submit(v, id) { return v.get(id).onsubmit({preventDefault() {}}); }
async function click(v, id) { return v.get(id).onclick({preventDefault() {}}); }
function button(v, title) {
  const all = [...v.ids.values()].flatMap(function walk(n) { return [n, ...n.children.flatMap(walk)]; });
  return all.find(n => n.tag === 'button' && n.textContent === title);
}

test('unauthenticated visitors only get login and submitted password is cleared while CSRF rotates', async () => {
  let authenticated = false;
  const v = await mount(async (op, body) => {
    if (op === 'session') return session(authenticated, true, authenticated ? 'new-csrf' : 'anonymous-csrf');
    if (op === 'login') { assert.equal(body.password, 'operator-password'); authenticated = true; return {}; }
    return base(op);
  });
  assert.equal(v.get('workspace').hidden, true);
  assert.equal(v.get('loginSection').hidden, false);
  v.get('operatorPassword').value = 'operator-password';
  await submit(v, 'loginForm');
  assert.equal(v.get('operatorPassword').value, '');
  assert.equal(v.get('workspace').hidden, false);
  assert.equal(v.get('loginSection').hidden, true);
  const login = v.calls.find(c => c.path.endsWith('/login'));
  assert.equal(login.options.headers['X-CSRF-Token'], 'anonymous-csrf');
  assert.equal(login.options.credentials, 'same-origin');
});

test('issuing requires a note and bounded quantity before any mutation', async () => {
  const v = await mount(base);
  v.get('orderRef').value = '  ';
  v.get('issueCount').value = '1';
  await submit(v, 'issueForm');
  assert.match(v.get('issueMessage').textContent, /购买备注/);
  v.get('orderRef').value = 'ORDER-1';
  v.get('issueCount').value = '501';
  await submit(v, 'issueForm');
  assert.match(v.get('issueMessage').textContent, /1.*500/);

  assert.ok(!v.calls.some(c => c.path.endsWith('/issue-purchase')));
});

test('test issuance labels simulation, sends normalized order and shows replay without implying delivery', async () => {
  const v = await mount(async (op, body) => {
    if (op === 'issue-purchase') {
      assert.equal(body.note,'ORDER-1'); assert.equal(body.count,1); assert.match(body.purchase_id,/^[0-9a-f]{32}$/); assert.equal(body.payment_confirmed,undefined);
      return {codes: [{id: 'c-1', code: 'TEST-CODE-ABCDE', batch_id: 'b-1', price_fen: 1990, words: 10000, replayed: true}], replayed: true};
    }
    return base(op);
  }, {writeText: async () => {}});
  assert.equal(v.get('testNotice').hidden, false);
  v.get('orderRef').value = ' ORDER-1 ';
  v.get('issueCount').value = '1';
  await submit(v, 'issueForm');
  assert.equal(v.get('issuedCodes').value, 'TEST-CODE-ABCDE');
  assert.match(v.get('issueMessage').textContent, /原兑换码/);
  await click(v, 'copyIssued');
  assert.ok(!v.calls.some(c => c.path.endsWith('/delivered')));
  assert.equal(v.calls.find(c => c.path.endsWith('/issue-purchase')).options.headers['X-CSRF-Token'], 'csrf-test');
});

test('copy failure keeps the revealed code selected so the operator can copy manually', async () => {
  const v = await mount(async op => op === 'reveal' ? {id: 'c-1', code: 'TEST-CODE-ABCDE'} : base(op), {writeText: async () => {throw Error('unavailable');}});
  await button(v, '查看 / 复制').onclick();
  assert.equal(v.get('issuedCodes').value, 'TEST-CODE-ABCDE');
  assert.equal(v.get('issuedCodes').selected, true);
  assert.match(v.get('issueMessage').textContent, /手动复制/);
  assert.ok(!v.calls.some(c => c.path.endsWith('/delivered')));
});

test('list searches and paginates through encoded query fields and safely renders account text', async () => {
  const v = await mount(async (op, body, params) => {
    if (op === 'codes') return {items: [code({redeemed_username: '<img src=x onerror=alert(1)>', status: 'redeemed', can_copy: false})], total: 42, page: Number(params.get('page')), page_size: 20};
    return base(op);
  });
  assert.match(text(v.get('codeRows')), /<img src=x onerror=alert\(1\)>/);
  assert.equal(button(v, '查看 / 复制'), undefined);
  v.get('searchInput').value = '订单 & 账号';
  v.get('statusFilter').value = 'redeemed';
  await submit(v, 'searchForm');
  const query = v.calls.filter(c => c.path.endsWith('/codes')).at(-1);
  assert.equal(query.params.get('search'), '订单 & 账号');
  assert.equal(query.params.get('status'), 'redeemed');
  await click(v, 'nextPage');
  assert.equal(v.calls.filter(c => c.path.endsWith('/codes')).at(-1).params.get('page'), '1');
  assert.match(v.get('pageSummary').textContent, /42/);
});

test('revocation waits for explicit confirmation explaining that it does not refund', async () => {
  const v = await mount(base);
  await button(v, '作废').onclick();
  assert.equal(v.get('confirmDialog').open, true);
  assert.match(v.get('confirmBody').textContent, /不会.*退款/);
  assert.ok(!v.calls.some(c => c.path.endsWith('/revoke')));
  await click(v, 'cancelRevoke');
  assert.ok(!v.calls.some(c => c.path.endsWith('/revoke')));
  await button(v, '作废').onclick();
  await click(v, 'confirmRevoke');
  assert.deepEqual(v.calls.find(c => c.path.endsWith('/revoke')).body, {id: 'c-1'});
});

test('logout removes visible codes and authentication errors hide the workspace', async () => {
  let authenticated = true;
  const v = await mount(async op => {
    if (op === 'session') return session(authenticated);
    if (op === 'logout') {authenticated = false; return {};}
    return base(op);
  });
  v.get('issuedCodes').value = 'SECRET';
  await click(v, 'logoutButton');
  assert.equal(v.get('issuedCodes').value, '');
  assert.equal(v.get('workspace').hidden, true);
});

test('wrong login password shows the server explanation rather than claiming a session expired', async () => {
  const v = await mount(async op => op === 'session' ? session(false) : {status: 401, error: {code: 'invalid_credentials', message: '管理密码不正确。'}});
  v.get('operatorPassword').value = 'wrong-password';
  await submit(v, 'loginForm');
  assert.equal(v.get('operatorPassword').value, '');
  assert.equal(v.get('workspace').hidden, true);
  assert.equal(v.get('globalMessage').textContent, '管理密码不正确。');
});

test('marking delivery requires its own explicit action and never exposes a code', async () => {
  const v = await mount(base);
  await button(v, '标记已交付').onclick();
  assert.deepEqual(v.calls.find(c => c.path.endsWith('/delivered')).body, {id: 'c-1'});
  assert.ok(!v.calls.some(c => c.path.endsWith('/reveal')));
  assert.match(v.get('listMessage').textContent, /已标记交付/);
});

test('a second generate click during the first request does not create a second issue request', async () => {
  let release;
  const waiting = new Promise(resolve => {release = resolve;});
  const v = await mount(async op => {
    if (op === 'issue-purchase') {await waiting; return {codes: [{id: 'c-1', code: 'TEST-CODE-ABCDE'}], replayed: false};}
    return base(op);
  });
  v.get('orderRef').value = 'ORDER-1';
  const issuing = submit(v, 'issueForm');
  await flush();
  assert.equal(v.get('issueFields').disabled, true);
  await submit(v, 'issueForm');
  assert.equal(v.calls.filter(c => c.path.endsWith('/issue-purchase')).length, 1);
  release();
  await issuing;
  assert.equal(v.get('issueFields').disabled, true);
  await submit(v,'issueForm');assert.equal(v.calls.filter(c=>c.path.endsWith('/issue-purchase')).length,1);
  await click(v,'newPurchase');assert.equal(v.get('issueFields').disabled,false);
});

test('expired authenticated session clears revealed codes and returns to login', async () => {
  let authenticated = true;
  const v = await mount(async op => {
    if (op === 'session') return session(authenticated);
    if (op === 'reveal') {authenticated = false; return {status: 401, error: {code: 'UNAUTHORIZED', message: '登录已过期'}};}
    return base(op);
  });
  v.get('issuedCodes').value = 'PREVIOUS-SECRET';
  await button(v, '查看 / 复制').onclick();
  assert.equal(v.get('issuedCodes').value, '');
  assert.equal(v.get('workspace').hidden, true);
  assert.equal(v.get('loginSection').hidden, false);
  assert.match(v.get('globalMessage').textContent, /登录已失效/);
});

test('record times interpret server Unix seconds as the actual 2026 date', async () => {
  const v = await mount(async op => op === 'codes' ? {items: [code({created_at: 1792288800, delivered_at: 1792288860, redeemed_at: 1792288920, redeemed_username: 'buyer', status: 'redeemed', can_copy: false})], total: 1, page: 0, page_size: 20} : base(op));
  assert.match(text(v.get('codeRows')), /2026/);
  assert.doesNotMatch(text(v.get('codeRows')), /1970/);
});

test('expired anonymous session refreshes CSRF and waits for another login without replaying the password', async () => {
  let sessionReads = 0;
  let authenticated = false;
  let loginCalls = 0;
  const v = await mount(async (op, body, params, calls) => {
    if (op === 'session') {sessionReads += 1; return session(authenticated, true, sessionReads === 1 ? 'expired-csrf' : 'fresh-csrf');}
    if (op === 'login') {
      loginCalls += 1;
      if (loginCalls === 1) return {status: 401, error: {code: 'operator_login_required', message: '请先登录兑换码管理页。'}};
      assert.equal(calls.at(-1).options.headers['X-CSRF-Token'], 'fresh-csrf');
      authenticated = true;
      return {};
    }
    return base(op);
  });
  v.get('issuedCodes').value = 'STALE-SECRET';
  v.get('operatorPassword').value = 'operator-password';
  await submit(v, 'loginForm');
  assert.equal(loginCalls, 1, '过期后不得自动重放管理密码');
  assert.equal(sessionReads, 2, '过期后应获取新的匿名 session 和 CSRF');
  assert.equal(v.get('operatorPassword').value, '');
  assert.equal(v.get('issuedCodes').value, '');
  assert.equal(v.get('workspace').hidden, true);
  assert.equal(v.get('loginButton').disabled, false);
  assert.match(v.get('globalMessage').textContent, /登录已失效/);
  v.get('operatorPassword').value = 'operator-password';
  await submit(v, 'loginForm');
  assert.equal(loginCalls, 2);
  assert.equal(v.get('workspace').hidden, false);
});

test('explicit session_expired on login refreshes session without replaying any operation', async () => {
  let sessionReads = 0;
  const v = await mount(async op => {
    if (op === 'session') {sessionReads += 1; return session(false);}
    return {status: 401, error: {code: 'session_expired', message: '会话过期。'}};
  });
  v.get('operatorPassword').value = 'operator-password';
  await submit(v, 'loginForm');
  assert.equal(sessionReads, 2);
  assert.equal(v.calls.filter(c => c.path.endsWith('/login')).length, 1);
  assert.equal(v.get('workspace').hidden, true);
  assert.equal(v.get('operatorPassword').value, '');
});

test('failed session refresh keeps login disabled until an explicit reconnect gets fresh CSRF', async () => {
  let sessionReads = 0;
  const v = await mount(async op => {
    if (op === 'session') {
      sessionReads += 1;
      if (sessionReads === 2) throw Error('connection lost');
      return session(false, true, sessionReads === 1 ? 'old-csrf' : 'new-csrf');
    }
    return {status: 401, error: {code: 'operator_login_required', message: '请先登录兑换码管理页。'}};
  });
  v.get('operatorPassword').value = 'operator-password';
  await submit(v, 'loginForm');
  assert.equal(v.get('loginButton').disabled, true);
  assert.equal(v.get('retrySession').hidden, false);
  assert.equal(v.calls.filter(c => c.path.endsWith('/login')).length, 1);
  await click(v, 'retrySession');
  assert.equal(v.get('loginButton').disabled, false);
  assert.equal(v.get('retrySession').hidden, true);
  assert.equal(v.calls.filter(c => c.path.endsWith('/login')).length, 1);
});

test('forbidden CSRF or origin response does not automatically replay or disguise the server reason', async () => {
  let sessionReads = 0;
  const v = await mount(async op => {
    if (op === 'session') {sessionReads += 1; return session();}
    if (op === 'reveal') return {status: 403, error: {code: 'forbidden', message: '操作验证已失效，请刷新管理页。'}};
    return base(op);
  });
  await button(v, '查看 / 复制').onclick();
  assert.equal(sessionReads, 1);
  assert.equal(v.get('workspace').hidden, false);
  assert.equal(v.calls.filter(c => c.path.endsWith('/reveal')).length, 1);
  assert.equal(v.get('listMessage').textContent, '操作验证已失效，请刷新管理页。');
});

test('supplier funds show successful value and stale time instead of zero on refresh failure', async () => {
  let fail=false;
  const v=await mount(async op => op==='supplier-balance' ? (fail ? {status:'unavailable',available_balance:'77.01',cash_balance:'83.01',arrears_balance:'0.00',checked_at:1000,message:'暂时无法查询'} : {status:'ok',available_balance:'77.01',cash_balance:'83.01',arrears_balance:'0.00',checked_at:1000}) : base(op));
  await flush();
  assert.equal(v.get('supplierAvailable').textContent,'77.01 元');
  fail=true;await click(v,'balanceRefresh');
  assert.equal(v.get('supplierAvailable').textContent,'77.01 元');
  assert.match(v.get('balanceMessage').textContent,/旧余额/);
  assert.match(v.get('balanceUpdated').textContent,/更新于/);
});

test('unconfigured funds display no invented zero and do not prevent issuing codes', async () => {
  const v=await mount(async op => op==='supplier-balance' ? {status:'unconfigured',message:'尚未配置供应商只读费用查询。'} : base(op));
  await flush();
  assert.equal(v.get('supplierAvailable').textContent,'—');
  assert.match(v.get('balanceMessage').textContent,/尚未配置/);
  assert.equal(v.get('workspace').hidden,false);
});

test('download clicks refresh, disclose measurement and disappear on logout', async () => {
  let count = 7, authenticated = true;
  const v = await mount(async op => {
    if (op === 'logout') { authenticated = false; return {}; }
    if (op === 'session') return session(authenticated);
    return op === 'download-stats' ? {clicks:count,started_at:1792377539} : base(op);
  });
  await v.flush();
  assert.equal(v.get('downloadClicks').textContent,'7 次');
  assert.match(v.get('downloadMessage').textContent,/页面打开时每分钟更新/);
  count = 8;
  await click(v,'downloadRefresh');
  assert.equal(v.get('downloadClicks').textContent,'8 次');
  await click(v,'logoutButton');
  assert.equal(v.get('downloadClicks').textContent,'—');
  assert.equal(v.get('workspace').hidden,true);
});


test('automatic records refresh picks up redemption, preserves filters, pauses hidden and cancels on logout', async () => {
  const timers = new Map(); let next = 0, redeemed = false, authenticated = true;
  const v = await mount(async op => {
    if (op === 'session') return session(authenticated);
    if (op === 'logout') { authenticated=false; return {}; }
    if (op === 'codes') return {items:[code(redeemed ? {status:'redeemed',redeemed_username:'test-buyer',redeemed_at:1792377539,can_copy:false} : {})],total:1,page:0,page_size:20};
    return base(op);
  }, undefined, {schedule:(fn, ms) => { const id=++next; timers.set(id,{fn,ms}); return id; },cancel:id=>timers.delete(id)});
  const tick = async () => { const entry=[...timers].find(([,t])=>t.ms===15000); assert.ok(entry); timers.delete(entry[0]); await entry[1].fn(); };
  await flush();
  redeemed=true; v.get('searchInput').value='ORDER-1'; v.get('statusFilter').value='redeemed';
  await tick();
  assert.match(text(v.get('codeRows')), /已兑换/);
  assert.match(text(v.get('codeRows')), /test-buyer/);
  const last=v.calls.filter(c=>c.path.endsWith('/codes')).at(-1);
  assert.equal(last.params.get('search'),'ORDER-1'); assert.equal(last.params.get('status'),'redeemed');
  const before=v.calls.length; v.document.hidden=true; await tick(); assert.equal(v.calls.length,before);
  v.document.hidden=false; v.get('confirmDialog').open=true; await tick(); assert.equal(v.calls.length,before);
  v.get('confirmDialog').open=false; await click(v,'logoutButton');
  assert.equal([...timers.values()].filter(t=>t.ms===15000).length,0);
});

test('guide entry total opens update separately from download clicks', async () => {
  let visits=2;
  const v=await mount(async op => {
    if(op==='guide-stats') return {visits,started_at:1792377539};
    if(op==='download-stats') return {clicks:9,started_at:1792377539};
    return base(op);
  });
  await flush(); assert.equal(v.get('guideVisitors').textContent,'2 次');
  assert.match(v.get('guideMessage').textContent, /统计自/);
  assert.equal(v.get('downloadClicks').textContent,'9 次');
  visits=3; await click(v,'downloadRefresh');
  assert.equal(v.get('guideVisitors').textContent,'3 次');
});

test('a lost reply survives reload; retries reuse purchase identity and next purchase accepts the same note', async () => {
  const values=new Map(),storage={setItem:(k,v)=>values.set(k,v),getItem:k=>values.get(k),removeItem:k=>values.delete(k)};
  let id, attempts=0;
  const handler=async (op,body)=>{
    if(op==='issue-purchase') {
      attempts++;
      if(attempts===1){id=body.purchase_id;throw Error('lost reply');}
      if(attempts===2)assert.equal(body.purchase_id,id);
      if(attempts===3)assert.notEqual(body.purchase_id,id);
      return {codes:[{id:'c-'+attempts,code:'TEST-'+attempts}],replayed:attempts===2};
    }
    return base(op);
  };
  const first=await mount(handler,undefined,{storage});first.get('orderRef').value='微信小王';
  await submit(first,'issueForm');assert.equal(attempts,1);
  const resumed=await mount(handler,undefined,{storage});
  assert.equal(resumed.get('orderRef').value,'微信小王');
  await submit(resumed,'issueForm');assert.equal(attempts,2);
  assert.equal(resumed.get('newPurchase').hidden,false);
  await submit(resumed,'issueForm');assert.equal(attempts,2);
  await click(resumed,'newPurchase');assert.equal(resumed.get('orderRef').value,'微信小王');
  await submit(resumed,'issueForm');assert.equal(attempts,3);
});
