"""Talking to the inverter's web server: the one aiohttp call, and its digest login.

Every request goes through `_exchange`, the one call into aiohttp; the tests
fake the inverter right there, so the digest login runs for real. `_http`
turns aiohttp's errors, whose text carries the URL, into ones that name the
error type alone.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import os
import re
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import parse_http_list

import aiohttp
from multidict import CIMultiDict

from .web_errors import FroniusWebUnreachable


class WebResponse:
    """An answer of the inverter, read whole: nothing waits on the connection after."""

    def __init__(
        self,
        status_code: int,
        headers: Mapping[str, str],
        body: bytes,
        url: str,
    ) -> None:
        """Keep the status, headers and body; header names compare case-blind."""
        self.status_code = status_code
        self.headers = CIMultiDict(headers)
        self.body = body
        self.url = url

    def json(self) -> Any:
        """The body as JSON; a ValueError when it is none, as requests raised."""
        return json.loads(self.body)


async def _exchange(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    payload: Any,
    headers: Mapping[str, str],
    timeout: float,
) -> WebResponse:
    """The one call into aiohttp; a guard in the tests keeps it that way."""
    async with session.request(
        method.upper(),
        url,
        json=payload,
        headers=dict(headers),
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as response:
        body = await response.read()
        return WebResponse(response.status, response.headers, body, url)


async def _http(
    session: aiohttp.ClientSession | None,
    method: str,
    url: str,
    *,
    payload: Any = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 4.0,
) -> WebResponse:
    """One request, its failure as FroniusWebUnreachable naming the error type alone.

    aiohttp, like requests before it, puts the URL and with it the host into
    its error text, and that text used to reach the config flow's log through
    the login. Without a session a short-lived one is opened for the request:
    the integration always passes Home Assistant's.
    """
    try:
        if session is None:
            async with aiohttp.ClientSession(
                cookie_jar=aiohttp.DummyCookieJar()
            ) as own:
                return await _exchange(
                    own,
                    method,
                    url,
                    payload=payload,
                    headers=headers or {},
                    timeout=timeout,
                )
        return await _exchange(
            session,
            method,
            url,
            payload=payload,
            headers=headers or {},
            timeout=timeout,
        )
    except (aiohttp.ClientError, TimeoutError) as err:
        raise FroniusWebUnreachable(type(err).__name__) from err


def _base_url(host_or_url: str) -> str:
    if "://" in host_or_url:
        parsed = urlparse(host_or_url)
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    return f"http://{host_or_url}".rstrip("/")


def _parse_dict_header(value: str) -> dict[str, str | None]:
    """`key="value", key2=value2` as a dict; a key without a value maps to None.

    requests.utils.parse_dict_header, kept to the letter: the digest
    challenge is parsed exactly as before the move to aiohttp.
    """
    result: dict[str, str | None] = {}
    for item in parse_http_list(value):
        if "=" not in item:
            result[item] = None
            continue
        name, item_value = item.split("=", 1)
        if item_value[:1] == item_value[-1:] == '"':
            item_value = item_value[1:-1].replace("\\\\", "\\").replace('\\"', '"')
        result[name] = item_value
    return result


def _digest_challenge(value: str) -> dict[str, str | None]:
    if not value:
        return {}
    return _parse_dict_header(re.sub(r"^Digest\s+", "", value, flags=re.IGNORECASE))


_HASH_MODES: dict[tuple[str, str], str] = {}


async def _hash_mode(
    session: aiohttp.ClientSession | None, base_url: str, user: str, timeout: float
) -> str:
    """The digest hash this device declares for `user`, remembered once answered.

    A transport failure returns the fallback without remembering it: caching
    that answer left md5 devices unable to log in until a restart.
    """
    remembered = _HASH_MODES.get((base_url, user))
    if remembered is not None:
        return remembered
    try:
        response = await _http(
            session, "get", f"{base_url}/api/status/common", timeout=timeout
        )
        if response.status_code >= 400:
            return "sha256"
        version = (
            response.json()
            .get("authenticationOptions", {})
            .get("digest", {})
            .get(f"{user}HashingVersion")
        )
    except FroniusWebUnreachable, ValueError, AttributeError:
        # A body of another shape is no answer either, and is not remembered.
        return "sha256"
    mode = "md5" if version == 1 else "sha256"
    _HASH_MODES[(base_url, user)] = mode
    return mode


class XHeaderDigestAuth:
    """The inverter's digest login, answered in an X-WWW-Authenticate header.

    A request goes out without a login; a 401 with a usable challenge is
    answered once with the digest, as requests' response hook did before.
    """

    def __init__(
        self,
        username: str,
        password: str = "",
        token: dict[str, str] | None = None,
        timeout: float = 4.0,
    ) -> None:
        """A login with a password, or with a token minted from one."""
        self.username = username
        self.password = password
        self.timeout = timeout
        self.base_url: str | None = None
        self.token = token if isinstance(token, dict) else None
        self.mode: str | None = None
        self.last_nonce = ""
        self.nonce_count = 0
        self.saved_token: dict[str, str] | None = None

    async def send(
        self,
        session: aiohttp.ClientSession | None,
        method: str,
        url: str,
        payload: Any = None,
    ) -> WebResponse:
        """The request, answered with the digest when the inverter asks for it."""
        if self.base_url is None:
            self.base_url = _base_url(url)
        if self.mode is None:
            self.mode = await _hash_mode(
                session, self.base_url, self.username, self.timeout
            )
        response = await _http(
            session, method, url, payload=payload, timeout=self.timeout
        )
        return await self._answer_401(session, method, url, payload, response)

    async def _answer_401(
        self,
        session: aiohttp.ClientSession | None,
        method: str,
        url: str,
        payload: Any,
        response: WebResponse,
    ) -> WebResponse:
        if response.status_code != 401:
            return response
        # Without a password or token the answer would be an empty-password
        # login on every poll of a no-login entry.
        if not self.password and self.token is None:
            return response

        challenge = _digest_challenge(
            response.headers.get("X-WWW-Authenticate")
            or response.headers.get("www-authenticate")
            or ""
        )
        # A key without a value parses to None: unusable like a missing one.
        if not all(challenge.get(key) for key in ("realm", "nonce", "qop")):
            return response
        realm = str(challenge["realm"])

        retried = await _http(
            session,
            method,
            url,
            payload=payload,
            headers={
                "Authorization": self._build_header(
                    method, self._digest_uri(url), challenge
                )
            },
            timeout=self.timeout,
        )
        if retried.status_code == 401:
            # Refused: the client lives on for the public reads, and would send
            # the same refused login on every poll. The device may
            # have changed its hash too; the next login asks it again.
            self.password = ""
            self.token = None
            _HASH_MODES.pop((self.base_url or "", self.username), None)
            self.mode = None
            return retried
        if self.password:
            self.saved_token = {"realm": realm, "token": self._secret(realm)}
        return retried

    def _digest_uri(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.path == "/api/commands/Login":
            return parsed.path
        return parsed.path + (f"?{parsed.query}" if parsed.query else "")

    def _secret(self, realm: str) -> str:
        if not self.password and self.token and self.token.get("realm") == realm:
            return str(self.token["token"])
        payload = f"{self.username}:{realm}:{self.password}".encode()
        if self.mode == "md5":
            return hashlib.md5(payload).hexdigest()
        return hashlib.sha256(payload).hexdigest()

    def _build_header(
        self, method: str, uri: str, challenge: Mapping[str, str | None]
    ) -> str:
        nonce = str(challenge["nonce"])
        qop = str(challenge["qop"]).split(",")[0]
        if nonce == self.last_nonce:
            self.nonce_count += 1
        else:
            self.nonce_count = 1
        self.last_nonce = nonce

        nc = f"{self.nonce_count:08x}"
        cnonce = os.urandom(8).hex()
        ha2 = hashlib.sha256(f"{method.upper()}:{uri}".encode()).hexdigest()
        realm = str(challenge["realm"])
        digest = hashlib.sha256(
            f"{self._secret(realm)}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()
        ).hexdigest()

        parts = [
            f'username="{self.username}"',
            f'realm="{realm}"',
            f'nonce="{nonce}"',
            f'uri="{uri}"',
            f'response="{digest}"',
            f"qop={qop}",
            f"nc={nc}",
            f'cnonce="{cnonce}"',
        ]
        if challenge.get("opaque"):
            parts.append(f'opaque="{challenge["opaque"]}"')
        return "Digest " + ", ".join(parts)


async def _login_response(
    host: str,
    user: str,
    password: str = "",
    token: dict[str, str] | None = None,
    timeout: float = 4.0,
    session: aiohttp.ClientSession | None = None,
) -> tuple[WebResponse, XHeaderDigestAuth]:
    auth = XHeaderDigestAuth(user, password=password, token=token, timeout=timeout)
    url = f"http://{host}/api/commands/Login?{urlencode({'user': user})}"
    return await auth.send(session, "get", url), auth


async def login(
    host: str,
    user: str,
    password: str = "",
    token: dict[str, str] | None = None,
    timeout: float = 4.0,
    session: aiohttp.ClientSession | None = None,
) -> bool:
    """Whether the inverter takes this login."""
    response, _auth = await _login_response(
        host, user, password=password, token=token, timeout=timeout, session=session
    )
    return response.status_code == 200


async def mint_token(
    host: str,
    user: str,
    password: str,
    timeout: float = 4.0,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, str] | None:
    """The token a password login yields; None when the inverter refuses it."""
    response, auth = await _login_response(
        host, user, password=password, timeout=timeout, session=session
    )
    return auth.saved_token if response.status_code == 200 else None
