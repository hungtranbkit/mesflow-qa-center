# QA Center 1.22.15 — Demo Runner Reliability

- Demo defaults to Auto so a run does not pause after the first case unless Presenter/Manual is explicitly selected.
- A failed testcase is recorded and the runner continues with subsequent testcases.
- Setup/auth seed failures are written to state.json with explicit stage/URL/message.
- Failed actions expose selector/target and exact Playwright/API error in the Demo Center UI.
- Failed screenshots are retained as FAILED-<step>.png evidence.
- Overall run is FAILED only after all planned cases have been attempted.
