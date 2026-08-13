from __future__ import annotations
import argparse, json, random, sys, time, uuid
from datetime import datetime
from typing import Any
import requests

PREFIX="QAV65813-"

def log(level:str, case:str, message:str, **data):
    suffix=(" | "+json.dumps(data,ensure_ascii=False,default=str)) if data else ""
    print(f"[{level}] {case}: {message}{suffix}", flush=True)

def assert_ok(resp:requests.Response, case:str, expected=(200,201)) -> dict[str,Any]:
    try: body=resp.json()
    except Exception: body={"raw":resp.text[:500]}
    if resp.status_code not in expected or body.get("ok") is False:
        raise AssertionError(f"{case}: HTTP {resp.status_code} {body}")
    log("PASS",case,f"HTTP {resp.status_code}")
    return body

class MESClient:
    def __init__(self,base:str,username:str,password:str,verify_ssl:bool=False):
        self.base=base.rstrip('/'); self.s=requests.Session(); self.s.verify=verify_ssl
        self.s.headers.update({'Accept':'application/json','User-Agent':'MESFlow-QA-Center/1.14-v65.8.13'})
        if not verify_ssl:
            try: requests.packages.urllib3.disable_warnings()
            except Exception: pass
        if not password:
            raise AssertionError('AUTH-000 Password required: production auto-login is disabled')
        r=self.s.post(self.base+'/api/auth/login',json={'username':username,'password':password},timeout=30)
        assert_ok(r,'AUTH-001 Login')
        assert_ok(self.s.get(self.base+'/api/auth/me',timeout=30),'AUTH-002 Session')
    def get(self,path,**kw): return self.s.get(self.base+path,timeout=30,**kw)
    def post(self,path,payload=None,**kw): return self.s.post(self.base+path,json=payload or {},timeout=30,**kw)
    def patch(self,path,payload=None,**kw): return self.s.patch(self.base+path,json=payload or {},timeout=30,**kw)
    def delete(self,path,payload=None,**kw): return self.s.delete(self.base+path,json=payload or {},timeout=30,**kw)

def find_or_create_employees(c:MESClient,count:int)->list[dict]:
    items=assert_ok(c.get('/api/employees?limit=1000'),'EMP-001 List employees').get('items',[])
    active=[x for x in items if x.get('active')]
    seq=1
    while len(active)<count:
        code=f'{PREFIX}EMP-{seq:03d}'; seq+=1
        if any(str(x.get('employee_no'))==code for x in items): continue
        body=assert_ok(c.post('/api/employees',{'employee_no':code,'name':['Nguyễn Văn Hùng','Trần Minh Tuấn','Lê Quốc Bảo','Phạm Hoàng Nam','Võ Thanh Tùng','Đặng Văn Phúc','Bùi Đức Anh','Hoàng Minh Khang'][(seq-2)%8],'department':'Xưởng Cơ khí Demo','team':'Tổ Sản xuất','position':'Công nhân vận hành','employment_status':'Đang làm','active':True,'qr':f'WF|EMP|{code}'}),'EMP-002 Create employee')
        active.append(body['item']); items.append(body['item'])
    return active[:count]

def get_template(c:MESClient)->dict:
    assert_ok(c.post('/api/templates/demo/seed'),'TPL-001 Seed demo templates')
    items=assert_ok(c.get('/api/templates/available-for-po'),'TPL-002 List templates').get('items',[])
    usable=[x for x in items if int(x.get('part_count') or 0)>0 and int(x.get('operation_count') or 0)>0]
    if not usable: raise AssertionError('TPL-003: Không có template dùng được')
    usable.sort(key=lambda x:(int(x.get('operation_count') or 0),int(x.get('part_count') or 0)))
    chosen=usable[0]
    log('PASS','TPL-003 Select template',str(chosen.get('code')),template_id=chosen.get('id'),operations=chosen.get('operation_count'))
    return chosen

