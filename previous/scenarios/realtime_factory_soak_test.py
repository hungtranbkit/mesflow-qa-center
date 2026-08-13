from __future__ import annotations
import argparse, json, os, random, time, uuid
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from typing import Any

from lifecycle_core import MESClient, assert_ok, log, find_or_create_employees, get_template, create_po, operations_for_po, _response_body
from p0_p3_regression import record as regression_record, run_periodic_regression, campaign_summary

PREFIX='QAV65817-'
STATION_NAMES=['Trạm Cắt - Đột 01','Trạm Chấn 01','Trạm Hàn 01','Trạm Lắp ráp 01','Trạm Hoàn thiện 01','Trạm Cắt - Đột 02','Trạm Chấn 02','Trạm Hàn 02']
RUNTIME_DIR=Path(os.environ.get("MESFLOW_QA_RUNTIME_DIR", str(Path(__file__).resolve().parent.parent/"runtime")))
STATE_FILE=RUNTIME_DIR/"state"/"realistic_soak_state.json"


def now_local(): return datetime.now()
def iso(dt:datetime): return dt.isoformat(timespec='seconds')

def parse_hhmm(v:str, default:str)->dtime:
    try: return datetime.strptime(v or default,'%H:%M').time()
    except Exception: return datetime.strptime(default,'%H:%M').time()

def load_state()->dict[str,Any]:
    try: return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except Exception: return {'sessions':{},'po_ids':[],'completed_pos':[],'started_at':iso(now_local()),'last_report_at':None,'cycle':0}

def save_state(state:dict[str,Any]):
    STATE_FILE.parent.mkdir(parents=True,exist_ok=True)
    tmp=STATE_FILE.with_suffix('.tmp'); tmp.write_text(json.dumps(state,ensure_ascii=False,indent=2,default=str),encoding='utf-8'); tmp.replace(STATE_FILE)

def ensure_stations(c:MESClient,count:int)->list[dict]:
    items=assert_ok(c.get('/api/stations?limit=1000'),'ST-001 List stations').get('items',[])
    qa=[x for x in items if str(x.get('code') or '').startswith(PREFIX+'ST-')]
    qa.sort(key=lambda x:str(x.get('code') or ''))
    # Đổi tên các trạm QA cũ sang tên công đoạn tự nhiên để dashboard kiosk dễ nhìn.
    for idx,item in enumerate(qa,1):
        desired=STATION_NAMES[(idx-1)%len(STATION_NAMES)]
        if str(item.get('name') or '')!=desired:
            r=c.patch(f"/api/stations/{int(item['id'])}",{'name':desired,'workshop':'Xưởng Cơ khí Demo','production_line':'Dây chuyền sản xuất số 1','active':True})
            if r.status_code in (200,201): item.update({'name':desired,'workshop':'Xưởng Cơ khí Demo','production_line':'Dây chuyền sản xuất số 1'})
            else: log('WARN','ST-DISPLAY-NAME',f'Không cập nhật được tên trạm {item.get("code")}',status=r.status_code)
    existing={str(x.get('code') or '') for x in items}
    seq=1
    while len(qa)<count:
        code=f'{PREFIX}ST-{seq:02d}'; seq+=1
        if code in existing: continue
        b=assert_ok(c.post('/api/stations',{'code':code,'name':STATION_NAMES[(seq-2)%len(STATION_NAMES)],'workshop':'Xưởng Cơ khí Demo','production_line':'Dây chuyền sản xuất số 1','active':True}),'ST-002 Create station')
        qa.append(b.get('item') or b); existing.add(code)
    qa.sort(key=lambda x:str(x.get('code') or ''))
    return qa[:count]

def bind_kiosks(c:MESClient,stations:list[dict])->dict[str,dict]:
    out={}
    for idx,st in enumerate(stations,1):
        device=f'{PREFIX}KIOSK-{idx:02d}'
        b=assert_ok(c.post('/api/kiosk/bind',{'device_id':device,'device_name':f'Kiosk {STATION_NAMES[(idx-1)%len(STATION_NAMES)]}','station_code':st['code'],'app_version':'QA-1.17.1'}),f'KIOSK-{idx:02d} Bind')
        out[device]={'token':b['kiosk_token'],'station':st}
    return out

def heartbeat(c:MESClient,kiosks:dict[str,dict],rng:random.Random):
    for device,meta in kiosks.items():
        payload={'device_id':device,'ui_state':rng.choice(['READY','WAIT_EMPLOYEE','WAIT_OPERATION']),'pending_events':0,'wifi_rssi':rng.randint(-68,-45),'free_heap':rng.randint(145000,240000)}
        r=c.post('/api/station/heartbeat',payload,headers={'X-Kiosk-Token':meta['token']},timeout=30)
        if r.status_code in MESClient.RETRYABLE_STATUSES:
            log('WARN','KIOSK-HB-DEFER',f'{device}: HTTP {r.status_code}; heartbeat sẽ thử lại ở tick sau')
            continue
        assert_ok(r,f'KIOSK-HB {device}')

def sync_event(c:MESClient,device:str,meta:dict,event_type:str,payload:dict):
    event={'event_id':f'{device}-{event_type}-{uuid.uuid4().hex}','event_type':event_type,'occurred_at':iso(now_local()),**payload}
    r=c.post('/api/station/events/sync',{'device_id':device,'events':[event]},headers={'X-Kiosk-Token':meta['token']},timeout=30)
    assert_ok(r,f'KIOSK-EVT {event_type}')

