const $=id=>document.getElementById(id);
function esc(v){return String(v??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]))}
function toast(msg,type='error'){const e=$('toast');e.textContent=msg;e.className='toast '+type;e.hidden=false;clearTimeout(toast.t);toast.t=setTimeout(()=>e.hidden=true,5500)}
async function req(url,opt={}){let r;try{r=await fetch(url,opt)}catch(e){throw new Error('Không gọi được QA Center: '+e.message)}const text=await r.text();let b={};try{b=text?JSON.parse(text):{}}catch(e){throw new Error(`API trả dữ liệu không hợp lệ HTTP ${r.status}`)}if(!r.ok||b.ok===false)throw new Error(b.message||b.error||`HTTP ${r.status}`);return b}
const post=(url,data={})=>req(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});

// "dashboard.production_overview" -> "Dashboard Production Overview". Purely
// a display nicety over the real registry key (which stays the identifier
// everywhere else -- API params, source-path mapping, bug.feature, etc).
function friendlyName(key){
  return String(key||'').split(/[._-]/).filter(Boolean).map(w=>w[0].toUpperCase()+w.slice(1)).join(' ');
}

let lastFeatures=[];

async function loadFeatures(){
  try{
    const j=await req('/api/regression/features');
    lastFeatures=j.features||[];
    const rows=lastFeatures;
    $('featureBody').innerHTML=rows.length?rows.map(f=>`<tr>
      <td><b>${esc(friendlyName(f.key))}</b><div class="muted" style="font-size:11px">${esc(f.key)}</div></td>
      <td><span class="pill ${esc(f.status)}">${esc(f.status)}</span></td>
      <td>${esc(f.consecutive_passes)}/${esc(f.min_consecutive_passes)}</td>
      <td>${f.coverage_gap?`<span class="pill BROKEN">GAP</span>`:`<span class="pill STABLE">OK</span>`}</td>
      <td>${f.needs_regression?`<span class="pill CHANGED">CẦN CHẠY</span>`:'-'}</td>
      <td>${esc(f.last_pass_at||'-')}</td>
      <td>${esc((f.regression_cases||[]).join(', ')||'-')}</td>
    </tr>`).join(''):'<tr><td colspan="7" class="empty">Chưa có feature nào được đăng ký.</td></tr>';
  }catch(e){$('featureBody').innerHTML=`<tr><td colspan="7" class="empty">${esc(e.message)}</td></tr>`}
}

async function computePlan(){
  const btn=$('computePlan'),old=btn.textContent;btn.disabled=true;btn.textContent='Đang tính…';
  try{
    const mode=$('planMode').value;
    const j=await req('/api/regression/plan?mode='+encodeURIComponent(mode));
    $('planSummary').textContent=`Chạy: ${j.run_count} · Bỏ qua (stable): ${j.skip_count}`;
    $('planDecisions').innerHTML=(j.decisions||[]).map(d=>`<div class="decision-row"><b>${esc(d.feature)}</b><span class="pill ${d.action==='RUN'?'CHANGED':'STABLE'}">${esc(d.action)}</span><span class="reason">${esc(d.reason)}</span></div>`).join('')||'<div class="empty">Không có feature nào.</div>';
    $('mandatoryTests').innerHTML=(j.mandatory_regression_test_cases||[]).length?`<p class="muted">Test case bắt buộc: ${j.mandatory_regression_test_cases.map(t=>`<code class="inline">${esc(t)}</code>`).join(' ')}</p>`:'';
  }catch(e){toast(e.message,'error')}finally{btn.disabled=false;btn.textContent=old}
}

async function computeImpact(){
  const btn=$('computeImpact'),old=btn.textContent;
  const previous_commit=$('prevCommit').value.trim();
  if(!previous_commit){toast('Cần nhập previous commit','error');return}
  btn.disabled=true;btn.textContent='Đang tính…';
  try{
    const j=await post('/api/regression/impact',{previous_commit,current_commit:$('currCommit').value.trim()||'HEAD'});
    const detail=j.impacted_detail||[];
    const rows=detail.map(d=>`<div class="decision-row">
        <b>${esc(friendlyName(d.key))}</b>
        <span class="pill CHANGED">NEEDS REGRESSION</span>
        <span class="reason">${d.regression_cases.length?`Test: ${d.regression_cases.map(esc).join(', ')}`:'Chưa có regression test case nào cho feature này'}${d.coverage_gap?' · <b>COVERAGE GAP</b> — cần testcase mới trước khi quay lại STABLE':''}</span>
      </div>`).join('');
    $('impactResult').innerHTML=`
      <div class="section-title simple" style="margin:0 0 10px"><div><h2 style="font-size:15px">Regression Impact</h2><p>${detail.length} feature${detail.length===1?'':'s'} affected by this change</p></div></div>
      ${rows||'<div class="empty">Không có feature STABLE nào bị ảnh hưởng bởi thay đổi này.</div>'}
      <p style="margin-top:10px"><b>${(j.changed_files||[]).length}</b> file đã đổi giữa ${esc(j.previous_commit)}..${esc(j.current_commit)}</p>
      <details><summary class="muted">Xem danh sách file đã đổi</summary><pre style="height:auto;max-height:260px">${esc((j.changed_files||[]).join('\n')||'(rỗng)')}</pre></details>`;
    await loadFeatures();
  }catch(e){toast(e.message,'error')}finally{btn.disabled=false;btn.textContent=old}
}

$('refreshFeatures').onclick=loadFeatures;
$('computePlan').onclick=computePlan;
$('computeImpact').onclick=computeImpact;
loadFeatures();