def create_po(c:MESClient,template:dict,index:int,plan:int)->dict:
    code=f"{PREFIX}{datetime.now().strftime('%y%m%d%H%M%S')}-{index}-{uuid.uuid4().hex[:4].upper()}"
    payload={'template_id':template['id'],'code':code,'planned_quantity':plan,'priority':'NORMAL','notes':'QA lifecycle v65.8.13'}
    body=assert_ok(c.post('/api/production-orders',payload),f'PO-{index:02d}-001 Create PO')
    po_id=int(body.get('production_order_id') or body.get('id'))
    assert_ok(c.post(f'/api/production-orders/{po_id}/start'),f'PO-{index:02d}-002 Start PO')
    po=assert_ok(c.get(f'/api/production-orders/{po_id}'),f'PO-{index:02d}-003 Read PO')['item']
    if str(po.get('status')).upper()!='IN_PROGRESS': raise AssertionError(f'PO chưa IN_PROGRESS: {po}')
    return {'id':po_id,'code':code,'plan':plan}

def operations_for_po(c:MESClient,po_id:int)->list[dict]:
    items=assert_ok(c.get('/api/operations?limit=1000'),'OP-001 List operations').get('items',[])
    ops=[x for x in items if int(x.get('production_order_id') or 0)==po_id]
    ops.sort(key=lambda x:(int(x.get('sort_order') or 0),int(x.get('id') or 0)))
    if not ops: raise AssertionError(f'Không có Operation cho PO {po_id}')
    return ops

def _response_body(resp:requests.Response)->dict[str,Any]:
    try: return resp.json()
    except Exception: return {"raw":resp.text[:500]}

def open_employee_ids(c:MESClient)->set[int]:
    body=assert_ok(c.get('/api/work-sessions'),'SES-MAP-001 Read open sessions')
    return {int(x['employee_id']) for x in body.get('items',[]) if str(x.get('status') or '').upper()=='OPEN'}

def scan_and_run(c:MESClient,employees:list[dict],op:dict,qty:int,case_prefix:str,preferred_index:int=0):
    if not employees: raise AssertionError('Không có nhân viên để chạy Operation')
    op_qr=str(op.get('qr') or f"WF|OP|{op['code']}")
    busy=open_employee_ids(c)
    ordered=employees[preferred_index%len(employees):]+employees[:preferred_index%len(employees)]
    candidates=[e for e in ordered if int(e['id']) not in busy]
    skipped=[str(e.get('employee_no') or e.get('id')) for e in ordered if int(e['id']) in busy]
    if skipped: log('WARN',case_prefix+' Employee map','Bỏ qua nhân viên đang có session OPEN',employees=skipped)
    if not candidates:
        raise AssertionError('SES-MAP-409: Tất cả nhân viên đang có session OPEN; QA không đóng phiên thật của hệ thống')

    selected=None; b=None; rid=None
    for employee in candidates:
        emp_code=str(employee.get('employee_no') or employee.get('id'))
        emp_qr=str(employee.get('qr') or f"WF|EMP|{employee['employee_no']}")
        scan=assert_ok(c.post('/api/kiosk-web/scan',{'qr':emp_qr}),case_prefix+f' Scan employee {emp_code}')
        if scan.get('type')!='employee':
            log('WARN',case_prefix+' Employee rejected','Kiosk không nhận dạng employee; thử người kế tiếp',employee=emp_code)
            continue
        scan_op=assert_ok(c.post('/api/kiosk-web/scan',{'qr':op_qr}),case_prefix+' Scan operation')
        if scan_op.get('type')!='operation': raise AssertionError('Kiosk không nhận dạng operation')
        rid='QA-START-'+uuid.uuid4().hex
        resp=c.post('/api/kiosk-web/start',{'request_id':rid,'employee_id':employee['id'],'operation_id':op['id'],'device_uuid':'QA-EXTERNAL-KIOSK'})
        body=_response_body(resp)
        if resp.status_code in (200,201) and body.get('ok') is not False:
            log('PASS',case_prefix+f' Start session [{emp_code}]',f'HTTP {resp.status_code}')
            selected=employee; b=body; break
        message=str(body.get('message') or body.get('error') or '')
        if resp.status_code==409 and ('open session' in message.lower() or body.get('error_code')=='SES-409'):
            log('WARN',case_prefix+' Employee busy race','Nhân viên vừa phát sinh session OPEN; chuyển người khác',employee=emp_code,http=resp.status_code,error_code=body.get('error_code'))
            continue
        raise AssertionError(f"{case_prefix} Start session [{emp_code}]: HTTP {resp.status_code} {body}")
    if not selected or not b or not rid:
        raise AssertionError('SES-MAP-409: Không tìm được nhân viên rảnh sau khi thử toàn bộ danh sách')
    employee=selected
    session_id=int(b['session']['id'])
    # idempotency replay must return same session
    replay=assert_ok(c.post('/api/kiosk-web/start',{'request_id':rid,'employee_id':employee['id'],'operation_id':op['id'],'device_uuid':'QA-EXTERNAL-KIOSK'}),case_prefix+' Start idempotency')
    if int(replay['session']['id'])!=session_id or not replay.get('idempotent_replay'):
        raise AssertionError('Start idempotency sai')
    assert_ok(c.get('/api/dashboard/active-sessions'),case_prefix+' Active session visible')
    fid='QA-FINISH-'+uuid.uuid4().hex
    b=assert_ok(c.post(f'/api/kiosk-web/finish/{session_id}',{'request_id':fid,'good_qty':qty,'defect_qty':0,'note':'QA v65.8.13'}),case_prefix+' Finish session')
    replay=assert_ok(c.post(f'/api/kiosk-web/finish/{session_id}',{'request_id':fid,'good_qty':qty,'defect_qty':0,'note':'QA v65.8.13'}),case_prefix+' Finish idempotency')
    if int(replay['session']['id'])!=session_id or not replay.get('idempotent_replay'):
        raise AssertionError('Finish idempotency sai')
    return session_id

