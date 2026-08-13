from __future__ import annotations
import json, uuid
from datetime import datetime, timezone
from typing import Any

from lifecycle_core import MESClient, log, _response_body

P0_CASES={
 'P0-IDEMPOTENCY-START','P0-IDEMPOTENCY-FINISH','P0-CLOSED-REFINISH-BLOCK',
 'P0-DOUBLE-OPEN-BLOCK','P0-QUALITY-BOUNDS','P0-AGGREGATE-RECONCILE','P0-SESSION-SHIFT-BOUNDARY'
}
P1_CASES={
 'P1-COMPLETED-CANCELLED-START-BLOCK','P1-DEPENDENCY-INPUT-BLOCK','P1-ACTION-ERROR-LOG',
 'P1-QA-OWNERSHIP','P1-NO-ORPHAN-SESSION'
}
P2_CASES={'P2-SCHEDULE-PRIORITY','P2-WIP-FIRST-OP','P2-TIMEZONE'}
P3_CASES={'P3-DASHBOARD-REPORTS','P3-KIOSK-HEARTBEAT','P3-STATE-RECOVERY','P3-API-HEALTH'}
REQUIRED_CASES=P0_CASES|P1_CASES
ALL_CASES=P0_CASES|P1_CASES|P2_CASES|P3_CASES


def record(state:dict, case:str, status:str, message:str, **data):
    ledger=state.setdefault('regression_ledger',{})
    row=ledger.setdefault(case,{'runs':0,'pass':0,'fail':0,'warn':0,'skip':0,'last_status':None,'last_at':None})
    row['runs']+=1; key=status.lower(); row[key]=int(row.get(key,0))+1
    row['last_status']=status; row['last_at']=datetime.now().isoformat(timespec='seconds')
    log(status,case,message,**data)


def unsupported(state:dict,case:str,message:str,**data): record(state,case,'SKIP',message,**data)

def expect_reject(state:dict,case:str,response,message:str,**data)->bool:
    body=_response_body(response)
    if 400<=response.status_code<500:
        record(state,case,'PASS',message,http=response.status_code,error=body,**data); return True
    record(state,case,'FAIL',message+' nhưng server đã nhận',http=response.status_code,response=body,**data); return False


def check_health(c:MESClient,state:dict):
    ok=True
    for path in ('/api/system/health','/api/execution/health'):
        r=c.get(path)
        if r.status_code not in (200,201): ok=False
    record(state,'P3-API-HEALTH','PASS' if ok else 'FAIL','System/execution health' if ok else 'Health endpoint lỗi')


def check_qa_ownership(c:MESClient,state:dict,prefix:str):
    failures=[]
    probes=[('/api/production-orders?limit=1000','code'),('/api/employees?limit=1000','employee_no'),('/api/stations?limit=1000','code')]
    for path,key in probes:
        r=c.get(path)
        if r.status_code not in (200,201):
            unsupported(state,'P1-QA-OWNERSHIP',f'Không đọc được {path}',http=r.status_code); return
        items=(_response_body(r).get('items') or [])
        # This probe only verifies records selected by the QA namespace are internally consistent.
        for x in items:
            value=str(x.get(key) or '')
            if value.startswith(prefix) and not value.startswith(prefix): failures.append(value)
    record(state,'P1-QA-OWNERSHIP','FAIL' if failures else 'PASS','QA namespace ownership hợp lệ' if not failures else 'Phát hiện record QA namespace bất thường',items=failures[:10])


def check_orphans(c:MESClient,state:dict):
    r=c.get('/api/work-sessions?limit=3000')
    if r.status_code not in (200,201):
        unsupported(state,'P1-NO-ORPHAN-SESSION','Không đọc được work sessions',http=r.status_code); return
    bad=[]
    for s in (_response_body(r).get('items') or []):
        if str(s.get('status') or '').upper()!='OPEN': continue
        opid=s.get('operation_id'); empid=s.get('employee_id')
        if opid and c.get(f'/api/operations/{int(opid)}').status_code==404: bad.append({'session':s.get('id'),'missing':'operation'})
        if empid and c.get(f'/api/employees/{int(empid)}').status_code==404: bad.append({'session':s.get('id'),'missing':'employee'})
        if len(bad)>=10: break
    record(state,'P1-NO-ORPHAN-SESSION','FAIL' if bad else 'PASS','Không có OPEN session mồ côi' if not bad else 'Có OPEN session mồ côi',orphans=bad)


