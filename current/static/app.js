let selectedRun=null;
let logPaused=false;
let lastLogText="";
let refreshBusy=false;
const $=id=>document.getElementById(id);
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const esc=value=>String(value??'').replace(/[&<>\"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[ch]));
function showToast(message,type='error'){const el=$('toast');el.textContent=message;el.className='toast '+type;el.hidden=false;clearTimeout(showToast.timer);showToast.timer=setTimeout(()=>el.hidden=true,6000)}
function setConnection(ok,text){$('connectionStatus').textContent=text||'';$('connectionBadge').textContent=ok?'Đã kết nối':'Kết nối lỗi';$('connectionBadge').className='pill '+(ok?'ok':'bad')}
function configPayload(){const internal=$('internal_base_url').value.trim()||'http://mesflow-app:8080';return{base_url:internal,internal_base_url:internal,username:$('username').value.trim(),password:$('password').value,database_url:$('database_url')?$('database_url').value.trim():'',verify_ssl:$('verify_ssl').checked}}
async function requestJson(url,options={}){let response;try{response=await fetch(url,options)}catch(e){throw new Error('Không gọi được QA Agent: '+e.message)}let body;const text=await response.text();try{body=text?JSON.parse(text):{}}catch(e){throw new Error(`API ${url} trả dữ liệu không hợp lệ (HTTP ${response.status}): ${text.slice(0,300)}`)}if(!response.ok||body.ok===false)throw new Error(body.error||body.message||`HTTP ${response.status}`);return body}
async function post(url,data={}){return requestJson(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})}
async function withButton(button,fn){const old=button.textContent;button.disabled=true;button.textContent='Đang xử lý…';try{return await fn()}catch(e){showToast(e.message,'error');$('logState').textContent='Lỗi: '+e.message;throw e}finally{button.disabled=false;button.textContent=old}}
$('saveConfig').onclick=e=>withButton(e.currentTarget,async()=>{await post('/api/config',configPayload());showToast('Đã lưu cấu hình','success')}).catch(()=>{});
$('checkConnection').onclick=e=>withButton(e.currentTarget,async()=>{await post('/api/config',configPayload());const j=await post('/api/check-connection');setConnection(true,`HTTP ${j.status} · ${j.url}`);showToast('Kết nối MESFlow thành công','success')}).catch(err=>setConnection(false,err.message));
document.querySelectorAll('[data-test]').forEach(button=>button.onclick=()=>withButton(button,async()=>{await post('/api/config',configPayload());const t=button.dataset.test;const data={test_type:t};if(t==='api_soak')Object.assign(data,{workers:+$('apiWorkers').value,duration_minutes:+$('apiMinutes').value});if(t==='browser_visual')Object.assign(data,{loops:+$('browserLoops').value,headless:$('headless').checked});if(t==='behavioral')Object.assign(data,{mode:$('behavioralMode').value,seed:+$('behavioralSeed').value,count:+$('behavioralCount').value});if(t==='factory_simulation')Object.assign(data,{workers:+$('factoryWorkers').value,target_active_pos:+$('factoryActivePos').value,planned_quantity:+$('factoryPlannedQty').value,use_public_url:false});if(t==='realtime_soak')Object.assign(data,{workers:+$('realtimeWorkers').value,target_active_pos:+$('realtimeActivePos').value,run_days:+$('realtimeRunDays').value,planned_quantity_min:+$('realtimePlanMin').value,planned_quantity_max:+$('realtimePlanMax').value,session_target_minutes_min:+$('realtimeTargetMin').value,session_target_minutes_max:+$('realtimeTargetMax').value,forgot_finish_rate_percent:+$('realtimeForgotRate').value,normal_variance_percent:+$('realtimeVariance').value,anomaly_rate_percent:+$('realtimeAnomalyRate').value,anomaly_multiplier_min:+$('realtimeAnomalyMin').value,anomaly_multiplier_max:+$('realtimeAnomalyMax').value,fallback_cycle_seconds:+$('realtimeFallbackCycle').value,report_interval_minutes:+$('realtimeReportMinutes').value,tick_seconds:+$('realtimeTickSeconds').value,seed:+$('realtimeSeed').value,use_public_url:false});const j=await post('/api/start',data);selectedRun=j.run.run_id;$('selectedRun').textContent=`${j.run.test_type} · ${selectedRun}`;$('logs').textContent='Đã gửi lệnh chạy. Đang chờ log đầu tiên…';$('logState').textContent='Đang khởi động';showToast('Đã bắt đầu '+j.run.test_type,'success');await sleep(300);await refresh();await loadLogs(true)}).catch(()=>{}));
$('refresh').onclick=()=>refresh(true);
$('pauseLogs').onclick=()=>{logPaused=!logPaused;$('pauseLogs').textContent=logPaused?'Tiếp tục':'Tạm dừng';$('logState').textContent=logPaused?'Đã tạm dừng log':'Đang cập nhật'};
async function copyTextReliable(text,label){
  if(!text){showToast('Không có nội dung để copy','error');return}
  try{
    if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(text)}
    else{
      const ta=document.createElement('textarea');ta.value=text;ta.setAttribute('readonly','');ta.style.position='fixed';ta.style.opacity='0';ta.style.pointerEvents='none';document.body.appendChild(ta);ta.focus();ta.select();
      if(!document.execCommand('copy'))throw new Error('execCommand copy failed');document.body.removeChild(ta)
    }
    showToast(`Đã copy ${label}`,'success')
  }catch(e){
    const range=document.createRange();range.selectNodeContents($('logs'));const sel=window.getSelection();sel.removeAllRanges();sel.addRange(range);showToast('Trình duyệt chặn clipboard. Log đã được chọn, nhấn Ctrl+C.','error')
  }
}
$('copyLogs').onclick=()=>copyTextReliable(lastLogText||$('logs').textContent,'toàn bộ log');
$('copyErrors').onclick=()=>{const source=lastLogText||$('logs').textContent||'';const lines=source.split(/\r?\n/);const picked=lines.filter(x=>/\[(FAIL|WARN)\]|FATAL|HTTP 4\d\d|HTTP 5\d\d|Traceback|AssertionError|Exception|error_code/i.test(x));copyTextReliable((picked.length?picked:lines).join('\n'),'log lỗi')};
async function stopRun(id){try{await post('/api/stop/'+id);await refresh(true)}catch(e){showToast(e.message,'error')}}
async function selectRun(id){selectedRun=id;$('selectedRun').textContent=id;await loadLogs(true)}
async function loadLogs(force=false){if(!selectedRun||(logPaused&&!force))return;try{const box=$('logs');const oldText=lastLogText;const wasNearBottom=(box.scrollHeight-box.scrollTop-box.clientHeight)<48;const oldTop=box.scrollTop;const j=await requestJson('/api/runs/'+encodeURIComponent(selectedRun)+'/logs');const next=(j.lines||[]).join('\n');lastLogText=next;if(next!==oldText){box.textContent=next||'Phiên đã tạo nhưng chưa có log.';if(force||($('autoScroll').checked&&wasNearBottom))box.scrollTop=box.scrollHeight;else box.scrollTop=Math.min(oldTop,Math.max(0,box.scrollHeight-box.clientHeight))}}catch(e){$('logs').textContent='Không tải được log: '+e.message;$('logState').textContent='Lỗi tải log'}}
function fmtNumber(value,suffix='%'){return Number.isFinite(Number(value))?Math.round(Number(value))+suffix:'-'}
function runLabel(type){return({functional:'Functional Smoke',api_soak:'API Soak',browser_visual:'Browser Visual',behavioral:'Behavioral Campaign',factory_simulation:'Factory Lifecycle',realtime_soak:'Mô phỏng nhiều ngày',demo:'Demo Center'})[type]||type}
async function refresh(force=false){if(refreshBusy&&!force)return;refreshBusy=true;try{const j=await requestJson('/api/status');$('agentState').textContent='Agent online';$('agentState').className='pill ok';$('cpu').textContent=fmtNumber(j.system?.cpu);$('ram').textContent=fmtNumber(j.system?.memory_percent);$('running').textContent=(j.runs||[]).filter(x=>x.status==='RUNNING').length;$('errors').textContent=(j.runs||[]).reduce((a,x)=>a+(Number(x.failed)||0),0);const runs=j.runs||[];$('runs').innerHTML=runs.length?runs.map(x=>`<div class="run" data-id="${x.run_id}"><div class="run-main"><div class="run-title">${runLabel(x.test_type)}</div><div class="run-meta"><span>${x.started_at||'-'}</span><span>${x.passed||0} pass</span><span>${x.failed||0} lỗi</span><span>${x.message||''}</span></div></div><div class="run-status"><span class="badge ${x.status}">${x.status}</span>${x.status==='RUNNING'?`<button class="secondary stop" data-stop="${x.run_id}">Dừng</button>`:''}</div></div>`).join(''):'<div class="empty">Chưa có phiên chạy.</div>';document.querySelectorAll('.run').forEach(el=>el.onclick=ev=>{if(!ev.target.dataset.stop)selectRun(el.dataset.id)});document.querySelectorAll('[data-stop]').forEach(el=>el.onclick=ev=>{ev.stopPropagation();stopRun(el.dataset.stop)});await loadLogs()}catch(e){$('agentState').textContent='Agent lỗi';$('agentState').className='pill bad';if(force)showToast(e.message,'error')}finally{refreshBusy=false}}
setInterval(()=>{$('clock').textContent=new Date().toLocaleString('vi-VN');refresh()},2000);$('clock').textContent=new Date().toLocaleString('vi-VN');refresh(true);
window.addEventListener('unhandledrejection',e=>{showToast(e.reason?.message||String(e.reason),'error')});

