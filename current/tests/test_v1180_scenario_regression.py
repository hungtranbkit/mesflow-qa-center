from pathlib import Path
from datetime import datetime
import importlib.util, random, sys
ROOT=Path(__file__).resolve().parents[1]; SCRIPT=ROOT/'scenarios'/'realtime_factory_soak_test.py'

def load_mod():
    sys.path.insert(0,str(ROOT/'scenarios')); spec=importlib.util.spec_from_file_location('qa1180',SCRIPT); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def test_shift_boundaries_and_no_cross_shift_session():
    m=load_mod(); cal={'work_start':'07:30','work_end':'17:00','working_weekdays':[0,1,2,3,4,5,6]}
    assert m.current_shift_end(datetime(2026,8,9,9),cal,'19:00','07:00')==datetime(2026,8,9,17)
    assert m.current_shift_end(datetime(2026,8,9,22),cal,'19:00','07:00')==datetime(2026,8,10,7)
    plan=m.plan_session({'standard_seconds_per_unit':60,'done_qty':0,'code':'OP'},{'planned_quantity':9999},{},random.Random(1),120,1440,30,0,1.8,4,300,available_minutes=150)
    assert 120<=plan['duration_minutes']<=150

def test_quality_never_has_fixable_over_defect():
    m=load_mod(); rng=random.Random(5)
    for good in [0,1,10,100,1000]:
        for _ in range(100):
            defect,fixable=m.quality_quantities(good,rng); assert 0<=fixable<=defect<=max(good,defect)

def test_target_duration_distribution_bounds():
    m=load_mod(); vals=[m.choose_target_minutes(random.Random(i),120,1440) for i in range(500)]
    assert min(vals)>=120 and max(vals)<=1440 and sum(v>=1152 for v in vals)>300

def test_negative_finish_case_catalog_present():
    text=SCRIPT.read_text(encoding='utf-8')
    for case in ['NEGATIVE_QTY','FIXABLE_GT_DEFECT','OUTPUT_OVER_PLAN','INPUT_MISMATCH','NEGATIVE-CASE-REJECTED','NEGATIVE-CASE-ACCEPTED']:
        assert case in text

def test_reports_cover_operational_surfaces():
    text=SCRIPT.read_text(encoding='utf-8')
    for path in ['/api/dashboard/summary','/api/dashboard/control-tower','/api/dashboard/active-sessions','/api/dashboard/daily-progress','/api/dashboard/daily-sessions','/api/dashboard/recent-activity','/api/production-schedule','/api/kiosk-management/overview','/api/reports/operation-sessions','/api/reports/employee-performance','/api/kpi/employees','/api/kpi/operations']:
        assert path in text
