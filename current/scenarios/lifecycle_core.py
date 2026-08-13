from __future__ import annotations
import argparse, json, random, sys, time, uuid
from datetime import datetime
from typing import Any
import requests

PREFIX="QAV65817-"
BUSY_WARNED:set[str]=set()

DEMO_EMPLOYEE_NAMES=[
    "Nguyễn Văn Hùng","Trần Minh Tuấn","Lê Quốc Bảo","Phạm Hoàng Nam",
    "Võ Thanh Tùng","Đặng Văn Phúc","Bùi Đức Anh","Hoàng Minh Khang",
    "Nguyễn Thành Đạt","Trần Quốc Khánh","Lê Văn Dũng","Phan Công Thành",
    "Vũ Minh Hiếu","Đỗ Anh Tuấn","Nguyễn Hoài Nam","Trương Văn Long",
    "Phạm Gia Huy","Lê Đức Thịnh","Bùi Quang Hòa","Đặng Minh Trí"
]
DEMO_PRODUCTS=[
    ("VO-TU-DIEN","Vỏ tủ điện công nghiệp"),
    ("HOP-DIEU-KHIEN","Hộp điều khiển máy"),
    ("KHUNG-MAY","Khung máy tự động"),
    ("TU-PCCC","Tủ thiết bị PCCC"),
    ("VO-MAY-BOM","Vỏ máy bơm công nghiệp"),
    ("HOP-INOX","Hộp inox kỹ thuật"),
    ("TU-DIEN-NGOAI-TROI","Tủ điện ngoài trời"),
    ("KHUNG-BANG-TAI","Khung băng tải"),
    ("VO-MAY-CAT","Vỏ máy cắt"),
    ("TU-PHAN-PHOI","Tủ phân phối điện")
]
DEMO_DEPARTMENTS=["Tổ Cắt - Đột","Tổ Chấn","Tổ Hàn","Tổ Lắp ráp","Tổ Hoàn thiện"]

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
    RETRYABLE_STATUSES={429,502,503,504,521,522,523,524}
    def __init__(self,base:str,username:str,password:str,verify_ssl:bool=False,fallback_base:str=""):
        self.username=username; self.password=password; self.verify_ssl=verify_ssl
        bases=[]
        for value in (base,fallback_base):
            value=str(value or "").rstrip('/')
            if value and value not in bases: bases.append(value)
        if not bases: raise ValueError('Thiếu base URL')
        self.base=bases[0]; self.bases=[]; self.sessions={}
        if not verify_ssl:
            try: requests.packages.urllib3.disable_warnings()
            except Exception: pass
        # URL chính là bắt buộc. URL dự phòng chỉ được đăng ký khi thực sự truy cập
        # và đăng nhập thành công; fallback hỏng không được làm dừng soak test.
        for idx,value in enumerate(bases):
            try:
                session=self._new_session(value)
            except (requests.RequestException, AssertionError) as exc:
                if idx==0:
                    raise
                log('WARN','NET-FALLBACK-DISABLED',
                    f'URL dự phòng không truy cập được; tiếp tục chỉ dùng URL chính {self.base}',
                    fallback=value,error=f'{type(exc).__name__}: {exc}')
                continue
            self.sessions[value]=session
            self.bases.append(value)
        if self.base not in self.sessions:
            raise requests.ConnectionError(f'Không thể đăng nhập URL chính {self.base}')
        self.s=self.sessions[self.base]

    def _new_session(self,base:str):
        session=requests.Session(); session.verify=self.verify_ssl
        session.headers.update({'Accept':'application/json','User-Agent':'MESFlow-QA-Center/1.19.0'})
        if not self.password:
            raise AssertionError('AUTH-000 Password required: production auto-login is disabled')
        r=session.post(base+'/api/auth/login',json={'username':self.username,'password':self.password},timeout=30)
        assert_ok(r,'AUTH-001 Login '+base)
        me=session.get(base+'/api/auth/me',timeout=30)
        assert_ok(me,'AUTH-002 Session '+base)
        return session

    def _request(self,method:str,path:str,payload=None,**kw):
        timeout=kw.pop('timeout',30); last=None
        ordered=[self.base]+[x for x in self.bases if x!=self.base]
        for idx,base in enumerate(ordered):
            session=self.sessions[base]
            url=path if str(path).startswith('http') else base+path
            # Absolute URLs are pinned to their own host; convert to path for fallback.
            if str(path).startswith('http') and base!=self.base:
                from urllib.parse import urlsplit
                parsed=urlsplit(str(path)); url=base+parsed.path+(('?'+parsed.query) if parsed.query else '')
            attempts=2 if idx==0 else 1
            for attempt in range(attempts):
                try:
                    args=dict(kw); args['timeout']=timeout
                    if method in ('POST','PATCH','DELETE'): args['json']=payload or {}
                    response=session.request(method,url,**args); last=response
                    if response.status_code not in self.RETRYABLE_STATUSES:
                        if base!=self.base:
                            log('WARN','NET-FALLBACK',f'Chuyển sang URL dự phòng {base}',primary=self.base,status=getattr(last,'status_code',None))
                            self.base=base; self.s=session
                        return response
                    body={}
                    try: body=response.json()
                    except Exception: pass
                    wait=min(10,max(1,int(body.get('retry_after') or response.headers.get('Retry-After') or 2)))
                    log('WARN','NET-RETRY',f'{method} {url} HTTP {response.status_code}; sẽ thử lại hoặc chuyển URL',attempt=attempt+1,wait_seconds=wait)
                    if attempt+1<attempts: time.sleep(wait)
                except requests.RequestException as exc:
                    last=exc; log('WARN','NET-ERROR',f'{method} {url}: {type(exc).__name__}: {exc}')
                    if attempt+1<attempts: time.sleep(2)
        if isinstance(last,requests.Response): return last
        raise requests.ConnectionError(f'Không thể kết nối MESFlow qua các URL {self.bases}: {last}')

    def get(self,path,**kw): return self._request('GET',path,**kw)
    def post(self,path,payload=None,**kw): return self._request('POST',path,payload,**kw)
    def patch(self,path,payload=None,**kw): return self._request('PATCH',path,payload,**kw)
    def delete(self,path,payload=None,**kw): return self._request('DELETE',path,payload,**kw)

