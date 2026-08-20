# Models

Vendored microWakeWord assets for «Джарвис»:

- `ru_jarvis_mww.tflite`
- `ru_jarvis_mww.json` (`version: 2`)

Runtime: `MWW_MODEL_CONFIG=/app/models/ru_jarvis_mww.json`.
Threshold comes from `WAKE_THRESHOLD` (legacy alias: `OWW_THRESHOLD`).

v3 (USB retrain) was rolled back — it scored ordinary speech too high.
Local backups: `*.bak-v2` / `*.bak-v3` (gitignored).
