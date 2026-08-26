# QA Center 1.22.1 — Separate Demo Presentation + Auth Preflight

- Demo Center moved to `/demo` with a dedicated presentation layout.
- Main QA dashboard only links to Demo Center; demo controls no longer share the legacy QA screen.
- Demo Run is locked until MESFlow credential preflight succeeds.
- HTTP 401 now reports `INVALID_CREDENTIALS` with an actionable Vietnamese message instead of an opaque requests traceback.
- Presenter/Auto/Manual, Pause/Resume, screenshot review and Return Live remain available.
