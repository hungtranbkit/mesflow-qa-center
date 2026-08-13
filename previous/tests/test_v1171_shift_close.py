from pathlib import Path
from datetime import datetime
import importlib.util, random, sys
ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/'scenarios'/'realtime_factory_soak_test.py'
def load_mod():
    sys.path.insert(0,str(ROOT/'scenarios'))
    spec=importlib.util.spec_from_file_location('qa1171',SCRIPT)
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

def test_shift_end_day_and_night():
    m=load_mod(); cal={'work_start':'07:30','work_end':'17:00','working_weekdays':[0,1,2,3,4,5,6]}
    assert m.current_shift_end(datetime(2026,8,8,9,0),cal,'19:00','07:00') == datetime(2026,8,8,17,0)
    assert m.current_shift_end(datetime(2026,8,8,22,0),cal,'19:00','07:00') == datetime(2026,8,9,7,0)
    assert m.current_shift_end(datetime(2026,8,9,2,0),cal,'19:00','07:00') == datetime(2026,8,9,7,0)

def test_plan_duration_is_capped_by_remaining_shift():
    m=load_mod(); rng=random.Random(1171)
    op={'standard_seconds_per_unit':60,'code':'OP1','done_qty':0}
    po={'planned_quantity':10000}
    plan=m.plan_session(op,po,{},rng,120,1440,30,0,1.8,4,300,available_minutes=180)
    assert 120 <= plan['duration_minutes'] <= 180
    assert 120 <= plan['target_minutes'] <= 180

def test_ui_has_forgot_rate_and_new_version():
    h=(ROOT/'templates'/'index.html').read_text(encoding='utf-8')
    assert 'id="realtimeForgotRate"' in h
    assert 'value="4"' in h
    assert (ROOT/'VERSION').read_text(encoding="utf-8").strip()=='1.19.0'
