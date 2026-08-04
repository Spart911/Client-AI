# Models

Wake models are **not** vendored in git anymore.

At Docker build time the image runs:

```python
import openwakeword
openwakeword.utils.download_models()
```

That fetches openWakeWord assets (mel / embedding / `alexa` and other pretrained heads) into the Python package directory.

Default runtime wake: `OWW_MODEL=alexa` (`OWW_FRAMEWORK=onnx`).
