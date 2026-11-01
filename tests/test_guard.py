import pytest

from browser_mcp.config import DenyConfig
from browser_mcp.guard import Guard, Refused


@pytest.fixture
def guard():
    return Guard(DenyConfig())


@pytest.mark.parametrize("url", [
    "https://www.example.com/login",
    "https://www.example.com/account/settings",
    "https://signin.example.com/",
    "https://www.example.com/cart",
    "https://www.example.com/checkout/step1",
    "https://www.example.com/item/123/bid",
    "https://www.example.com/buy/123",
    "https://offer.example.com/make",
    "https://www.example.com/user/register?next=/",
    "https://www.example.com/sign-in",
])
def test_deny_listed_urls_are_refused(guard, url):
    with pytest.raises(Refused, match="deny list"):
        guard.check_url(url)


@pytest.mark.parametrize("url", [
    "https://www.example.com/",
    "https://www.example.com/buying-guide/ram",
    "https://www.example.com/itm/123456",
    "https://www.example.com/sch/i.html?_nkw=buy+ddr4",  # the query isn't checked
    "https://www.example.com/product/accounting-software",
])
def test_ordinary_urls_pass(guard, url):
    guard.check_url(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "ftp://example.com/",
                                 "https://user:pw@example.com/"])
def test_other_schemes_and_credentials_are_refused(guard, url):
    with pytest.raises(Refused):
        guard.check_url(url)


def test_patterns_are_config():
    g = Guard(DenyConfig(words=[], patterns=[r"example\.com/secret"]))
    g.check_url("https://example.com/login")
    with pytest.raises(Refused, match="deny pattern"):
        g.check_url("https://example.com/secret/1")


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://127.0.0.1:8080/x", "http://localhost/", "http://10.1.2.3/",
    "http://192.168.1.1/", "http://172.16.0.1/", "http://100.64.0.1/", "http://169.254.169.254/latest",
    "http://[::1]/", "http://[fd00::1]/", "http://[fe80::1]/", "http://[::ffff:10.0.0.1]/",
    "http://0.0.0.0/",
])
async def test_private_addresses_are_refused(guard, url):
    with pytest.raises(Refused, match="denied network"):
        await guard.check(url)


async def test_a_name_resolving_to_a_private_address_is_refused(guard, monkeypatch):
    import asyncio, socket

    async def fake(self, host, port, **kw):
        assert host == "intranet.example"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.20.30.40", port))]

    monkeypatch.setattr(asyncio.get_running_loop().__class__, "getaddrinfo", fake)
    with pytest.raises(Refused, match="10.20.30.40, in a denied network"):
        await guard.check("https://intranet.example/")


async def test_any_private_address_among_several_refuses(guard, monkeypatch):
    import asyncio, socket

    async def fake(self, host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.0.5", port))]

    monkeypatch.setattr(asyncio.get_running_loop().__class__, "getaddrinfo", fake)
    with pytest.raises(Refused):
        await guard.check("https://mixed.example/")


async def test_unresolvable_is_refused_unless_via_proxy_allowed(monkeypatch):
    import asyncio, socket

    async def fake(self, host, port, **kw):
        raise socket.gaierror("no such name")

    monkeypatch.setattr(asyncio.get_running_loop().__class__, "getaddrinfo", fake)
    with pytest.raises(Refused, match="doesn't resolve"):
        await Guard(DenyConfig()).check("https://nowhere.example/", via_proxy=True)
    await Guard(DenyConfig(unresolvable_via_proxy=True)).check("https://nowhere.example/", via_proxy=True)
    with pytest.raises(Refused):
        await Guard(DenyConfig(unresolvable_via_proxy=True)).check("https://nowhere.example/", via_proxy=False)


async def test_networks_are_config():
    g = Guard(DenyConfig(networks=[]))
    await g.check("http://127.0.0.1/")
