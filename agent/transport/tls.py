"""CA certificates for every HTTPS call the agent makes (MCP and the model).

The agent's HTTP is urllib, which trusts whatever CA store the Python build
has. Some builds have none (python.org's macOS installer until "Install
Certificates" is run; found in the hosted-run rehearsal: every call failed
with CERTIFICATE_VERIFY_FAILED). If certifi is installed, point
SSL_CERT_FILE at its bundle; urllib's default SSL context reads that, and so
do the agent subprocesses, which inherit the environment. An SSL_CERT_FILE
that is already set is left alone, and without certifi nothing changes.
"""
from __future__ import annotations

import os


def use_certifi_bundle() -> str | None:
    """Returns the CA file now in effect via SSL_CERT_FILE, or None."""
    if os.environ.get("SSL_CERT_FILE"):
        return os.environ["SSL_CERT_FILE"]
    try:
        import certifi
    except ImportError:
        return None
    os.environ["SSL_CERT_FILE"] = certifi.where()
    return os.environ["SSL_CERT_FILE"]
