import { chromium } from '@playwright/test';
import fs from 'node:fs/promises';
import path from 'node:path';

const baseURL = process.env.QA_CENTER_AUDIT_URL || 'http://127.0.0.1:28095';
const output = path.resolve('../artifacts/ui-audit/qa-center/final');
await fs.mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const manifest = [];
const layoutFailures = [];

async function capture({ screen, route, state, viewport = { width: 1366, height: 768 }, prepare }) {
  const page = await browser.newPage({ viewport });
  await page.goto(baseURL + route, { waitUntil: 'networkidle' });
  if (prepare) await prepare(page);
  await page.waitForTimeout(150);
  const geometry = await page.evaluate(() => {
    const root = document.documentElement;
    const badClickTargets = [...document.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),select:not([disabled])')]
      .filter(el => el.offsetParent !== null && !el.closest('[hidden]'))
      .filter(el => { const r = el.getBoundingClientRect(); return r.width <= 0 || r.height <= 0 || r.right > root.clientWidth + 1 || r.left < -1; })
      .map(el => `${el.tagName.toLowerCase()}#${el.id || ''}.${el.className || ''}`);
    const dialog = document.querySelector('dialog[open]');
    const dr = dialog?.getBoundingClientRect();
    return { viewportWidth: root.clientWidth, scrollWidth: root.scrollWidth, badClickTargets, dialogClipped: Boolean(dr && (dr.top < 0 || dr.bottom > innerHeight)) };
  });
  const name = `${screen}-${state}-${viewport.width}x${viewport.height}.png`;
  await page.screenshot({ path: path.join(output, name), fullPage: false });
  const verdict = geometry.scrollWidth <= geometry.viewportWidth && geometry.badClickTargets.length === 0 && !geometry.dialogClipped ? 'PASS' : 'FAIL';
  if (verdict === 'FAIL') layoutFailures.push({ screen, state, geometry });
  manifest.push({ screen, route, state, viewport: `${viewport.width}x${viewport.height}`, screenshot: name, verdict });
  await page.close();
}

const setRunState = status => async page => {
  await page.evaluate(value => {
    const runs = document.querySelector('#runs');
    if (runs) runs.innerHTML = `<div class="run"><div class="run-main"><div class="run-title">Functional Smoke</div><div class="run-meta"><span>LOCAL · MESFlow Local</span><span>12 pass</span><span>${value === 'FAILED' ? '1 lỗi' : '0 lỗi'}</span><span>00:42</span></div></div><div class="run-status"><span class="badge ${value}">${value}</span></div></div>`;
    const logState = document.querySelector('#logState'); if (logState) logState.textContent = value === 'RUNNING' ? 'Suite Authentication · attempt 1 · 6/12 · 00:42' : `Kết quả ${value}`;
  }, status);
};

await capture({ screen: 'dashboard', route: '/', state: 'normal' });
await capture({ screen: 'dashboard', route: '/', state: 'desktop-wide', viewport: { width: 1920, height: 1080 } });
await capture({ screen: 'dashboard', route: '/', state: 'desktop', viewport: { width: 1440, height: 900 } });
await capture({ screen: 'dashboard', route: '/', state: 'tablet', viewport: { width: 1024, height: 768 } });
await capture({ screen: 'dashboard', route: '/', state: 'no-data', prepare: async page => page.locator('#runs').evaluate(el => el.innerHTML = '<div class="empty"><strong>Chưa có phiên chạy</strong><br>Chạy một scenario để tạo kết quả và evidence.</div>') });
await capture({ screen: 'target', route: '/', state: 'connected', prepare: async page => page.evaluate(() => { const el=document.querySelector('#contextConnection'); el.textContent='CONNECTED'; el.className='pill ok'; }) });
await capture({ screen: 'target', route: '/', state: 'unavailable', prepare: async page => page.evaluate(() => { const el=document.querySelector('#contextConnection'); el.textContent='UNAVAILABLE'; el.className='pill bad'; }) });
await capture({ screen: 'scenario-list', route: '/', state: 'normal' });
await capture({ screen: 'scenario', route: '/regression', state: 'filtered' });
await capture({ screen: 'preview-lab', route: '/preview-lab', state: 'no-environments' });
await capture({ screen: 'bug-center', route: '/bugs', state: 'normal' });
await capture({ screen: 'run', route: '/simulations', state: 'setup' });
await capture({ screen: 'run', route: '/', state: 'running', prepare: setRunState('RUNNING') });
await capture({ screen: 'result', route: '/', state: 'passed', prepare: setRunState('PASSED') });
await capture({ screen: 'result', route: '/', state: 'failed', prepare: setRunState('FAILED') });
await capture({ screen: 'result', route: '/qualifications', state: 'blocked' });
await capture({ screen: 'run-detail', route: '/demo', state: 'ready' });
await capture({ screen: 'evidence', route: '/qualifications', state: 'ledger' });
await capture({ screen: 'logs', route: '/', state: 'empty' });
await capture({ screen: 'settings', route: '/', state: 'connection' });
await capture({ screen: 'modal', route: '/demo', state: 'generated-data', prepare: async page => page.evaluate(() => { document.querySelector('#generatedDataBody').innerHTML='<div class="generated-list"><div class="generated-item"><span>SCREENSHOT · 1</span><code>dashboard-pass.png · 2026-08-29 10:30</code></div><div class="generated-item"><span>LOG · SHA256</span><code>run.log · local-audit</code></div></div>'; document.querySelector('#generatedDataDialog').showModal(); }) });
await capture({ screen: 'dashboard', route: '/', state: 'mobile', viewport: { width: 390, height: 844 } });
await capture({ screen: 'demo', route: '/demo', state: 'mobile', viewport: { width: 360, height: 800 } });

await fs.writeFile(path.join(output, 'manifest.json'), JSON.stringify({ generated_at: new Date().toISOString(), source: baseURL, screens: manifest, layout_failures: layoutFailures }, null, 2) + '\n');
await browser.close();
if (layoutFailures.length) {
  console.error(JSON.stringify(layoutFailures, null, 2));
  process.exitCode = 1;
} else {
  console.log(`UI audit PASS: ${manifest.length} screenshots, no horizontal overflow or clipped active controls.`);
}
