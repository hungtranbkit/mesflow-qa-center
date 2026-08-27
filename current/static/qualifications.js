const $=id=>document.getElementById(id);
const esc=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const statusClass=status=>status==='PASSED'||status==='CERTIFIED'?'status-pass':status==='FLAKY'?'status-flaky':status==='FAILED'||status==='BLOCKED'?'status-fail':'status-neutral';
async function getJson(path){const response=await fetch(path,{headers:{Accept:'application/json'}});const body=await response.json().catch(()=>({}));if(!response.ok||body.ok===false)throw new Error(body.message||body.error||`HTTP ${response.status}`);return body}
function renderRun(run){
  $('artifactName').textContent=run.artifact_filename||'—';$('artifactSha').textContent=run.artifact_sha256||'—';
  $('environmentIdentity').textContent=`${run.environment||'—'} · ${run.environment_identity||'—'} · DB ${run.database_identity||'—'} · ${run.target_url||'—'}`;
  $('runStatus').textContent=run.status||'NOT_TESTED';$('runStatus').className=`status ${statusClass(run.status)}`;
  const suites=run.suites||[];$('suiteRows').innerHTML=suites.length?suites.map(s=>{let note='—';try{const summary=JSON.parse(s.summary_json||'{}');if(summary.reason)note=summary.reason}catch(e){}return `<tr><td><strong>${esc(s.suite_key)}</strong></td><td>${esc(s.layer)}</td><td>${s.required?'YES':'NO'}</td><td><span class="status ${statusClass(s.status)}">${esc(s.status)}</span></td><td>${s.exit_code??'—'}</td><td class="suite-note">${esc(note)}</td></tr>`}).join(''):'<tr><td colspan="6" class="empty-cell">Chưa có suite evidence.</td></tr>';
}
function renderCertification(cert){const decision=cert?.decision||{};const eligible=Boolean(cert?.production_eligible);$('eligibilityValue').textContent=eligible?'TRUE':'FALSE';$('eligibilityBlock').classList.toggle('is-eligible',eligible);const blockers=[...(decision.missing_suites||[]).map(x=>`Thiếu suite: ${x}`),...(decision.failed_suites||[]).map(x=>`Suite chưa PASS: ${x}`),...(decision.critical_flaky_suites||[]).map(x=>`Critical flaky: ${x}`)];if(decision.invariant_failures)blockers.push(`${decision.invariant_failures} invariant violation`);if(decision.hil_required_missing)blockers.push('Policy yêu cầu ESP HIL nhưng chưa có PASS');if(!cert)blockers.push('Chưa có certification cùng artifact SHA256.');$('eligibilityReason').textContent=eligible?'Đã đủ policy kỹ thuật; vẫn cần human approval.':(blockers[0]||'Policy chưa đạt.');$('blockerList').innerHTML=(blockers.length?blockers:['Không có blocker kỹ thuật; vẫn cần human approval.']).map(x=>`<li>${esc(x)}</li>`).join('')}
function renderCoverage(data){const percent=data.total_features?data.covered_features*100/data.total_features:0;$('coverageCount').textContent=`${data.covered_features} / ${data.total_features}`;$('criticalCoverage').textContent=`Critical ${data.critical_feature_coverage_percent}%`;$('coverageFill').style.width=`${percent}%`;const meter=document.querySelector('.coverage-meter');meter.setAttribute('aria-valuenow',String(Math.round(percent)));$('coverageRows').innerHTML=data.features.map(f=>`<tr><td><strong>${esc(f.name)}</strong><br><code>${esc(f.key)}</code><br><small>${esc((f.supporting_scenarios||[]).map(x=>x.scenario_key).join(', ')||'No supporting run evidence')}</small></td><td>${f.critical?'YES':'NO'}</td><td>${esc(f.missing_layers.join(', ')||'—')}</td><td>${esc(f.missing_drivers.join(', ')||'—')}</td><td><span class="status ${f.status==='COVERED'?'status-pass':f.status==='BLOCKED'?'status-fail':'status-neutral'}">${esc(f.status||'UNCOVERED')}</span></td></tr>`).join('')}
function renderNoRun(){$('artifactName').textContent='Chưa có qualification run';$('artifactSha').textContent='—';$('environmentIdentity').textContent='Chưa có environment identity';$('runStatus').textContent='NOT_TESTED';$('runStatus').className='status status-neutral';$('suiteRows').innerHTML='<tr><td colspan="6" class="empty-cell">Chưa có suite evidence.</td></tr>';renderCertification(null);$('coverageCount').textContent='NO EVIDENCE';$('criticalCoverage').textContent='';$('coverageFill').style.width='0';document.querySelector('.coverage-meter').setAttribute('aria-valuenow','0');$('coverageRows').innerHTML='<tr><td colspan="5" class="empty-cell">Chưa có run-scoped coverage evidence. Registry requirements are not test results.</td></tr>';$('sandboxRows').innerHTML='<tr><td colspan="6" class="empty-cell">Chưa có sandbox nào cho run này.</td></tr>'}
function sandboxOpenUrl(sandbox){return sandbox.app_port?`http://127.0.0.1:${sandbox.app_port}`:sandbox.target_url}
function renderSandboxes(sandboxes,runId){const mine=(sandboxes||[]).filter(s=>s.qualification_run_id===runId);$('sandboxRows').innerHTML=mine.length?mine.map(s=>{const base=sandboxOpenUrl(s);const alive=s.status==='READY'||s.status==='STOPPED';return `<tr><td><strong>${esc(s.id)}</strong><br><small>${esc(s.namespace)}</small></td><td>${esc(s.version||'—')}<br><code>${esc((s.artifact_sha256||'').slice(0,16))}</code></td><td>${esc(s.sandbox_type)}</td><td><span class="status ${statusClass(s.status)}">${esc(s.status)}</span></td><td>${esc(s.health||'—')}</td><td class="sandbox-actions">`+
    (alive&&base?`<a href="${esc(base)}" target="_blank" rel="noopener">Mở MESFlow</a> <a href="${esc(base)}/kiosk" target="_blank" rel="noopener">Kiosk</a> <a href="${esc(base)}/admin" target="_blank" rel="noopener">Admin</a> `:'')+
    `<button type="button" data-sandbox-action="logs" data-sandbox-id="${esc(s.id)}">Logs</button>`+
    (s.status==='READY'?`<button type="button" data-sandbox-action="stop" data-sandbox-id="${esc(s.id)}">Stop</button>`:'')+
    (s.status==='STOPPED'?`<button type="button" data-sandbox-action="start" data-sandbox-id="${esc(s.id)}">Start</button>`:'')+
    (s.sandbox_type!=='PERSISTENT'&&s.status!=='DESTROYED'?`<button type="button" data-sandbox-action="retain" data-sandbox-id="${esc(s.id)}">Giữ lại (debug)</button>`:'')+
    (s.status!=='DESTROYED'?`<button type="button" data-sandbox-action="destroy" data-sandbox-id="${esc(s.id)}">Destroy</button>`:'')+
    `</td></tr>`}).join(''):'<tr><td colspan="6" class="empty-cell">Chưa có sandbox nào cho run này.</td></tr>';
  $('sandboxRows').querySelectorAll('[data-sandbox-action]').forEach(btn=>btn.onclick=()=>sandboxAction(btn.dataset.sandboxId,btn.dataset.sandboxAction))}
