// 在浏览器页面上下文里调用教务 getCalendar 接口，取回完整 JSON 数据。
// 用法：node cdp_grab.mjs <port> <out.json> <d1> <d2>
// 依赖 Node 22+（自带全局 WebSocket / fetch）。
import fs from 'node:fs';

const PORT = process.argv[2];
const OUT = process.argv[3];
const D1 = process.argv[4];
const D2 = process.argv[5];
const BASE_URL = (process.argv[6] || 'https://jw.example.edu.cn').replace(/\/+$/, '');
if (!PORT || !OUT || !D1 || !D2) {
  console.error('usage: node cdp_grab.mjs <port> <out.json> <d1> <d2>');
  process.exit(2);
}
const base = `http://127.0.0.1:${PORT}`;

async function jget(path, method = 'GET') {
  const r = await fetch(base + path, { method });
  const t = await r.text();
  try { return JSON.parse(t); } catch { return { raw: t, status: r.status }; }
}

function connect(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const pending = new Map();
    let id = 0;
    ws.onopen = () => resolve({
      send(method, params = {}) {
        return new Promise((res, rej) => {
          const mid = ++id;
          pending.set(mid, { res, rej });
          ws.send(JSON.stringify({ id: mid, method, params }));
        });
      },
      close() { try { ws.close(); } catch { } },
    });
    ws.onerror = (e) => reject(new Error('ws error: ' + (e.message || e)));
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.id && pending.has(msg.id)) {
        const { res, rej } = pending.get(msg.id);
        pending.delete(msg.id);
        if (msg.error) rej(new Error(msg.error.message || JSON.stringify(msg.error)));
        else res(msg.result);
      }
    };
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const result = { ok: false, cookies: [], fetchOk: false, count: null, error: null, url: null };
try {
  const targets = await jget('/json/list');
  let page = (targets || []).find((t) => t.type === 'page');
  if (!page) page = await jget('/json/new?url=about:blank', 'PUT');
  if (!page || !page.webSocketDebuggerUrl) throw new Error('no page target');

  const cdp = await connect(page.webSocketDebuggerUrl);
  await cdp.send('Page.enable');
  await cdp.send('Network.enable');

  const fetchCalendar = async () => {
    const evalRes = await cdp.send('Runtime.evaluate', {
      expression: `(async () => {
        try {
          const r = await fetch('/new/desktop/getCalendar', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8' },
            body: new URLSearchParams({ d1: '${D1}', d2: '${D2}' }).toString()
          });
          const t = await r.text();
          let n = null, data = null, html = false;
          try { data = JSON.parse(t); n = data.length; } catch {
            html = /<!doctype|<html|登录|login/i.test(t.slice(0, 400));
          }
          return JSON.stringify({ status: r.status, len: t.length, count: n, data, html,
                                  text: html ? t.slice(0, 200) : null });
        } catch (e) { return JSON.stringify({ error: String((e && e.message) || e) }); }
      })()`,
      awaitPromise: true, returnByValue: true,
    });
    const info = evalRes && evalRes.result ? evalRes.result.value : null;
    let parsed = {};
    try { parsed = JSON.parse(info); } catch { }
    return { info, parsed };
  };

  // 先直奔欢迎页；若被踢回登录页，再走一次 SSO（有 CAS 票据时会自动回跳）
  await cdp.send('Page.navigate', { url: BASE_URL + '/new/welcome.page?ui=new' });
  await sleep(3500);
  let attempt = await fetchCalendar();
  if (typeof attempt.parsed.count !== 'number') {
    await cdp.send('Page.navigate', { url: BASE_URL + '/new/ssoLogin' });
    await sleep(4500);
    await cdp.send('Page.navigate', { url: BASE_URL + '/new/welcome.page?ui=new' });
    await sleep(2000);
    attempt = await fetchCalendar();
  }
  result.fetchInfo = attempt.info;
  result.fetchOk = typeof attempt.parsed.count === 'number';
  result.count = attempt.parsed.count === undefined ? null : attempt.parsed.count;
  result.data = attempt.parsed.data || null;
  result.loginHtml = !!attempt.parsed.html;

  const cookies = await cdp.send('Storage.getCookies', {});
  result.cookies = (cookies.cookies || []).map((c) => ({ name: c.name, domain: c.domain, session: c.session }));
  result.url = page.url;
  result.ok = result.cookies.length > 0;
  cdp.close();
} catch (e) {
  result.error = String((e && e.stack) || e);
}
fs.writeFileSync(OUT, JSON.stringify(result, null, 1), 'utf8');
console.log('ok=%s fetchOk=%s count=%s cookies=%d error=%s', result.ok, result.fetchOk, result.count,
  result.cookies.length, result.error ? String(result.error).slice(0, 200) : '-');
