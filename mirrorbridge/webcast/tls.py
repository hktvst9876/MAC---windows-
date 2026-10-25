"""Self-signed certificate for the browser-cast page.

Browsers only allow screen capture on secure (HTTPS) pages, so the page is
served over TLS with a certificate generated on first use. Browsers will warn
that it is not issued by a known authority; the user accepts it once.
"""

import datetime
import ipaddress
import logging
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

log = logging.getLogger(__name__)


def _covered(cert, addresses):
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        ips = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
    except x509.ExtensionNotFound:
        return False
    not_after = cert.not_valid_after_utc if hasattr(cert, "not_valid_after_utc") else cert.not_valid_after
    if not_after.timestamp() < datetime.datetime.now(datetime.timezone.utc).timestamp() + 7 * 86400:
        return False
    return set(addresses) <= ips


def ensure_certificate(folder: Path, addresses):
    """Return (certfile, keyfile), creating or renewing them when needed."""
    folder.mkdir(parents=True, exist_ok=True)
    cert_path = folder / "webcast-cert.pem"
    key_path = folder / "webcast-key.pem"
    addresses = list(addresses) + ["127.0.0.1"]
    if cert_path.exists() and key_path.exists():
        try:
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            if _covered(cert, addresses):
                return str(cert_path), str(key_path)
        except Exception:
            pass

    key = ec.generate_private_key(ec.SECP256R1())
    host = socket.gethostname() or "mirrorbridge"
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MirrorBridge (%s)" % host)])
    alt = [x509.DNSName("localhost")]
    try:
        alt.append(x509.DNSName(host))
    except Exception:
        pass
    for addr in sorted(set(addresses)):
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(addr)))
        except ValueError:
            pass
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    log.info("created browser-cast certificate for %s", ", ".join(addresses))
    return str(cert_path), str(key_path)