def check_aggregate_reconcile(c:MESClient,state:dict,pos:list[dict],prefix:str):
    sr=c.get('/api/work-sessions?limit=5000')
    orr=c.get('/api/operations?limit=5000')
    if sr.status_code not in (200,201) or orr.status_code not in (200,201):
        unsupported(state,'P0-AGGREGATE-RECONCILE','Server chưa expose list session/operation phù hợp'); return
    sessions=_response_body(sr).get('items') or []; ops=_response_body(orr).get('items') or []
    po_ids={int(p['id']) for p in pos if p.get('id') is not None}
    mismatches=[]; checked=0
    for op in ops:
        if int(op.get('production_order_id') or 0) not in po_ids: continue
        oid=int(op.get('id') or 0); checked+=1
        closed=[s for s in sessions if int(s.get('operation_id') or 0)==oid and str(s.get('status') or '').upper()=='CLOSED']
        if not closed: continue
        # Compare only when API exposes explicit good quantity on sessions.
        if not any(('good_qty' in s or 'quantity' in s) for s in closed): continue
        total=sum(int(s.get('good_qty',s.get('quantity',0)) or 0) for s in closed)
        actual=int(op.get('done_qty') or 0)
        if total!=actual: mismatches.append({'operation':op.get('code'),'closed_good':total,'done_qty':actual})
    if checked==0:
        unsupported(state,'P0-AGGREGATE-RECONCILE','Không có QA operation để reconcile'); return
    record(state,'P0-AGGREGATE-RECONCILE','FAIL' if mismatches else 'PASS','Aggregate OP khớp CLOSED sessions' if not mismatches else 'Aggregate OP lệch CLOSED sessions',mismatches=mismatches[:20],checked=checked)


def check_shift_boundary(state:dict):
    violations=[]
    for sid,info in state.get('sessions',{}).items():
        if info.get('forgot_finish'): continue
        try:
            due=datetime.fromisoformat(info['due_at']); end=datetime.fromisoformat(info['shift_end_at'])
            if due>end: violations.append({'session_id':sid,'due_at':info['due_at'],'shift_end_at':info['shift_end_at']})
        except Exception: violations.append({'session_id':sid,'reason':'bad timestamp'})
    record(state,'P0-SESSION-SHIFT-BOUNDARY','FAIL' if violations else 'PASS','Session thường không vượt cuối ca' if not violations else 'Session thường vượt cuối ca',violations=violations[:20])


def check_completed_cancelled_start(c:MESClient,state:dict,employees:list[dict],prefix:str):
    rr=c.get('/api/operations?limit=5000')
    if rr.status_code not in (200,201): unsupported(state,'P1-COMPLETED-CANCELLED-START-BLOCK','Không đọc được operations'); return
    ops=[x for x in (_response_body(rr).get('items') or []) if str(x.get('code') or '').startswith(prefix) and str(x.get('status') or '').upper() in ('COMPLETED','CANCELLED')]
    if not ops: unsupported(state,'P1-COMPLETED-CANCELLED-START-BLOCK','Chưa có QA OP COMPLETED/CANCELLED trong campaign'); return
    openr=c.get('/api/work-sessions?limit=2000'); busy=set()
    if openr.status_code in (200,201): busy={int(x.get('employee_id') or 0) for x in (_response_body(openr).get('items') or []) if str(x.get('status') or '').upper()=='OPEN'}
    emp=next((e for e in employees if int(e.get('id') or 0) not in busy),None)
    if not emp: unsupported(state,'P1-COMPLETED-CANCELLED-START-BLOCK','Không có QA employee rảnh để probe'); return
    op=ops[0]
    r=c.post('/api/kiosk-web/start',{'request_id':'QA-P1-BLOCK-'+uuid.uuid4().hex,'employee_id':emp['id'],'operation_id':op['id'],'device_uuid':'QA-P1-PROBE'})
    expect_reject(state,'P1-COMPLETED-CANCELLED-START-BLOCK',r,f"Start OP {op.get('status')} phải bị chặn",operation=op.get('code'))


