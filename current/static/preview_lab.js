const $=id=>document.getElementById(id);
function esc(v){return String(v??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]))}
function toast(msg,type='error'){const e=$('toast');e.textContent=msg;e.className='toast '+type;e.hidden=false;clearTimeout(toast.t);toast.t=setTimeout(()=>e.hidden=true,5500)}
async function req(url,opt={}){let r;try{r=await fetch(url,opt)}catch(e){throw new Error('Không gọi được QA Center: '+e.message)}const text=await r.text();let b={};try{b=text?JSON.parse(text):{}}catch(e){throw new Error(`API trả dữ liệu không hợp lệ HTTP ${r.status}`)}if(!r.ok||b.ok===false)throw new Error(b.message||b.error||`HTTP ${r.status}`);return b}
const post=(url,data={})=>req(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
const del=url=>req(url,{method:'DELETE'});

let envs=[],presets=[];

// Human-readable copy for each lifecycle phase (requirement 1: no raw
// Docker/implementation language on the main screen).
const PHASE_COPY={
  CREATING:{label:'CREATING',title:'Đang tạo môi trường Preview',body:'Đang tạo database và container riêng...'},
  STARTING:{label:'STARTING',title:'Đang khởi động lại',body:'Đang khởi động lại container Preview...'},
  SEEDING:{label:'SEEDING',title:'Đang tạo dữ liệu',body:'Đang tạo dữ liệu Preview...'},
};

async function loadPresets(){
  try{
    const j=await req('/api/preview/presets');
    presets=j.presets||[];
  }catch(e){presets=[]}
}

function activeEnv(){
  // Single-active-preview UX (requirement 3): once one exists, Start
  // Preview is hidden until it's deleted -- never show two at once.
  return envs.find(e=>e.status!=='DELETED')||null;
}

function renderStartForm(){
  const options=presets.map(p=>`<option value="${esc(p.key)}" title="${esc(p.description)}">${esc(p.key)}</option>`).join('');
  $('envArea').innerHTML=`
    <div class="empty-preview">
      <h3>Chưa có môi trường Preview</h3>
      <p>Chọn MESFlow image + Dataset rồi bấm Start Preview.</p>
      <div class="start-preview-form">
        <label><span>MESFlow image (bỏ trống = dùng đúng bản mesflow-app đang chạy)</span><input id="cloneImage" placeholder="ví dụ: mesflow-app:71.0.0.52"></label>
        <label><span>Dataset</span><select id="cloneDataset">${options}</select></label>
        <button id="startPreviewBtn">Start Preview</button>
      </div>
    </div>`;
  $('startPreviewBtn').onclick=startPreview;
}

async function startPreview(){
  const btn=$('startPreviewBtn'),old=btn.textContent;
  const preset=$('cloneDataset').value;
  const image=$('cloneImage').value.trim();
  if(!preset){toast('Chưa có dataset để chọn','error');return}
  btn.disabled=true;btn.textContent='Đang tạo…';
  try{
    await post('/api/preview/environments',image?{preset,image}:{preset});
    toast('Đã bắt đầu tạo môi trường Preview','success');
    await loadEnvs();
  }catch(e){toast(e.message,'error')}finally{btn.disabled=false;btn.textContent=old}
}

function statusRows(env){
  const rows=[
    ['Trạng thái', `<span class="pill ${esc(env.status)}">${esc(env.status)}</span>`,true],
    ['Dataset', esc(env.preset)],
    ['MESFlow', esc(env.mesflow_version||'-')],
    ['Backend Port', esc(env.port)],
    ['Database', esc(env.db_name)],
    ['App', esc(env.app_container)],
    ['DB', esc(env.db_container)],
    ['Created', esc(env.created_at)],
    ['Seed version', esc(env.seed_version||'-')],
  ];
  return rows.map(([label,value,raw])=>`<div><span>${label}</span><b>${raw?value:value}</b></div>`).join('');
}

function runtimeWarning(env){
  const rt=env.runtime||{};
  if(env.status==='READY'&&rt.app_status&&rt.app_status!=='running'){
    return `<div class="env-error">⚠ Container thực tế đang "${esc(rt.app_status)}", khác với trạng thái đã lưu. Bấm Làm mới để cập nhật, hoặc Stop/Start lại nếu cần.</div>`;
  }
  if(env.status==='READY'&&rt.health===false){
    return `<div class="env-error">⚠ Container đang chạy nhưng health check chưa trả lời OK.</div>`;
  }
  return '';
}

function technicalDetails(env){
  const dbHost=`${env.db_container}:5432/${env.db_name}`;
  const health=`http://${env.app_container}:8080/api/system/ready`;
  const rows=[
    ['Docker labels','com.mesflow.qa.preview=1'],
    ['Network',env.network],
    ['App container',env.app_container],
    ['DB container',env.db_container],
    ['Image',env.image],
    ['Database hostname',dbHost],
    ['Health endpoint',health],
  ];
  return `<details class="tech-details"><summary>Chi tiết kỹ thuật</summary><div class="status-rows">${
    rows.map(([label,value])=>`<div><span>${esc(label)}</span><b>${esc(value)}</b></div>`).join('')
  }</div></details>`;
}

function actionsFor(env){
  const out=[];
  if(env.status==='READY'){
    out.push(`<a class="btn-link" target="_blank" rel="noopener" href="${esc(env.base_url)}">Open Preview →</a>`);
    out.push(`<button data-coverage="${env.id}">Run UI Coverage</button>`);
    out.push(`<select id="resetPreset-${env.id}" title="Dataset để reseed vào (đổi testcase mà không tạo môi trường mới)">${
      presets.map(p=>`<option value="${esc(p.key)}" ${p.key===env.preset?'selected':''}>${esc(p.key)}</option>`).join('')
    }</select>`);
    out.push(`<button class="secondary" data-reset="${env.id}">Reset / Re-seed</button>`);
    out.push(`<button class="secondary" data-stop="${env.id}">Stop</button>`);
    out.push(`<button class="danger" data-delete="${env.id}">Delete Environment</button>`);
  }else if(env.status==='STOPPED'){
    out.push(`<button data-start="${env.id}">Start lại</button>`);
    out.push(`<button class="danger" data-delete="${env.id}">Delete Environment</button>`);
  }else if(env.status==='FAILED'){
    out.push(`<button class="secondary" data-view-error="${env.id}">Xem lỗi</button>`);
    out.push(`<button class="danger" data-delete="${env.id}">Delete Environment</button>`);
  }
  // CREATING/STARTING/SEEDING: no destructive actions at all -- only the
  // page-level "Làm mới" button (requirement 3).
  return out.join('');
}

function renderEnv(env){
  if(['CREATING','STARTING','SEEDING'].includes(env.status)){
    const copy=PHASE_COPY[env.status];
    return `<div class="card phase-panel"><div class="spinner"></div><div><h3>${copy.title}</h3><p>${copy.body}</p></div></div>${technicalDetails(env)}`;
  }
  if(env.status==='FAILED'){
    return `<div class="card phase-panel error"><div><h3>ERROR</h3><p>Không thể tạo môi trường Preview.</p></div></div>
      <div class="row-actions" style="margin-top:12px">${actionsFor(env)}</div>${technicalDetails(env)}`;
  }
  return `<div class="card status-card">
    <div class="status-rows">${statusRows(env)}</div>
    ${runtimeWarning(env)}
    <div class="row-actions">${actionsFor(env)}</div>
    ${technicalDetails(env)}
  </div>`;
}

function bindEnvActions(env){
  const q=sel=>document.querySelector(sel);
  if(q(`[data-start="${env.id}"]`)) q(`[data-start="${env.id}"]`).onclick=b=>runAction(b.target,post(`/api/preview/environments/${env.id}/start`),'Đã bắt đầu start lại');
  if(q(`[data-stop="${env.id}"]`)) q(`[data-stop="${env.id}"]`).onclick=b=>runAction(b.target,post(`/api/preview/environments/${env.id}/stop`),'Đã stop');
  if(q(`[data-reset="${env.id}"]`)) q(`[data-reset="${env.id}"]`).onclick=b=>{
    const sel=q(`#resetPreset-${env.id}`),preset=sel?sel.value:env.preset;
    runAction(b.target,post(`/api/preview/environments/${env.id}/reset`,{preset}),
      preset===env.preset?'Đang reset/reseed':`Đang chuyển sang testcase ${preset}`);
  };
  if(q(`[data-coverage="${env.id}"]`)) q(`[data-coverage="${env.id}"]`).onclick=b=>runAction(b.target,post(`/api/preview/environments/${env.id}/coverage`),'Coverage run đã bắt đầu');
  if(q(`[data-view-error="${env.id}"]`)) q(`[data-view-error="${env.id}"]`).onclick=()=>{$('errorDialogBody').textContent=env.last_error||'(không có chi tiết lỗi)';$('errorDialog').showModal()};
  if(q(`[data-delete="${env.id}"]`)) q(`[data-delete="${env.id}"]`).onclick=b=>{
    if(!confirm('Xoá môi trường Preview này? Toàn bộ app/DB/network/volume riêng của nó sẽ bị xoá vĩnh viễn.'))return;
    runAction(b.target,del(`/api/preview/environments/${env.id}`),'Đã xoá môi trường Preview');
  };
}

async function runAction(btn,promise,okMsg){
  btn.disabled=true;
  try{await promise;toast(okMsg,'success')}catch(e){toast(e.message,'error')}finally{await loadEnvs();await loadCoverage()}
}

async function loadEnvs(){
  try{
    const j=await req('/api/preview/environments');
    envs=j.environments||[];
    const env=activeEnv();
    if(!env){
      renderStartForm();
    }else{
      $('envArea').innerHTML=renderEnv(env);
      bindEnvActions(env);
    }
  }catch(e){$('envArea').innerHTML=`<div class="empty">${esc(e.message)}</div>`}
}

async function loadCoverage(){
  try{
    const j=await req('/api/preview/coverage');
    const runs=j.runs||[];
    $('coverageBody').innerHTML=runs.length?runs.map(r=>`<tr><td>${esc(r.run_id)}</td><td>${esc(r.preview_id)}</td><td><span class="pill ${esc(r.status)}">${esc(r.status)}</span></td><td>${esc(r.bug_count)}</td><td>${esc(r.started_at)}</td><td>${esc(r.finished_at||'-')}</td></tr>`).join(''):'<tr><td colspan="6" class="empty">Chưa có coverage run.</td></tr>';
  }catch(e){$('coverageBody').innerHTML=`<tr><td colspan="6" class="empty">${esc(e.message)}</td></tr>`}
}

$('refreshEnvs').onclick=async()=>{
  // Requirement 2: this button (and this button alone triggering it on
  // demand) must ONLY read status/health/port -- loadEnvs()/loadCoverage()
  // never call anything but GET routes.
  const btn=$('refreshEnvs'),old=btn.textContent;btn.disabled=true;btn.textContent='Đang làm mới…';
  try{await loadEnvs();await loadCoverage()}finally{btn.disabled=false;btn.textContent=old}
};

async function pollLoop(){
  await loadEnvs();
  await loadCoverage();
  const env=activeEnv();
  const busy=env&&['CREATING','STARTING','SEEDING'].includes(env.status);
  setTimeout(pollLoop,busy?2500:8000);
}

(async()=>{
  await loadPresets();
  pollLoop();
})();
