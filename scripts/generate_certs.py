#!/usr/bin/env python3
"""Generate SSL/TLS private key and self-signed certificate for whisper_server."""

import argparse
import hashlib
import logging
import os
import shutil
import ssl
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("generate_certs")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "certs"


def get_cert_fingerprint(cert_path: Path) -> str:
    """Compute and format the SHA-256 fingerprint of a PEM certificate."""
    pem_text = cert_path.read_text(encoding="ascii")
    der_bytes = ssl.PEM_cert_to_DER_cert(pem_text)
    fp = hashlib.sha256(der_bytes).hexdigest()
    return ":".join(fp[i : i + 2].upper() for i in range(0, len(fp), 2))


def generate_certs(
    out_dir: Path = DEFAULT_OUT_DIR,
    force: bool = False,
) -> tuple[Path, Path]:
    """Generate RSA private key and self-signed certificate with kasupc SAN."""
    out_dir.mkdir(parents=True, exist_ok=True)
    key_path = out_dir / "key.pem"
    cert_path = out_dir / "cert.pem"

    if cert_path.exists() and key_path.exists() and not force:
        logger.info(
            "Certificates already exist in %s (use --force to overwrite)", out_dir
        )
        return cert_path, key_path

    openssl_bin = shutil.which("openssl")
    if not openssl_bin:
        logger.error("openssl executable not found on PATH. Please install OpenSSL.")
        sys.exit(1)

    cmd = [
        openssl_bin,
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(key_path),
        "-out",
        str(cert_path),
        "-days",
        "3650",
        "-subj",
        "/CN=kasupc",
        "-addext",
        "subjectAltName=DNS:kasupc",
    ]

    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        logger.error("openssl command failed:\n%s\n%s", e.stdout, e.stderr)
        sys.exit(1)

    try:
        os.chmod(key_path, 0o600)
        os.chmod(cert_path, 0o644)
    except OSError as e:
        logger.debug("Could not set file permissions: %s", e)

    fp = get_cert_fingerprint(cert_path)
    logger.info("Generated Whisper certificate for kasupc (SHA-256: %s)", fp)
    return cert_path, key_path


def main():
    parser = argparse.ArgumentParser(
        description="Generate keypair and certificate for whisper_server"
    )
    parser.add_argument(
        "-o",
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="Directory to save cert.pem and key.pem (default: ./certs)",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Overwrite existing certificate and key files",
    )
    args = parser.parse_args()
    generate_certs(out_dir=args.out_dir, force=args.force)


if __name__ == "__main__":
    main()