async function sandboxAction(id,action){try{if(action==='logs'){const body=await getJson(`/api/qualification/sandboxes/${encodeURIComponent(id)}/logs`);alert(JSON.stringify(body.logs,null,2).slice(0,4000))}else{const response=await fetch(`/api/qualification/sandboxes/${encodeURIComponent(id)}/${action}`,{method:'POST'});const body=await response.json().catch(()=>({}));if(!response.ok||body.ok===false)throw new Error(body.message||body.error||`HTTP ${response.status}`)}await refreshSandboxesForCurrentRun()}catch(error){$('qualificationError').hidden=false;$('qualificationError').textContent=`Sandbox action thất bại: ${error.message}`}}
let currentRunId=null;
let currentArtifactSha=null;
let coverageScope='run';
let liveTimer=null;
let liveErrorStreak=0;
const compareSelection=new Set();
async function refreshSandboxesForCurrentRun(){if(!currentRunId)return;try{const body=await getJson('/api/qualification/sandboxes');renderSandboxes(body.sandboxes,currentRunId)}catch(error){$('sandboxRows').innerHTML=`<tr><td colspan="6" class="empty-cell">Không tải được sandboxes: ${esc(error.message)}</td></tr>`}}

// ---- Coverage RUN vs ARTIFACT toggle (spec section 10) ----
async function loadCoverage(){
  if(!currentRunId){renderCoverage({total_features:0,covered_features:0,critical_feature_coverage_percent:0,features:[]});return}
  $('coverageToggleRun').classList.toggle('toggle-active',coverageScope==='run');
  $('coverageToggleArtifact').classList.toggle('toggle-active',coverageScope==='artifact');
  try{
    const path=coverageScope==='artifact'&&currentArtifactSha?`/api/qualification/coverage/artifact/${encodeURIComponent(currentArtifactSha)}`:`/api/qualification/coverage?run_id=${encodeURIComponent(currentRunId)}`;
    const body=await getJson(path);
    renderCoverage(body.coverage);
  }catch(error){$('coverageRows').innerHTML=`<tr><td colspan="5" class="empty-cell">Không tải được coverage: ${esc(error.message)}</td></tr>`}
}
$('coverageToggleRun').onclick=()=>{coverageScope='run';loadCoverage()};
$('coverageToggleArtifact').onclick=()=>{coverageScope='artifact';loadCoverage()};

