# LLM providers

## 1. The contract

A provider subclasses `LLMProvider` (`base.py`) and implements
`build(settings) -> livekit.agents.llm.LLM`. The runtime layer calls
`app.llm.build_llm()`, which reads `LLM_PROVIDER`, resolves the class lazily
through `REGISTRY`, fills an `LLMSettings` from the environment
(`settings_from_env`) and hands the result to the `AgentSession`. Nothing else
in the app constructs an LLM.

## 2. Choosing one

`LLM_PROVIDER` — `openai_compatible` (default, and the only one today).

| variable | meaning | default |
|---|---|---|
| `LLM_PROVIDER` | which backend | `openai_compatible` |
| `LLM_MODEL` | model id sent to the endpoint | provider `default_model` |
| `LLM_BASE_URL` | OpenAI-shaped base URL | provider `default_base_url` |
| `LLM_API_KEY` | bearer token; `EMPTY` means "send none" | `EMPTY` |

Swapping models or hosts — llama-server to vLLM, Gemma to anything else — is
these two variables, not a code change.

## 3. The providers

| name | hosted by | endpoint | tools | notes |
|---|---|---|---|---|
| `openai_compatible` | remote / self-hosted, whatever serves the URL | `http://10.160.144.101:51028/v1`, model `gemma-4-31B` | yes | covers llama-server, vLLM and SGLang — anything speaking `/v1/chat/completions` |

`supports_tools` must be True for this app: the assistant declares
`research_wiki`, `read_result` and `stop_research`, and a server built without
tool calling looks like a model that simply never researches anything.

## 4. Adding an OpenAI-shaped backend

You almost certainly do not need a new file — point `LLM_BASE_URL` and
`LLM_MODEL` at it. Add one only when the wire format differs.

## 5. Adding a non-OpenAI-shaped backend

1. copy `openai_compatible.py` to `<name>.py`;
2. subclass `LLMProvider`, set `name`, `default_model`, `default_base_url` and
   `supports_tools`;
3. implement `build()`, returning a `livekit.agents.llm.LLM` — usually a
   `livekit.plugins.<vendor>.LLM`, and add that plugin to `pyproject.toml`
   (as an optional extra if it is heavy);
4. add one line to `REGISTRY` in `__init__.py` — the value is the
   `"module:Class"` string, never an imported class, so selecting one provider
   never imports the others' dependencies;
5. document its env block in this README and in `.env.example`.

## 6. Verifying without LiveKit

```bash
curl -s http://10.160.144.101:51028/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemma-4-31B","messages":[{"role":"user","content":"ping"}]}'
```

Tool calling is the part worth checking separately — add a `tools` array to that
body and confirm the response carries `tool_calls`. `tests/test_providers.py`
covers the offline half: every registered provider imports, builds settings and
returns the right LiveKit base type.

## 7. Gotchas

- The default `api_key` is the literal `EMPTY`, which is what local servers
  expect; a real gateway needs `LLM_API_KEY` set or every call 401s.
- `LLM_BASE_URL` must include the `/v1` suffix.
- A model that ignores tool definitions fails silently — the assistant just
  answers from its own knowledge instead of researching. Check `tool_calls`
  before blaming the prompt.
