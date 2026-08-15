"""Tests for the single-user OAuth provider.

The SDK enforces PKCE, code expiry and redirect matching before calling the
provider, so these tests cover what the provider itself is responsible for:
passphrase handling, code/token lifecycle, rotation and persistence.
"""

import asyncio
import time

import pytest
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from garmin_mcp.auth import MAX_ATTEMPTS, SingleUserOAuthProvider

PASSPHRASE = "a-long-enough-passphrase"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def provider(tmp_path):
    return SingleUserOAuthProvider(
        passphrase=PASSPHRASE,
        base_url="https://example.test",
        storage=tmp_path / "oauth_state.json",
    )


@pytest.fixture
def client(provider):
    info = OAuthClientInformationFull(
        client_id="client-1",
        client_secret="shh",
        redirect_uris=[AnyUrl(REDIRECT)],
        grant_types=["authorization_code", "refresh_token"],
    )
    run(provider.register_client(info))
    return info


def params(**overrides):
    base = dict(
        state="state-123",
        scopes=["garmin:read"],
        code_challenge="challenge",
        redirect_uri=AnyUrl(REDIRECT),
        redirect_uri_provided_explicitly=True,
    )
    base.update(overrides)
    return AuthorizationParams(**base)


def authorize_and_login(provider, client, passphrase=PASSPHRASE):
    url = run(provider.authorize(client, params()))
    session_id = url.split("session=")[1]
    assert provider.check_passphrase(passphrase)
    redirect = provider.complete_login(session_id)
    return redirect.split("code=")[1].split("&")[0]


def test_short_passphrase_is_refused(tmp_path):
    with pytest.raises(ValueError, match="at least 12 characters"):
        SingleUserOAuthProvider("short", "https://example.test", tmp_path / "s.json")


def test_authorize_points_at_login_page(provider, client):
    url = run(provider.authorize(client, params()))
    assert url.startswith("https://example.test/login?session=")


def test_wrong_passphrase_is_rejected(provider):
    assert provider.check_passphrase("nope") is False
    assert provider.check_passphrase(PASSPHRASE) is True


def test_lockout_after_repeated_failures(provider):
    for _ in range(MAX_ATTEMPTS):
        provider.check_passphrase("nope")
    assert provider.locked_out
    # Even the right passphrase is refused while locked out.
    assert provider.check_passphrase(PASSPHRASE) is False
    assert provider.lockout_remaining > 0


def test_login_produces_code_and_preserves_state(provider, client):
    url = run(provider.authorize(client, params()))
    session_id = url.split("session=")[1]
    provider.check_passphrase(PASSPHRASE)
    redirect = provider.complete_login(session_id)
    assert redirect.startswith(REDIRECT)
    assert "state=state-123" in redirect
    assert "code=code_" in redirect


def test_unknown_login_session_is_refused(provider):
    with pytest.raises(AuthorizeError):
        provider.complete_login("not-a-session")


def test_expired_login_session_is_refused(provider, client, monkeypatch):
    url = run(provider.authorize(client, params()))
    session_id = url.split("session=")[1]
    monkeypatch.setattr("garmin_mcp.auth.LOGIN_SESSION_TTL", -1)
    with pytest.raises(AuthorizeError):
        provider.complete_login(session_id)


def test_code_exchange_issues_tokens(provider, client):
    code = authorize_and_login(provider, client)
    auth_code = run(provider.load_authorization_code(client, code))
    token = run(provider.exchange_authorization_code(client, auth_code))
    assert token.access_token.startswith("gmt_")
    assert token.refresh_token.startswith("gmr_")
    assert token.expires_in == 3600
    assert run(provider.load_access_token(token.access_token)) is not None


def test_code_cannot_be_replayed(provider, client):
    code = authorize_and_login(provider, client)
    auth_code = run(provider.load_authorization_code(client, code))
    run(provider.exchange_authorization_code(client, auth_code))
    with pytest.raises(TokenError) as excinfo:
        run(provider.exchange_authorization_code(client, auth_code))
    assert excinfo.value.error == "invalid_grant"
    assert "already been used" in (excinfo.value.error_description or "")


def test_expired_access_token_is_rejected(provider, client):
    code = authorize_and_login(provider, client)
    auth_code = run(provider.load_authorization_code(client, code))
    token = run(provider.exchange_authorization_code(client, auth_code))
    provider.access_tokens[token.access_token].expires_at = int(time.time()) - 1
    assert run(provider.load_access_token(token.access_token)) is None


def test_unknown_token_is_rejected(provider):
    assert run(provider.load_access_token("gmt_made_up")) is None


def test_refresh_rotates_and_kills_the_old_token(provider, client):
    code = authorize_and_login(provider, client)
    auth_code = run(provider.load_authorization_code(client, code))
    first = run(provider.exchange_authorization_code(client, auth_code))

    refresh = run(provider.load_refresh_token(client, first.refresh_token))
    second = run(provider.exchange_refresh_token(client, refresh, ["garmin:read"]))

    assert second.refresh_token != first.refresh_token
    assert run(provider.load_refresh_token(client, first.refresh_token)) is None
    # The access token minted alongside the old refresh token is invalidated too.
    assert run(provider.load_access_token(first.access_token)) is None
    assert run(provider.load_access_token(second.access_token)) is not None


def test_revocation(provider, client):
    code = authorize_and_login(provider, client)
    auth_code = run(provider.load_authorization_code(client, code))
    token = run(provider.exchange_authorization_code(client, auth_code))
    access = provider.access_tokens[token.access_token]
    run(provider.revoke_token(access))
    assert run(provider.load_access_token(token.access_token)) is None


def test_state_survives_a_restart(tmp_path, client):
    storage = tmp_path / "state.json"
    first = SingleUserOAuthProvider(PASSPHRASE, "https://example.test", storage)
    run(first.register_client(client))
    code = authorize_and_login(first, client)
    auth_code = run(first.load_authorization_code(client, code))
    token = run(first.exchange_authorization_code(client, auth_code))

    second = SingleUserOAuthProvider(PASSPHRASE, "https://example.test", storage)
    assert run(second.get_client(client.client_id)) is not None
    assert run(second.load_access_token(token.access_token)) is not None
    assert storage.stat().st_mode & 0o777 == 0o600


def test_corrupt_state_file_does_not_block_startup(tmp_path):
    storage = tmp_path / "state.json"
    storage.write_text("{not json")
    provider = SingleUserOAuthProvider(PASSPHRASE, "https://example.test", storage)
    assert provider.clients == {}


def test_login_page_escapes_input():
    from garmin_mcp.auth import render_login_page

    page = render_login_page("<script>x</script>", "<img onerror=1>")
    assert "<script>x</script>" not in page
    assert "&lt;script&gt;" in page
    assert "<img onerror=1>" not in page
