export const timingFor = mode => mode === 'guide'
  ? { key: 55, before: 450, after: 700, page: 1400, important: 2600, chapter: 1900 }
  : { key: 5, before: 50, after: 100, page: 250, important: 400, chapter: 250 };

export async function installOverlays(page) {
  await page.evaluate(() => {
    if (document.getElementById('mesflow-demo-style')) return;
    const style = document.createElement('style'); style.id = 'mesflow-demo-style';
    style.textContent = `
      #demo-cursor{position:fixed;z-index:2147483647;width:24px;height:24px;border:3px solid #fff;background:#1264e8aa;border-radius:50%;pointer-events:none;transform:translate(-50%,-50%);box-shadow:0 0 0 2px #1264e8;transition:left .08s linear,top .08s linear}
      .demo-ripple{position:fixed;z-index:2147483646;width:20px;height:20px;border:4px solid #ffca28;border-radius:50%;pointer-events:none;transform:translate(-50%,-50%);animation:demoRipple .55s ease-out forwards}@keyframes demoRipple{to{width:70px;height:70px;opacity:0}}
      #demo-caption{position:fixed;z-index:2147483645;left:50%;bottom:22px;transform:translateX(-50%);background:#0b172acc;color:#fff;padding:12px 24px;border-radius:8px;font:600 22px system-ui;max-width:75vw;text-align:center;pointer-events:none;opacity:0;transition:opacity .2s}
      #demo-chapter{position:fixed;z-index:2147483644;inset:0;background:#081426ed;color:#fff;display:grid;place-content:center;text-align:center;font-family:system-ui;pointer-events:none;opacity:0;transition:opacity .3s}#demo-chapter b{font-size:24px;letter-spacing:5px;color:#67a8ff}#demo-chapter strong{font-size:72px;margin:10px}#demo-chapter span{font-size:34px;font-weight:700;max-width:900px}
      .demo-highlight{outline:5px solid #ffca28!important;outline-offset:5px!important;box-shadow:0 0 0 10px #ffca2833!important}
    `;
    document.head.append(style);
    for (const [id] of [['demo-cursor'],['demo-caption'],['demo-chapter']]) { const x=document.createElement('div');x.id=id;document.body.append(x); }
    document.getElementById('demo-chapter').innerHTML='<b>MESFLOW</b><strong></strong><span></span>';
    addEventListener('mousemove',e=>{const c=document.getElementById('demo-cursor');if(c){c.style.left=e.clientX+'px';c.style.top=e.clientY+'px'}});
    addEventListener('click',e=>{const r=document.createElement('i');r.className='demo-ripple';r.style.left=e.clientX+'px';r.style.top=e.clientY+'px';document.body.append(r);setTimeout(()=>r.remove(),650)},true);
  });
}

export async function chapter(page, number, title, ms) {
  await installOverlays(page);
  await page.evaluate(({number,title})=>{const x=document.getElementById('demo-chapter');x.querySelector('strong').textContent=String(number).padStart(2,'0');x.querySelector('span').textContent=title;x.style.opacity='1'}, {number,title});
  await page.waitForTimeout(ms); await page.evaluate(()=>document.getElementById('demo-chapter').style.opacity='0'); await page.waitForTimeout(300);
}

export async function caption(page, text, visible=true) {
  await installOverlays(page); await page.evaluate(({text,visible})=>{const x=document.getElementById('demo-caption');x.textContent=text;x.style.opacity=visible?'1':'0'}, {text,visible});
}

export async function humanClick(page, locator, text, timing) {
  await locator.waitFor({state:'visible'}); await locator.scrollIntoViewIfNeeded(); await caption(page,text,true);
  await locator.evaluate(x=>x.classList.add('demo-highlight')); const b=await locator.boundingBox();
  if(b) await page.mouse.move(b.x+b.width/2,b.y+b.height/2,{steps:12}); await page.waitForTimeout(timing.before); await locator.click();
  await page.waitForTimeout(timing.after); await caption(page,'',false).catch(()=>{});
}

export async function humanType(page, locator, value, text, timing) {
  await humanClick(page,locator,text,timing); await locator.fill(''); await locator.pressSequentially(String(value),{delay:timing.key});
}

export async function checkpoint(page, dir, name) { await page.screenshot({path:`${dir}/${name}.png`,fullPage:false}); }

export async function api(page, path, options={}) {
  return page.evaluate(async ({path,options})=>{const r=await fetch(path,{headers:{'Content-Type':'application/json'},...options,body:options.body?JSON.stringify(options.body):undefined});const d=await r.json().catch(()=>({}));if(!r.ok||d.ok===false)throw new Error(`${path}: ${d.message||d.error||r.status}`);return d}, {path,options});
}
