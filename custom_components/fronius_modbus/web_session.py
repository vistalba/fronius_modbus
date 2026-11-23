"""The aiohttp session the web API client talks through.

Its own session, not Home Assistant's shared one: it keeps no cookies, as
requests without a session kept none, so nothing the inverter sets is sent
back to it or to anything else. Home Assistant closes it when it stops.
"""

from __future__ import annotations

import aiohttp

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN

_WEB_SESSION: HassKey[aiohttp.ClientSession] = HassKey(f"{DOMAIN}_web_session")


@callback
def async_web_session(hass: HomeAssistant) -> aiohttp.ClientSession:
    """One session for every entry, created on first use."""
    session = hass.data.get(_WEB_SESSION)
    if session is None:
        session = async_create_clientsession(hass, cookie_jar=aiohttp.DummyCookieJar())
        hass.data[_WEB_SESSION] = session
    return session
