const $=id=>document.getElementById(id);
function esc(v){return String(v??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]))}
function toast(msg,type='error'){const e=$('toast');e.textContent=msg;e.className='toast '+type;e.hidden=false;clearTimeout(toast.t);toast.t=setTimeout(()=>e.hidden=true,5500)}
async function req(url,opt={}){let r;try{r=await fetch(url,opt)}catch(e){throw new Error('Không gọi được QA Center: '+e.message)}const text=await r.text();let b={};try{b=text?JSON.parse(text):{}}catch(e){throw new Error(`API trả dữ liệu không hợp lệ HTTP ${r.status}`)}if(!r.ok||b.ok===false)throw new Error(b.message||b.error||`HTTP ${r.status}`);return b}
const post=(url,data={})=>req(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});

const STATUS_ORDER=['OPEN','FIXING','READY_FOR_VERIFY','RESOLVED','REGRESSION','IGNORED'];

async function loadSummary(){
  try{
    const j=await req('/api/bugs/summary');
    const counts=j.counts||{};
    $('summaryPills').innerHTML=STATUS_ORDER.map(s=>`<span class="pill ${s}">${s}: ${counts[s]||0}</span>`).join('');
  }catch(e){$('summaryPills').innerHTML=`<span class="pill BROKEN">${esc(e.message)}</span>`}
}

function bugActions(bug){
  const actions=[];
  if(bug.status==='OPEN'||bug.status==='REGRESSION'){
    actions.push(`<button data-fixing="${bug.bug_id}">Mark Fixing</button>`);
  }
  if(bug.status==='FIXING'){
    actions.push(`<button data-ready="${bug.bug_id}">Ready for Verify</button>`);
  }
  if(bug.status==='READY_FOR_VERIFY'){
    actions.push(`<input type="text" placeholder="regression test case (bắt buộc để PASS)" data-tc="${bug.bug_id}" style="min-width:220px">`);
    actions.push(`<button data-verify-pass="${bug.bug_id}">Verify PASS</button>`);
    actions.push(`<button class="secondary" data-verify-fail="${bug.bug_id}">Verify FAIL</button>`);
  }
  if(!['RESOLVED','IGNORED'].includes(bug.status)){
    actions.push(`<button class="secondary" data-ignore="${bug.bug_id}">Ignore</button>`);
  }
  return actions.join('');
}

function bugCard(bug){
  const history=(bug.evidence&&bug.evidence.history)||[];
  const latest=history.length?history[history.length-1]:{};
  return `<article class="card bug-card">
    <div class="bug-head">
      <div><span class="bug-id">${esc(bug.bug_id)}</span><h3>${esc(bug.title)}</h3></div>
      <div><span class="pill ${esc(bug.severity)}" style="margin-right:6px">${esc(bug.severity)}</span><span class="pill ${esc(bug.status)}">${esc(bug.status)}</span></div>
    </div>
    <div class="bug-body">
      <div class="box"><span>Feature</span>${esc(bug.feature||'-')}</div>
      <div class="box"><span>Loại lỗi</span>${esc(bug.type||'-')}</div>
      <div class="box"><span>Expected</span>${esc(bug.expected||'-')}</div>
      <div class="box"><span>Actual</span>${esc(bug.actual||'-')}</div>
      <div class="box"><span>URL / API</span>${esc(bug.url||bug.api||'-')}</div>
      <div class="box"><span>Occurrences</span>${esc(bug.occurrences)} · first ${esc(bug.first_seen)} · last ${esc(bug.last_seen)}</div>
      <div class="box"><span>MESFlow version</span>${esc(latest.mesflow_version||'-')}</div>
      <div class="box"><span>Commit</span>${esc(latest.commit_sha||'-')}</div>
      <div class="box"><span>Preview preset</span>${esc(latest.scenario||'-')}</div>
      <div class="box"><span>Run</span>${esc(bug.run_id||'-')}</div>
      <div class="box"><span>Regression testcase</span>${esc(bug.regression_test_case||'-')}</div>
    </div>
    ${latest.console_log&&latest.console_log.length?`<div class="box" style="margin-top:8px"><span>Browser console errors</span><pre style="height:auto;max-height:140px">${esc(latest.console_log.join('\n'))}</pre></div>`:''}
    ${latest.pageerror?`<div class="box" style="margin-top:8px"><span>Page error</span><pre style="height:auto;max-height:120px">${esc(latest.pageerror)}</pre></div>`:''}
    ${latest.backend_log_excerpt?`<div class="box" style="margin-top:8px"><span>Backend log</span><pre style="height:auto;max-height:160px">${esc(latest.backend_log_excerpt)}</pre></div>`:''}
    ${latest.screenshot_path?`<div class="box" style="margin-top:8px"><span>Screenshot</span><img src="${esc(latest.screenshot_path)}" style="max-width:100%;border-radius:8px;border:1px solid #dfe6f1" alt="screenshot"></div>`:''}
    ${history.length?`<details style="margin-top:8px"><summary class="muted">Evidence history đầy đủ (${history.length})</summary><pre style="height:auto;max-height:220px">${esc(JSON.stringify(history,null,2))}</pre></details>`:''}
    <div class="row-actions" style="margin-top:10px">${bugActions(bug)}</div>
  </article>`;
}

function bindBugActions(){
  document.querySelectorAll('[data-fixing]').forEach(b=>b.onclick=()=>act(b,post(`/api/bugs/${b.dataset.fixing}/fixing`)));
  document.querySelectorAll('[data-ready]').forEach(b=>b.onclick=()=>act(b,post(`/api/bugs/${b.dataset.ready}/ready-for-verify`)));
  document.querySelectorAll('[data-verify-pass]').forEach(b=>b.onclick=()=>{
    const id=b.dataset.verifyPass;
    const tc=document.querySelector(`[data-tc="${id}"]`);
    const test_case=tc?tc.value.trim():'';
    if(!test_case){toast('Cần nhập regression test case để verify PASS','error');return}
    act(b,post(`/api/bugs/${id}/verify`,{passed:true,test_case}));
  });
  document.querySelectorAll('[data-verify-fail]').forEach(b=>b.onclick=()=>act(b,post(`/api/bugs/${b.dataset.verifyFail}/verify`,{passed:false})));
  document.querySelectorAll('[data-ignore]').forEach(b=>b.onclick=()=>act(b,post(`/api/bugs/${b.dataset.ignore}/ignore`)));
}

async function act(btn,promise){
  btn.disabled=true;
  try{await promise;toast('Đã cập nhật bug','success')}catch(e){toast(e.message,'error')}finally{await loadBugs();await loadSummary()}
}

function currentFilters(){
  const p=new URLSearchParams();
  const feature=$('fFeature').value.trim(),severity=$('fSeverity').value,status=$('fStatus').value,run=$('fRun').value.trim();
  if(feature)p.set('feature',feature);
  if(severity)p.set('severity',severity);
  if(status)p.set('status',status);
  if(run)p.set('run_id',run);
  return p.toString();
}

async function loadBugs(){
  try{
    const qs=currentFilters();
    const j=await req('/api/bugs'+(qs?'?'+qs:''));
    const bugs=j.bugs||[];
    $('bugList').innerHTML=bugs.length?bugs.map(bugCard).join(''):'<div class="empty">Không có bug nào khớp bộ lọc.</div>';
    bindBugActions();
  }catch(e){$('bugList').innerHTML=`<div class="empty">${esc(e.message)}</div>`}
}

$('applyFilter').onclick=loadBugs;
$('refreshBugs').onclick=()=>{loadBugs();loadSummary()};
loadSummary();
loadBugs();
