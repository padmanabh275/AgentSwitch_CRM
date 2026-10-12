"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

transport.tls.use_certifi_bundle: points SSL_CERT_FILE at certifi's bundle
when certifi is importable, never overrides an SSL_CERT_FILE already set,
and is a no-op without certifi.
"""
from __future__ import annotations

import builtins
import sys
import types

import pytest

from transport import tls


@pytest.fixture
def fake_certifi(monkeypatch):
    mod = types.SimpleNamespace(where=lambda: "/bundle/cacert.pem")
    monkeypatch.setitem(sys.modules, "certifi", mod)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)


def test_sets_ssl_cert_file_from_certifi(fake_certifi):
    import os
    assert tls.use_certifi_bundle() == "/bundle/cacert.pem"
    assert os.environ["SSL_CERT_FILE"] == "/bundle/cacert.pem"


def test_existing_ssl_cert_file_wins(fake_certifi, monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", "/platform/ca.pem")
    assert tls.use_certifi_bundle() == "/platform/ca.pem"


def test_without_certifi_nothing_changes(monkeypatch):
    import os
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delitem(sys.modules, "certifi", raising=False)
    real_import = builtins.__import__

    def no_certifi(name, *a, **kw):
        if name == "certifi":
            raise ImportError("no certifi")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_certifi)
    assert tls.use_certifi_bundle() is None
    assert "SSL_CERT_FILE" not in os.environ


def test_requirements_ship_certifi():
    from config import REPO_DIR
    lines = (REPO_DIR / "requirements.txt").read_text().splitlines()
    assert any(l.strip().startswith("certifi") for l in lines)