// ---- Release policy tiers (spec section 11 / demo F) ----
async function loadPolicyTiers(){
  if(!currentArtifactSha){$('policyRows').innerHTML='<tr><td colspan="6" class="empty-cell">Chưa có artifact để đánh giá.</td></tr>';return}
  try{
    const tiersBody=await getJson('/api/qualification/policy/tiers');
    const names=Object.keys(tiersBody.tiers||{});
    const decisions=await Promise.all(names.map(name=>getJson(`/api/qualification/policy/evaluate-tier?tier=${encodeURIComponent(name)}&artifact_sha256=${encodeURIComponent(currentArtifactSha)}`).then(b=>b.decision).catch(error=>({policy:tiersBody.tiers[name],error:error.message}))));
    $('policyRows').innerHTML=names.map((name,i)=>{
      const d=decisions[i];
      if(d.error)return `<tr><td>${esc(name)}</td><td><code>${esc(tiersBody.tiers[name].key)}</code></td><td colspan="3" class="empty-cell">Không đánh giá được: ${esc(d.error)}</td><td>—</td></tr>`;
      const required=d.policy.required_suites.length;
      const satisfied=(d.satisfied_suites||[]).length;
      const missing=(d.missing_suites||[]).concat(d.failed_suites||[]);
      return `<tr><td><strong>${esc(name)}</strong></td><td><code>${esc(d.policy.key)}</code></td><td>${required}</td><td>${satisfied}</td><td>${esc(missing.join(', ')||'—')}</td><td><span class="status ${d.production_eligible?'status-pass':'status-fail'}">${d.production_eligible?'YES':'NO'}</span></td></tr>`;
    }).join('');
  }catch(error){$('policyRows').innerHTML=`<tr><td colspan="6" class="empty-cell">Không tải được policy tiers: ${esc(error.message)}</td></tr>`}
}