// --- Release Package Builder --------------------------------------------
// QA Center only BUILDS (this section); Deploy Agent only DEPLOYS. Build
// runs as a server-side background job -- this UI polls the lightweight
// /api/release/build-status endpoint (no logs) only while QUEUED/BUILDING,
// and stops the moment it sees a terminal SUCCESS/FAILED. Full log text is
// fetched once, on demand, via /api/release/build-log (never auto-polled).
let releaseInfo=null;
let releaseBuildTimer=null;
function fmtBytes(n){if(!Number.isFinite(n))return '-';const units=['B','KB','MB','GB'];let i=0,v=n;while(v>=1024&&i<units.length-1){v/=1024;i++}return v.toFixed(i>0?2:0)+' '+units[i]}
function showReleaseResult(version,pkg){
  if(!pkg)return;
  $('releaseResult').hidden=false;
  $('relResVersion').textContent=version||pkg.version||'-';
  $('relResFilename').textContent=pkg.filename||'-';
  $('relResSize').textContent=fmtBytes(pkg.size_bytes);
  $('relResSha256').textContent=pkg.sha256||'-';
  $('relResCommit').textContent=pkg.source_commit||'-';
  $('relResBuiltAt').textContent=pkg.built_at||'-';
  $('relDownloadLink').href='/api/release/download/'+encodeURIComponent(version||pkg.version||'');
}
function renderReleaseJob(job){
  const status=(job&&job.status)||'';
  const btn=$('buildReleaseBtn');
  const building=status==='QUEUED'||status==='BUILDING';
  btn.disabled=building||!(releaseInfo&&releaseInfo.build_available);
  btn.textContent=building?'BUILDING…':'Build Release ZIP';
  $('releaseBuildStatus').textContent=building?(job.message||'Đang build…'):(status==='FAILED'?('Lỗi: '+(job.message||'')):(status==='SUCCESS'?'READY':''));
  if(status==='SUCCESS'&&job.package)showReleaseResult(job.version,job.package);
  return status;
}
function stopReleasePolling(){if(releaseBuildTimer){clearInterval(releaseBuildTimer);releaseBuildTimer=null}}
function startReleasePolling(){
  if(releaseBuildTimer)return;
  releaseBuildTimer=setInterval(async()=>{
    try{
      const j=await requestJson('/api/release/build-status');
      const status=renderReleaseJob(j.job);
      if(status==='SUCCESS'||status==='FAILED'){
        stopReleasePolling();
        showToast(status==='SUCCESS'?'Build release hoàn tất':'Build release thất bại: '+(j.job.message||''),status==='SUCCESS'?'success':'error');
        await loadReleaseInfo();
      }
    }catch(e){stopReleasePolling();showToast('Mất kết nối khi theo dõi build: '+e.message,'error')}
  },2000);
}
async function loadReleaseInfo(){
  try{
    const j=await requestJson('/api/release/info');
    releaseInfo=j;
    $('releaseBuildAvailable').textContent=j.build_available?'Build khả dụng (local/DEV)':'Không khả dụng ở môi trường này';
    $('releaseBuildAvailable').className='pill '+(j.build_available?'ok':'neutral');
    $('relSourceVersion').textContent=j.source.version||'-';
    $('relGitCommit').textContent=j.source.git_commit||'-';
    $('relWorkingTree').textContent=j.source.working_tree||'-';
    $('relLatestRelease').textContent=j.latest_release?j.latest_release.version+(j.current_version_already_released?' (= source hiện tại)':''):'(chưa có release nào)';
    const status=renderReleaseJob(j.build_job);
    if(status==='QUEUED'||status==='BUILDING')startReleasePolling();
    else if(j.current_version_already_released&&j.current_version_package)showReleaseResult(j.source.version,j.current_version_package);
  }catch(e){$('releaseBuildAvailable').textContent='Lỗi tải thông tin release';$('releaseBuildAvailable').className='pill bad';showToast(e.message,'error')}
}
$('buildReleaseBtn').onclick=e=>withButton(e.currentTarget,async()=>{
  const j=await post('/api/release/build');
  showToast('Đã bắt đầu build release','success');
  renderReleaseJob(j.job);
  startReleasePolling();
}).catch(()=>{});
$('relShowLogBtn').onclick=e=>withButton(e.currentTarget,async()=>{
  const j=await requestJson('/api/release/build-log');
  $('relBuildLog').textContent=j.log||'(trống)';
  $('relBuildLog').hidden=false;
}).catch(()=>{});
loadReleaseInfo();

