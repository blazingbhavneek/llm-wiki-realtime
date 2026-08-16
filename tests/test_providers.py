"""Every registered provider resolves, offline.

No GPU, no model file, no network: this file never calls ``build()`` for STT,
TTS or LLM, because building one needs a live LiveKit job context and would
import the model stacks. What it does check is the half of a registration that
silently rots — the registry line pointing at a class that no longer exists, a
provider that forgets one of the capability flags its base declares (a missing
``native_sample_rate`` fails as garbled audio, not as an error), and the
laziness of the registries themselves.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

import app.llm
import app.rag
import app.stt
import app.tts
from app.llm.base import LLMProvider
from app.rag.base import ResearchBackend
from app.stt.base import STTProvider
from app.tts.base import TTSProvider


def declared_capabilities(base: type) -> list[str]:
    """The ``ClassVar`` names the base declares, which every provider must fill.

    ``from __future__ import annotations`` means the annotations are strings, so
    this is a text match rather than a ``typing.get_type_hints`` round-trip that
    would need the base's TYPE_CHECKING imports to be real.
    """
    names = []
    for klass in reversed(base.__mro__):
        for attr, annotation in vars(klass).get("__annotations__", {}).items():
            if str(annotation).startswith("ClassVar") and attr not in names:
                names.append(attr)
    return names


def is_declared(value: object) -> bool:
    """A flag that was actually set. ``False`` and ``0`` count; ``None`` and ``""`` do not."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return True


class CapabilityDeclarationTests(unittest.TestCase):
    """Every provider fills in every ClassVar its base declares."""

    def _check(self, resolve, registry, base):
        expected = declared_capabilities(base)
        self.assertTrue(expected, f"{base.__name__} declares no capability attributes")
        for name in sorted(registry):
            with self.subTest(provider=name):
                provider = resolve(name)
                self.assertTrue(
                    issubclass(provider, base),
                    f"{name} resolves to {provider!r}, which is not a {base.__name__}",
                )
                for attr in expected:
                    self.assertTrue(
                        hasattr(provider, attr),
                        f"{name} never declares {base.__name__}.{attr}",
                    )
                    self.assertTrue(
                        is_declared(getattr(provider, attr)),
                        f"{name}.{attr} is declared but empty",
                    )
                self.assertEqual(provider.name, name, "the class name and the registry key differ")

    def test_stt_providers_declare_every_flag(self):
        self._check(app.stt.get_provider, app.stt.REGISTRY, STTProvider)

    def test_tts_providers_declare_every_flag(self):
        self._check(app.tts.get_provider, app.tts.REGISTRY, TTSProvider)

    def test_llm_providers_declare_every_flag(self):
        self._check(app.llm.get_provider, app.llm.REGISTRY, LLMProvider)

    def test_rag_backends_expose_the_whole_protocol(self):
        # app.rag.base is Protocols, not a base class with ClassVars, so the
        # equivalent check is that the backend answers everything the Conductor
        # is allowed to ask of it.
        expected = [attr for attr in vars(ResearchBackend) if not attr.startswith("_")]
        self.assertIn("foreground_run", expected)
        for name in sorted(app.rag.REGISTRY):
            with self.subTest(provider=name):
                backend = app.rag.get_backend(name)
                for attr in expected:
                    self.assertTrue(
                        hasattr(backend, attr),
                        f"{name} is missing ResearchBackend.{attr}",
                    )


class SettingsFromEnvTests(unittest.TestCase):
    """With nothing in the environment, a provider's own defaults must stand on their own."""

    def _check(self, resolve, registry):
        for name in sorted(registry):
            with self.subTest(provider=name):
                provider = resolve(name)
                # Cleared, not mocked: the defaults are what a fresh deployment
                # gets, and an empty default_model is a 404 at the first turn.
                with mock.patch.dict(os.environ, {}, clear=True):
                    settings = provider.settings_from_env()
                self.assertEqual(settings.provider, name)
                self.assertTrue(settings.model.strip(), f"{name} has no default model")
                self.assertTrue(settings.base_url.strip(), f"{name} has no default base_url")

    def test_stt_settings(self):
        self._check(app.stt.get_provider, app.stt.REGISTRY)

    def test_tts_settings(self):
        self._check(app.tts.get_provider, app.tts.REGISTRY)

    def test_llm_settings(self):
        self._check(app.llm.get_provider, app.llm.REGISTRY)


class UnknownProviderTests(unittest.TestCase):
    """An unregistered name fails loudly, and says what it would have accepted."""

    def _check(self, resolve, registry):
        with self.assertRaises(ValueError) as caught:
            resolve("no-such-provider")
        message = str(caught.exception)
        self.assertIn("no-such-provider", message)
        for name in registry:
            self.assertIn(name, message)

    def test_unknown_stt(self):
        self._check(app.stt.get_provider, app.stt.REGISTRY)

    def test_unknown_tts(self):
        self._check(app.tts.get_provider, app.tts.REGISTRY)

    def test_unknown_llm(self):
        self._check(app.llm.get_provider, app.llm.REGISTRY)

    def test_unknown_rag(self):
        self._check(app.rag.get_backend, app.rag.REGISTRY)


class LazyRegistryTests(unittest.TestCase):
    def test_selecting_qwen3_does_not_import_supertonics_dependencies(self):
        # The one regression this file exists to catch. The registry holds import
        # strings, not classes, so that TTS_PROVIDER=qwen3 never drags
        # supertonic/soundfile/onnxruntime into the agent process - which is not
        # the machine that hosts the ONNX model, and may not have them at all.
        provider = app.tts.get_provider("qwen3")
        self.assertEqual(provider.name, "qwen3")
        for module in ("supertonic", "soundfile"):
            self.assertNotIn(module, sys.modules, f"resolving qwen3 imported {module}")


if __name__ == "__main__":
    unittest.main()