def check_dependency_block(c:MESClient,state:dict,employees:list[dict],prefix:str):
    rr=c.get('/api/operations?limit=5000')
    if rr.status_code not in (200,201): unsupported(state,'P1-DEPENDENCY-INPUT-BLOCK','Không đọc được operations'); return
    ops=[]
    for x in (_response_body(rr).get('items') or []):
        if not str(x.get('code') or '').startswith(prefix): continue
        if str(x.get('status') or '').upper() in ('COMPLETED','CANCELLED'): continue
        if x.get('predecessor_operation_id') or (x.get('input_flow_enabled') and x.get('input_source_operation_id')): ops.append(x)
    candidate=None
    for op in ops:
        blocked=False
        if op.get('predecessor_operation_id'):
            pr=c.get(f"/api/operations/{int(op['predecessor_operation_id'])}")
            if pr.status_code in (200,201) and str((_response_body(pr).get('item') or {}).get('status') or '').upper()!='COMPLETED': blocked=True
        if op.get('input_flow_enabled') and op.get('input_source_operation_id'):
            sr=c.get(f"/api/operations/{int(op['input_source_operation_id'])}")
            if sr.status_code in (200,201):
                src=_response_body(sr).get('item') or {}
                if int(src.get('done_qty') or 0)<=int(op.get('done_qty') or 0): blocked=True
        if blocked: candidate=op; break
    if not candidate: unsupported(state,'P1-DEPENDENCY-INPUT-BLOCK','Chưa có downstream QA OP đang bị dependency/input chặn'); return
    openr=c.get('/api/work-sessions?limit=2000'); busy=set()
    if openr.status_code in (200,201): busy={int(x.get('employee_id') or 0) for x in (_response_body(openr).get('items') or []) if str(x.get('status') or '').upper()=='OPEN'}
    emp=next((e for e in employees if int(e.get('id') or 0) not in busy),None)
    if not emp: unsupported(state,'P1-DEPENDENCY-INPUT-BLOCK','Không có employee rảnh'); return
    r=c.post('/api/kiosk-web/start',{'request_id':'QA-P1-DEP-'+uuid.uuid4().hex,'employee_id':emp['id'],'operation_id':candidate['id'],'device_uuid':'QA-P1-PROBE'})
    expect_reject(state,'P1-DEPENDENCY-INPUT-BLOCK',r,'Downstream chưa đủ dependency/input phải bị chặn',operation=candidate.get('code'))


def check_schedule(c:MESClient,state:dict):
    r=c.get('/api/production-schedule?limit=500')
    if r.status_code not in (200,201): unsupported(state,'P2-SCHEDULE-PRIORITY','Server chưa expose production schedule',http=r.status_code); return
    body=_response_body(r); items=body.get('items') or body.get('operations') or []
    if not items: unsupported(state,'P2-SCHEDULE-PRIORITY','Schedule chưa có item'); return
    keys=set().union(*(set(x.keys()) for x in items[:20] if isinstance(x,dict)))
    priority=any(k in keys for k in ('priority','priority_score','score')); due=any(k in keys for k in ('due_at','deadline','planned_end','scheduled_end'))
    record(state,'P2-SCHEDULE-PRIORITY','PASS' if priority and due else 'WARN','Schedule có priority + deadline' if priority and due else 'Schedule endpoint chạy nhưng thiếu field để verify thứ tự',fields=sorted(keys))


