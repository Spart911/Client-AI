# Models

Wake models are **not** vendored in git anymore.

At Docker build time the image runs:

```python
import openwakeword
openwakeword.utils.download_models()
```

That fetches openWakeWord assets (mel / embedding / `alexa` and other pretrained heads) into the Python package directory.

Default runtime wake: `OWW_MODEL=alexa` (`OWW_FRAMEWORK=tflite`).

For custom Russian wake words (`interkelstar/microwakeword-trainer`):

- Copy `<name>_mww.tflite` and `<name>_mww.json` into this `models/` directory.
- Set `WAKE_ENGINE=mww` and `MWW_MODEL_CONFIG=/app/models/<name>_mww.json`.