def calendar(c:MESClient)->dict:
    try: return assert_ok(c.get('/api/settings/working-calendar'),'CAL-001 Working calendar').get('item') or {}
    except Exception as exc:
        log('WARN','CAL-001',f'Không đọc được lịch, dùng mặc định: {exc}')
        return {'work_start':'07:30','lunch_start':'11:30','lunch_end':'13:00','work_end':'17:00','working_weekdays':[0,1,2,3,4,5]}

def is_work_time(dt:datetime,cal:dict,night_start:str='19:00',night_end:str='07:00')->bool:
    # Ca ngày lấy theo working-calendar của MES. Ca tối được QA bật mặc định để mô phỏng 24h thực tế.
    weekdays=[int(x) for x in cal.get('working_weekdays',[0,1,2,3,4,5])]
    t=dt.time(); ws=parse_hhmm(cal.get('work_start'),'07:30'); ls=parse_hhmm(cal.get('lunch_start'),'11:30'); le=parse_hhmm(cal.get('lunch_end'),'13:00'); we=parse_hhmm(cal.get('work_end'),'17:00')
    day_ok=dt.weekday() in weekdays and (ws<=t<ls or le<=t<we)
    ns=parse_hhmm(night_start,'19:00'); ne=parse_hhmm(night_end,'07:00')
    if ns<=ne:
        night_ok=ns<=t<ne
    else:
        night_ok=t>=ns or t<ne
    return day_ok or night_ok

def next_work_start(dt:datetime,cal:dict)->datetime:
    ws=parse_hhmm(cal.get('work_start'),'07:30'); le=parse_hhmm(cal.get('lunch_end'),'13:00'); ls=parse_hhmm(cal.get('lunch_start'),'11:30'); we=parse_hhmm(cal.get('work_end'),'17:00')
    weekdays=[int(x) for x in cal.get('working_weekdays',[0,1,2,3,4,5])]
    if dt.weekday() in weekdays and ls<=dt.time()<le: return datetime.combine(dt.date(),le)
    cur=dt
    for _ in range(8):
        if cur.weekday() in weekdays:
            candidate=datetime.combine(cur.date(),ws)
            if candidate>dt: return candidate
        cur=datetime.combine(cur.date()+timedelta(days=1),ws)
    return dt+timedelta(hours=1)

def current_shift_end(dt:datetime,cal:dict,night_start:str='19:00',night_end:str='07:00')->datetime|None:
    """Return the end of the shift containing dt. Day sessions close by work_end; night sessions close by night_end."""
    weekdays=[int(x) for x in cal.get('working_weekdays',[0,1,2,3,4,5])]
    ws=parse_hhmm(cal.get('work_start'),'07:30'); we=parse_hhmm(cal.get('work_end'),'17:00')
    t=dt.time()
    if dt.weekday() in weekdays and ws<=t<we:
        return datetime.combine(dt.date(),we)
    ns=parse_hhmm(night_start,'19:00'); ne=parse_hhmm(night_end,'07:00')
    if ns<=ne:
        if ns<=t<ne: return datetime.combine(dt.date(),ne)
    else:
        if t>=ns: return datetime.combine(dt.date()+timedelta(days=1),ne)
        if t<ne: return datetime.combine(dt.date(),ne)
    return None

def next_day_entry_time(shift_end:datetime,cal:dict,rng:random.Random)->datetime:
    """A forgotten quantity is entered next calendar day near the day-shift start."""
    ws=parse_hhmm(cal.get('work_start'),'07:30')
    weekdays=[int(x) for x in cal.get('working_weekdays',[0,1,2,3,4,5])]
    day=shift_end.date()+timedelta(days=1)
    for _ in range(8):
        candidate=datetime.combine(day,ws)+timedelta(minutes=rng.randint(0,90))
        if candidate.weekday() in weekdays:
            return candidate
        day+=timedelta(days=1)
    return shift_end+timedelta(days=1)

def active_qa_pos(c:MESClient)->list[dict]:
    items=assert_ok(c.get('/api/production-orders?limit=1000'),'PO-LIST Active QA').get('items',[])
    return [x for x in items if str(x.get('code') or '').startswith(PREFIX) and str(x.get('status') or '').upper()=='IN_PROGRESS']

def ensure_po_pool(c:MESClient,template:dict,target:int,plan_min:int,plan_max:int,rng:random.Random)->list[dict]:
    active=active_qa_pos(c)
    while len(active)<target:
        idx=int(time.time())%100000+len(active)
        plan=rng.randint(plan_min,plan_max)
        po=create_po(c,template,idx,plan)
        active.append({'id':po['id'],'code':po['code'],'planned_quantity':plan,'status':'IN_PROGRESS'})
        log('PASS','POOL-NEW',f'Mở PO mới {po["code"]}',planned_quantity=plan,active=len(active))
    return active

def get_open_sessions(c:MESClient)->list[dict]:
    return assert_ok(c.get('/api/work-sessions?limit=2000'),'SES-LIST').get('items',[])

def eligible_ops(c:MESClient,pos:list[dict])->list[dict]:
    all_ops=assert_ok(c.get('/api/operations?limit=5000'),'OP-LIST').get('items',[])
    po_ids={int(p['id']) for p in pos}
    candidates=[]
    for op in all_ops:
        if int(op.get('production_order_id') or 0) not in po_ids: continue
        if str(op.get('status') or '').upper() in ('COMPLETED','CANCELLED'): continue
        pred=op.get('predecessor_operation_id'); source=op.get('input_source_operation_id') if op.get('input_flow_enabled') else None
        # Backend remains source of truth; these checks reduce predictable 409 noise.
        if pred:
            try:
                p=assert_ok(c.get(f'/api/operations/{int(pred)}'),'OP-PRED')['item']
                if str(p.get('status') or '').upper()!='COMPLETED': continue
            except Exception: continue
        if source:
            try:
                s=assert_ok(c.get(f'/api/operations/{int(source)}'),'OP-SOURCE')['item']
                if int(s.get('done_qty') or 0)<=int(op.get('done_qty') or 0): continue
            except Exception: continue
        candidates.append(op)
    return candidates



