# QA Center 1.22.2 — Editable Demo Target

Demo Center target is now independent from the main QA target. Presets:
- Docker MESFlow: `http://mesflow-app:8080`
- Local Host from QA container: `http://host.docker.internal:8080`
- Custom URL: any valid HTTP/HTTPS test target (cloud metadata targets are blocked).

Changing target invalidates the previous auth preflight. The user must Test Login again before Run Demo.
Linux Docker compose includes `host.docker.internal:host-gateway` for local-host access.
