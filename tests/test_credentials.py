"""Credential resolution: env-var aliases and private-key encodings.

A misconfigured credential is the single most common reason the trading loop
refuses to start, and the failure is invisible (the value is sitting right there
in the .env file). These tests pin down exactly which names and formats are
accepted so that behaviour can't regress silently.

No real key material appears here -- keys are generated per-test.
"""

import base64
import os
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config  # noqa: E402
import kalshi_client  # noqa: E402


ALL_CRED_VARS = tuple(config.KEY_ID_ALIASES) + tuple(config.PRIVATE_KEY_ALIASES)


@pytest.fixture
def clean_env(monkeypatch):
    """Remove every credential var so each test states its own inputs."""
    for name in ALL_CRED_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture(scope="module")
def pem_text():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


# ---- key id aliases ---------------------------------------------------------


@pytest.mark.parametrize("var", config.KEY_ID_ALIASES)
def test_every_key_id_alias_resolves(clean_env, var):
    """Each documented alias supplies the key id on its own."""
    clean_env.setenv(var, "key-id-from-" + var)
    assert config._first_env(config.KEY_ID_ALIASES) == "key-id-from-" + var


def test_canonical_key_id_wins_over_legacy_alias(clean_env):
    """When both are set the canonical name takes precedence."""
    clean_env.setenv("KALSHI_API_KEY_ID", "canonical")
    clean_env.setenv("ACCESS_KEY", "legacy")
    assert config._first_env(config.KEY_ID_ALIASES) == "canonical"


def test_blank_alias_is_skipped_for_later_one(clean_env):
    """An empty canonical var must not shadow a populated alias.

    Compose files often declare a var with no value; that must not mask a
    working legacy alias.
    """
    clean_env.setenv("KALSHI_API_KEY_ID", "   ")
    clean_env.setenv("ACCESS_KEY", "real-key-id")
    assert config._first_env(config.KEY_ID_ALIASES) == "real-key-id"


def test_key_id_is_whitespace_stripped(clean_env):
    """Trailing newlines from heredocs/CRLF files must not corrupt the id."""
    clean_env.setenv("ACCESS_KEY", "  abc-123\n")
    assert config._first_env(config.KEY_ID_ALIASES) == "abc-123"


def test_missing_everything_returns_default(clean_env):
    assert config._first_env(config.KEY_ID_ALIASES, "fallback") == "fallback"


# ---- diagnostics ------------------------------------------------------------


def test_credential_source_names_the_var(clean_env):
    clean_env.setenv("KALSHI_PRIVATE_KEY_BASE64", "irrelevant")
    assert (
        config.credential_source(config.PRIVATE_KEY_ALIASES)
        == "KALSHI_PRIVATE_KEY_BASE64"
    )


def test_credential_source_blank_when_unset(clean_env):
    assert config.credential_source(config.KEY_ID_ALIASES) == ""


def test_credential_source_never_returns_a_value(clean_env):
    """The diagnostic must leak names only -- never secret material."""
    secret = "super-secret-key-material"
    clean_env.setenv("ACCESS_KEY", secret)
    source = config.credential_source(config.KEY_ID_ALIASES)
    assert source == "ACCESS_KEY"
    assert secret not in source


# ---- private key encodings --------------------------------------------------


def test_plain_pem_loads(pem_text):
    assert kalshi_client._load_private_key(pem_text) is not None


def test_escaped_newline_pem_loads(pem_text):
    """Single-line PEM with literal backslash-n, as env vars commonly hold."""
    single_line = pem_text.replace("\n", "\\n")
    assert "\n" not in single_line.strip("\\n")
    assert kalshi_client._load_private_key(single_line) is not None


def test_base64_wrapped_pem_loads(pem_text):
    """KALSHI_PRIVATE_KEY_BASE64 style: the whole PEM, base64-encoded."""
    wrapped = base64.b64encode(pem_text.encode("utf-8")).decode("ascii")
    assert "-----BEGIN" not in wrapped
    assert kalshi_client._load_private_key(wrapped) is not None


def test_base64_with_embedded_whitespace_loads(pem_text):
    """Line-wrapped base64 (as `base64` emits by default) is accepted."""
    raw = base64.b64encode(pem_text.encode("utf-8")).decode("ascii")
    wrapped = "\n".join(raw[i : i + 64] for i in range(0, len(raw), 64))
    assert kalshi_client._load_private_key(wrapped) is not None


def test_base64_of_escaped_newline_pem_loads(pem_text):
    """Both transformations at once: base64 of a backslash-n PEM."""
    inner = pem_text.replace("\n", "\\n")
    wrapped = base64.b64encode(inner.encode("utf-8")).decode("ascii")
    assert kalshi_client._load_private_key(wrapped) is not None


def test_real_pem_is_not_routed_through_base64(pem_text):
    """A value containing a PEM header must be parsed directly.

    Guards the ordering in _load_private_key: base64 decoding is a fallback,
    never applied to something already recognisable as a PEM.
    """
    assert kalshi_client._decode_if_base64(pem_text) == ""


def test_empty_key_returns_none():
    assert kalshi_client._load_private_key("") is None
    assert kalshi_client._load_private_key(None) is None


def test_garbage_raises_auth_error():
    with pytest.raises(kalshi_client.KalshiAuthError):
        kalshi_client._load_private_key("not a key at all")


def test_base64_of_non_pem_raises_auth_error():
    """Valid base64 that isn't a PEM must fail loudly, not silently pass."""
    wrapped = base64.b64encode(b"hello world, definitely not a key").decode("ascii")
    with pytest.raises(kalshi_client.KalshiAuthError):
        kalshi_client._load_private_key(wrapped)


def test_error_message_lists_accepted_vars_and_hides_material():
    """The failure must be actionable without echoing the bad value."""
    with pytest.raises(kalshi_client.KalshiAuthError) as excinfo:
        kalshi_client._load_private_key("bogus-value-do-not-echo")
    message = str(excinfo.value)
    for name in config.PRIVATE_KEY_ALIASES:
        assert name in message
    assert "bogus-value-do-not-echo" not in message


def test_binary_base64_is_rejected_not_crashed():
    """Base64 of non-UTF8 bytes must be rejected cleanly."""
    wrapped = base64.b64encode(bytes(range(200, 256))).decode("ascii")
    assert kalshi_client._decode_if_base64(wrapped) == ""


# ---- end-to-end through Settings -------------------------------------------


def test_settings_reads_legacy_pair(clean_env, pem_text):
    """A .env written for an older version must now satisfy problems()."""
    clean_env.setenv("ACCESS_KEY", "legacy-key-id")
    clean_env.setenv(
        "KALSHI_PRIVATE_KEY_BASE64",
        base64.b64encode(pem_text.encode("utf-8")).decode("ascii"),
    )
    clean_env.setenv("TRADING_MODE", "dryrun")

    settings = config.Settings()
    assert settings.key_id == "legacy-key-id"
    assert settings.private_key_pem
    assert kalshi_client._load_private_key(settings.private_key_pem) is not None
    credential_issues = [
        issue for issue in settings.problems() if "Kalshi API key id" in issue
    ]
    assert credential_issues == []


def test_settings_still_flags_a_truly_missing_credential(clean_env):
    """The guard must keep firing when no alias at all is present."""
    clean_env.setenv("TRADING_MODE", "dryrun")
    settings = config.Settings()
    assert any("Kalshi API key id" in issue for issue in settings.problems())
