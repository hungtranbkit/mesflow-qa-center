from pathlib import Path
import importlib.util
import sys
from version_contract import assert_version_contract

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/'scenarios'/'realtime_factory_soak_test.py'

def load_mod():
    sys.path.insert(0,str(ROOT/'scenarios'))
    spec=importlib.util.spec_from_file_location('qa1170',SCRIPT)
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

def test_v1170_contract_tokens():
    s=SCRIPT.read_text(encoding='utf-8')
    for token in ['repairable_qty','discover_finish_quality_fields','NEGATIVE-CASE-REJECTED','INPUT_MISMATCH','choose_target_minutes','night-shift-start','default=20','default=120','default=1440']:
        assert token in s

def test_long_session_distribution_and_bounds():
    import random
    m=load_mod(); rng=random.Random(1170)
    vals=[m.choose_target_minutes(rng,120,1440) for _ in range(1000)]
    assert min(vals)>=120 and max(vals)<=1440
    assert sum(v>=1152 for v in vals) > 650

def test_night_shift_crosses_midnight():
    from datetime import datetime
    m=load_mod(); cal={'work_start':'07:30','lunch_start':'11:30','lunch_end':'13:00','work_end':'17:00','working_weekdays':[0,1,2,3,4,5,6]}
    assert m.is_work_time(datetime(2026,8,7,22,0),cal,'19:00','07:00')
    assert m.is_work_time(datetime(2026,8,8,2,0),cal,'19:00','07:00')
    assert not m.is_work_time(datetime(2026,8,7,18,0),cal,'19:00','07:00')

def test_ui_defaults():
    h=(ROOT/'templates'/'index.html').read_text(encoding='utf-8')
    assert 'id="realtimeWorkers" type="number" value="20"' in h
    assert 'id="realtimeTargetMin" type="number" value="120"' in h
    assert 'id="realtimeTargetMax" type="number" value="1440"' in h

def test_version():
    assert_version_contract(ROOT)
