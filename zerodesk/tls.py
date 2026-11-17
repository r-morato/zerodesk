"""Self-signed HTTPS certificate, created once per device and kept in
~/.config/zerodesk/tls (private key readable by the user only)."""

import hashlib
import os
import ssl
import subprocess


def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "zerodesk")


def ensure_cert(directory=None):
    """Return (cert_path, key_path), creating them if needed."""
    d = os.path.join(directory or config_dir(), "tls")
    cert, key = os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")
    if os.path.exists(cert) and os.path.exists(key):
        st = os.lstat(key)
        if st.st_uid == os.getuid() and not (st.st_mode & 0o077) and not os.path.islink(key):
            return cert, key
        os.remove(key)                 # someone else's or too open: make a new one
        os.remove(cert)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    old = os.umask(0o077)
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
             "-nodes", "-keyout", key, "-out", cert, "-days", "3650", "-sha256",
             "-subj", "/CN=CardputerZero ZeroDesk",
             "-addext", "basicConstraints=critical,CA:FALSE",
             "-addext", "keyUsage=critical,digitalSignature",
             "-addext", "extendedKeyUsage=serverAuth"],
            check=True, capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
    finally:
        os.umask(old)
    os.chmod(key, 0o600)
    return cert, key


def fingerprint(cert_path):
    """SHA-256 fingerprint as shown by browsers (AB:CD:...)."""
    with open(cert_path) as fh:
        der = ssl.PEM_cert_to_DER_cert(fh.read())
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def server_context(cert, key):
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx
