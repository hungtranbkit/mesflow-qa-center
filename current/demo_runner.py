from __future__ import annotations
import argparse, json, os, sys, time
from datetime import datetime
from pathlib import Path
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

SCENARIOS = {
    'full-production': {
        'name': 'Full Production Demo',
        'description': 'Template → PO → Kiosk → Session → Material Flow → Dashboard → Trace/Audit',
        'chapters': ['overview','employee','template','po','kiosk','overview_after','material','session','trace','audit','dashboard'],
    },
    'planning-po': {
        'name': 'Planning & Production Order',
        'description': 'Tổng quan → Template → tạo/khởi động PO → Gantt & Material Flow',
        'chapters': ['overview','template','po','material'],
    },
    'kiosk-realtime': {
        'name': 'Kiosk & Realtime',
        'description': 'Quét thẻ → quét Operation → start/finish → nhập OK/lỗi/rework → xem realtime',
        'chapters': ['kiosk','overview_after','session'],
    },
    'quality-rework': {
        'name': 'Quality / Defect / Rework',
        'description': 'Kiosk ghi nhận sản lượng đạt, lỗi, lỗi sửa được và xem ảnh hưởng lên tiến độ',
        'chapters': ['kiosk_quality','material','dashboard'],
    },
    'trace-audit': {
        'name': 'Traceability & Audit',
        'description': 'Production Trace → Business Audit → Session Management → System Logs',
        'chapters': ['trace','audit','session','logs'],
    },
    'feature-tour': {
        'name': 'MESFlow Feature Tour',
        'description': 'Đi qua các màn hình chính để giới thiệu toàn bộ chức năng hiện có',
        'chapters': ['overview','dashboard','po','template','session','exceptions','trace','audit','material','kiosk_mgmt','employees','qr','equipment','users','calendar','logs'],
    },
}


CASE_DETAILS = {
    'overview': {'title':'Tổng quan sản xuất','test':'Mở trang Tổng quan và xác nhận trang chính có thể render.','input':'Điều hướng UI → Tổng quan sản xuất','expected':'Trang Tổng quan hiển thị và không phát sinh lỗi điều hướng.'},
    'employee': {'title':'Nhân viên demo','test':'Tìm đúng nhân viên demo vừa tạo qua giao diện Nhân viên.','input':'Mã nhân viên: {employee_code}','expected':'Danh sách Nhân viên lọc được bản ghi demo tương ứng.'},
    'template': {'title':'Template quy trình','test':'Tìm template demo và xác nhận cây Part/Operation đã được tạo.','input':'Template: {template_code}','expected':'Template demo xuất hiện trong màn Template quy trình.'},
    'po': {'title':'Production Order','test':'Tìm Production Order được instantiate từ template và đã Start.','input':'PO: {po_code}; planned_quantity={planned}','expected':'PO demo xuất hiện và sẵn sàng chạy sản xuất.'},
    'kiosk': {'title':'Kiosk: quét thẻ, Operation và nhập sản lượng','test':'Mô phỏng worker quét thẻ → quét Operation → Start → Finish.','input':'Employee={employee_code}; Operation={operation_code}; OK=8; defect=1; rework=1','expected':'Kiosk kết thúc session thành công và ghi nhận 8 OK, 1 lỗi, 1 rework.'},
    'kiosk_quality': {'title':'Kiosk: lỗi và rework','test':'Kiểm thử luồng chất lượng với lỗi và số lượng sửa lại.','input':'Employee={employee_code}; Operation={operation_code}; OK=7; defect=3; rework=2','expected':'Session hoàn tất và số liệu quality/rework được ghi nhận.'},
    'overview_after': {'title':'Realtime Overview sau sản xuất','test':'Quay lại Overview sau khi có session và lọc theo PO demo.','input':'PO filter={po_code}','expected':'Overview phản ánh dữ liệu sản xuất mới của PO demo.'},
    'material': {'title':'Gantt & Material Flow','test':'Mở Production Schedule/Material Flow và lọc PO demo.','input':'PO filter={po_code}','expected':'Gantt/Material Flow hiển thị luồng của PO demo.'},
    'session': {'title':'Quản lý Session','test':'Mở Session Management và lọc session theo PO demo.','input':'PO filter={po_code}','expected':'Session vừa chạy được hiển thị đúng trong quản lý Session.'},
    'trace': {'title':'Production Trace','test':'Mở Production Trace để kiểm tra traceability của dữ liệu demo.','input':'Điều hướng UI → Production Trace','expected':'Trang trace render và có thể truy vết dữ liệu sản xuất.'},
    'audit': {'title':'Nhật ký nghiệp vụ','test':'Mở Business Audit để xác nhận audit trail có thể truy cập.','input':'Điều hướng UI → Business Audit','expected':'Trang audit render và sẵn sàng hiển thị sự kiện nghiệp vụ.'},
    'dashboard': {'title':'Dashboard theo ngày / KPI','test':'Mở Dashboard sau khi phát sinh dữ liệu sản xuất.','input':'Điều hướng UI → Dashboard','expected':'Dashboard render thành công và nhận dữ liệu mới.'},
    'exceptions': {'title':'Trung tâm ngoại lệ','test':'Mở màn hình ngoại lệ Session.','input':'Điều hướng UI → Session Exceptions','expected':'Trang ngoại lệ render thành công.'},
    'kiosk_mgmt': {'title':'Quản lý trạm kiosk','test':'Mở quản lý kiosk.','input':'Điều hướng UI → Kiosk Management','expected':'Trang quản lý kiosk render thành công.'},
    'employees': {'title':'Danh mục nhân viên','test':'Mở danh mục nhân viên.','input':'Điều hướng UI → Employees','expected':'Danh mục nhân viên render thành công.'},
    'qr': {'title':'Danh sách QR Code','test':'Mở màn in/tra cứu QR.','input':'Điều hướng UI → QR Print','expected':'Trang QR render thành công.'},
    'equipment': {'title':'Thiết bị','test':'Mở danh mục thiết bị.','input':'Điều hướng UI → Equipment','expected':'Danh mục thiết bị render thành công.'},
    'users': {'title':'Người dùng & phân quyền','test':'Mở quản lý người dùng/RBAC.','input':'Điều hướng UI → Users','expected':'Trang users/RBAC render thành công.'},
    'calendar': {'title':'Lịch làm việc','test':'Mở Working Calendar.','input':'Điều hướng UI → Working Calendar','expected':'Lịch làm việc render thành công.'},
    'logs': {'title':'Nhật ký ứng dụng','test':'Mở System Logs.','input':'Điều hướng UI → System Logs','expected':'Trang System Logs render thành công.'},
}