def _create_qa_employee(c:MESClient, seq:int)->dict:
    code=f'{PREFIX}EMP-{seq:03d}'
    name=DEMO_EMPLOYEE_NAMES[(seq-1)%len(DEMO_EMPLOYEE_NAMES)]
    department=DEMO_DEPARTMENTS[(seq-1)%len(DEMO_DEPARTMENTS)]
    body=assert_ok(c.post('/api/employees',{
        'employee_no':code,
        'name':name,
        'department':department,
        'team':department,
        'position':'Công nhân vận hành',
        'employment_status':'Đang làm',
        'active':True,
        'qr':f'WF|EMP|{code}'
    }),'EMP-002 Create QA employee')
    return body['item']

def find_or_create_employees(c:MESClient,count:int)->list[dict]:
    items=assert_ok(c.get('/api/employees?limit=1000'),'EMP-001 List employees').get('items',[])
    # Không dùng nhân viên thật của xưởng. Chỉ map các tài khoản QA có tiền tố riêng.
    qa=[x for x in items if x.get('active') and str(x.get('employee_no') or '').startswith(PREFIX+'EMP-')]
    qa.sort(key=lambda x:str(x.get('employee_no') or ''))
    # Chuẩn hóa cả nhân viên QA đã tồn tại từ bản cũ để giao diện demo không còn tên QA Worker.
    for idx,item in enumerate(qa,1):
        desired_name=DEMO_EMPLOYEE_NAMES[(idx-1)%len(DEMO_EMPLOYEE_NAMES)]
        desired_department=DEMO_DEPARTMENTS[(idx-1)%len(DEMO_DEPARTMENTS)]
        if str(item.get('name') or '')!=desired_name or str(item.get('department') or '')!=desired_department:
            r=c.patch(f"/api/employees/{int(item['id'])}",{
                'name':desired_name,'department':desired_department,'team':desired_department,
                'position':'Công nhân vận hành','active':True
            })
            if r.status_code in (200,201):
                item.update({'name':desired_name,'department':desired_department,'team':desired_department,'position':'Công nhân vận hành'})
            else:
                log('WARN','EMP-DISPLAY-NAME',f'Không cập nhật được tên {item.get("employee_no")}',status=r.status_code)
    existing_codes={str(x.get('employee_no') or '') for x in items}
    seq=1
    while len(qa)<count:
        code=f'{PREFIX}EMP-{seq:03d}'
        seq+=1
        if code in existing_codes: continue
        item=_create_qa_employee(c,seq-1)
        qa.append(item); existing_codes.add(code)
    log('PASS','EMP-003 QA employee pool',f'{len(qa[:count])} nhân viên QA chuyên dụng',employees=[x.get('employee_no') for x in qa[:count]])
    return qa[:count]

