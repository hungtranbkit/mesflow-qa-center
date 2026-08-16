from pathlib import Path
from version_contract import assert_version_contract
ROOT=Path(__file__).resolve().parents[1]
def test_demo_names_and_version():
    core=(ROOT/'scenarios/lifecycle_core.py').read_text(encoding='utf-8')
    soak=(ROOT/'scenarios/realtime_factory_soak_test.py').read_text(encoding='utf-8')
    assert 'Nguyễn Văn Hùng' in core
    assert 'Vỏ tủ điện công nghiệp' in core
    assert 'QAV65817-' in core
    assert 'Trạm Cắt - Đột 01' in soak
    assert_version_contract(ROOT)