// ---- Live observation (spec sections 2-6): real progress_json /
// qa_scenario_runs / qa_resource_samples state only -- current_action stays
// literally "no data" whenever the runner behind a given run never called
// touch_progress(), rather than inventing plausible-looking actor/action text.
function stopLive(){if(liveTimer){clearTimeout(liveTimer);liveTimer=null}}
function renderLive(live){
  $('livePanel').hidden=false;
  $('liveStatus').textContent=live.identity.status||'—';
  $('liveStatus').className=`status ${statusClass(live.identity.status)}`;
  $('liveScenario').textContent=`${live.current_action.phase||'—'} / ${live.current_action.scenario||'—'}`;
  $('liveSimTime').textContent=live.current_action.simulated_time_seconds!=null?`${live.current_action.simulated_time_seconds}s (sim)`:'—';
  $('liveAction').textContent=(live.current_action.actor||live.current_action.action)?`${live.current_action.actor||'?'} → ${live.current_action.action||'?'}`:'Chưa có dữ liệu current-action từ runner này';
  const latest=live.resources.latest||{};
  $('liveResources').textContent=latest.app_cpu_percent!=null?`${latest.app_cpu_percent}% CPU · ${latest.app_rss_kb||'—'} KB RSS`:'—';
  const probe=latest.probe||{};
  $('liveLatency').textContent=probe.latency_ms!=null?`${probe.latency_ms} ms (mẫu mới nhất)`:'—';
  $('liveDbConn').textContent=latest.db_connections!=null?String(latest.db_connections):'—';
  $('liveSessions').textContent=`${live.domain.passed_session_scenarios||0} scenario sessions PASSED`;
  $('liveIntegrity').textContent=`${live.domain.integrity_violations||0} violation(s)`;
  const ready=(live.sandboxes||[]).find(s=>s.status==='READY'&&s.app_port);
  const base=ready?sandboxOpenUrl(ready):null;
  $('liveNav').innerHTML=base?
    `<a href="${esc(base)}" target="_blank" rel="noopener">Mở MESFlow</a> <a href="${esc(base)}/kiosk" target="_blank" rel="noopener">Kiosk</a> <a href="${esc(base)}/admin" target="_blank" rel="noopener">Admin</a> <a href="${esc(base)}/dashboard" target="_blank" rel="noopener">Dashboard</a> <a href="${esc(base)}/app" target="_blank" rel="noopener">Session Management</a> <button type="button" data-sandbox-action="logs" data-sandbox-id="${esc(ready.id)}">Xem logs</button>`:
    'Chưa có sandbox READY để mở.';
  const logsBtn=$('liveNav').querySelector('[data-sandbox-action="logs"]');
  if(logsBtn)logsBtn.onclick=()=>sandboxAction(logsBtn.dataset.sandboxId,'logs');
}
function renderIncidents(incidents){
  const cls={EXPECTED_FAULT:'status-neutral',RECOVERED_FAULT:'status-pass',UNEXPECTED_INCIDENT:'status-fail',QUALIFICATION_FAILURE:'status-fail'};
  $('incidentRows').innerHTML=incidents.length?incidents.map(i=>`<tr><td>${esc(i.timestamp||'—')}</td><td>${esc(i.severity)}</td><td><span class="status ${cls[i.classification]||'status-neutral'}">${esc(i.classification)}</span></td><td>${esc(i.summary)}</td></tr>`).join(''):'<tr><td colspan="4" class="empty-cell">Chưa có incident.</td></tr>';
}
async function pollLive(runId){
  if(runId!==currentRunId)return; // page navigated to a different run; this poll chain is dead
  try{
    const [liveBody,incidentsBody]=await Promise.all([
      getJson(`/api/qualification/runs/${encodeURIComponent(runId)}/live`),
      getJson(`/api/qualification/runs/${encodeURIComponent(runId)}/incidents`)]);
    liveErrorStreak=0;
    if(liveBody.terminal){stopLive();$('livePanel').hidden=true;return} // run finished: stop polling cleanly, hide the "live" framing (final state lives in Suite ledger/History instead)
    renderLive(liveBody);
    renderIncidents(incidentsBody.incidents||[]);
  }catch(error){
    liveErrorStreak+=1;
    if(liveErrorStreak>=5){stopLive();return} // never spin forever on a broken connection
  }
  if(runId===currentRunId)liveTimer=setTimeout(()=>pollLive(runId),4000);
}
function updateLivePanelForRun(run){
  stopLive();
  if(run.status==='RUNNING'){liveErrorStreak=0;pollLive(run.id)}
  else{$('livePanel').hidden=true}
}

