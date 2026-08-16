from pathlib import Path
from version_contract import assert_version_contract
ROOT=Path(__file__).resolve().parents[1]

def test_version_bumped():
    assert_version_contract(ROOT)

def test_required_release_files_exist():
    for rel in ["VERSION","agent.py","docker/compose.yml","docker/runtime-manifest.json"]:
        assert (ROOT/rel).exists(), rel
