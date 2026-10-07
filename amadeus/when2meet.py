import re
from urllib.parse import urljoin, urlsplit

import aiohttp

BASE_URL = "https://www.when2meet.com/"
EVENT_QUERY = re.compile(r"\d+-[A-Za-z0-9]+")


class When2meetError(Exception):
    """The event could not be created or its share URL could not be verified."""


def event_url(response_url: str, body: str) -> str:
    # The form may redirect via HTTP or via JavaScript in the response body.
    candidates = [response_url]
    candidates.extend(re.findall(r"[\"']([^\"'\s<>]*\?\d+-[A-Za-z0-9]+)[\"']", body))
    for candidate in candidates:
        parsed = urlsplit(urljoin(BASE_URL, candidate))
        if (
            parsed.scheme == "https"
            and parsed.netloc == "www.when2meet.com"
            and parsed.path in ("", "/")
            and EVENT_QUERY.fullmatch(parsed.query)
        ):
            return BASE_URL + "?" + parsed.query
    raise When2meetError("When2meet 没有返回可验证的活动链接。")


async def create_event(session: aiohttp.ClientSession, form: dict[str, str]) -> str:
    try:
        # Do not retry: a timed-out request may already have created an event.
        async with session.post(
            BASE_URL + "SaveNewEvent.php",
            data=form,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as response:
            response.raise_for_status()
            return event_url(str(response.url), await response.text())
    except (TimeoutError, aiohttp.ClientError) as exc:
        raise When2meetError("When2meet 暂时无法连接，或请求超时。") from exc
