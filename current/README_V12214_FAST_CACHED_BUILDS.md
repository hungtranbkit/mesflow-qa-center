# QA Center 1.22.14 — Fast Cached Builds

QA release builds now use a fingerprinted heavy base image. The fingerprint is SHA256 over `current/docker/Dockerfile.base` and `current/requirements.txt`.

- Cache hit: skip apt, PostgreSQL client, Python dependency, Playwright, Chromium, font/X11 installation.
- Cache miss: build the base once, then build the thin QA application image.
- Changing dependencies automatically creates a new base tag; changing normal QA source does not.
- Promotion still ships the final immutable QA image, so Production Test does not need the base image.
