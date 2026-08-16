"""The browser-facing HTTP side: the frontend, the token endpoint, and TLS.

Deliberately free of re-exports. ``app.runtime.worker`` imports ``app.web.http``
directly so that importing this package costs nothing and cannot create a cycle
back into the runtime layer.
"""