def get_template(c:MESClient)->dict:
    assert_ok(c.post('/api/templates/demo/seed'),'TPL-001 Seed demo templates')
    items=assert_ok(c.get('/api/templates/available-for-po'),'TPL-002 List templates').get('items',[])
    candidates=[x for x in items if int(x.get('part_count') or 0)>0 and int(x.get('operation_count') or 0)>0]
    if not candidates:
        raise AssertionError('TPL-003: Không có template có Part và Operation')

    # API instantiate yêu cầu mã Part duy nhất trong từng PO bởi uq_parts_po_code.
    # Một số demo template cũ (ví dụ DEMO-E10-LAP-RAP) có mã Part trùng,
    # nên phải kiểm tra tree trước khi chọn thay vì chỉ dựa trên số Operation.
    valid=[]
    rejected=[]
    for item in candidates:
        tree=assert_ok(c.get(f"/api/templates/{int(item['id'])}/tree"),f"TPL-VAL-{int(item['id'])} Read tree")
        parts=tree.get('parts') or []
        codes=[str(p.get('code') or '').strip().upper() for p in parts]
        duplicates=sorted({code for code in codes if code and codes.count(code)>1})
        empty=sum(1 for code in codes if not code)
        if duplicates or empty:
            rejected.append({'id':item.get('id'),'code':item.get('code'),'duplicates':duplicates,'empty_codes':empty})
            log('WARN','TPL-004 Reject invalid template',str(item.get('code')),duplicates=duplicates,empty_codes=empty)
            continue
        operations=[]
        for part in parts:
            operations.extend(part.get('operations') or [])
        # Một số API trả operations ở cấp root.
        operations.extend(tree.get('operations') or [])
        cycle_map={}
        for op in operations:
            code=str(op.get('code') or '').strip().upper()
            seconds=float(op.get('standard_seconds_per_unit') or 0)
            if code and seconds>0:
                cycle_map[code]=seconds
        enriched=dict(item); enriched['_tree']=tree; enriched['_cycle_times_by_code']=cycle_map
        enriched['_cycle_configured']=len(cycle_map); enriched['_cycle_total']=max(1,len(operations))
        valid.append(enriched)

    if not valid:
        raise AssertionError('TPL-005: Tất cả Template đều không thể instantiate do mã Part trùng/rỗng: '+json.dumps(rejected,ensure_ascii=False))
    # Ưu tiên Template có định mức thời gian đầy đủ nhất, sau đó mới xét độ phức tạp.
    valid.sort(key=lambda x:(-float(x.get('_cycle_configured',0))/max(1,int(x.get('_cycle_total',1))),-int(x.get('_cycle_configured',0)),int(x.get('operation_count') or 0),str(x.get('code') or '')))
    chosen=valid[0]
    coverage=round(100*int(chosen.get('_cycle_configured',0))/max(1,int(chosen.get('_cycle_total',1))),1)
    log('PASS','TPL-005 Select valid template',str(chosen.get('code')),template_id=chosen.get('id'),parts=chosen.get('part_count'),operations=chosen.get('operation_count'),cycle_time_coverage_percent=coverage,rejected=len(rejected))
    if not chosen.get('_cycle_times_by_code'):
        log('WARN','TPL-006 Missing cycle times','Template không có standard_seconds_per_unit; mô phỏng sẽ dùng định mức dự phòng có cảnh báo')
    return chosen

