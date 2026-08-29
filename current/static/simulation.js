const $=id=>document.getElementById(id);
function esc(v){return String(v??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]))}
function toast(msg,type='error'){const e=$('toast');e.textContent=msg;e.className='toast '+type;e.hidden=false;clearTimeout(toast.t);toast.t=setTimeout(()=>e.hidden=true,5500)}
async function req(url,opt={}){let r;try{r=await fetch(url,opt)}catch(e){throw new Error('Không gọi được QA Center: '+e.message)}const text=await r.text();let b={};try{b=text?JSON.parse(text):{}}catch(e){throw new Error(`API trả dữ liệu không hợp lệ HTTP ${r.status}`)}if(!r.ok||b.ok===false)throw new Error(b.message||b.error||`HTTP ${r.status}`);return b}
const post=(url,data={})=>req(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});

let pollTimer=null;

async function loadEnvironments(){
  try{
    const j=await req('/api/preview/environments');
    const envs=(j.environments||[]).filter(e=>e.status==='READY');
    const sel=$('previewSelect');
    sel.innerHTML=envs.length
      ? envs.map(e=>`<option value="${e.id}">${esc(e.id)} · ${esc(e.preset)} · port ${e.port}</option>`).join('')
      : '<option value="">Không có môi trường READY -- mở tab UI Preview Lab và tạo một môi trường trước</option>';
  }catch(e){
    $('previewSelect').innerHTML='<option value="">Lỗi tải danh sách môi trường</option>';
  }
}

function fmtMetrics(m){
  if(!m) return '';
  return `<table class="ops-table"><tbody>
    <tr><td>Business events</td><td>${m.business_events??0}</td></tr>
    <tr><td>Sessions started / finished</td><td>${m.sessions_started??0} / ${m.sessions_finished??0}</td></tr>
    <tr><td>GOOD / DEFECT / REWORK</td><td>${m.good_qty??0} / ${m.defect_qty??0} / ${m.rework_qty??0}</td></tr>
    <tr><td>Heartbeats</td><td>${m.heartbeats??0}</td></tr>
    <tr><td>Web reads</td><td>${m.web_reads??0}</td></tr>
    <tr><td>Errors</td><td>${m.errors??0}</td></tr>
  </tbody></table>`;
}

async function refreshStatus(){
  try{
    const j=await req('/api/simulation/status');
    const run=j.run;
    if(!run){
      $('statusBody').innerHTML='<div class="empty">No active run.</div>';
      $('stopBtn').disabled=true;
      return;
    }
    $('stopBtn').disabled=(run.status!=='RUNNING');
    $('statusBody').innerHTML=`
      <p><b>Run ${esc(run.run_id)}</b> — status <b>${esc(run.status)}</b>${run.stop_reason?` (${esc(run.stop_reason)})`:''}</p>
      <p class="muted">Employees ${run.employees} · Web users ${run.web_users} · Kiosks ${run.kiosks} · Scheduled actions pending ${run.scheduled}</p>
      ${fmtMetrics(run.metrics)}
    `;
  }catch(e){
    // Transient poll failure -- do not spam a toast every 5s.
  }
}

async function refreshIncidents(){
  try{
    const j=await req('/api/simulation/incidents');
    const rows=j.incidents||[];
    $('incidentBody').innerHTML=rows.length
      ? rows.map(b=>`<tr><td>${esc(b.bug_id)}</td><td>${esc(b.severity)}</td><td>${esc(b.type)}</td><td>${esc(b.title)}</td><td>${esc(b.status)}</td><td>${b.occurrences}</td><td>${esc(b.last_seen)}</td></tr>`).join('')
      : '<tr><td colspan="7" class="empty">No incidents yet.</td></tr>';
  }catch(e){/* transient */}
}

function startPolling(){
  if(pollTimer) return;
  pollTimer=setInterval(()=>{refreshStatus();refreshIncidents()},5000);
}

$('startBtn').onclick=async()=>{
  const preview_id=$('previewSelect').value;
  if(!preview_id){toast('Chọn một môi trường Preview Lab đang READY trước.');return}
  $('startBtn').disabled=true;
  $('startMsg').textContent='Đang bootstrap factory (employees/templates/PO qua API thật)…';
  try{
    await post('/api/simulation/start',{
      preview_id, profile:$('profileSelect').value, duration:$('durationSelect').value, speed:$('speedSelect').value,
    });
    $('startMsg').textContent='Run đã bắt đầu.';
    toast('Simulation started','ok');
    startPolling();
    refreshStatus();refreshIncidents();
  }catch(e){
    toast(e.message);
    $('startMsg').textContent='';
  }finally{
    $('startBtn').disabled=false;
  }
};

$('stopBtn').onclick=async()=>{
  try{
    await post('/api/simulation/stop',{reason:'manual stop from QA Center UI'});
    toast('Stop requested','ok');
    refreshStatus();
  }catch(e){toast(e.message)}
};

loadEnvironments();
refreshStatus();
refreshIncidents();
startPolling();
