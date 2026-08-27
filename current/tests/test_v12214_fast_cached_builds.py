from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT.parent


def test_version_sync_v12214():
    version=(ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert (WRAPPER / "VERSION").read_text(encoding="utf-8").strip() == version
    assert f'APP_VERSION = "{version}"' in (ROOT / "agent.py").read_text(encoding="utf-8")


def test_heavy_dependencies_live_only_in_base_recipe():
    base = (ROOT / "docker" / "Dockerfile.base").read_text(encoding="utf-8")
    app = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert "playwright install --with-deps chromium" in base
    assert "postgresql-client-17" in base
    assert "pip install --no-cache-dir -r" in base
    assert "playwright install" not in app
    assert "apt-get install" not in app
    assert "ARG QA_BASE_IMAGE" in app
    assert "FROM ${QA_BASE_IMAGE}" in app


def test_release_builder_uses_dependency_fingerprint_and_cache():
    build = (WRAPPER / "scripts" / "build-release.sh").read_text(encoding="utf-8")
    assert 'Dockerfile.base' in build
    assert 'base_hash=' in build
    assert 'requirements.txt' in build
    assert 'docker image inspect "$base_image"' in build
    assert 'QA BASE CACHE HIT' in build
    assert 'QA BASE CACHE MISS' in build
    assert '--build-arg QA_BASE_IMAGE="$base_image"' in build
