from engine import qa_store
from qualification.coverage import report


def test_registry_has_explicit_honest_denominator(tmp_path):
    qa_store.reset_for_tests(tmp_path / "meta.sqlite3")
    result = report([])
    assert result["total_features"] >= 20
    assert result["covered_features"] == 0
    assert result["critical_features"] > 0
    assert result["critical_feature_coverage_percent"] == 0
    assert any(item["key"] == "kiosk.offline_recovery" for item in result["features"])
