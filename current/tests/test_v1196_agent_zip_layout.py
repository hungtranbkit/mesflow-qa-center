from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_version_bumped():
    assert (ROOT/"VERSION").read_text(encoding="utf-8").strip()=="1.19.6"

def test_required_release_files_exist():
    for rel in ["VERSION","agent.py","docker/compose.yml","docker/runtime-manifest.json"]:
        assert (ROOT/rel).exists(), rel