def build_plan(chapters, data):
    values={**data,'operation_code':data['ops'][-1]['code'] if data.get('ops') else ''}
    plan=[]
    for idx,key in enumerate(chapters,1):
        meta=dict(CASE_DETAILS.get(key) or {'title':key,'test':'Mở và kiểm tra chức năng.','input':'Điều hướng UI','expected':'Bước hoàn tất không lỗi.'})
        for field in ('title','test','input','expected'):
            try: meta[field]=str(meta.get(field,'')).format(**values)
            except Exception: meta[field]=str(meta.get(field,''))
        meta.update({'key':key,'step_id':f'{idx:02d}-{key}','index':idx})
        plan.append(meta)
    return plan


def emit(event_kind: str, **payload):
    # Action records contain their own `kind` (NAVIGATE/FILL/VERIFY).
    # A distinct envelope parameter prevents emit('action_start', **item)
    # from passing the Python argument `kind` twice before the first capture.
    print('DEMO_EVENT|' + json.dumps({'kind': event_kind, 'at': datetime.now().isoformat(timespec='seconds'), **payload}, ensure_ascii=False), flush=True)


def api_session(base: str, username: str, password: str) -> requests.Session:
    s = requests.Session(); s.verify = False
    r = s.post(base + '/api/auth/login', json={'username': username, 'password': password}, timeout=20)
    if r.status_code == 401:
        emit('auth_error', error='INVALID_CREDENTIALS', message='Sai tài khoản hoặc mật khẩu MESFlow. Hãy quay lại Demo Center → Kết nối MESFlow → Kiểm tra đăng nhập.')
        raise RuntimeError('MESFlow authentication failed: INVALID_CREDENTIALS (HTTP 401)')
    if r.status_code >= 400:
        try: detail=r.json()
        except Exception: detail=r.text[:300]
        raise RuntimeError(f'MESFlow authentication failed: HTTP {r.status_code} {detail}')
    me=s.get(base + '/api/auth/me', timeout=20)
    if me.status_code >= 400:
        raise RuntimeError(f'MESFlow login did not create a usable session: HTTP {me.status_code} {me.text[:300]}')
    return s


