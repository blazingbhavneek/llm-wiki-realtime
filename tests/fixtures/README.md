# Fixtures

`sample_ja.wav` — 16-bit PCM mono, 44.1 kHz, ~2.4s. The Japanese sentence
「こんにちは、これはテストです。」("Hello, this is a test."), synthesized once
with this project's own `supertonic` voice `F1` (`app/tts/supertonic.py`) so
the live STT tests (`tests/live/test_stt_live.py`) have real speech to
transcribe without checking in a recording of an actual person or depending
on network access at test time.

Regenerate it with:

```bash
uv run python - <<'EOF'
import soundfile as sf
from huggingface_hub import snapshot_download
from supertonic import TTS

model_dir = snapshot_download(repo_id="Supertone/supertonic-3", local_files_only=True)
engine = TTS(model="supertonic-3", model_dir=model_dir, auto_download=False)
style = engine.get_voice_style("F1")
wav, _duration = engine.synthesize(
    text="こんにちは、これはテストです。", voice_style=style, total_steps=8, speed=1.05, lang="ja"
)
sf.write("tests/fixtures/sample_ja.wav", wav.reshape(-1), engine.sample_rate, format="WAV", subtype="PCM_16")
EOF
```
