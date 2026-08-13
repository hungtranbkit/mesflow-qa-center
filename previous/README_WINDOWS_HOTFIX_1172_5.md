# Windows Hotfix 5 / QA Center 1.17.2

- Dashboard CSS/JS are inlined into `/` so a running backend cannot produce a blank page just because `/static` failed.
- `config.json` is loaded with UTF-8 BOM tolerance and safe defaults; a corrupt preserved config no longer prevents the dashboard from rendering.
- Root route returns a visible diagnostic HTML page if rendering fails.
- API remains available at `/api/version` and `/api/status`.