// ---- Re-run / Clone / Compare (spec section 7) ----
async function postJson(path,body){const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});const data=await response.json().catch(()=>({}));if(!response.ok||data.ok===false)throw new Error(data.message||data.error||`HTTP ${response.status}`);return data}
async function pollJobUntilDone(jobId){
  for(let i=0;i<300;i++){
    const body=await getJson(`/api/qualification/jobs/${encodeURIComponent(jobId)}`);
    if(['SUCCESS','FAILED','CANCELLED'].includes(body.job.status))return body.job;
    await new Promise(r=>setTimeout(r,2000));
  }
  throw new Error('job did not reach a terminal state in time (still running in background)');
}
async function triggerRerun(runId,btn){
  btn.disabled=true;btn.textContent='Đang chạy…';
  try{const started=await postJson(`/api/qualification/runs/${encodeURIComponent(runId)}/rerun`,{});const job=await pollJobUntilDone(started.job.id);if(job.status!=='SUCCESS')throw new Error(job.error||`job kết thúc với trạng thái ${job.status}`);await refresh()}
  catch(error){$('qualificationError').hidden=false;$('qualificationError').textContent=`Re-run thất bại: ${error.message}`}
  finally{btn.disabled=false;btn.textContent='Re-run'}
}
async function triggerClone(runId,btn){
  const seed=prompt('Seed mới cho bản clone (để trống = giữ nguyên seed gốc):','');
  const overrides=(seed&&seed.trim())?{seed:seed.trim()}:{};
  btn.disabled=true;btn.textContent='Đang clone…';
  try{const started=await postJson(`/api/qualification/runs/${encodeURIComponent(runId)}/clone`,overrides);const job=await pollJobUntilDone(started.job.id);if(job.status!=='SUCCESS')throw new Error(job.error||`job kết thúc với trạng thái ${job.status}`);await refresh()}
  catch(error){$('qualificationError').hidden=false;$('qualificationError').textContent=`Clone thất bại: ${error.message}`}
  finally{btn.disabled=false;btn.textContent='Clone'}
}
function toggleCompareSelection(runId,checked){if(checked)compareSelection.add(runId);else compareSelection.delete(runId)}
async function runCompare(){
  const ids=Array.from(compareSelection);
  if(ids.length!==2){alert('Chọn đúng 2 run để so sánh.');return}
  try{
    const body=await getJson(`/api/qualification/runs/compare?run_id=${encodeURIComponent(ids[0])}&run_id=${encodeURIComponent(ids[1])}`);
    const [a,b]=body.runs;
    $('compareColA').textContent=a.error?`${ids[0]} (${a.error})`:`${a.run_kind}/${a.profile} · ${a.status}`;
    $('compareColB').textContent=b.error?`${ids[1]} (${b.error})`:`${b.run_kind}/${b.profile} · ${b.status}`;
    const rows=[
      ['Artifact SHA256',x=>x.artifact_sha256],['Version',x=>x.application_version],['Trạng thái',x=>x.status],
      ['Bắt đầu',x=>x.started_at],['Kết thúc',x=>x.finished_at],['Request count',x=>x.request_count],
      ['Error count',x=>x.error_count],['P95 latency (ms)',x=>x.p95_latency_ms],['CPU % (avg)',x=>x.cpu_percent_avg],
      ['RSS KB (max)',x=>x.rss_kb_max],['Incidents',x=>x.incidents],['Invariant violations',x=>x.invariant_violations],
    ];
    $('compareRows').innerHTML=rows.map(([label,get])=>`<tr><td>${esc(label)}</td><td>${esc(a.error?'N/A':(get(a)??'N/A'))}</td><td>${esc(b.error?'N/A':(get(b)??'N/A'))}</td></tr>`).join('');
    $('compareWrap').hidden=false;
  }catch(error){$('qualificationError').hidden=false;$('qualificationError').textContent=`So sánh thất bại: ${error.message}`}
}

