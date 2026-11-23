"""The one aiohttp session every entry's web client talks through."""

import aiohttp

from custom_components.fronius_modbus.web_session import async_web_session


async def test_every_entry_shares_one_session(hass):
    assert async_web_session(hass) is async_web_session(hass)


async def test_the_session_keeps_no_cookie_of_the_inverter(hass):
    """requests without a session kept none; nothing the inverter sets goes anywhere."""
    assert isinstance(async_web_session(hass).cookie_jar, aiohttp.DummyCookieJar)