def reconcile_state(c:MESClient,state:dict)->dict:
    """Remove stale local sessions that no longer exist on the live MES server."""
    local=state.setdefault('sessions',{})
    try:
        live=get_open_sessions(c)
    except Exception as exc:
        log('WARN','STATE-RECONCILE-SKIP',f'Không đọc được session server: {exc}')
        return state
    live_by_id={str(x.get('id')):x for x in live if x.get('id') is not None}
    removed=[]
    for sid,info in list(local.items()):
        row=live_by_id.get(str(sid))
        if not row or str(row.get('status') or '').upper()!='OPEN':
            removed.append({'session_id':sid,'operation_id':info.get('operation_id'),'reason':'not_open_on_server'})
            local.pop(sid,None)
            continue
        op_resp=c.get(f"/api/operations/{int(info.get('operation_id') or 0)}")
        po_resp=c.get(f"/api/production-orders/{int(info.get('po_id') or 0)}")
        if op_resp.status_code==404 or po_resp.status_code==404:
            removed.append({'session_id':sid,'operation_id':info.get('operation_id'),'po_id':info.get('po_id'),'reason':'missing_operation_or_po'})
            local.pop(sid,None)
    if removed:
        log('WARN','STATE-RECONCILE',f'Đã loại {len(removed)} session local mồ côi',removed=removed[:20])
    else:
        log('PASS','STATE-RECONCILE','State local khớp với session đang mở trên server',sessions=len(local))
    return state

def operation_cycle_seconds(op:dict, template:dict, fallback_seconds:float)->tuple[float,str]:
    value=float(op.get('standard_seconds_per_unit') or 0)
    if value>0:
        return value,'operation'
    code=str(op.get('code') or '').strip().upper()
    value=float((template.get('_cycle_times_by_code') or {}).get(code) or 0)
    if value>0:
        return value,'template'
    return max(1.0,float(fallback_seconds)),'fallback'

def choose_target_minutes(rng:random.Random,target_min:int,target_max:int)->int:
    lo=max(120,int(target_min)); hi=max(lo,min(1440,int(target_max)))
    # 72% session dài: tập trung 80-100% giới hạn trên, tức đa số gần 1 ngày khi max=1440.
    roll=rng.random()
    if roll<0.72:
        return rng.randint(max(lo,int(hi*0.80)),hi)
    if roll<0.90:
        return rng.randint(max(lo,int(hi*0.45)),max(lo,int(hi*0.80)))
    return rng.randint(lo,hi)

def plan_session(op:dict,po:dict,template:dict,rng:random.Random,target_min:int,target_max:int,variance_pct:float,anomaly_rate:float,anomaly_min:float,anomaly_max:float,fallback_cycle_seconds:float,available_minutes:int|None=None)->dict:
    cycle_seconds,source=operation_cycle_seconds(op,template,fallback_cycle_seconds)
    remaining=max(0,int(po.get('planned_quantity') or 0)-int(op.get('done_qty') or 0))
    if remaining<=0:
        return {'qty':0,'duration_minutes':0,'standard_minutes':0,'cycle_seconds':cycle_seconds,'cycle_source':source,'profile':'NO_WORK','variance':0}
    effective_max=int(target_max)
    if available_minutes is not None:
        effective_max=max(int(target_min),min(effective_max,int(available_minutes)))
    target_minutes=choose_target_minutes(rng,target_min,effective_max)
    # Số lượng được suy ra từ định mức API/Template, không random độc lập với thời gian.
    qty=max(1,int(round(target_minutes*60.0/cycle_seconds)))
    qty=min(remaining,qty)
    standard_minutes=max(1.0,qty*cycle_seconds/60.0)
    variance=max(0.0,min(0.30,float(variance_pct)/100.0))
    jitter=rng.uniform(1.0-variance,1.0+variance)
    profile='NORMAL'
    if rng.random()<max(0.0,min(0.20,anomaly_rate/100.0)):
        # Vẫn giữ trần 1 ngày; anomaly dùng để kiểm thử tốc độ bất thường chứ không tạo session vô hạn.
        jitter=rng.uniform(max(1.31,anomaly_min),max(max(1.31,anomaly_min),anomaly_max))
        profile=rng.choice(['MACHINE_JAM','MATERIAL_WAIT','FORGOT_FINISH'])
    raw_duration=int(round(standard_minutes*jitter))
    duration=max(120,min(effective_max,raw_duration))
    return {'qty':qty,'duration_minutes':duration,'standard_minutes':round(standard_minutes,2),'cycle_seconds':round(cycle_seconds,3),'cycle_source':source,'profile':profile,'variance':round(jitter-1.0,4),'target_minutes':target_minutes}

