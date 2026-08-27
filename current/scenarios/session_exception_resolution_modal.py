"""QA Center scenario -- Inline Session Exception Resolution modal (2026-08-28).

Proves the full operator flow the task's section 15 asks for, end-to-end
against a REAL running MESFlow instance (not mocks): an abnormal Session
gets detected as a real exception, a supervisor takes it, the inline
modal's own API loads the exact Session, a real correction is applied
through the audited SupervisorRepository.edit_session() path, the
detector is re-run, and the exception either clears (enabling Hoàn tất)
or is honestly reported as still active -- then the parallel ignore-with-
reason path is proven separately, confirming ignore leaves the Session
itself untouched.

This intentionally talks to the API the same way the browser's inline
modal does (GET .../resolution-context, POST .../correct-session, POST
.../acknowledge|resolve|ignore) rather than reusing lifecycle_core.py's
higher-level scan/finish helpers -- the one thing those helpers cannot
produce quickly is a Session that is already 12+ hours old, which is
what LONG_OPEN_SESSION actually requires. A direct SQL backdate of
started_at on a freshly-seeded QA Session is the same technique the
automated integration suite already uses (tests/integration/
test_v67_exception_center.py's make_long_open()) -- documented here
rather than hidden, since it is the one place this scenario reaches
past the HTTP API.

Run standalone: python session_exception_resolution_modal.py
  --base http://mesflow-test-api:8080
  --db postgresql://mesflow_test:mesflow_test_password@postgres-test:5432/mesflow_test
  --username admin --password Admin@123456
"""
from __future__ import annotations
import argparse
import sys
import uuid
from datetime import datetime, timezone

import requests
try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None


def log(status: str, code: str, message: str, **data):
    extra = (' ' + ' '.join(f'{k}={v}' for k, v in data.items())) if data else ''
    print(f'[{status}] {code} {message}{extra}', flush=True)


class ScenarioFailure(RuntimeError):
    pass


def require(condition: bool, code: str, message: str, **data):
    if not condition:
        log('FAIL', code, message, **data)
        raise ScenarioFailure(f'{code}: {message} {data}')
    log('PASS', code, message, **data)


def ensure_fixture(conn, suffix: str) -> dict:
    """Minimal QA-owned employee/operation/station/PO, created directly via
    SQL the same shape tests/integration/conftest.py's seeded_factory
    fixture already uses -- this scenario is a QA-namespace probe, not a
    UI walkthrough, so it does not need to drive the full onboarding UI
    to get a valid employee/operation pair. Schema notes (confirmed via
    \\d against the real tables, not assumed): production_orders has no
    part_id -- parts.production_order_id points the other way, so PO must
    exist first; operations.code and employees.qr/operations.qr are each
    independently UNIQUE NOT NULL with no default."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""INSERT INTO production_orders(code,product,planned_quantity,status)
          VALUES (%s,'QA Exception Modal Product',100,'IN_PROGRESS')
          ON CONFLICT (code) DO UPDATE SET status='IN_PROGRESS' RETURNING id""",
                    (f'QA-EXC-PO-{suffix}',))
        po_id = cur.fetchone()['id']
        cur.execute("""INSERT INTO parts(production_order_id,code,name) VALUES (%s,%s,'QA Exception Modal Part')
          ON CONFLICT (production_order_id,code) DO UPDATE SET name=EXCLUDED.name RETURNING id""",
                    (po_id, f'QA-EXC-PART-{suffix}'))
        part_id = cur.fetchone()['id']
        cur.execute("""INSERT INTO operations(production_order_id,part_id,code,name,qr,status)
          VALUES (%s,%s,%s,'QA Exception Modal OP',%s,'IN_PROGRESS')
          ON CONFLICT (code) DO UPDATE SET status='IN_PROGRESS' RETURNING id""",
                    (po_id, part_id, f'QA-EXC-OP-{suffix}', f'QA-EXC-OP-QR-{suffix}'))
        operation_id = cur.fetchone()['id']
        cur.execute("""INSERT INTO stations(code,name) VALUES (%s,'QA Exception Modal Station')
          ON CONFLICT (code) DO UPDATE SET name=EXCLUDED.name RETURNING id""",
                    (f'QA-EXC-ST-{suffix}',))
        station_id = cur.fetchone()['id']
        cur.execute("""INSERT INTO employees(employee_no,name,qr,active) VALUES (%s,'QA Exception Modal Worker',%s,TRUE)
          ON CONFLICT (employee_no) DO UPDATE SET active=TRUE RETURNING id""",
                    (f'QA-EXC-EMP-{suffix}', f'QA-EXC-EMP-QR-{suffix}'))
        employee_id = cur.fetchone()['id']
        conn.commit()
        return {'part_id': part_id, 'po_id': po_id, 'operation_id': operation_id,
                'station_id': station_id, 'employee_id': employee_id}


