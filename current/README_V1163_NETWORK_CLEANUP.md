# V1.16.6 – Network resilience and cleanup verification

- HTTP 521/522/523/524, 502/503/504 and 429 are retryable.
- The realistic simulator retries briefly, then switches between public and internal MESFlow URLs.
- Kiosk heartbeat failure is deferred to the next scheduler tick instead of terminating a multi-day run.
- Cleanup recognizes current `QAV65817-*` and legacy `QAV65813-*` QA records.
- Employee matching uses `employee_no`; stations use `code`.
- Cleanup order: force-delete QA PO → delete QA employees → delete QA stations.
- Preview and execution return PO/employee/station counts and per-record failures.
