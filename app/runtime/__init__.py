"""The only layer that knows every module exists.

It reads the settings, asks each provider registry for an implementation, and
hands the built objects to ``app.core``. No re-exports: importing this package
must not drag the LiveKit worker into a process that only wants the web app.
"""