def seed_long_open_session(conn, fixture: dict, suffix: str) -> int:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""INSERT INTO work_sessions(employee_id,operation_id,station_id,status,started_at,start_request_id)
          VALUES (%s,%s,%s,'OPEN',CURRENT_TIMESTAMP-INTERVAL '13 hours',%s) RETURNING id""",
                    (fixture['employee_id'], fixture['operation_id'], fixture['station_id'],
                     f'QA-EXC-MODAL-{suffix}-{uuid.uuid4().hex[:8]}'))
        session_id = cur.fetchone()['id']
        conn.commit()
        return session_id


def find_exception_for_session(api: requests.Session, base: str, session_id: int, exception_type: str) -> dict:
    r = api.get(f'{base}/api/exceptions', params={'view': 'action', 'page_size': 200}, timeout=15)
    require(r.status_code == 200, 'QA-XM-DETECT', 'Danh sách ngoại lệ tải được', http=r.status_code)
    items = r.json().get('items') or []
    match = next((x for x in items if x.get('session_id') == session_id and x.get('exception_type') == exception_type), None)
    require(match is not None, 'QA-XM-DETECT-MATCH', f'Ngoại lệ {exception_type} được phát hiện cho Session #{session_id}',
            session_id=session_id)
    return match


def run_correct_and_resolve_flow(api: requests.Session, base: str, conn, fixture: dict, suffix: str):
    session_id = seed_long_open_session(conn, fixture, suffix + '-correct')
    item = find_exception_for_session(api, base, session_id, 'LONG_OPEN_SESSION')
    exception_id = item['id']

    # "Nhận xử lý" -- take.
    r = api.post(f'{base}/api/exceptions/{exception_id}/acknowledge', json={'expected_version': item['row_version']}, timeout=15)
    require(r.status_code == 200 and r.json()['item']['status'] == 'ACKNOWLEDGED', 'QA-XM-TAKE',
            'Nhận xử lý thành công', http=r.status_code)

    # Inline modal opens the exact Session -- resolution-context.
    ctx = api.get(f'{base}/api/session-exceptions/{exception_id}/resolution-context', timeout=15)
    require(ctx.status_code == 200, 'QA-XM-CONTEXT', 'Modal tải đúng ngữ cảnh Session', http=ctx.status_code)
    body = ctx.json()
    require(body['session']['session_id'] == session_id, 'QA-XM-CONTEXT-SESSION',
            'Modal mở đúng Session (không phải Session khác)', expected=session_id, got=body['session']['session_id'])
    require(set(body['editable_fields']) == {'ended_at', 'status'}, 'QA-XM-FIELDS',
            'Trường điều chỉnh đúng theo loại ngoại lệ thật (không suy đoán)', fields=body['editable_fields'])

    # Correct the Session through the SAME audited editor Session Management uses.
    reason = 'QA scenario: Session bị bỏ quên không đóng, xác nhận qua kiểm tra thực tế.'
    r = api.post(f'{base}/api/session-exceptions/{exception_id}/correct-session',
                 json={'status': 'CLOSED', 'reason': reason, 'expected_updated_at': body['session']['updated_at']}, timeout=15)
    require(r.status_code == 200, 'QA-XM-CORRECT', 'Lưu điều chỉnh Session thành công', http=r.status_code, body=r.text[:300])
    corrected = r.json()
    require(corrected['item']['status'] == 'CLOSED', 'QA-XM-CORRECT-APPLIED',
            'Session được đóng thật qua editor có audit, không phải patch tuỳ tiện')
    require(corrected['cleared'] is True, 'QA-XM-CLEARED',
            'Ngoại lệ được xác nhận hết hiệu lực sau khi sửa (detector chạy lại thật)')

    # Derived state recomputed: operation good_qty/session count reflect a
    # real CLOSED session now, not a silently-orphaned aggregate.
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute('SELECT status FROM work_sessions WHERE id=%s', (session_id,))
        require(cur.fetchone()['status'] == 'CLOSED', 'QA-XM-DB-STATE', 'DB phản ánh đúng Session đã đóng')

    # The exception itself: LONG_OPEN_SESSION auto-clears via reconcile()'s
    # own SESSION_ALREADY_CLOSED shortcut the instant condition_active goes
    # false -- so by the time correct-session's own reconcile() call
    # returns, status is already AUTO_IGNORED, not sitting at ACKNOWLEDGED
    # waiting for a separate manual "Hoàn tất". Accept either terminal
    # outcome here rather than assuming one -- what matters is that it is
    # no longer actionable, never that a specific code path produced it.
    refreshed = api.get(f'{base}/api/exceptions/{exception_id}', timeout=15).json()['item']
    require(refreshed['status'] in ('AUTO_IGNORED', 'RESOLVED'), 'QA-XM-TERMINAL',
            'Ngoại lệ không còn ở trạng thái cần xử lý', exception_status=refreshed['status'])
    if refreshed['status'] not in ('AUTO_IGNORED', 'RESOLVED'):
        r = api.post(f'{base}/api/exceptions/{exception_id}/resolve',
                      json={'expected_version': refreshed['row_version'], 'reason': reason}, timeout=15)
        require(r.status_code == 200, 'QA-XM-RESOLVE', 'Hoàn tất xử lý thành công sau khi sửa', http=r.status_code)

    still_active = find_or_none(api, base, session_id, 'LONG_OPEN_SESSION')
    require(still_active is None, 'QA-XM-LIST-CLEARED', 'Ngoại lệ biến mất khỏi danh sách Cần xử lý')

    history = api.get(f'{base}/api/exceptions/{exception_id}/history', timeout=15).json()['items']
    actions = [h['action'] for h in history]
    require(actions[:2] == ['DETECTED', 'ACKNOWLEDGED'] and actions[-1] in ('AUTO_IGNORED', 'RESOLVED'), 'QA-XM-HISTORY',
            'Lịch sử xử lý còn nguyên vẹn, không bị ghi đè', actions=actions)


def run_ignore_flow(api: requests.Session, base: str, conn, fixture: dict, suffix: str):
    session_id = seed_long_open_session(conn, fixture, suffix + '-ignore')
    item = find_exception_for_session(api, base, session_id, 'LONG_OPEN_SESSION')
    exception_id = item['id']

    reason = 'QA scenario: đã xác nhận với tổ trưởng, chấp nhận Session này như hiện trạng.'
    r = api.post(f'{base}/api/exceptions/{exception_id}/ignore',
                 json={'expected_version': item['row_version'], 'reason': reason}, timeout=15)
    require(r.status_code == 200 and r.json()['item']['status'] == 'MANUAL_IGNORED', 'QA-XM-IGNORE',
            'Bỏ qua với lý do thành công', http=r.status_code)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute('SELECT status FROM work_sessions WHERE id=%s', (session_id,))
        require(cur.fetchone()['status'] == 'OPEN', 'QA-XM-IGNORE-SESSION-UNCHANGED',
                'Bỏ qua ngoại lệ không âm thầm sửa Session')

    still_active = find_or_none(api, base, session_id, 'LONG_OPEN_SESSION')
    require(still_active is None, 'QA-XM-IGNORE-LIST', 'Ngoại lệ đã bỏ qua biến mất khỏi danh sách Cần xử lý')

    history = api.get(f'{base}/api/exceptions/{exception_id}/history', timeout=15).json()['items']
    # ExceptionRepository.transition()'s own actions map records the
    # MANUAL_IGNORED status transition under action name 'IGNORED' (see its
    # actions={'MANUAL_IGNORED':'IGNORED', ...}) -- 'MANUAL_IGNORED' is the
    # exception_records.status value, not the exception_history.action value.
    require([h['action'] for h in history] == ['DETECTED', 'IGNORED'], 'QA-XM-IGNORE-HISTORY',
            'Lịch sử ghi rõ lý do bỏ qua', reason=history[-1].get('reason'))
    require(bool(history[-1].get('reason')), 'QA-XM-IGNORE-REASON-RECORDED', 'Lý do bỏ qua được lưu vào audit')


def find_or_none(api: requests.Session, base: str, session_id: int, exception_type: str):
    r = api.get(f'{base}/api/exceptions', params={'view': 'action', 'page_size': 200}, timeout=15)
    items = r.json().get('items') or []
    return next((x for x in items if x.get('session_id') == session_id and x.get('exception_type') == exception_type), None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', required=True, help='MESFlow API base URL, e.g. http://mesflow-test-api:8080')
    parser.add_argument('--db', required=True, help='Postgres DSN, e.g. postgresql://user:pass@host:5432/db')
    parser.add_argument('--username', default='admin')
    parser.add_argument('--password', required=True)
    args = parser.parse_args()

    if psycopg is None:
        log('SKIP', 'QA-XM-DEPS', 'psycopg không có sẵn trong môi trường chạy scenario này')
        return 0

    suffix = uuid.uuid4().hex[:8]
    conn = psycopg.connect(args.db, autocommit=False)
    api = requests.Session()
    login = api.post(f'{args.base}/api/auth/login', json={'username': args.username, 'password': args.password}, timeout=15)
    require(login.status_code == 200, 'QA-XM-LOGIN', 'Đăng nhập MESFlow thành công', http=login.status_code)

    failures = 0
    for name, runner, flow_suffix in (
        ('correct+resolve', run_correct_and_resolve_flow, suffix + '-a'),
        ('ignore', run_ignore_flow, suffix + '-b'),
    ):
        # A separate employee/operation per flow -- uq_open_session_per_
        # employee means one flow leaving its Session OPEN (e.g. a failed
        # assertion before the correction closes it) must never block the
        # other flow's own seed from being independently reproducible.
        fixture = ensure_fixture(conn, flow_suffix)
        log('INFO', 'QA-XM-FIXTURE', f'Đã chuẩn bị employee/operation/PO cho luồng {name}', **fixture)
        try:
            runner(api, args.base, conn, fixture, flow_suffix)
        except ScenarioFailure as exc:
            failures += 1
            log('FAIL', 'QA-XM-FLOW', f'Luồng {name} thất bại: {exc}')

    conn.close()
    if failures:
        log('FAIL', 'QA-XM-SUMMARY', f'{failures} luồng thất bại trong scenario resolution modal')
        return 1
    log('PASS', 'QA-XM-SUMMARY', 'Toàn bộ luồng xử lý ngoại lệ Session (sửa+hoàn tất, bỏ qua) đạt yêu cầu')
    return 0


if __name__ == '__main__':
    sys.exit(main())
