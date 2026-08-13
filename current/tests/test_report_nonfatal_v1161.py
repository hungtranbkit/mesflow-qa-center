from pathlib import Path


def test_report_errors_are_nonfatal_and_accumulated():
    root=Path(__file__).resolve().parents[1]
    text=(root/'scenarios/realtime_factory_soak_test.py').read_text(encoding='utf-8')
    assert "UI-REPORT-ERROR" in text
    assert "mô phỏng vẫn tiếp tục" in text
    block=text.split('def inspect_reports',1)[1].split('def main',1)[0]
    assert 'assert_ok(c.get(path)' not in block