def start_session(c:MESClient,employee:dict,op:dict,station:dict,device:str,meta:dict,plan:dict,rng:random.Random,state:dict,cal:dict,shift_end:datetime,forgot_finish_rate:float)->bool:
    emp_qr=str(employee.get('qr') or f"WF|EMP|{employee['employee_no']}"); op_qr=str(op.get('qr') or f"WF|OP|{op['code']}")
    assert_ok(c.post('/api/kiosk-web/scan',{'qr':emp_qr}),'SCAN-EMP')
    assert_ok(c.post('/api/kiosk-web/scan',{'qr':op_qr}),'SCAN-OP')
    rid='QA-REAL-START-'+uuid.uuid4().hex
    r=c.post('/api/kiosk-web/start',{'request_id':rid,'employee_id':employee['id'],'operation_id':op['id'],'station_id':station['id'],'device_uuid':device})
    b=_response_body(r)
    if r.status_code==409:
        log('WARN','SES-START-SKIP',str(b.get('message') or b),employee=employee.get('employee_no'),operation=op.get('code')); return False
    assert_ok(r,'SES-START')
    sid=int(b['session']['id'])
    # P0 regression: replay same request_id must return the exact same session.
    replay=c.post('/api/kiosk-web/start',{'request_id':rid,'employee_id':employee['id'],'operation_id':op['id'],'station_id':station['id'],'device_uuid':device})
    rb=_response_body(replay)
    if replay.status_code in (200,201) and int((rb.get('session') or {}).get('id') or 0)==sid and rb.get('idempotent_replay'):
        regression_record(state,'P0-IDEMPOTENCY-START','PASS','Start replay không tạo session trùng',session_id=sid)
    else:
        regression_record(state,'P0-IDEMPOTENCY-START','FAIL','Start replay sai idempotency',session_id=sid,http=replay.status_code,response=rb)
    # P0 regression: same employee cannot open a second concurrent session with a new request id.
    dup=c.post('/api/kiosk-web/start',{'request_id':'QA-P0-DOUBLE-'+uuid.uuid4().hex,'employee_id':employee['id'],'operation_id':op['id'],'station_id':station['id'],'device_uuid':device})
    if 400<=dup.status_code<500:
        regression_record(state,'P0-DOUBLE-OPEN-BLOCK','PASS','Chặn employee mở session thứ hai',session_id=sid,http=dup.status_code)
    else:
        regression_record(state,'P0-DOUBLE-OPEN-BLOCK','FAIL','Server cho mở session trùng employee',session_id=sid,http=dup.status_code,response=_response_body(dup))
    started=now_local(); planned_due=min(shift_end,started+timedelta(minutes=int(plan['duration_minutes'])))
    forgot_finish=rng.random()<max(0.0,min(0.20,float(forgot_finish_rate)/100.0))
    due=next_day_entry_time(shift_end,cal,rng) if forgot_finish else planned_due
    profile='FORGOT_FINISH_NEXT_DAY' if forgot_finish else plan['profile']
    state['sessions'][str(sid)]={'employee_id':employee['id'],'employee_no':employee.get('employee_no'),'operation_id':op['id'],'operation_code':op.get('code'),'po_id':op.get('production_order_id'),'station_id':station['id'],'device':device,'due_at':iso(due),'shift_end_at':iso(shift_end),'forgot_finish':forgot_finish,'profile':profile,'planned_good_qty':int(plan['qty']),'standard_minutes':plan['standard_minutes'],'duration_minutes':int(plan['duration_minutes']),'cycle_seconds':plan['cycle_seconds'],'cycle_source':plan['cycle_source'],'variance':plan['variance']}
    sync_event(c,device,meta,'START',{'session_id':sid,'employee_id':employee['id'],'operation_id':op['id'],'station_id':station['id']})
    log('PASS','SES-REAL-START',f'{employee.get("employee_no")} bắt đầu {op.get("code")}',quantity=plan['qty'],cycle_seconds=plan['cycle_seconds'],standard_minutes=plan['standard_minutes'],duration_minutes=plan['duration_minutes'],variance_percent=round(plan['variance']*100,1),profile=profile,cycle_source=plan['cycle_source'],due_at=iso(due),shift_end_at=iso(shift_end),forgot_finish=forgot_finish,station=station.get('code'))
    return True

def discover_finish_quality_fields(c:MESClient)->dict[str,str]:
    """Best-effort schema discovery for the evolving repairable/fixable quantity field."""
    defaults={'good':'good_qty','defect':'defect_qty','fixable':'repairable_qty'}
    for path in ('/openapi.json','/api/openapi.json'):
        r=c.get(path)
        if r.status_code!=200:
            continue
        try: spec=r.json()
        except Exception: continue
        text=json.dumps(spec,ensure_ascii=False).lower()
        finish_path=next((k for k in (spec.get('paths') or {}) if '/api/kiosk-web/finish/' in k),None)
        props={}
        if finish_path:
            post=((spec.get('paths') or {}).get(finish_path) or {}).get('post') or {}
            schema=((((post.get('requestBody') or {}).get('content') or {}).get('application/json') or {}).get('schema') or {})
            if '$ref' in schema:
                name=str(schema['$ref']).split('/')[-1]
                schema=(((spec.get('components') or {}).get('schemas') or {}).get(name) or {})
            props=schema.get('properties') or {}
        keys=[str(k) for k in props]
        aliases=['repairable_qty','fixable_defect_qty','fixable_qty','rework_qty','repair_qty','defect_fixable_qty']
        match=next((k for k in keys if k.lower() in aliases),None)
        if not match:
            match=next((k for k in keys if any(t in k.lower() for t in ('repair','fixable','rework'))),None)
        if match:
            log('PASS','SCHEMA-FIXABLE',f'Phát hiện field lỗi sửa được: {match}',source=path)
            return {'good':'good_qty','defect':'defect_qty','fixable':match}
        if 'repairable_qty' in text:
            return defaults
    log('WARN','SCHEMA-FIXABLE-FALLBACK','Không thấy schema field lỗi sửa được; dùng repairable_qty làm mặc định')
    return defaults

