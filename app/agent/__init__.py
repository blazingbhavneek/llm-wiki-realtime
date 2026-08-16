"""What the LLM is told, and what it is allowed to call.

Deliberately free of re-exports: ``app.core`` imports ``app.agent.prompts``
directly, and importing ``assistant`` here would pull ``livekit`` into the
decision layer.
"""