async function selectRun(run,certs){const button=$('refreshQualification');button.disabled=true;document.body.setAttribute('aria-busy','true');currentRunId=run.id;currentArtifactSha=run.artifact_sha256;try{const detail=await getJson(`/api/qualification/runs/${encodeURIComponent(run.id)}`);const matching=certs.find(c=>c.artifact_id===run.artifact_id&&c.environment_id===run.environment_id);renderRun(detail.run);renderCertification(matching);await loadCoverage();await loadPolicyTiers();await refreshSandboxesForCurrentRun();updateLivePanelForRun(detail.run);$('qualificationError').hidden=true;$('qualificationError').textContent=''}catch(error){$('qualificationError').hidden=false;$('qualificationError').textContent=`Không tải được qualification evidence: ${error.message}`}finally{button.disabled=false;document.body.removeAttribute('aria-busy')}}
async function refresh(){const button=$('refreshQualification');button.disabled=true;button.textContent='Đang tải…';document.body.setAttribute('aria-busy','true');stopLive();compareSelection.clear();$('compareWrap').hidden=true;try{const [runsBody,certBody]=await Promise.all([getJson('/api/qualification/runs'),getJson('/api/qualification/certifications')]);const runs=runsBody.runs||[],certs=certBody.certifications||[];$('qualificationRuns').innerHTML=runs.length?runs.map(r=>`<div class="qualification-run-row"><label class="compare-check"><input type="checkbox" data-compare="${esc(r.id)}"></label><button type="button" class="qualification-run" data-run="${esc(r.id)}"><strong>${esc(r.status)} · ${esc(r.profile)}</strong><code>${esc(r.artifact_sha256)}</code><span>${esc(r.environment)} · ${esc(r.started_at)}</span><small data-incident-count="${esc(r.id)}">Đang đếm incident…</small></button><span class="run-row-actions"><button type="button" data-rerun="${esc(r.id)}">Re-run</button><button type="button" data-clone="${esc(r.id)}">Clone</button></span></div>`).join(''):'<p class="empty-cell">Chưa có qualification run.</p>';if(runs[0])await selectRun(runs[0],certs);else{currentRunId=null;currentArtifactSha=null;renderNoRun();updateLivePanelForRun({status:'NOT_TESTED'})}document.querySelectorAll('[data-run]').forEach((row,index)=>row.onclick=()=>selectRun(runs[index],certs));document.querySelectorAll('[data-rerun]').forEach(btn=>btn.onclick=()=>triggerRerun(btn.dataset.rerun,btn));document.querySelectorAll('[data-clone]').forEach(btn=>btn.onclick=()=>triggerClone(btn.dataset.clone,btn));document.querySelectorAll('[data-compare]').forEach(cb=>cb.onchange=()=>toggleCompareSelection(cb.dataset.compare,cb.checked));runs.forEach(r=>{getJson(`/api/qualification/runs/${encodeURIComponent(r.id)}/incidents`).then(b=>{const el=document.querySelector(`[data-incident-count="${r.id}"]`);if(el)el.textContent=`${(b.incidents||[]).length} incident(s)`}).catch(()=>{const el=document.querySelector(`[data-incident-count="${r.id}"]`);if(el)el.textContent='— incident(s)'})});$('qualificationError').hidden=true;$('qualificationError').textContent=''}catch(error){$('qualificationError').hidden=false;$('qualificationError').textContent=`Không tải được qualification evidence: ${error.message}`}finally{button.disabled=false;button.textContent='Làm mới dữ liệu';document.body.removeAttribute('aria-busy')}}
$('refreshQualification').onclick=refresh;$('refreshSandboxes').onclick=refreshSandboxesForCurrentRun;$('compareRuns').onclick=runCompare;refresh();