def api_json(s: requests.Session, method: str, url: str, **kwargs):
    r = s.request(method, url, timeout=30, **kwargs)
    if r.status_code >= 400:
        raise RuntimeError(f'{method} {url}: HTTP {r.status_code} {r.text[:300]}')
    data = r.json() if r.content else {}
    if isinstance(data, dict) and data.get('ok') is False:
        raise RuntimeError(data.get('message') or data.get('error') or f'{method} {url} failed')
    return data


def make_data(run_id):
    token=''.join(ch for ch in str(run_id).upper() if ch.isalnum())[-18:]
    prefix=f'QA-DEMO-{token}'
    return {
        'qa_run_id': run_id, 'qa_marker': f'QA_RUN_ID={run_id}',
        'employee_code': f'{prefix}-NV01', 'employee_name': 'Nguyễn Văn An',
        'template_code': f'{prefix}-TPL', 'template_name': 'Khung máy Demo',
        'po_code': f'{prefix}-PO', 'part_code': f'{prefix}-P01',
        'part_name': 'Tấm thân', 'planned': 20,
        'ops': [
            {'code': f'{prefix}-CAT', 'name': 'Cắt Laser', 'seconds': 30},
            {'code': f'{prefix}-CHAN', 'name': 'Chấn', 'seconds': 40},
            {'code': f'{prefix}-KT', 'name': 'Kiểm tra', 'seconds': 25},
        ]
    }