def complete_po(c:MESClient,po:dict,employees:list[dict]):
    ops=operations_for_po(c,po['id'])
    completed=set()
    pending=ops[:]
    safety=0
    while pending and safety<500:
        safety+=1; progress=False
        for op in pending[:]:
            pred=op.get('predecessor_operation_id')
            if pred and int(pred) not in completed: continue
            source=op.get('input_source_operation_id') if op.get('input_flow_enabled') else None
            if source and int(source) not in completed: continue
            refreshed=assert_ok(c.get(f"/api/operations/{op['id']}"),'OP-002 Read operation')['item']
            remaining=max(0,int(po['plan'])-int(refreshed.get('done_qty') or 0))
            if remaining:
                emp=employees[len(completed)%len(employees)]
                scan_and_run(c,employees,refreshed,remaining,f"RUN-{po['code']}-{refreshed['code']}",preferred_index=len(completed))
            final=assert_ok(c.get(f"/api/operations/{op['id']}"),'OP-003 Verify operation')['item']
            if str(final.get('status')).upper()!='COMPLETED': raise AssertionError(f"Operation chưa COMPLETED: {final}")
            completed.add(int(op['id'])); pending.remove(op); progress=True
        if not progress:
            raise AssertionError('Không thể tiếp tục do dependency/input flow không hợp lệ: '+','.join(str(x.get('code')) for x in pending))
    po_final=assert_ok(c.get(f"/api/production-orders/{po['id']}"),'PO-004 Verify completed')['item']
    if str(po_final.get('status')).upper()!='COMPLETED': raise AssertionError(f"PO chưa COMPLETED: {po_final}")
    return len(ops)

def verify_dashboard(c:MESClient,pos:list[dict]):
    items=assert_ok(c.get('/api/dashboard/production-orders?limit=500'),'DASH-001 PO progress').get('items',[])
    # v65.8.13 endpoint currently returns all PO; this is a product defect if completed QA PO is present.
    visible={str(x.get('code')):str(x.get('status')) for x in items}
    bad=[p['code'] for p in pos if p['code'] in visible and visible[p['code']].upper()=='COMPLETED']
    if bad:
        raise AssertionError('DASH-002 dashboard_completed_po_visible: '+','.join(bad))
    log('PASS','DASH-002 Completed PO hidden','Không còn PO hoàn tất trong dashboard active')

