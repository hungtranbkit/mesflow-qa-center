#!/usr/bin/env bash
# Build Once / Promote Same Artifact for QA Center. Mirrors
# mesflow/scripts/build-release.sh exactly (same immutable-once guard, same
# tag-contamination guard, same reproducible-build flags): QA source lives
# under current/; released artifacts land in
# artifacts/qa-center/releases/<version>/. Never rebuilds a version that
# has already been frozen.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
die(){ echo "ERROR: $*" >&2; exit 1; }
command -v docker >/dev/null || die "DOCKER_NOT_FOUND"

SRC="$ROOT/current"
[[ -d "$SRC" ]] || die "SOURCE_NOT_FOUND: $SRC"
[[ -f "$SRC/VERSION" ]] || die "VERSION file missing: $SRC/VERSION"
[[ -f "$ROOT/compose.yml" ]] || die "compose.yml missing at $ROOT (deployment-target compose)"
version="$(tr -d '[:space:]' < "$SRC/VERSION")"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?$ ]] || die "VERSION_INVALID: expected X.Y.Z or X.Y.Z.W, got '$version'"

# QA declares its version twice -- current/VERSION (read by Deploy Agent /
# this script) and agent.py's APP_VERSION constant (what /api/version
# actually reports at runtime). Same spirit as MESFlow's multi-file version
# sync rule in AGENTS.md: refuse to freeze a release where they disagree.
declared="$(grep -m1 '^APP_VERSION = ' "$SRC/agent.py" | sed -E 's/APP_VERSION = "([^"]+)"/\1/')"
[[ -n "$declared" ]] || die "Could not read APP_VERSION from $SRC/agent.py"
[[ "$declared" == "$version" ]] || die "VERSION_MISMATCH: current/VERSION=$version but agent.py APP_VERSION=$declared -- keep both in sync before building."

image="${MESFLOW_QA_IMAGE_REPOSITORY:-mesflow-qa-center}:$version"
dist="$ROOT/../artifacts/qa-center/releases/$version"

# --- immutable release guard, identical policy to mesflow/scripts/build-release.sh ---
if [[ -f "$dist/release.json" ]]; then
  die "VERSION_ALREADY_RELEASED: artifacts/qa-center/releases/$version/release.json already exists (frozen). A version may be built only once. Bump current/VERSION (and agent.py's APP_VERSION) and rebuild."
fi
mkdir -p "$dist"

# Resolve/build the dependency image before running tests. Tests execute in
# this same dependency environment with the full wrapper/current source tree
# bind-mounted, so host Python packages can neither create false failures nor
# silently skip the release gate.
base_recipe="$SRC/docker/Dockerfile.base"
[[ -f "$base_recipe" ]] || die "QA_BASE_DOCKERFILE_NOT_FOUND: $base_recipe"
base_hash="$(cat "$base_recipe" "$SRC/requirements.txt" | sha256sum | awk '{print $1}')"
base_image="${MESFLOW_QA_BASE_IMAGE_REPOSITORY:-mesflow-qa-base}:py312-${base_hash:0:16}"
if docker image inspect "$base_image" >/dev/null 2>&1; then
  echo "QA BASE CACHE HIT: $base_image"
else
  echo "QA BASE CACHE MISS: building $base_image (first build or dependencies changed)"
  docker build -f current/docker/Dockerfile.base -t "$base_image" current
fi

# --- required tests ---------------------------------------------------
# A release may never be frozen when its source suite fails. Historical tests
# must express historical fixtures explicitly; stale current-version
# assertions are test debt to fix, never a reason to publish a FAILED build.
test_log="$dist/.test-output.tmp"
test_source="$ROOT"
# When this script runs inside Deploy Agent, its Docker CLI talks to the
# host daemon through /var/run/docker.sock. A bind source must therefore be
# a HOST path, not the container-only /workspace/... path. Resolve the
# source of the enclosing bind dynamically from this container's mounts;
# direct host execution keeps using $ROOT unchanged.
if [[ -f /.dockerenv ]]; then
  container_id="$(hostname)"
  while IFS=$'\t' read -r destination source; do
    [[ -n "$destination" && -n "$source" ]] || continue
    case "$ROOT/" in
      "$destination"/*)
        relative="${ROOT#"$destination"}"
        test_source="${source}${relative}"
        break
        ;;
    esac
  done < <(docker inspect "$container_id" --format '{{range .Mounts}}{{printf "%s\t%s\n" .Destination .Source}}{{end}}')
  [[ "$test_source" != "$ROOT" ]] || die "DOCKER_HOST_SOURCE_UNRESOLVED: cannot map container source $ROOT to a host bind path"
fi
echo "QA TEST SOURCE: $test_source"
if ! docker run --rm -v "$test_source:/source:ro" -w /tmp "$base_image" \
    sh -lc 'cp -a /source /tmp/workspace && cd /tmp/workspace/current && python3 -m pip install --quiet pytest && python3 -m pytest -q' > "$test_log" 2>&1; then
  cp "$test_log" "$dist/TEST_REPORT.txt"
  die "TESTS_FAILED: QA Center source suite failed; see $dist/TEST_REPORT.txt. No image/package was frozen."
fi
test_status="PASS"
test_summary="$(tail -3 "$test_log" | tr '\n' ' ' | sed 's/  */ /g')"

# --- build --------------------------------------------------------------
# Plain `docker build`, matching mesflow/scripts/build-release.sh exactly --
# no BuildKit-only flags (--provenance/--sbom/DOCKER_BUILDKIT=1). An earlier
# version of this script forced those to suppress BuildKit's build
# attestation layer (which can make the image id differ between two builds
# of byte-identical source). That is real, but the Deploy Agent's own
# container only has the legacy (non-BuildKit) docker CLI builder available
# -- no buildx component -- so forcing BuildKit here made the QA build fail
# outright when triggered through the real Agent ("buildx component is
# missing or broken"), even though it worked from an interactive host shell
# that defaults to BuildKit. The version-based immutable-once guard (a
# release is refused if artifacts/qa-center/releases/<version>/release.json
# already exists, checked before this line ever runs) does not depend on
# image-id determinism; the tag-contamination check right below only cares
# that an existing `image` tag isn't silently repointed at different bytes,
# which holds regardless of builder.
# Build/reuse a fingerprinted heavy QA base image. This turns normal QA
# releases into a thin source-copy build while preserving automatic
# invalidation whenever browser/system/Python dependencies change.
build_tag="${image}__building_$$"
cleanup_build_tag(){ docker rmi "$build_tag" >/dev/null 2>&1 || true; }
trap cleanup_build_tag EXIT
echo "QA APP BUILD: thin source layer on $base_image"
docker build --build-arg QA_BASE_IMAGE="$base_image" --build-arg QA_RUNTIME_VERSION="$version" -f current/docker/Dockerfile -t "$build_tag" current
new_id="$(docker image inspect "$build_tag" --format '{{.Id}}')"
[[ "$new_id" == sha256:* ]] || die "IMAGE_ID_UNAVAILABLE"
if docker image inspect "$image" >/dev/null 2>&1; then
  existing_id="$(docker image inspect "$image" --format '{{.Id}}')"
  if [[ "$existing_id" != "$new_id" ]]; then
    die "IMAGE_TAG_CONTAMINATED: $image already exists (id=$existing_id) and differs from the image just built from current source (id=$new_id). A version may be built only once. Bump the version and rebuild -- never retag over an existing QA release."
  fi
  echo "NOTE: $image already exists and matches the freshly built image exactly (idempotent rebuild, no source change) — proceeding."
fi
docker tag "$build_tag" "$image"
image_id="$new_id"
digest="$image_id"
source_commit="$(git -C "$ROOT" -c safe.directory='*' rev-parse HEAD 2>/dev/null || echo unknown)"
built_at="$(date -Is)"

tmp="$(mktemp -d)"
cleanup_all(){ cleanup_build_tag; rm -rf "$tmp"; }
trap cleanup_all EXIT
root="$tmp/qa-center-release"; mkdir -p "$root"
docker save "$image" -o "$root/QACenter_${version}.tar"
cp "$ROOT/compose.yml" "$root/compose.yml"
printf '%s' "$version" > "$root/VERSION"
# QA Center has no database of its own (state/logs/reports are JSON/log
# files under /data, not SQL) -- never invent a schema_revision it doesn't
# have. schema_revision stays null; requires_migration stays false.
cat > "$root/release.json" <<JSON
{"type":"qa-center-image-release","application":"qa-center","version":"$version","image":"$image","image_digest":"$digest","image_id":"$image_id","source_commit":"$source_commit","built_at":"$built_at","release_type":"image","schema_revision":null,"requires_migration":false,"distribution":"bundle","bundle":"QACenter_${version}.tar"}
JSON
cat > "$root/PROMOTION.json" <<JSON
{"application":"qa-center","version":"$version","image_digest":"$digest","source_commit":"$source_commit","local":{"status":"NOT_DEPLOYED"},"production_test":{"status":"NOT_DEPLOYED"},"production":{"status":"NOT_DEPLOYED"}}
JSON

# --- provenance: previous release + changed files + diff stat + commit log
# Frozen once, here, at build time (task rule: never regenerated at deploy
# time). "Previous release" = the most recently frozen
# artifacts/qa-center/releases/*/release.json found on disk right now
# (this build's own $dist does not exist yet -- the immutable-once guard
# above already refused to proceed if it did). manifest.json/CHANGELOG.md
# are written into the zip staging root so they ride inside the immutable
# ZIP like compose.yml/release.json do, then also copied to $dist for local
# browsing. Deploy Agent's validate_qa_image_release_contract() only checks
# for its five required files (VERSION/release.json/compose.yml/
# checksums.txt/PROMOTION.json) and does not reject extra files, so this is
# purely additive -- no package-contract change.
python3 - "$ROOT" "$version" "$image" "$digest" "$source_commit" "$built_at" "$root" <<'PY'
import json, subprocess, sys, pathlib
repo_root, version, image, digest, source_commit, built_at, staging = sys.argv[1:8]
repo_root = pathlib.Path(repo_root); staging = pathlib.Path(staging)
releases_root = (repo_root / ".." / "artifacts" / "qa-center" / "releases").resolve()

candidates = []
if releases_root.is_dir():
    for d in releases_root.iterdir():
        rj = d / "release.json"
        if rj.is_file() and d.name != version:
            try:
                meta = json.loads(rj.read_text(encoding="utf-8-sig"))
                candidates.append((rj.stat().st_mtime, meta))
            except Exception:
                pass
candidates.sort(key=lambda x: x[0], reverse=True)
previous = candidates[0][1] if candidates else None
previous_version = previous.get("version", "") if previous else ""
previous_commit = previous.get("source_commit", "") if previous else ""

changed_files, diff_stat, change_summary = [], "N/A", "Initial release (no previous frozen QA release found)."
if previous_commit and previous_commit != "unknown":
    check = subprocess.run(["git", "-C", str(repo_root), "cat-file", "-e", previous_commit + "^{commit}"], capture_output=True)
    if check.returncode == 0:
        cf = subprocess.run(["git", "-C", str(repo_root), "diff", "--name-only", previous_commit, "HEAD"], capture_output=True, text=True)
        changed_files = [l.strip() for l in cf.stdout.splitlines() if l.strip()]
        ds = subprocess.run(["git", "-C", str(repo_root), "diff", "--shortstat", previous_commit, "HEAD"], capture_output=True, text=True)
        diff_stat = ds.stdout.strip() or "No changes"
        cl = subprocess.run(["git", "-C", str(repo_root), "log", "--oneline", previous_commit + "..HEAD"], capture_output=True, text=True)
        change_summary = cl.stdout.strip() or "No commits between previous release and HEAD."
    else:
        change_summary = f"Previous release commit {previous_commit} not found in this checkout (shallow clone?); diff unavailable."

manifest = {
    "package_type": "qa-center-release-package",
    "application": "qa-center",
    "version": version,
    "image": image,
    "image_digest": digest,
    "source_commit": source_commit,
    "built_at": built_at,
    # artifact_sha256 of the outer deploy.zip cannot be known yet (the zip
    # is built from this staging dir *after* this script runs, and can't
    # contain its own hash) -- build-release.sh fills it in on the $dist
    # copy of this file once the zip + its .sha256 sidecar exist.
    "artifact_sha256": None,
    "previous_release": {"version": previous_version, "source_commit": previous_commit} if previous else None,
    "changed_files": changed_files,
    "changed_files_count": len(changed_files),
    "diff_stat": diff_stat,
    "change_summary": change_summary,
}
(staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

lines = [
    f"# QA Center {version}", "",
    f"- Built: {built_at}",
    f"- Source commit: {source_commit}",
    "- Previous release: " + (f"{previous_version} (commit {previous_commit})" if previous else "(none -- first frozen release)"),
    "", "## Diff stat", "", "```", diff_stat, "```", "",
    f"## Changed files ({len(changed_files)})", "",
] + ([f"- {f}" for f in changed_files] or ["(none)"]) + [
    "", "## Commits", "", "```", change_summary, "```", "",
]
(staging / "CHANGELOG.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

(cd "$root" && sha256sum "QACenter_${version}.tar" compose.yml release.json VERSION PROMOTION.json manifest.json CHANGELOG.md > checksums.txt)
cp "$root/release.json" "$dist/release.json"
cp "$root/checksums.txt" "$dist/checksums.txt"
cp "$root/PROMOTION.json" "$dist/PROMOTION.json"
cp "$root/manifest.json" "$dist/manifest.json"
cp "$root/CHANGELOG.md" "$dist/CHANGELOG.md"
cp "$test_log" "$dist/TEST_REPORT.txt"; rm -f "$test_log"
printf '{"image":"%s","digest":"%s","source_commit":"%s","version":"%s"}\n' "$image" "$digest" "$source_commit" "$version" > "$dist/image-info.json"

package="$dist/QACenter_${version}.deploy.zip"
if command -v zip >/dev/null 2>&1; then
  (cd "$tmp" && zip -qr "$package" qa-center-release)
else
  python3 - "$tmp" "$package" <<'PY'
import pathlib, sys, zipfile
root = pathlib.Path(sys.argv[1]); out = pathlib.Path(sys.argv[2])
with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
    for path in (root / "qa-center-release").rglob("*"):
        if path.is_file():
            z.write(path, path.relative_to(root).as_posix())
PY
fi
(cd "$dist" && sha256sum "$(basename "$package")" > "$(basename "$package").sha256")

# Fill in the artifact's own SHA256 on the $dist copy of manifest.json only
# (the copy inside the immutable ZIP deliberately stays without it -- see
# comment above). This is the copy the QA Center Release UI reads.
python3 -c "
import json, pathlib
p = pathlib.Path('$dist/manifest.json')
m = json.loads(p.read_text(encoding='utf-8-sig'))
m['filename'] = '$(basename "$package")'
m['artifact_sha256'] = pathlib.Path('$package.sha256').read_text(encoding='utf-8').split()[0]
p.write_text(json.dumps(m, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
"

artifact_sha256="$(cut -d' ' -f1 "$package.sha256")"
cat > "$dist/BUILD_REPORT.md" <<EOF
# QA Center image build

- Version: $version
- Image: $image
- Digest: $digest
- Source commit: $source_commit
- Built at: $built_at
- Package: $(basename "$package")
- Package SHA256: $artifact_sha256
- Distribution: bundle (Docker image + compose.yml + metadata only -- no
  application source; the deploy ZIP never contains agent.py/templates/
  static/scenarios)
- Tests: $test_status
- Test summary (tail): $test_summary
- Provenance: see manifest.json / CHANGELOG.md in this directory for
  previous release, changed files, diff stat and commit log frozen at
  build time.
EOF

echo "QA RELEASE PASS"
echo "Version: $version"
echo "Image: $image"
echo "Digest: $digest"
echo "Tests: $test_status"
echo "Package: $package"