def quality_quantities(good:int,rng:random.Random)->tuple[int,int]:
    if good<=0: return 0,0
    # Khoảng 12% session có lỗi; lỗi sửa được là một phần của tổng lỗi.
    if rng.random()>=0.12: return 0,0
    defect=max(1,min(good,max(1,int(round(good*rng.uniform(0.01,0.05))))))
    fixable=rng.randint(1,defect) if defect and rng.random()<0.70 else 0
    return defect,fixable

def maybe_test_invalid_finish(c:MESClient,sid:str,op:dict,po:dict,info:dict,fields:dict[str,str],rng:random.Random)->str|None:
    """Inject one intentionally-invalid finish before the real finish. Expected result is 4xx + error log."""
    if rng.random()>=0.10:
        return None
    remaining=max(0,int(po.get('planned_quantity') or 0)-int(op.get('done_qty') or 0))
    cases=[]
    cases.append(('NEGATIVE_QTY',{fields['good']:-1,fields['defect']:0,fields['fixable']:0}))
    cases.append(('FIXABLE_GT_DEFECT',{fields['good']:max(1,min(remaining,1)),fields['defect']:1,fields['fixable']:2}))
    cases.append(('OUTPUT_OVER_PLAN',{fields['good']:remaining+max(10,int(po.get('planned_quantity') or 1)),fields['defect']:0,fields['fixable']:0}))
    if op.get('input_flow_enabled') and op.get('input_source_operation_id'):
        src=c.get(f"/api/operations/{int(op['input_source_operation_id'])}")
        if src.status_code in (200,201):
            src_item=(_response_body(src).get('item') or {})
            available=max(0,int(src_item.get('done_qty') or 0)-int(op.get('done_qty') or 0))
            # Cố tình cao hơn input thực có nhưng không nhất thiết vượt planned_quantity.
            bad_input=max(available+1,1)
            cases.append(('INPUT_MISMATCH',{fields['good']:bad_input,fields['defect']:0,fields['fixable']:0}))
    name,payload=rng.choice(cases)
    payload.update({'request_id':'QA-INVALID-'+uuid.uuid4().hex,'note':f'QA EXPECT_REJECT {name}'})
    r=c.post(f'/api/kiosk-web/finish/{sid}',payload)
    body=_response_body(r)
    if 400<=r.status_code<500:
        log('PASS','NEGATIVE-CASE-REJECTED',f'{name} bị từ chối đúng',session_id=sid,http=r.status_code,error=body)
        return name
    log('FAIL','NEGATIVE-CASE-ACCEPTED',f'{name} không bị từ chối',session_id=sid,http=r.status_code,response=body,payload=payload)
    return 'ACCEPTED:'+name

def inspect_error_logs(c:MESClient):
    for path in ('/api/action-error-logs?limit=100','/api/logs/action-errors?limit=100','/api/audit/action-errors?limit=100'):
        r=c.get(path)
        if r.status_code in (200,201):
            body=_response_body(r); items=body.get('items') or body.get('logs') or []
            hits=[x for x in items if 'QA EXPECT_REJECT' in json.dumps(x,ensure_ascii=False)]
            log('PASS' if hits else 'WARN','NEGATIVE-LOG-CHECK',f'{path}: tìm thấy {len(hits)} log lỗi QA',matches=len(hits))
            return
        if r.status_code not in (404,405):
            log('WARN','NEGATIVE-LOG-CHECK',f'{path}: HTTP {r.status_code}',error=_response_body(r))
    log('WARN','NEGATIVE-LOG-CHECK','Server chưa expose API action-error-log mà QA nhận diện được')

