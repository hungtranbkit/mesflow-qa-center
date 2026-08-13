import { chromium } from '@playwright/test';

const base = process.env.MESFLOW_BASE_URL || 'https://mesflow.net';
const browser = await chromium.launch({headless:true});
const page = await browser.newPage({viewport:{width:1920,height:1080}});
await page.goto(`${base}/login`,{waitUntil:'domcontentloaded'});
await page.getByLabel(/tên đăng nhập|username/i).fill(process.env.MESFLOW_USERNAME || 'admin');
await page.getByLabel(/mật khẩu|password/i).fill(process.env.MESFLOW_PASSWORD || '');
await page.getByRole('button',{name:/đăng nhập/i}).click();
await page.waitForURL(/\/app/,{timeout:20000});
await page.waitForLoadState('networkidle');
const inventory=await page.locator('.sidebar-item,.sidebar-group-trigger,.sidebar-sub-item').evaluateAll(nodes=>nodes.map(n=>({text:(n.textContent||'').trim().replace(/\s+/g,' '),page:n.getAttribute('data-page'),kind:n.className})));
console.log(JSON.stringify({version:await page.locator('.sidebar-brand-text small').textContent(),title:await page.title(),inventory},null,2));
await browser.close();
