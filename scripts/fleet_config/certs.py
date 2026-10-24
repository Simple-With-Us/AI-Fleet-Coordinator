"""Reading an Apple certificate's expiry,  with the standard library only.

The fleet's signing identities live in the login keychain and its provisioning profiles
live under ``~/Library/MobileDevice/Provisioning Profiles``.  Neither is a file this
package can parse on its own:  a keychain is a database and a provisioning profile is a
CMS blob.  So the two readers here ask macOS for the parts they need:

  * ``security find-certificate -a -c NAME -p`` writes the matching certificates as PEM,
    and this module reads the expiry out of that PEM without openssl.
  * ``security cms -D -i FILE`` writes a provisioning profile as a plist,  and the expiry
    is the profile's own ``ExpirationDate``.

The PEM reader is a small DER walker:  it finds the ``validity`` field of the
``TBSCertificate`` and reads ``notAfter``.  Writing it here keeps the engine free of a
third-party package and of a temporary file,  and it means the expiry a rule reports is
the expiry the certificate carries.

Nothing here prints a certificate's contents.  A certificate is public material,  but the
rule that uses these readers reports dates and names,  never a body.

Stdlib only,  and Python 3.9 safe,  matching ``fleet_knobs``.
"""

from __future__ import annotations

import base64
import binascii
import plistlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone

__all__ = [
    "CertificateExpiry",
    "ExpiryUnknown",
    "keychain_expiries",
    "not_after_from_der",
    "not_after_from_pem",
    "profile_expiry",
]


class ExpiryUnknown(Exception):
    """The expiry could not be read,  and the message says why in a short token.

    The message never carries a value:  it names the shape that was wrong,  and the
    caller turns it into an unknown result rather than a guess.
    """


@dataclass(frozen=True)
class CertificateExpiry:
    """One certificate and the day it stops being valid.

    ``label`` names the certificate without naming its owner:  the rule's declared
    source and the certificate's common name,  which is public material.  ``expires_at``
    is the moment the certificate stops being valid.
    """

    label: str
    expires_at: datetime

    def days_left(self, now: datetime) -> int:
        """The whole days between `now` and the expiry;  negative once it has expired."""
        return (self.expires_at - now).days


# The DER tags this module reads.
_SEQUENCE = 0x30
_UTC_TIME = 0x17
_GENERALIZED_TIME = 0x18
_CONTEXT_0 = 0xA0

_PEM_BLOCK = re.compile(
    r"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", re.DOTALL
)


def _read_tlv(buf: bytes, pos: int) -> tuple[int, bytes, int]:
    """One DER element:  (tag, content bytes,  the position after it).

    Only the definite-length form is read,  which is what every certificate uses.  A
    truncated or malformed buffer raises,  and the caller turns that into an unknown
    expiry rather than a guess.
    """
    if pos >= len(buf):
        raise ExpiryUnknown("the certificate data ends early")
    tag = buf[pos]
    pos += 1
    if pos >= len(buf):
        raise ExpiryUnknown("the certificate data has no length")
    first = buf[pos]
    pos += 1
    if first & 0x80:
        count = first & 0x7F
        if count == 0 or pos + count > len(buf):
            raise ExpiryUnknown("the certificate data has an unusable length")
        length = int.from_bytes(buf[pos : pos + count], "big")
        pos += count
    else:
        length = first
    end = pos + length
    if end > len(buf):
        raise ExpiryUnknown("the certificate data is shorter than it says")
    return tag, buf[pos:end], end


def not_after_from_der(der: bytes) -> datetime:
    """The ``notAfter`` moment of a DER certificate,  as an aware UTC datetime.

    The walk is fixed:  a certificate is a sequence of a signed certificate and its
    signature,  the signed certificate holds a version,  a serial number,  a signature,
    an issuer and a validity,  and the validity holds two times.  A buffer that is not
    that shape raises ``ExpiryUnknown``.
    """
    tag, certificate, _ = _read_tlv(der, 0)
    if tag != _SEQUENCE:
        raise ExpiryUnknown("the certificate data is not a sequence")
    tag, signed, _ = _read_tlv(certificate, 0)
    if tag != _SEQUENCE:
        raise ExpiryUnknown("the certificate holds no signed certificate")
    pos = 0
    tag, _, pos = _read_tlv(signed, pos)
    if tag == _CONTEXT_0:
        tag, _, pos = _read_tlv(signed, pos)
    if tag != 0x02:
        raise ExpiryUnknown("the certificate holds no serial number")
    tag, _, pos = _read_tlv(signed, pos)
    if tag != _SEQUENCE:
        raise ExpiryUnknown("the certificate holds no signature algorithm")
    tag, _, pos = _read_tlv(signed, pos)
    if tag != _SEQUENCE:
        raise ExpiryUnknown("the certificate holds no issuer")
    tag, validity, _ = _read_tlv(signed, pos)
    if tag != _SEQUENCE:
        raise ExpiryUnknown("the certificate holds no validity")
    tag, _, inner = _read_tlv(validity, 0)
    if tag not in (_UTC_TIME, _GENERALIZED_TIME):
        raise ExpiryUnknown("the certificate has no start time")
    tag, not_after_bytes, _ = _read_tlv(validity, inner)
    if tag not in (_UTC_TIME, _GENERALIZED_TIME):
        raise ExpiryUnknown("the certificate has no end time")
    text = not_after_bytes.decode("ascii", "replace")
    try:
        if tag == _UTC_TIME:
            moment = datetime.strptime(text, "%y%m%d%H%M%SZ")
        else:
            moment = datetime.strptime(text, "%Y%m%d%H%M%SZ")
    except ValueError:
        raise ExpiryUnknown("the certificate end time is not a time") from None
    return moment.replace(tzinfo=timezone.utc)


def not_after_from_pem(pem: str) -> datetime:
    """The ``notAfter`` moment of the first certificate in a PEM bundle.

    A bundle with several certificates gives the first one,  because the caller asks
    for one certificate at a time;  a bundle with none raises.
    """
    match = _PEM_BLOCK.search(pem)
    if match is None:
        raise ExpiryUnknown("the answer holds no certificate")
    try:
        der = base64.b64decode(re.sub(r"\s+", "", match.group(1)), validate=True)
    except (binascii.Error, ValueError):
        raise ExpiryUnknown("the certificate is not base64") from None
    return not_after_from_der(der)


def profile_expiry(data: bytes) -> datetime:
    """The expiry of a provisioning profile,  from the plist the profile decodes to.

    The profile's own ``ExpirationDate`` is the day the profile stops being valid,  and
    it is what the fleet's own tooling reads.
    """
    try:
        document = plistlib.loads(data)
    except Exception:  # noqa: BLE001 - any malformed profile is one unknown expiry
        raise ExpiryUnknown("the profile is not a readable plist") from None
    if not isinstance(document, dict):
        raise ExpiryUnknown("the profile is not a plist dictionary")
    moment = document.get("ExpirationDate")
    if not isinstance(moment, datetime):
        raise ExpiryUnknown("the profile carries no expiry date")
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def keychain_expiries(pem: str, label: str) -> list[CertificateExpiry]:
    """Every certificate in a PEM bundle,  with its expiry.

    A bundle with no certificate gives an empty list,  which is how the caller knows the
    keychain holds no certificate with that name.
    """
    out: list[CertificateExpiry] = []
    for block in _PEM_BLOCK.finditer(pem):
        try:
            der = base64.b64decode(re.sub(r"\s+", "", block.group(1)), validate=True)
        except (binascii.Error, ValueError):
            continue
        out.append(CertificateExpiry(label=label, expires_at=not_after_from_der(der)))
    return out
