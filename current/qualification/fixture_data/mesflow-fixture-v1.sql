BEGIN;
INSERT INTO employees(employee_no,name,department,position,qr) VALUES
 ('QA-EMP-001','QA Operator One','QA','Operator','WF|EMP|QA-EMP-001'),
 ('QA-EMP-002','QA Operator Two','QA','Operator','WF|EMP|QA-EMP-002'),
 ('QA-EMP-ACTIVE','QA Active Operator','QA','Operator','WF|EMP|QA-EMP-ACTIVE');
INSERT INTO stations(code,name,workshop,production_line) VALUES ('QA-ST-001','QA Station','QA','QA-LINE');
INSERT INTO production_orders(code,product,planned_quantity,status,priority,due_date,notes) VALUES
 ('QA-PO-WAIT','QA Waiting Product',100,'PLANNED','NORMAL',CURRENT_DATE+7,'fixture:mesflow-fixture-v1'),
 ('QA-PO-RUN','QA Running Product',100,'IN_PROGRESS','HIGH',CURRENT_DATE+2,'fixture:mesflow-fixture-v1'),
 ('QA-PO-LATE','QA Late Product',50,'IN_PROGRESS','HIGH',CURRENT_DATE-2,'fixture:mesflow-fixture-v1'),
 ('QA-PO-DONE','QA Completed Product',10,'COMPLETED','NORMAL',CURRENT_DATE-1,'fixture:mesflow-fixture-v1');
INSERT INTO parts(production_order_id,code,name,sort_order)
SELECT id,code||'-PART','Fixture Part',1 FROM production_orders WHERE code LIKE 'QA-PO-%';
INSERT INTO operations(production_order_id,part_id,code,name,done_qty,defect_qty,rework_qty,status,sort_order,qr)
SELECT po.id,p.id,po.code||'-OP','Fixture Operation',
 CASE WHEN po.code='QA-PO-DONE' THEN 10 ELSE 0 END,0,0,
 CASE WHEN po.code='QA-PO-DONE' THEN 'COMPLETED' WHEN po.status='IN_PROGRESS' THEN 'IN_PROGRESS' ELSE 'PLANNED' END,
 1,'WF|OP|'||po.code||'-OP'
FROM production_orders po JOIN parts p ON p.production_order_id=po.id WHERE po.code LIKE 'QA-PO-%';
INSERT INTO work_sessions(employee_id,operation_id,station_id,status,started_at,ended_at,good_qty,defect_qty,rework_qty,note,start_request_id,finish_request_id)
SELECT e.id,o.id,s.id,'CLOSED',CURRENT_TIMESTAMP-INTERVAL '2 days',CURRENT_TIMESTAMP-INTERVAL '2 days'+INTERVAL '1 hour',10,0,0,
 'fixture historical','QA-HIST-START','QA-HIST-FINISH'
FROM employees e,operations o,stations s WHERE e.employee_no='QA-EMP-001' AND o.code='QA-PO-DONE-OP' AND s.code='QA-ST-001';
INSERT INTO work_sessions(employee_id,operation_id,station_id,status,started_at,good_qty,defect_qty,rework_qty,note,start_request_id)
SELECT e.id,o.id,s.id,'OPEN',CURRENT_TIMESTAMP-INTERVAL '10 minutes',0,0,0,'fixture active','QA-ACTIVE-START'
FROM employees e,operations o,stations s WHERE e.employee_no='QA-EMP-ACTIVE' AND o.code='QA-PO-RUN-OP' AND s.code='QA-ST-001';
COMMIT;
