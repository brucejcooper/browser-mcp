"""Extra CA files (a TLS-inspecting proxy's root): for the browser, imported
into the profile's NSS store with certutil; for the server's own connections
(to an https:// upstream proxy), added to its SSL context."""

from __future__ import annotations

import hashlib
import logging
import re
import ssl
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

NICK_PREFIX = "browser-mcp-ca-"
_PEM = re.compile(rb"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.S)


def certificates(paths: list[str]) -> list[bytes]:
    """Every certificate in the files, as PEM (a file may hold a bundle)."""
    out = []
    for p in paths:
        data = Path(p).read_bytes()
        found = _PEM.findall(data)
        if not found:
            # One DER certificate.
            found = [ssl.DER_cert_to_PEM_cert(data).encode()]
        out += [c + b"\n" for c in found]
    return out


def ssl_context(paths: list[str]) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    for pem in certificates(paths):
        ctx.load_verify_locations(cadata=pem.decode())
    return ctx


def install_into_profile(profile: Path, paths: list[str]) -> None:
    """Make the profile's NSS store trust exactly these extra CAs: add what's
    missing, remove ones we added earlier that are no longer configured."""
    profile.mkdir(parents=True, exist_ok=True)
    db = f"sql:{profile}"
    if not (profile / "cert9.db").exists():
        if not paths:
            return
        _certutil("-N", "-d", db, "--empty-password")
    wanted = {}
    for pem in certificates(paths):
        der = ssl.PEM_cert_to_DER_cert(pem.decode())
        wanted[NICK_PREFIX + hashlib.sha256(der).hexdigest()[:16]] = pem
    have = set()
    for line in _certutil("-L", "-d", db).splitlines():
        nick = line.rsplit(None, 1)[0].strip() if line.strip() else ""
        if nick.startswith(NICK_PREFIX):
            have.add(nick)
    for nick in have - wanted.keys():
        _certutil("-D", "-d", db, "-n", nick)
        log.info("removed CA %s from the profile", nick)
    for nick in wanted.keys() - have:
        _certutil("-A", "-d", db, "-n", nick, "-t", "C,,", "-a", input=wanted[nick])
        log.info("trusted CA %s in the profile", nick)


def _certutil(*args: str, input: bytes | None = None) -> str:
    try:
        r = subprocess.run(["certutil", *args], input=input, capture_output=True, check=True)
    except FileNotFoundError:
        raise RuntimeError("certutil (NSS tools) is needed to trust extra CA files") from None
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"certutil {' '.join(args[:1])} failed: {e.stderr.decode().strip()}") from None
    return r.stdout.decode()
