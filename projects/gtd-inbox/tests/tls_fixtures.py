"""Generate loopback-only TLS fixtures in temporary storage. Never commit keys."""
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path
from tempfile import TemporaryDirectory

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
except ImportError as exc:
    raise ImportError('Install test dependencies: python -m pip install -r requirements-dev.txt') from exc

_temporary = TemporaryDirectory(prefix='gtd-synthetic-tls-')
FIXTURES = Path(_temporary.name)
now = datetime.now(timezone.utc)
ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Synthetic GTD test CA')])
server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
      .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
      .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=7))
      .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
      .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                     key_encipherment=False, data_encipherment=False, key_agreement=False,
                     key_cert_sign=True, crl_sign=True, encipher_only=None, decipher_only=None), critical=True)
      .sign(ca_key, hashes.SHA256()))
server = (x509.CertificateBuilder().subject_name(server_name).issuer_name(ca_name)
          .public_key(server_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=7))
          .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
          .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),
                         x509.IPAddress(ip_address('127.0.0.1'))]), critical=False)
          .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
          .sign(ca_key, hashes.SHA256()))
(FIXTURES / 'imap-ca.pem').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
(FIXTURES / 'imap-server.pem').write_bytes(server.public_bytes(serialization.Encoding.PEM))
key_path = FIXTURES / 'imap-server.key'
key_path.write_bytes(server_key.private_bytes(serialization.Encoding.PEM,
                     serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
key_path.chmod(0o600)
del ca_key, server_key