def finish_due(c:MESClient,state:dict,rng:random.Random,kiosks:dict[str,dict],batch_min:int,batch_max:int,fields:dict[str,str])->int:
    finished=0
    for sid,info in list(state.get('sessions',{}).items()):
        if datetime.fromisoformat(info['due_at'])>now_local(): continue
        op_resp=c.get(f"/api/operations/{int(info['operation_id'])}")
        po_resp=c.get(f"/api/production-orders/{int(info['po_id'])}")
        if op_resp.status_code==404 or po_resp.status_code==404:
            log('WARN','ORPHAN-SESSION','Bỏ session local vì Operation hoặc PO không còn trên server',session_id=sid,operation_id=info.get('operation_id'),po_id=info.get('po_id'),op_status=op_resp.status_code,po_status=po_resp.status_code)
            state['sessions'].pop(sid,None)
            continue
        op=assert_ok(op_resp,'OP-REFRESH')['item']
        po=assert_ok(po_resp,'PO-REFRESH')['item']
        remaining=max(0,int(po.get('planned_quantity') or 0)-int(op.get('done_qty') or 0))
        if remaining<=0: good=0; defect=0; fixable=0
        else:
            good=min(remaining,max(1,int(info.get('planned_good_qty') or batch_min)))
            defect,fixable=quality_quantities(good,rng)
            if info.get('profile') in ('MACHINE_JAM','MATERIAL_WAIT') and rng.random()<0.30:
                defect=max(defect,1); fixable=max(fixable,1 if rng.random()<0.65 else 0)
        if 0 <= fixable <= defect and good >= 0:
            regression_record(state,'P0-QUALITY-BOUNDS','PASS','good/defect/repairable hợp lệ trước finish',session_id=sid,good=good,defect=defect,repairable=fixable)
        else:
            regression_record(state,'P0-QUALITY-BOUNDS','FAIL','Quantity quality vi phạm invariant trước finish',session_id=sid,good=good,defect=defect,repairable=fixable)
        invalid=maybe_test_invalid_finish(c,sid,op,po,info,fields,rng)
        if invalid and invalid.startswith('ACCEPTED:'):
            state['sessions'].pop(sid,None); finished+=1; continue
        fid='QA-REAL-FINISH-'+uuid.uuid4().hex
        payload={'request_id':fid,fields['good']:good,fields['defect']:defect,fields['fixable']:fixable,'note':f"QA realistic {info.get('profile')}"}
        r=c.post(f'/api/kiosk-web/finish/{sid}',payload)
        b=_response_body(r)
        if r.status_code not in (200,201):
            log('WARN','SES-FINISH-RETRY',str(b),session_id=sid); continue
        # P0 regression: finish replay with the same request_id must be idempotent.
        replay=c.post(f'/api/kiosk-web/finish/{sid}',payload)
        rb=_response_body(replay)
        if replay.status_code in (200,201) and int((rb.get('session') or {}).get('id') or 0)==int(sid) and rb.get('idempotent_replay'):
            regression_record(state,'P0-IDEMPOTENCY-FINISH','PASS','Finish replay không cộng aggregate lần hai',session_id=sid)
        else:
            regression_record(state,'P0-IDEMPOTENCY-FINISH','FAIL','Finish replay sai idempotency',session_id=sid,http=replay.status_code,response=rb)
        # New request_id against an already CLOSED session must not mutate production again.
        closed_payload=dict(payload); closed_payload['request_id']='QA-P0-REFINISH-'+uuid.uuid4().hex
        closed=c.post(f'/api/kiosk-web/finish/{sid}',closed_payload)
        if 400<=closed.status_code<500:
            regression_record(state,'P0-CLOSED-REFINISH-BLOCK','PASS','Finish CLOSED session với request mới bị chặn',session_id=sid,http=closed.status_code)
        else:
            regression_record(state,'P0-CLOSED-REFINISH-BLOCK','FAIL','Server nhận finish lần hai với request mới',session_id=sid,http=closed.status_code,response=_response_body(closed))
        device=info['device']; sync_event(c,device,kiosks[device],'FINISH',{'session_id':int(sid),'employee_id':info['employee_id'],'operation_id':info['operation_id'],'good_qty':good,'defect_qty':defect,'repairable_qty':fixable})
        log('PASS','SES-REAL-FINISH',f"Kết thúc {info['operation_code']}",session_id=sid,good_qty=good,defect_qty=defect,repairable_qty=fixable,profile=info.get('profile'),forgot_finish=bool(info.get('forgot_finish')),shift_end_at=info.get('shift_end_at'))
        del state['sessions'][sid]; finished+=1
    return finished

def inspect_reports(c:MESClient,pos:list[dict],state:dict):
    endpoints=['/api/dashboard/summary','/api/dashboard/control-tower?limit=500','/api/dashboard/active-sessions?limit=500','/api/dashboard/daily-progress?limit=1000','/api/dashboard/daily-sessions?limit=3000','/api/dashboard/recent-activity?limit=300','/api/production-schedule?limit=500','/api/kiosk-management/overview','/api/reports/operation-sessions?limit=5000','/api/reports/employee-performance?limit=10000','/api/kpi/employees?limit=1000','/api/kpi/operations?limit=1000']
    failures=[]
    report_failures=state.setdefault('report_failures',{})
    for path in endpoints:
        response=c.get(path)
        body=_response_body(response)
        key=path.split('?')[0]
        if response.status_code not in (200,201):
            report_failures[key]=int(report_failures.get(key,0))+1
            failures.append({'endpoint':key,'http':response.status_code,'body':body,'count':report_failures[key]})
            log('WARN','UI-REPORT-ERROR',f'{key}: HTTP {response.status_code}',endpoint=key,error=body,occurrences=report_failures[key])
        else:
            log('PASS','UI-REPORT',f'{key}: HTTP {response.status_code}')
    for po in pos[:3]:
        path=f"/api/reports/production-orders/{int(po['id'])}"
        response=c.get(path); body=_response_body(response)
        if response.status_code not in (200,201):
            report_failures[path]=int(report_failures.get(path,0))+1
            failures.append({'endpoint':path,'http':response.status_code,'body':body,'count':report_failures[path]})
            log('WARN','PO-REPORT-ERROR',f'{po.get("code")}: HTTP {response.status_code}',error=body,occurrences=report_failures[path])
    state['last_report_at']=iso(now_local())
    state['last_report_failures']=failures
    if failures:
        log('WARN','UI-REPORT-SNAPSHOT',f'Có {len(failures)} report lỗi; mô phỏng vẫn tiếp tục',failures=failures)
    else:
        log('PASS','UI-REPORT-SNAPSHOT','Đã kiểm tra dashboard, timeline, kiosk management và report')

def _mark_pause(state:dict, reason:str):
    now=now_local()
    if not state.get("pause_started_at"):
        state["pause_started_at"]=iso(now)
    state["runtime_status"]="WAITING_MESFLOW"
    state["pause_reason"]=reason
    state["last_tick_at"]=iso(now)
    save_state(state)


def _clear_pause(state:dict):
    raw=state.pop("pause_started_at",None)
    if raw:
        try:
            seconds=max(0.0,(now_local()-datetime.fromisoformat(raw)).total_seconds())
            state["paused_seconds"]=float(state.get("paused_seconds",0.0))+seconds
        except Exception:
            pass
    state["runtime_status"]="RUNNING"
    state["pause_reason"]=""
    state["last_tick_at"]=iso(now_local())
    save_state(state)


def _campaign_end(state:dict, run_days:int)->datetime:
    started=datetime.fromisoformat(state.get("started_at") or iso(now_local()))
    return started+timedelta(days=max(1,run_days),seconds=float(state.get("paused_seconds",0.0)))


