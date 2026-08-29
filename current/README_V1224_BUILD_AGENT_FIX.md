# QA Center v1.22.5 — Deploy Agent build fix

- Bumps the immutable release version from 1.22.3 to 1.22.5 so Deploy Agent never reuses a previously frozen/tagged version.
- Fixes the Dockerfile actually used by `scripts/build-release.sh` (`current/docker/Dockerfile`) to install `postgresql-client`, required by Database Cleanup (`pg_dump`).
- Keeps `install.sh` source-only: it copies source into the workspace and performs no Docker build/deploy/restart.