def verify_timeline(c:MESClient,session_ids:list[int]):
    day=datetime.now().strftime('%Y-%m-%d')
    items=assert_ok(c.get('/api/dashboard/daily-sessions',params={'date':day,'limit':3000}),'TIME-001 Daily sessions').get('items',[])
    found={int(x['session_id']) for x in items if x.get('session_id') is not None}
    missing=[x for x in session_ids if x not in found]
    if missing: raise AssertionError(f'TIME-002 thiếu session trên timeline: {missing[:10]}')
    log('PASS','TIME-002 Session timeline complete',f'{len(session_ids)} session được hiển thị')

def negative_cases(c:MESClient,employee:dict,op:dict):
    r=c.post('/api/kiosk-web/scan',{'qr':'INVALID-QR'})
    if r.status_code!=400 or r.json().get('error_code')!='SCN-002': raise AssertionError(f'NEG-001 sai phản hồi QR: {r.status_code} {r.text}')
    log('PASS','NEG-001 Invalid QR','SCN-002')
    r=c.post('/api/kiosk-web/scan',{'qr':'WF|OP|NOT-FOUND'})
    if r.status_code!=404 or r.json().get('error_code')!='OP-001': raise AssertionError(f'NEG-002 sai phản hồi OP: {r.status_code} {r.text}')
    log('PASS','NEG-002 Unknown OP','OP-001')

def main()->int:
    p=argparse.ArgumentParser()
    p.add_argument('--base-url',required=True); p.add_argument('--username',default='admin'); p.add_argument('--password',default='')
    p.add_argument('--workers',type=int,default=5); p.add_argument('--target-active-pos',type=int,default=2)
    p.add_argument('--planned-quantity',type=int,default=3); p.add_argument('--workdays',type=int,default=1)
    p.add_argument('--loop',action='store_true'); p.add_argument('--interval-seconds',type=int,default=3600); p.add_argument('--verify-ssl',action='store_true')
    p.add_argument('--fresh',action='store_true'); p.add_argument('--db-path',default='')
    args=p.parse_args()
    try:
        c=MESClient(args.base_url,args.username,args.password,args.verify_ssl)
        assert_ok(c.get('/api/system/health'),'SYS-001 Health')
        assert_ok(c.get('/api/execution/health'),'SYS-002 Execution health')
        employees=find_or_create_employees(c,max(2,min(args.workers,30)))
        template=get_template(c)
        cycle=0
        while True:
            cycle+=1; pos=[]; sessions_before=assert_ok(c.get('/api/work-sessions'),'SES-000 Sessions before').get('items',[])
            before_ids={int(x['id']) for x in sessions_before}
            for i in range(max(2,args.target_active_pos)):
                pos.append(create_po(c,template,i+1,max(1,args.planned_quantity)))
            active=assert_ok(c.get('/api/dashboard/summary'),'DASH-000 Summary after start')['summary']
            if int(active.get('po_active') or 0)<2: raise AssertionError(f"Không có ít nhất 2 PO active: {active}")
            first_ops=operations_for_po(c,pos[0]['id']); negative_cases(c,employees[0],first_ops[0])
            total_ops=0
            # Interleave PO completion by one PO at a time; both are active before first completion.
            for po in pos: total_ops+=complete_po(c,po,employees)
            sessions_after=assert_ok(c.get('/api/work-sessions'),'SES-001 Sessions after').get('items',[])
            new_ids=[int(x['id']) for x in sessions_after if int(x['id']) not in before_ids]
            verify_timeline(c,new_ids)
            verify_dashboard(c,pos)
            log('PASS','LIFE-999 Cycle complete',f'cycle={cycle}, po={len(pos)}, operations={total_ops}, sessions={len(new_ids)}')
            if not args.loop: break
            time.sleep(max(60,args.interval_seconds))
        return 0
    except Exception as exc:
        log('FAIL','FATAL',f'{type(exc).__name__}: {exc}')
        return 1
if __name__=='__main__': raise SystemExit(main())