def ensure_data(base, username, password, data, registered=None):
    registered=registered or (lambda _kind,_item: None)
    s = api_session(base, username, password)
    emps = api_json(s,'GET',base+'/api/employees?limit=1000').get('items',[])
    emp = next((x for x in emps if x.get('employee_no') == data['employee_code']), None)
    if not emp:
        emp = api_json(s,'POST',base+'/api/employees',json={'employee_no':data['employee_code'],'name':data['employee_name'],'department':data['qa_marker'],'team':'QA Demo','position':'Công nhân','employment_status':'Đang làm','active':True}).get('item',{})
    registered('employee',emp)
    tpls = api_json(s,'GET',base+'/api/templates?limit=1000').get('items',[])
    tpl = next((x for x in tpls if x.get('code') == data['template_code']), None)
    if not tpl:
        tpl = api_json(s,'POST',base+'/api/templates',json={'code':data['template_code'],'name':data['template_name'],'product':data['qa_marker'],'version':'1.0','active':True}).get('item',{})
        registered('template',tpl)
        api_json(s,'PUT',base+f"/api/templates/{tpl['id']}/tree",json={
            'parts':[{'key':'part-1','code':data['part_code'],'name':data['part_name'],'sort_order':0}],
            'operations':[{'part_key':'part-1','code':o['code'],'name':o['name'],'sort_order':i,'standard_seconds_per_unit':o['seconds'],'input_flow_enabled':False,'defects_consume_input':True} for i,o in enumerate(data['ops'])],
            'equipment':[]})
    else:
        registered('template',tpl)
    pos = api_json(s,'GET',base+'/api/production-orders?limit=1000').get('items',[])
    po = next((x for x in pos if x.get('code') == data['po_code']), None)
    if not po:
        made = api_json(s,'POST',base+f"/api/templates/{tpl['id']}/instantiate",json={'code':data['po_code'],'planned_quantity':data['planned'],'priority':'NORMAL','notes':data['qa_marker']})
        po = api_json(s,'GET',base+f"/api/production-orders/{made['production_order_id']}").get('item',{})
    registered('production_order',po)
    if po.get('status') not in ('IN_PROGRESS','COMPLETED'):
        api_json(s,'POST',base+f"/api/production-orders/{po['id']}/start",json={})
    return {'employee': emp, 'template': tpl, 'po': po}


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--run-id',required=True); ap.add_argument('--scenario',default='full-production',choices=SCENARIOS); ap.add_argument('--base-url',required=True); ap.add_argument('--username',default='admin'); ap.add_argument('--password',required=True); ap.add_argument('--output-dir',required=True); ap.add_argument('--pace',type=float,default=1.0); ap.add_argument('--mode',choices=['auto','presenter','manual'],default='auto')
    args=ap.parse_args(); base=args.base_url.rstrip('/'); out=Path(args.output_dir); out.mkdir(parents=True,exist_ok=True); (out/'screenshots').mkdir(exist_ok=True)
    live=out/'live.png'; state_path=out/'state.json'; control_path=out/'control.json'; pace=max(.25,min(5,args.pace)); control_path.write_text(json.dumps({'pause_requested':False,'next_seq':0},indent=2),encoding='utf-8'); last_next_seq=0
    scenario=SCENARIOS[args.scenario]; chapters=scenario['chapters']; results=[]; action_log=[]; action_seq=0
    data=make_data(args.run_id); plan=build_plan(chapters,data); current_case=None; current_action=None
    ownership_path=out/'ownership.json'
    ownership={'run_id':args.run_id,'created_at':datetime.now().isoformat(timespec='seconds'),'scenario':args.scenario,
               'scenario_name':scenario['name'],'status':'PREPARING','cleanup_status':'RETAINED','target_url':base,
               'marker':data['qa_marker'],'generated':{}}
    def save_ownership(): ownership_path.write_text(json.dumps(ownership,ensure_ascii=False,indent=2),encoding='utf-8')
    def register(kind,item):
        ownership['generated'][kind]={'id':item.get('id'),'code':item.get('code') or item.get('employee_no'),'name':item.get('name')}
        save_ownership()
    def refresh_generated():
        po=ownership['generated'].get('production_order') or {}; po_id=po.get('id')
        if not po_id: return
        sess=api_session(base,args.username,args.password)
        parts=[x for x in api_json(sess,'GET',base+'/api/parts?limit=1000').get('items',[]) if int(x.get('production_order_id') or 0)==int(po_id)]
        operations=[x for x in api_json(sess,'GET',base+'/api/operations?limit=1000').get('items',[]) if int(x.get('production_order_id') or 0)==int(po_id)]
        sessions=api_json(sess,'GET',base+f'/api/session-management?po_id={po_id}&limit=3000').get('items',[])
        trace=api_json(sess,'GET',base+f'/api/production-orders/{po_id}/trace?limit=200').get('events',[])
        ownership['generated'].update(
            parts=[{'id':x.get('id'),'code':x.get('code'),'name':x.get('name')} for x in parts],
            operations=[{'id':x.get('id'),'code':x.get('code'),'name':x.get('name')} for x in operations],
            sessions=[{'id':x.get('session_id'),'status':x.get('status'),'operation_code':x.get('operation_code'),'good_qty':x.get('good_qty'),'defect_qty':x.get('defect_qty'),'rework_qty':x.get('rework_qty')} for x in sessions],
            trace_events=[{'id':x.get('id'),'event_type':x.get('event_type'),'title':x.get('title')} for x in trace],
        )
        ownership['generated_counts']={'employees':1,'templates':1,'production_orders':1,'parts':len(parts),'operations':len(operations),'sessions':len(sessions),'trace_events':len(trace)}
        save_ownership()
    save_ownership()

    def write_state(**extra):
        payload={'scenario':args.scenario,'scenario_name':scenario['name'],'scenario_description':scenario['description'],'mode':args.mode,'base_url':base,
                 'updated_at':datetime.now().isoformat(timespec='seconds'),'heartbeat_at':datetime.now().isoformat(timespec='seconds'),
                 'results':results,'plan':plan,'total_steps':len(plan),'current_case':current_case,'current_action':current_action,
                 'action_log':action_log[-80:],**extra}
        state_path.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')

    def action_start(kind,target,detail='',value=None,expected=''):
        nonlocal action_seq,current_action
        action_seq+=1
        item={'seq':action_seq,'kind':kind,'target':target,'detail':detail,'value':value,'expected':expected,'status':'ACTIVE','at':datetime.now().isoformat(timespec='seconds')}
        action_log.append(item); current_action=item
        emit('action_start',**item); write_state(status='RUNNING')
        return item

    def action_done(item, detail=''):
        nonlocal current_action
        item['status']='DONE'; item['done_at']=datetime.now().isoformat(timespec='seconds')
        if detail: item['result']=detail
        current_action=item
        emit('action_done',seq=item['seq'],kind=item['kind'],target=item['target'],result=item.get('result',''))
        write_state(status='RUNNING')

    def action_fail(item, error):
        nonlocal current_action
        item['status']='FAIL'; item['error']=str(error); item['done_at']=datetime.now().isoformat(timespec='seconds'); current_action=item
        detail={'stage':'action','case':(current_case or {}).get('title',''),'step_id':(current_case or {}).get('step_id',''),
                'action':item.get('kind',''),'target':item.get('target',''),'message':str(error),'url':base}
        emit('action_fail',seq=item['seq'],kind=item['kind'],target=item['target'],error=str(error)); write_state(status='RUNNING',error=str(error),error_detail=detail)

    write_state(status='PREPARING',current_step='setup',current_title='Chuẩn bị dữ liệu demo',
                current_action={'seq':0,'kind':'API','target':'Demo seed','detail':'Tạo/kiểm tra Employee, Template và Production Order demo','status':'ACTIVE','at':datetime.now().isoformat(timespec='seconds')})
    try:
        entities=ensure_data(base,args.username,args.password,data,register)
    except Exception as exc:
        err={'stage':'setup','case':'Demo seed','action':'API','target':base,'message':str(exc),'url':base}
        write_state(status='FAILED',current_step='setup',current_title='Chuẩn bị dữ liệu demo thất bại',
                    error=str(exc),error_detail=err,
                    current_action={'seq':0,'kind':'API','target':'Demo seed','detail':'Không thể tạo/kiểm tra dữ liệu demo','status':'FAIL','error':str(exc),'at':datetime.now().isoformat(timespec='seconds')})
        ownership.update(status='FAILED',finished_at=datetime.now().isoformat(timespec='seconds')); save_ownership()
        emit('fatal',error=str(exc),stage='setup',target=base)
        return 3
    write_state(status='PREPARING',current_step='setup',current_title='Dữ liệu demo đã sẵn sàng',
                current_action={'seq':0,'kind':'VERIFY','target':'Demo seed','detail':'Employee, Template và PO đã sẵn sàng','status':'DONE','at':datetime.now().isoformat(timespec='seconds')})

    def read_control():
        try: return json.loads(control_path.read_text(encoding="utf-8"))
        except Exception: return {"pause_requested":False,"next_seq":0}

    def wait_after_step(step_id,title):
        nonlocal last_next_seq
        initial=read_control(); start_seq=int(initial.get("next_seq") or 0)
        should_wait=bool(initial.get("pause_requested")) or args.mode in {"presenter","manual"}
        if not should_wait: return
        write_state(status="PAUSED",current_step=step_id,current_title=title,pause_reason="presenter" if args.mode!="auto" else "manual_pause")
        emit("paused",step_id=step_id,title=title,mode=args.mode)
        while True:
            ctl=read_control(); seq=int(ctl.get("next_seq") or 0)
            if seq>start_seq or (args.mode=="auto" and not ctl.get("pause_requested")):
                last_next_seq=seq; break
            time.sleep(.2)
        write_state(status="RUNNING",current_step=step_id,current_title=title)
        emit("resumed",step_id=step_id,title=title,mode=args.mode)

    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        context=browser.new_context(viewport={'width':1600,'height':900})
        page=context.new_page()
        browser_console=[]; failed_requests=[]
        page.on('console',lambda msg: browser_console.append({'type':msg.type,'text':msg.text}) if msg.type in {'error','warning'} else None)
        page.on('pageerror',lambda exc: browser_console.append({'type':'pageerror','text':str(exc)}))
        page.on('requestfailed',lambda req: failed_requests.append({'url':req.url,'error':str(req.failure or '')}))
        page.on('response',lambda response: failed_requests.append({'url':response.url,'status':response.status}) if response.status>=400 else None)

        def snap(name='live'):
            page.screenshot(path=str(live),full_page=False)
            if name!='live': page.screenshot(path=str(out/'screenshots'/f'{name}.png'),full_page=False)

        def pause(mult=1):
            snap(); time.sleep(1.0*pace*mult)

        def step(step_id,title,fn):
            nonlocal current_case,current_action
            current_case=next((x for x in plan if x.get('step_id')==step_id),{'step_id':step_id,'title':title})
            current_action=None
            emit('step_start',step_id=step_id,title=title,case=current_case); write_state(status='RUNNING',current_step=step_id,current_title=title,current_index=current_case.get('index'))
            try:
                fn(); snap(step_id); results.append({'id':step_id,'title':title,'status':'PASS'}); emit('step_pass',step_id=step_id,title=title); write_state(status='RUNNING',current_step=step_id,current_title=title,current_index=current_case.get('index')); wait_after_step(step_id,title)
            except Exception as exc:
                failed_shot='FAILED-'+step_id
                try: snap(failed_shot)
                except Exception: failed_shot=''
                detail={'stage':'testcase','case':title,'step_id':step_id,'action':(current_action or {}).get('kind',''),
                        'target':(current_action or {}).get('target',''),'message':str(exc),'url':base,'screenshot':failed_shot}
                results.append({'id':step_id,'title':title,'status':'FAIL','error':str(exc),'error_detail':detail})
                emit('step_fail',step_id=step_id,title=title,error=str(exc),error_detail=detail)
                write_state(status='RUNNING',current_step=step_id,current_title=title,current_index=current_case.get('index'),error=str(exc),error_detail=detail)
                return False
            return True

        def do_action(kind,target,detail,fn,value=None,expected=''):
            item=action_start(kind,target,detail,value,expected)
            try:
                result=fn(); action_done(item); return result
            except Exception as exc:
                action_fail(item,exc); raise

        def login():
            do_action('NAVIGATE','/login','Mở trang đăng nhập MESFlow',lambda: page.goto(base+'/login',wait_until='domcontentloaded'),expected='Login page loaded')
            u=page.locator('input[name="username"],#username').first; pw=page.locator('input[name="password"],#password').first
            if u.is_visible():
                do_action('FOCUS','Username','Focus ô tài khoản',lambda: u.focus())
                do_action('TYPE','Username','Nhập tài khoản',lambda: u.fill(args.username),value=args.username)
                do_action('FOCUS','Password','Focus ô mật khẩu',lambda: pw.focus())
                do_action('TYPE','Password','Nhập mật khẩu',lambda: pw.fill(args.password),value='••••••')
                pause(.35)
                do_action('CLICK','Đăng nhập','Click nút submit',lambda: page.locator('button[type="submit"]').first.click(),expected='Redirect /app')
            do_action('WAIT','URL /app','Chờ MESFlow đăng nhập thành công',lambda: page.wait_for_url('**/app**',timeout=20000),expected='URL matches /app')
            do_action('WAIT','Navigation overview','Chờ thanh điều hướng MESFlow',lambda: page.wait_for_selector('.nav-item[data-page="overview"]',timeout=20000),expected='Overview nav visible')
            pause(.5)

        def nav(page_id):
            loc=page.locator(f'.nav-item[data-page="{page_id}"],.sidebar-sub-item[data-page="{page_id}"]').first
            do_action('FOCUS',f'nav:{page_id}',f'Xác định menu {page_id}',lambda: loc.wait_for(state='attached',timeout=10000))
            if not loc.is_visible():
                do_action('CLICK',f'group:{page_id}','Mở nhóm menu chứa mục cần test',lambda: loc.evaluate("el=>{const g=el.closest('.sidebar-group');if(g&&!g.classList.contains('open'))g.querySelector('.sidebar-group-trigger')?.click()}"))
                time.sleep(.15)
            do_action('CLICK',f'nav:{page_id}',f'Đi tới màn hình {page_id}',lambda: loc.click(),expected=f'Page {page_id} visible')
            do_action('WAIT','#content','Chờ nội dung trang render',lambda: page.locator('#content').wait_for(state='visible'),expected='Content visible')
            pause(.6)

        def select_contains(locator, needle, target='select'):
            do_action('FOCUS',target,'Focus dropdown',lambda: locator.wait_for(state='visible',timeout=10000))
            for opt in locator.locator('option').all():
                txt=(opt.text_content() or '')
                if needle in txt:
                    val=opt.get_attribute('value')
                    return do_action('SELECT',target,f'Chọn option chứa {needle}',lambda: locator.select_option(val),value=needle,expected='Option selected')
            raise RuntimeError(f'Không tìm thấy option: {needle}')

        def fill_visible(selector,value,target):
            loc=page.locator(selector)
            if loc.count() and loc.first.is_visible():
                do_action('FOCUS',target,'Focus ô tìm kiếm',lambda: loc.first.focus())
                do_action('TYPE',target,'Nhập giá trị lọc/tìm kiếm',lambda: loc.first.fill(value),value=value,expected='Danh sách được lọc')
                pause(.5)

        def kiosk_once(good=8, defect=1, rework=1):
            do_action('NAVIGATE','/kiosk','Mở Kiosk MESFlow',lambda: page.goto(base+'/kiosk',wait_until='domcontentloaded'),expected='Kiosk loaded'); pause(.6)
            do_action('CLICK','#demo-toggle','Mở bảng Demo Scanner',lambda: page.locator('#demo-toggle').click())
            def wait_demo_panel():
                try:
                    page.locator('[data-testid="kiosk-demo-content"],#demo-content').first.wait_for(state='visible',timeout=8000)
                except Exception as exc:
                    toggle=page.locator('[data-testid="kiosk-demo-toggle"],#demo-toggle').first
                    panel=page.locator('[data-testid="kiosk-demo-content"],#demo-content').first
                    def inspect(loc):
                        if not loc.count(): return {'exists':False}
                        return loc.evaluate("e=>({exists:true,visible:!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length),enabled:!e.disabled,hidden:!!e.hidden,display:getComputedStyle(e).display,visibility:getComputedStyle(e).visibility,ariaExpanded:e.getAttribute('aria-expanded')})")
                    evidence={'code':'KIOSK_DEMO_PANEL_DID_NOT_OPEN','clicked':'#demo-toggle','url':page.url,
                              'toggle':inspect(toggle),'panel':inspect(panel),'console':browser_console[-10:],
                              'failed_requests':failed_requests[-10:]}
                    raise RuntimeError(json.dumps(evidence,ensure_ascii=False)) from exc
            do_action('WAIT','[data-testid="kiosk-demo-content"]','Chờ Demo Scanner hiển thị',wait_demo_panel)
            select_contains(page.locator('#demo-employee'),data['employee_code'],'#demo-employee')
            select_contains(page.locator('#demo-operation'),data['ops'][-1]['code'],'#demo-operation'); pause(.5)
            do_action('CLICK','#demo-scan-employee','Quét thẻ nhân viên',lambda: page.locator('#demo-scan-employee').click(),value=data['employee_code'])
            do_action('CLICK','#demo-close','Đóng Demo Scanner',lambda: page.locator('#demo-close').click())
            do_action('WAIT','#screen-operation','Chờ Kiosk yêu cầu Operation',lambda: page.locator('#screen-operation.active').wait_for(timeout=10000),expected='Operation screen active'); pause(.5)
            do_action('CLICK','#demo-toggle','Mở Demo Scanner để quét Operation',lambda: page.locator('#demo-toggle').click())
            do_action('CLICK','#demo-scan-operation','Quét Operation',lambda: page.locator('#demo-scan-operation').click(),value=data['ops'][-1]['code'])
            do_action('CLICK','#demo-close','Đóng Demo Scanner',lambda: page.locator('#demo-close').click())
            do_action('WAIT','#screen-started','Chờ session bắt đầu',lambda: page.locator('#screen-started.active').wait_for(timeout=15000),expected='Session started'); pause(1)
            do_action('CLICK','#demo-toggle','Mở Demo Scanner để kết thúc',lambda: page.locator('#demo-toggle').click())
            do_action('CLICK','#demo-scan-employee','Quét lại thẻ nhân viên',lambda: page.locator('#demo-scan-employee').click(),value=data['employee_code'])
            do_action('CLICK','#demo-close','Đóng Demo Scanner',lambda: page.locator('#demo-close').click())
            do_action('WAIT','#screen-quantity-good','Chờ nhập số lượng đạt',lambda: page.locator('#screen-quantity-good.active').wait_for(timeout=10000),expected='Good quantity input visible')
            do_action('FOCUS','#good-qty','Focus số lượng đạt',lambda: page.locator('#good-qty').focus())
            do_action('TYPE','#good-qty','Nhập sản lượng đạt',lambda: page.locator('#good-qty').fill(str(good)),value=good); pause(.4)
            do_action('CLICK','#good-next','Xác nhận số lượng đạt',lambda: page.locator('#good-next').click())
            do_action('FOCUS','#defect-qty','Focus số lượng lỗi',lambda: page.locator('#defect-qty').focus())
            do_action('TYPE','#defect-qty','Nhập sản lượng lỗi',lambda: page.locator('#defect-qty').fill(str(defect)),value=defect); pause(.4)
            do_action('CLICK','#defect-next','Xác nhận số lượng lỗi',lambda: page.locator('#defect-next').click())
            if defect>0:
                do_action('WAIT','#screen-ask-rework','Chờ câu hỏi rework',lambda: page.locator('#screen-ask-rework.active').wait_for(timeout=10000))
                if rework>0:
                    do_action('CLICK','#rework-yes','Chọn có rework',lambda: page.locator('#rework-yes').click())
                    do_action('TYPE','#rework-qty','Nhập số lượng rework',lambda: page.locator('#rework-qty').fill(str(rework)),value=rework); pause(.4)
                    do_action('CLICK','#rework-next','Xác nhận rework',lambda: page.locator('#rework-next').click())
                else: do_action('CLICK','#rework-none','Chọn không rework',lambda: page.locator('#rework-none').click())
            do_action('WAIT','#screen-finish-confirm','Chờ màn xác nhận kết thúc',lambda: page.locator('#screen-finish-confirm.active').wait_for(timeout=10000)); pause(.8)
            do_action('CLICK','#finish-confirm-ok','Xác nhận hoàn tất session',lambda: page.locator('#finish-confirm-ok').click(),expected='Finished screen')
            do_action('WAIT','#screen-finished','Chờ Kiosk báo hoàn tất',lambda: page.locator('#screen-finished.active').wait_for(timeout=15000),expected='Session finished'); pause(.8)

        def safe_filter(selector, needle):
            loc=page.locator(selector)
            if loc.count() and loc.first.is_visible(): select_contains(loc.first,needle,selector); pause(.4)

        login(); emit('scenario_start',scenario=args.scenario,name=scenario['name']); write_state(status='RUNNING',current_step='login',current_title='Đăng nhập')

        actions={
            'overview': ('Tổng quan sản xuất', lambda: nav('overview')),
            'employee': ('Nhân viên demo', lambda: (nav('employees'), fill_visible('#employeeSearch',data['employee_code'],'#employeeSearch'))),
            'template': ('Template quy trình', lambda: (nav('templates'), fill_visible('#tplSearch',data['template_code'],'#tplSearch'))),
            'po': ('Production Order', lambda: (nav('production-orders'), fill_visible('#poSearch,input[placeholder*="Tìm"]',data['po_code'],'PO search'))),
            'kiosk': ('Kiosk: quét thẻ, Operation và nhập sản lượng', lambda: kiosk_once(8,1,1)),
            'kiosk_quality': ('Kiosk: lỗi và rework', lambda: kiosk_once(7,3,2)),
            'overview_after': ('Realtime Overview sau sản xuất', lambda: (page.goto(base+'/app',wait_until='domcontentloaded'), page.wait_for_selector('.nav-item[data-page="overview"]'), nav('overview'), safe_filter('#overviewPoFilter',data['po_code']))),
            'material': ('Gantt & Material Flow', lambda: (nav('production-schedule'), safe_filter('#schedulePoFilter',data['po_code']))),
            'session': ('Quản lý Session', lambda: (nav('session-management'), safe_filter('#smPo',data['po_code']))),
            'trace': ('Production Trace', lambda: nav('production-trace')),
            'audit': ('Nhật ký nghiệp vụ', lambda: nav('business-audit')),
            'dashboard': ('Dashboard theo ngày / KPI', lambda: nav('dashboard')),
            'exceptions': ('Trung tâm ngoại lệ', lambda: nav('session-exceptions')),
            'kiosk_mgmt': ('Quản lý trạm kiosk', lambda: nav('kiosk-management')),
            'employees': ('Danh mục nhân viên', lambda: nav('employees')),
            'qr': ('Danh sách QR Code', lambda: nav('qr-print')),
            'equipment': ('Thiết bị', lambda: nav('equipment')),
            'users': ('Người dùng & phân quyền', lambda: nav('users')),
            'calendar': ('Lịch làm việc', lambda: nav('working-calendar')),
            'logs': ('Nhật ký ứng dụng', lambda: nav('system-logs')),
        }
        for idx,key in enumerate(chapters,1):
            title,fn=actions[key]
            step(f'{idx:02d}-{key}',title,fn)
        failed=[r for r in results if r.get('status')=='FAIL']
        if failed:
            emit('scenario_fail',scenario=args.scenario,name=scenario['name'],steps=len(results),failed=len(failed))
            write_state(status='FAILED',current_step='',current_title=f'Hoàn tất với {len(failed)} testcase lỗi',
                        error=f'{len(failed)}/{len(results)} testcase failed',failed_cases=failed)
        else:
            emit('scenario_pass',scenario=args.scenario,name=scenario['name'],steps=len(results)); write_state(status='PASSED',current_step='',current_title='Hoàn tất')
        try: refresh_generated()
        except Exception as exc: ownership['inventory_error']=str(exc)
        ownership.update(status='FAILED' if failed else 'PASSED',finished_at=datetime.now().isoformat(timespec='seconds')); save_ownership()
        (out/'demo-data.json').write_text(json.dumps({'run_id':args.run_id,'data':data,'entities':entities},ensure_ascii=False,indent=2),encoding='utf-8')
        context.close(); browser.close()
    return 2 if any(r.get('status')=='FAIL' for r in results) else 0

if __name__=='__main__':
    try: raise SystemExit(main())
    except Exception as exc:
        emit('fatal',error=str(exc)); raise