// Legacy prefix cleanup disabled in v1.22.11.

// Demo Center v1.22.1 — Presenter controls + screenshot review
let demoRunId=null,demoTimer=null,demoScreens=[],demoReviewIndex=-1,demoReviewMode=false;
const demoDescriptions={
  'full-production':'Template → PO → Kiosk → Session → Material Flow → Dashboard → Trace/Audit',
  'planning-po':'Tổng quan → Template → Production Order → Gantt & Material Flow',
  'kiosk-realtime':'Quét thẻ → Operation → start/finish → nhập sản lượng → realtime dashboard',
  'quality-rework':'Nhập sản lượng đạt, lỗi và rework rồi xem tác động lên tiến độ',
  'trace-audit':'Production Trace → Business Audit → Session Management → System Logs',
  'feature-tour':'Đi qua toàn bộ các màn hình chính để giới thiệu chức năng MESFlow'
};
if($('demoScenario')) $('demoScenario').onchange=()=>{$('demoScenarioDesc').textContent=demoDescriptions[$('demoScenario').value]||''};
function setDemoControlState(st){const paused=st.status==='PAUSED';$('pauseDemo').disabled=!demoRunId||paused;$('resumeDemo').disabled=!demoRunId||!paused;$('demoLiveHint').textContent=paused?'Demo đang dừng ở checkpoint — có thể trả lời câu hỏi hoặc Review Previous.':'Ảnh được cập nhật tự động từ browser automation';}
function renderDemoState(st){
  $('demoCurrentStep').textContent=st.current_title||st.current_step||'Đang chuẩn bị';
  const rows=st.results||[];$('demoSteps').innerHTML=rows.length?rows.map(x=>`<div class="demo-step-row ${String(x.status||'').toLowerCase()}"><b>${x.status==='PASS'?'✓':x.status==='FAIL'?'✕':'•'}</b><span>${x.title||x.id}</span></div>`).join(''):'<div class="empty">Đang chuẩn bị dữ liệu demo…</div>';
  const status=st.status||'RUNNING';$('demoStatus').textContent=status;$('demoStatus').className='pill '+(status==='PASSED'?'ok':status==='FAILED'?'bad':'neutral');setDemoControlState(st);
}
async function loadDemoScreens(){if(!demoRunId)return;try{const j=await requestJson(`/api/demo/${demoRunId}/screenshots`);demoScreens=j.items||[];$('demoPrev').disabled=demoScreens.length<1;$('demoNextShot').disabled=demoScreens.length<1;if(!demoReviewMode)demoReviewIndex=demoScreens.length-1;}catch(e){}}
function showDemoScreenshot(index){if(!demoScreens.length)return;demoReviewIndex=Math.max(0,Math.min(index,demoScreens.length-1));demoReviewMode=true;const item=demoScreens[demoReviewIndex],img=$('demoLiveImage');img.src=`/api/demo/${demoRunId}/screenshots/${encodeURIComponent(item.name)}?t=${Date.now()}`;img.hidden=false;$('demoLiveEmpty').hidden=true;$('demoViewLabel').textContent='REVIEW MODE';$('demoReviewLabel').textContent=`${demoReviewIndex+1}/${demoScreens.length} · ${item.label}`;$('demoReturnLive').disabled=false;$('demoPrev').disabled=demoReviewIndex<=0;$('demoNextShot').disabled=demoReviewIndex>=demoScreens.length-1;}
function returnDemoLive(){demoReviewMode=false;$('demoViewLabel').textContent='LIVE VIEW';$('demoReviewLabel').textContent='';$('demoReturnLive').disabled=true;$('demoPrev').disabled=demoScreens.length<1;$('demoNextShot').disabled=demoScreens.length<1;}
async function pollDemo(){
  if(!demoRunId)return;
  try{const j=await requestJson(`/api/demo/${demoRunId}/state`);renderDemoState(j.state||{});await loadDemoScreens();if(!demoReviewMode){const img=$('demoLiveImage');img.src=`/api/demo/${demoRunId}/live.png?t=${Date.now()}`;img.onload=()=>{img.hidden=false;$('demoLiveEmpty').hidden=true}};
    const run=await requestJson(`/api/runs/${demoRunId}`);const st=run.run?.status||'';if(st&&st!=='RUNNING'){clearInterval(demoTimer);demoTimer=null;$('runDemo').disabled=false;$('pauseDemo').disabled=true;$('resumeDemo').disabled=true;$('stopDemo').disabled=true;$('demoStatus').textContent=st;$('demoStatus').className='pill '+(st==='PASSED'?'ok':st==='FAILED'?'bad':'neutral');await refresh(true)}}catch(e){}
}
if($('runDemo')) $('runDemo').onclick=()=>withButton($('runDemo'),async()=>{await post('/api/config',configPayload());const j=await post('/api/start',{test_type:'demo',scenario:$('demoScenario').value,pace:+$('demoPace').value,mode:$('demoMode').value});demoRunId=j.run.run_id;demoScreens=[];demoReviewMode=false;selectedRun=demoRunId;$('selectedRun').textContent=`Demo · ${demoRunId}`;$('pauseDemo').disabled=false;$('resumeDemo').disabled=true;$('stopDemo').disabled=false;$('demoStatus').textContent='RUNNING';$('demoCurrentStep').textContent='Khởi động Chromium';$('demoSteps').innerHTML='<div class="empty">Đang chuẩn bị dữ liệu demo…</div>';$('demoLiveImage').hidden=true;$('demoLiveEmpty').hidden=false;returnDemoLive();if(demoTimer)clearInterval(demoTimer);demoTimer=setInterval(pollDemo,700);await pollDemo();showToast('Đã bắt đầu Demo Runner','success')}).catch(()=>{});
if($('pauseDemo')) $('pauseDemo').onclick=async()=>{if(!demoRunId)return;await post(`/api/demo/${demoRunId}/control`,{action:'pause'});$('pauseDemo').disabled=true;showToast('Demo sẽ dừng ở checkpoint an toàn kế tiếp','success')};
if($('resumeDemo')) $('resumeDemo').onclick=async()=>{if(!demoRunId)return;await post(`/api/demo/${demoRunId}/control`,{action:'resume'});$('resumeDemo').disabled=true;returnDemoLive();showToast('Tiếp tục demo','success')};
if($('stopDemo')) $('stopDemo').onclick=async()=>{if(!demoRunId)return;await post(`/api/stop/${demoRunId}`,{});$('stopDemo').disabled=true;showToast('Đã gửi lệnh dừng demo','success')};
if($('demoPrev')) $('demoPrev').onclick=async()=>{await loadDemoScreens();showDemoScreenshot((demoReviewMode?demoReviewIndex:demoScreens.length)-1)};
if($('demoNextShot')) $('demoNextShot').onclick=()=>showDemoScreenshot(demoReviewIndex+1);
if($('demoReturnLive')) $('demoReturnLive').onclick=()=>{returnDemoLive();pollDemo()};