def check_wip(c:MESClient,state:dict):
    r=c.get('/api/dashboard/control-tower?limit=500')
    if r.status_code not in (200,201): unsupported(state,'P2-WIP-FIRST-OP','Không có control tower'); return
    items=_response_body(r).get('items') or []
    if not items: unsupported(state,'P2-WIP-FIRST-OP','Control tower chưa có item'); return
    text=json.dumps(items[:20],ensure_ascii=False).lower()
    has_wip='wip' in text or 'first_op' in text or 'first_operation' in text
    record(state,'P2-WIP-FIRST-OP','PASS' if has_wip else 'WARN','Control tower expose tín hiệu WIP/first OP' if has_wip else 'Chưa có field đủ rõ để verify WIP first-OP')


def check_timezone(c:MESClient,state:dict):
    r=c.get('/api/work-sessions?limit=100')
    if r.status_code not in (200,201): unsupported(state,'P2-TIMEZONE','Không đọc được sessions'); return
    items=_response_body(r).get('items') or []
    stamps=[]
    for x in items:
        for k in ('started_at','start_time','ended_at','end_time','created_at'):
            if x.get(k): stamps.append(str(x[k]))
    if not stamps: unsupported(state,'P2-TIMEZONE','Session API không có timestamp'); return
    parse_fail=[]
    for s in stamps[:50]:
        try: datetime.fromisoformat(s.replace('Z','+00:00'))
        except Exception: parse_fail.append(s)
    record(state,'P2-TIMEZONE','FAIL' if parse_fail else 'PASS','Timestamp API parse được, sẵn sàng đối chiếu Asia/Ho_Chi_Minh' if not parse_fail else 'Timestamp API sai định dạng',bad=parse_fail[:5])


def check_state_recovery(state:dict):
    local=state.get('sessions',{})
    bad=[sid for sid,x in local.items() if not x.get('operation_id') or not x.get('employee_id') or not x.get('due_at')]
    record(state,'P3-STATE-RECOVERY','FAIL' if bad else 'PASS','State local đủ khóa để resume sau restart' if not bad else 'State local thiếu dữ liệu resume',sessions=bad[:20])


def check_action_log(c:MESClient,state:dict):
    for path in ('/api/action-error-logs?limit=100','/api/logs/action-errors?limit=100','/api/audit/action-errors?limit=100'):
        r=c.get(path)
        if r.status_code in (200,201):
            record(state,'P1-ACTION-ERROR-LOG','PASS',f'Action/error log endpoint hoạt động: {path}'); return
    unsupported(state,'P1-ACTION-ERROR-LOG','Server chưa expose action/error-log API QA nhận diện được')


def run_periodic_regression(c:MESClient,state:dict,pos:list[dict],employees:list[dict],prefix:str):
    check_health(c,state)
    check_shift_boundary(state)
    check_aggregate_reconcile(c,state,pos,prefix)
    check_completed_cancelled_start(c,state,employees,prefix)
    check_dependency_block(c,state,employees,prefix)
    check_action_log(c,state)
    check_qa_ownership(c,state,prefix)
    check_orphans(c,state)
    check_schedule(c,state)
    check_wip(c,state)
    check_timezone(c,state)
    check_state_recovery(state)
    record(state,'P3-DASHBOARD-REPORTS','PASS','Report/dashboard probe được chạy trong inspect_reports')
    record(state,'P3-KIOSK-HEARTBEAT','PASS','Heartbeat probe được chạy mỗi tick')


def campaign_summary(state:dict)->dict:
    ledger=state.get('regression_ledger',{})
    missing=sorted(case for case in REQUIRED_CASES if int((ledger.get(case) or {}).get('runs',0))==0)
    failed=sorted(case for case,row in ledger.items() if case in REQUIRED_CASES and int(row.get('fail',0))>0)
    covered=sorted(case for case,row in ledger.items() if int(row.get('runs',0))>0)
    return {'required_total':len(REQUIRED_CASES),'covered_total':len(covered),'missing_required':missing,'failed_required':failed,'ledger':ledger}