def _account_restart_gap(state:dict, tick_seconds:int):
    """Exclude QA Center/container downtime from the campaign duration."""
    last=state.get("last_tick_at")
    if not last:
        return
    try:
        gap=max(0.0,(now_local()-datetime.fromisoformat(last)).total_seconds())
    except Exception:
        return
    threshold=max(30,int(tick_seconds)*2)
    if gap>threshold:
        state["paused_seconds"]=float(state.get("paused_seconds",0.0))+gap
        log("WARN","SIM-RESUME-GAP","QA Center đã restart/deploy; loại thời gian gián đoạn khỏi campaign",paused_seconds=round(gap,1))


def _connect_context(args, state, rng):
    retry=max(15,int(os.environ.get("MESFLOW_QA_MES_RETRY_SECONDS","30")))
    attempt=0
    while True:
        try:
            c=MESClient(args.base_url,args.username,args.password,args.verify_ssl,args.fallback_base_url)
            assert_ok(c.get('/api/system/health'),'SYS-001 Health')
            assert_ok(c.get('/api/execution/health'),'SYS-002 Execution')
            employees=find_or_create_employees(c,max(2,min(args.workers,30)))
            stations=ensure_stations(c,len(employees))
            kiosks=bind_kiosks(c,stations)
            template=get_template(c)
            cal=calendar(c)
            finish_fields=discover_finish_quality_fields(c)
            reconcile_state(c,state)
            _clear_pause(state)
            if attempt:
                log("PASS","MES-RECOVERED","MESFlow đã hoạt động lại; tiếp tục campaign",attempts=attempt)
            return c,employees,stations,kiosks,template,cal,finish_fields
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            attempt+=1
            _mark_pause(state,f"{type(exc).__name__}: {exc}")
            log("WARN","MES-WAIT",f"MESFlow chưa sẵn sàng; QA sẽ chờ và tự thử lại sau {retry}s",attempt=attempt,error=f"{type(exc).__name__}: {exc}")
            time.sleep(retry)