def create_po(c:MESClient,template:dict,index:int,plan:int)->dict:
    product_code,product_name=DEMO_PRODUCTS[(max(1,index)-1)%len(DEMO_PRODUCTS)]
    day=datetime.now().strftime('%y%m%d')
    lot=(max(1,index)%999)+1
    code=f"{PREFIX}{day}-{product_code}-{lot:03d}"
    priority=['NORMAL','NORMAL','HIGH','NORMAL','URGENT'][(max(1,index)-1)%5]
    payload={
        'template_id':template['id'],
        'code':code,
        'planned_quantity':plan,
        'priority':priority,
        'notes':f'Dữ liệu mô phỏng nhà máy · {product_name} · Lô {lot:03d}'
    }
    body=assert_ok(c.post('/api/production-orders',payload),f'PO-{index:02d}-001 Create PO')
    po_id=int(body.get('production_order_id') or body.get('id'))
    # Đổi tên sản phẩm hiển thị sau khi instantiate để dashboard/report giống dữ liệu xưởng thật.
    patch=c.patch(f'/api/production-orders/{po_id}',{'product':product_name,'priority':priority,'notes':payload['notes']})
    if patch.status_code not in (200,201):
        log('WARN','PO-DISPLAY-NAME',f'Không cập nhật được tên hiển thị {product_name}',status=patch.status_code)
    assert_ok(c.post(f'/api/production-orders/{po_id}/start'),f'PO-{index:02d}-002 Start PO')
    po=assert_ok(c.get(f'/api/production-orders/{po_id}'),f'PO-{index:02d}-003 Read PO')['item']
    if str(po.get('status')).upper()!='IN_PROGRESS': raise AssertionError(f'PO chưa IN_PROGRESS: {po}')
    return {'id':po_id,'code':code,'plan':plan,'product':product_name}

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
    # Chỉ cảnh báo một lần cho mỗi QA employee bận, tránh spam hàng chục dòng/Operation.
    newly_skipped=[x for x in skipped if x not in BUSY_WARNED]
    if newly_skipped:
        BUSY_WARNED.update(newly_skipped)
        log('WARN','EMP-MAP Busy QA employees','Bỏ qua QA employee đang có session OPEN',employees=newly_skipped)
    if not candidates:
        # Tạo thêm một QA employee riêng thay vì dùng/đóng phiên của nhân viên thật.
        seq=max([int(str(e.get('employee_no') or '').rsplit('-',1)[-1]) for e in employees if str(e.get('employee_no') or '').rsplit('-',1)[-1].isdigit()] or [0])+1
        extra=_create_qa_employee(c,seq)
        employees.append(extra); candidates=[extra]
        log('PASS','EMP-MAP Expand pool','Tự tạo thêm QA employee vì toàn bộ pool đang bận',employee=extra.get('employee_no'))

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
    b=assert_ok(c.post(f'/api/kiosk-web/finish/{session_id}',{'request_id':fid,'good_qty':qty,'defect_qty':0,'note':'QA v65.8.17'}),case_prefix+' Finish session')
    replay=assert_ok(c.post(f'/api/kiosk-web/finish/{session_id}',{'request_id':fid,'good_qty':qty,'defect_qty':0,'note':'QA v65.8.17'}),case_prefix+' Finish idempotency')
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
    # Control Tower là tập PO đang chạy thực sự; completed PO tuyệt đối không được xuất hiện tại đây.
    tower=assert_ok(c.get('/api/dashboard/control-tower?limit=500'),'DASH-001 Control tower active PO')
    active_items=tower.get('po_health') or tower.get('items') or []
    active_codes={str(x.get('code')) for x in active_items}
    bad_active=[p['code'] for p in pos if p['code'] in active_codes]
    if bad_active:
        raise AssertionError('DASH-002 completed_po_still_active: '+','.join(bad_active))
    log('PASS','DASH-002 Completed PO removed from active dashboard','PO hoàn tất không còn trong Control Tower')

    # Endpoint production-orders của v65.8.17 hiện là danh sách lịch sử tất cả PO, không phải active-only.
    # Nếu UI dùng endpoint này mà không lọc COMPLETED thì ghi cảnh báo sản phẩm, không làm sai lifecycle test.
    history=assert_ok(c.get('/api/dashboard/production-orders?limit=500'),'DASH-003 PO history endpoint').get('items',[])
    completed_visible=[p['code'] for p in pos if any(str(x.get('code'))==p['code'] and str(x.get('status')).upper()=='COMPLETED' for x in history)]
    if completed_visible:
        log('WARN','DASH-004 Historical endpoint contains COMPLETED','Đây là endpoint lịch sử; frontend phải lọc nếu dùng cho dashboard active',production_orders=completed_visible)

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
