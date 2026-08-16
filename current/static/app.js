let selectedRun=null;
let logPaused=false;
let lastLogText="";
let refreshBusy=false;
const $=id=>document.getElementById(id);
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function showToast(message,type='error'){const el=$('toast');el.textContent=message;el.className='toast '+type;el.hidden=false;clearTimeout(showToast.timer);showToast.timer=setTimeout(()=>el.hidden=true,6000)}
function setConnection(ok,text){$('connectionStatus').textContent=text||'';$('connectionBadge').textContent=ok?'Đã kết nối':'Kết nối lỗi';$('connectionBadge').className='pill '+(ok?'ok':'bad')}
function configPayload(){const internal=$('internal_base_url').value.trim()||'http://mesflow-app:8080';return{base_url:internal,internal_base_url:internal,username:$('username').value.trim(),password:$('password').value,verify_ssl:false}}
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
function runLabel(type){return({functional:'Functional Smoke',api_soak:'API Soak',browser_visual:'Browser Visual',behavioral:'Behavioral Campaign',factory_simulation:'Factory Lifecycle',realtime_soak:'Mô phỏng nhiều ngày'})[type]||type}
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

let cleanupPreview=null;
$('previewCleanup').onclick=e=>withButton(e.currentTarget,async()=>{
  await post('/api/config',configPayload());
  const j=await post('/api/cleanup',{preview:true});
  cleanupPreview=j;
  const po=j.counts?.production_orders||0, emp=j.counts?.employees||0, st=j.counts?.stations||0;
  $('cleanupSummary').textContent=`Sẽ xóa ${po} PO QA, ${emp} nhân viên QA và ${st} trạm QA. Chỉ tiền tố QAV65817-/QAV65813- được xử lý.`;
  $('cleanupBadge').textContent=`${po} PO · ${emp} NV · ${st} trạm`;
  $('cleanupBadge').className='pill '+((po+emp+st)>0?'bad':'ok');
  $('cleanupTestData').disabled=(po+emp+st)===0;
  showToast((po+emp+st)>0?'Đã tải danh sách dữ liệu test':'Không có dữ liệu test để xóa','success');
}).catch(()=>{});

$('cleanupTestData').onclick=e=>withButton(e.currentTarget,async()=>{
  if(!cleanupPreview)throw new Error('Hãy bấm Kiểm tra dữ liệu trước');
  const po=cleanupPreview.counts?.production_orders||0, emp=cleanupPreview.counts?.employees||0, st=cleanupPreview.counts?.stations||0;
  const typed=window.prompt(`Sắp xóa ${po} PO QA, ${emp} nhân viên QA và ${st} trạm QA.\nKhông thể hoàn tác.\n\nNhập chính xác: DELETE QA DATA`,'');
  if(typed===null)return;
  if(typed.trim().toUpperCase()!=='DELETE QA DATA')throw new Error('Chuỗi xác nhận không đúng. Không xóa dữ liệu.');
  if(!window.confirm('XÁC NHẬN LẦN CUỐI: xóa toàn bộ dữ liệu QA đã liệt kê?'))return;
  const j=await post('/api/cleanup',{confirm:'DELETE QA DATA'});
  const c=j.counts||{};
  $('cleanupSummary').textContent=`Đã xóa ${c.deleted_production_orders||0} PO, ${c.deleted_employees||0} nhân viên, ${c.deleted_stations||0} trạm. Lỗi/được giữ lại: ${c.failed||0}.`;
  $('cleanupBadge').textContent=(c.failed||0)?'Xóa một phần':'Đã dọn sạch';
  $('cleanupBadge').className='pill '+((c.failed||0)?'bad':'ok');
  $('cleanupTestData').disabled=true;
  cleanupPreview=null;
  if(c.failed)throw new Error(`Có ${c.failed} bản ghi không xóa được. Xem phản hồi API/service log.`);
  showToast('Đã xóa dữ liệu test an toàn','success');
}).catch(()=>{});
