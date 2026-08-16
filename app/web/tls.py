"""Local TLS material for browser testing.

Only used when ``TEXT_TEST_TLS`` is on; in the normal deployment Caddy
terminates TLS and this module never runs.
"""

from __future__ import annotations

from pathlib import Path

import trustme

from app.config import WebSettings


def ensure_local_https_certificate(settings: WebSettings) -> tuple[Path, Path, Path]:
    """Create a reusable local CA and server certificate for browser testing."""
    cert_dir = Path(settings.cert_dir)
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / "text-test-cert.pem"
    key_path = cert_dir / "text-test-key.pem"
    ca_path = cert_dir / "text-test-ca.pem"
    if cert_path.exists() and key_path.exists() and ca_path.exists():
        return cert_path, key_path, ca_path

    hostnames = [value.strip() for value in settings.https_hosts.split(",") if value.strip()]
    ca = trustme.CA()
    certificate = ca.issue_cert(*hostnames)
    ca.cert_pem.write_to_path(ca_path)
    certificate.cert_chain_pems[0].write_to_path(cert_path)
    certificate.private_key_pem.write_to_path(key_path)
    return cert_path, key_path, ca_path
