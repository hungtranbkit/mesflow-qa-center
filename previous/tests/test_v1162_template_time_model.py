from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_time_model_contract():
    s=(ROOT/'scenarios/realtime_factory_soak_test.py').read_text(encoding='utf-8')
    for token in ['standard_seconds_per_unit','plan_session','normal_variance_percent','MACHINE_JAM','planned_good_qty','max(500,args.planned_quantity_min)']:
        assert token in s

def test_ui_defaults_are_realistic():
    h=(ROOT/'templates/index.html').read_text(encoding='utf-8')
    assert 'value="500" min="500"' in h
    assert 'realtimeVariance' in h and 'max="30"' in h

def test_version():
    assert (ROOT/'VERSION').read_text(encoding="utf-8").strip()=='1.19.0'
