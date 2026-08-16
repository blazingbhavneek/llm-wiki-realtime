# Realtime Voice UI

The frontend has exactly two build modes.

## Development mode

```bash
npm run dev
```

Development mode needs no FastAPI, LiveKit, LLM, or Wiki backend. It:

- loads a scripted conversation and a two-level research event stream;
- lets every submitted text question replay the placeholder research flow;
- captures the microphone when the orb is clicked;
- plays that microphone audio back after one second; and
- feeds the delayed audio through the same `agent` visualizer input used by
  production.

Use headphones during the delayed microphone test to avoid acoustic feedback.

## Production mode

```bash
npm run build
cd ..
uv run python -m app dev
```

`npm run build` always uses production mode. The resulting `frontend/dist`
connects to `/token` and LiveKit; it includes no scripted research and never
enables the local audio loopback. `python -m app` serves the built files and
runs the LiveKit agent.