// Demo database automation (v1.22.11).
let demoDbPreview=null;
function renderDemoDbPreview(j){
  demoDbPreview=j;
  const badge=$('demoDbBadge'),summary=$('demoDbSummary'),target=$('demoDbTarget'),rows=$('demoDbChecks'),prepare=$('prepareDemoDb'),reset=$('resetDemoDb');
  if(!badge)return;
  if(!j.ok){badge.textContent=j.enabled===false?'Đang khóa':'Bị chặn';badge.className='pill bad';summary.textContent=j.error||'Không thể kiểm tra Demo DB';target.innerHTML='';rows.innerHTML='';prepare.disabled=true;reset.disabled=true;return;}
  badge.textContent='Sẵn sàng';badge.className='pill ok';
  summary.innerHTML=`Nguồn <strong>${esc(j.source_database)}</strong> chỉ được READ/CLONE. Baseline <strong>${esc(j.template_database)}</strong> và target <strong>${esc(j.database)}</strong> là disposable.`;
  target.innerHTML=`<div><span>Source DB</span><strong>${esc(j.source_database)}</strong></div><div><span>Demo Template</span><strong>${esc(j.template_database)}</strong></div><div><span>Demo DB</span><strong>${esc(j.database)}</strong></div><div><span>Safety</span><strong>${esc(j.safety||'-')}</strong></div>`;
  const checks=(j.template_verification?.checks||j.target_verification?.checks||[]);
  rows.innerHTML=checks.length?checks.map(x=>`<div class="cleanup-row"><span>${esc(x.name)} ${esc(x.rule||'')}</span><b>${x.ok?'PASS':'FAIL'} · ${x.actual==null?'-':Number(x.actual).toLocaleString('vi-VN')}</b></div>`).join(''):'<div class="empty">Chưa có baseline demo. Bấm “Chuẩn bị Demo DB”.</div>';
  prepare.disabled=false;reset.disabled=!j.template_exists;
}
if($('previewDemoDb')) $('previewDemoDb').onclick=()=>withButton($('previewDemoDb'),async()=>{await post('/api/config',configPayload());const j=await requestJson('/api/database/demo/preview');renderDemoDbPreview(j);showToast('Đã kiểm tra Demo DB','success')}).catch(()=>{});
if($('prepareDemoDb')) $('prepareDemoDb').onclick=async()=>{
  if(!demoDbPreview?.ok)return;
  const code=demoDbPreview.prepare_confirm_text;
  const typed=prompt(`CHUẨN BỊ DEMO DATABASE\n\nSource: ${demoDbPreview.source_database} (READ ONLY)\nTemplate: ${demoDbPreview.template_database}\nTarget: ${demoDbPreview.database}\n\nQA Center chỉ xóa dữ liệu trên clone disposable.\nNhập chính xác: ${code}`,'');
  if(typed!==code){showToast('Đã hủy: mã xác nhận không đúng','error');return;}
  const btn=$('prepareDemoDb');btn.disabled=true;btn.textContent='Đang tạo baseline + verify…';
  try{await post('/api/config',configPayload());const j=await post('/api/database/demo/prepare',{confirm:typed});showToast(j.message||'Demo DB đã sẵn sàng','success');renderDemoDbPreview(await requestJson('/api/database/demo/preview'))}catch(e){showToast(e.message||String(e),'error')}finally{btn.textContent='Chuẩn bị Demo DB'}
};
if($('resetDemoDb')) $('resetDemoDb').onclick=async()=>{
  if(!demoDbPreview?.ok||!demoDbPreview.template_exists)return;
  const code=demoDbPreview.reset_confirm_text;
  const typed=prompt(`RESET DEMO DATABASE\n\nTarget: ${demoDbPreview.database}\nFrom: ${demoDbPreview.template_database}\n\nNhập chính xác: ${code}`,'');
  if(typed!==code){showToast('Đã hủy: mã xác nhận không đúng','error');return;}
  const btn=$('resetDemoDb');btn.disabled=true;btn.textContent='Đang reset + verify…';
  try{const j=await post('/api/database/demo/reset',{confirm:typed});showToast(j.message||'Demo DB đã reset','success');renderDemoDbPreview(await requestJson('/api/database/demo/preview'))}catch(e){showToast(e.message||String(e),'error')}finally{btn.textContent='Reset Demo DB'}
};
