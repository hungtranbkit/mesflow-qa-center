# V1.16.6 – Template-driven realistic time model

- PO quantity is clamped to at least 500 units.
- Reads `standard_seconds_per_unit` from cloned Operation, with Template tree mapping as fallback.
- Chooses a realistic session quantity from a 60–180 minute work target.
- Normal duration = quantity × cycle time × random factor within ±30%.
- A small configurable anomaly rate creates MACHINE_JAM, MATERIAL_WAIT, or FORGOT_FINISH sessions at 1.8–4.0× standard duration.
- Missing cycle times are logged and use a configurable fallback, default 300 seconds/unit.