def main()->int:
    p=argparse.ArgumentParser()
    p.add_argument('--base-url',required=True); p.add_argument('--fallback-base-url',default=''); p.add_argument('--username',default='admin'); p.add_argument('--password',default='')
    p.add_argument('--workers',type=int,default=20); p.add_argument('--target-active-pos',type=int,default=2)
    p.add_argument('--planned-quantity-min',type=int,default=500); p.add_argument('--planned-quantity-max',type=int,default=1200)
    p.add_argument('--run-days',type=int,default=7); p.add_argument('--tick-seconds',type=int,default=60)
    p.add_argument('--session-target-minutes-min',type=int,default=120); p.add_argument('--session-target-minutes-max',type=int,default=1440)
    p.add_argument('--normal-variance-percent',type=float,default=30.0)
    p.add_argument('--anomaly-rate-percent',type=float,default=2.0); p.add_argument('--anomaly-multiplier-min',type=float,default=1.8); p.add_argument('--anomaly-multiplier-max',type=float,default=4.0)
    p.add_argument('--fallback-cycle-seconds',type=float,default=300.0)
    p.add_argument('--batch-qty-min',type=int,default=1); p.add_argument('--batch-qty-max',type=int,default=9999)
    p.add_argument('--report-interval-minutes',type=int,default=60); p.add_argument('--seed',type=int,default=65817)
    p.add_argument('--night-shift-start',default='19:00'); p.add_argument('--night-shift-end',default='07:00'); p.add_argument('--forgot-finish-rate-percent',type=float,default=4.0)
    p.add_argument('--verify-ssl',action='store_true'); p.add_argument('--reset-state',action='store_true')
    p.add_argument('--p0-p3-regression',action=argparse.BooleanOptionalAction,default=True,help='Chạy probe regression P0/P1/P2/P3 trong soak')
    p.add_argument('--strict-p0-p1',action=argparse.BooleanOptionalAction,default=True,help='Kết thúc FAIL nếu P0/P1 bắt buộc có FAIL hoặc chưa từng được cover')
    args=p.parse_args()
    args.planned_quantity_min=max(500,args.planned_quantity_min)
    args.planned_quantity_max=max(args.planned_quantity_min,args.planned_quantity_max)
    args.normal_variance_percent=max(0.0,min(30.0,args.normal_variance_percent))
    args.session_target_minutes_min=max(120,min(1440,args.session_target_minutes_min))
    args.session_target_minutes_max=max(args.session_target_minutes_min,min(1440,args.session_target_minutes_max))
    args.forgot_finish_rate_percent=max(0.0,min(20.0,args.forgot_finish_rate_percent))
    try:
        if args.reset_state and STATE_FILE.exists(): STATE_FILE.unlink()
        rng=random.Random(args.seed)
        state=load_state(); state.setdefault('sessions',{}); state.setdefault('completed_pos',[]); state.setdefault('cycle',0); state.setdefault('paused_seconds',0.0)
        _account_restart_gap(state,args.tick_seconds)
        state['runtime_status']='STARTING'; state['last_tick_at']=iso(now_local()); save_state(state)
        c,employees,stations,kiosks,template,cal,finish_fields=_connect_context(args,state,rng)
        started=datetime.fromisoformat(state.get('started_at') or iso(now_local())); end_at=_campaign_end(state,args.run_days)
        log('PASS','SIM-START','Mô phỏng thời gian thật bắt đầu',started_at=iso(started),end_at=iso(end_at),workers=len(employees),active_pos=args.target_active_pos,seed=args.seed,planned_quantity_range=[args.planned_quantity_min,args.planned_quantity_max],duration_model='qty × standard_seconds_per_unit ±30%',anomaly_rate_percent=args.anomaly_rate_percent,session_minutes_range=[args.session_target_minutes_min,args.session_target_minutes_max],session_policy='close_by_shift_end',forgot_finish_rate_percent=args.forgot_finish_rate_percent,night_shift=[args.night_shift_start,args.night_shift_end],fixable_field=finish_fields['fixable'])
        while now_local()<_campaign_end(state,args.run_days):
            end_at=_campaign_end(state,args.run_days)
            state['cycle']=int(state.get('cycle',0))+1
            state['last_tick_at']=iso(now_local()); state['runtime_status']='RUNNING'; save_state(state)
            try:
                heartbeat(c,kiosks,rng)
                pos=ensure_po_pool(c,template,max(2,args.target_active_pos),args.planned_quantity_min,args.planned_quantity_max,rng)
                finish_due(c,state,rng,kiosks,args.batch_qty_min,args.batch_qty_max,finish_fields)
                # Completed PO disappear naturally; replacement is opened on next ensure_po_pool.
                pos=ensure_po_pool(c,template,max(2,args.target_active_pos),args.planned_quantity_min,args.planned_quantity_max,rng)
                now=now_local()
                if is_work_time(now,cal,args.night_shift_start,args.night_shift_end):
                    open_items=get_open_sessions(c); busy_emp={int(x['employee_id']) for x in open_items if str(x.get('status') or '').upper()=='OPEN'}; busy_ops={int(x['operation_id']) for x in open_items if str(x.get('status') or '').upper()=='OPEN'}
                    ops=[x for x in eligible_ops(c,pos) if int(x['id']) not in busy_ops]; rng.shuffle(ops)
                    free=[e for e in employees if int(e['id']) not in busy_emp]; rng.shuffle(free)
                    for employee,op in zip(free,ops):
                        idx=employees.index(employee)%len(stations)
                        po=next((p for p in pos if int(p['id'])==int(op.get('production_order_id') or 0)),None)
                        if not po: continue
                        shift_end=current_shift_end(now,cal,args.night_shift_start,args.night_shift_end)
                        if not shift_end:
                            continue
                        available_minutes=int((shift_end-now).total_seconds()//60)
                        if available_minutes<args.session_target_minutes_min:
                            log('INFO','SHIFT-NO-NEW-SESSION','Gần cuối ca; không mở session mới để tránh treo qua ca',available_minutes=available_minutes,minimum_minutes=args.session_target_minutes_min,shift_end=iso(shift_end))
                            break
                        refreshed=assert_ok(c.get(f"/api/operations/{int(op['id'])}"),'OP-TIME Read operation')['item']
                        plan=plan_session(refreshed,po,template,rng,args.session_target_minutes_min,args.session_target_minutes_max,args.normal_variance_percent,args.anomaly_rate_percent,args.anomaly_multiplier_min,args.anomaly_multiplier_max,args.fallback_cycle_seconds,available_minutes=available_minutes)
                        if plan['cycle_source']=='fallback':
                            log('WARN','OP-TIME-FALLBACK','Operation chưa có định mức; dùng thời gian dự phòng',operation=refreshed.get('code'),fallback_seconds=args.fallback_cycle_seconds)
                        start_session(c,employee,refreshed,stations[idx],list(kiosks.keys())[idx],list(kiosks.values())[idx],plan,rng,state,cal,shift_end,args.forgot_finish_rate_percent)
                else:
                    nxt=next_work_start(now,cal); log('INFO','SHIFT-WAIT','Ngoài ca ngày/ca tối; không mở session mới',next_work_start=iso(nxt),night_shift=[args.night_shift_start,args.night_shift_end])
                last=state.get('last_report_at')
                if not last or now-datetime.fromisoformat(last)>=timedelta(minutes=max(5,args.report_interval_minutes)):
                    inspect_reports(c,pos,state)
                    inspect_error_logs(c)
                    if args.p0_p3_regression:
                        run_periodic_regression(c,state,pos,employees,PREFIX)
                save_state(state)
                time.sleep(max(15,args.tick_seconds))
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                _mark_pause(state,f'{type(exc).__name__}: {exc}')
                log('WARN','MES-WAIT-TICK',f'MESFlow lỗi/mất kết nối; giữ state và chờ reconnect: {type(exc).__name__}: {exc}')
                c,employees,stations,kiosks,template,cal,finish_fields=_connect_context(args,state,rng)
                continue
        final_pos=active_qa_pos(c); inspect_reports(c,final_pos,state)
        if args.p0_p3_regression:
            run_periodic_regression(c,state,final_pos,employees,PREFIX)
        summary=campaign_summary(state); state['regression_summary']=summary; state['runtime_status']='COMPLETED'; state['last_tick_at']=iso(now_local()); save_state(state)
        log('PASS' if not summary['failed_required'] else 'FAIL','P0-P3-COVERAGE','Tổng kết regression P0-P3',required_total=summary['required_total'],covered_total=summary['covered_total'],missing_required=summary['missing_required'],failed_required=summary['failed_required'])
        log('PASS','SIM-END','Đã kết thúc thời gian mô phỏng',days=args.run_days,cycles=state.get('cycle'),open_sessions=len(state.get('sessions',{})),forgotten_open_sessions=sum(1 for x in state.get('sessions',{}).values() if x.get('forgot_finish')))
        if args.strict_p0_p1 and (summary['failed_required'] or summary['missing_required']):
            return 2
        return 0
    except KeyboardInterrupt:
        log('WARN','SIM-STOP','Dừng bởi người dùng'); return 0
    except Exception as exc:
        log('FAIL','FATAL',f'{type(exc).__name__}: {exc}'); return 1
if __name__=='__main__': raise SystemExit(main())
