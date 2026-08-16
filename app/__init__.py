"""The realtime voice wiki assistant.

Layout: ``core`` decides, ``agent`` holds what the LLM is told, ``stt``/``tts``/
``llm``/``rag`` are swappable providers, ``web``/``runtime`` wire it together.
See docs/REORGANIZATION.md.
"""
