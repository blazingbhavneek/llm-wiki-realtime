"""The decision layer: one event loop, one speech slot, one memory.

Nothing here may import a provider module, ``livekit.plugins``, ``httpx`` or
``aiohttp``. Decisions live here; I/O lives behind the provider packages.
"""
