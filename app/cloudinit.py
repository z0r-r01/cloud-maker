import base64
import binascii
import json
import os
import re
import subprocess
import tempfile

from pathlib import Path


DEFAULT_SSH_KEY = Path.home() / ".ssh/cloud-maker-test.pub"


def validate_hostname(hostname):
    pattern = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"

    if not re.fullmatch(pattern, hostname):
        raise ValueError("Invalid hostname")


def validate_ssh_key(key):
    parts = key.strip().split()

    if len(parts) < 2:
        raise ValueError("Invalid SSH public key")

    allowed_types = (
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
    )

    if parts[0] not in allowed_types:
        raise ValueError("Unsupported SSH key type")

    try:
        decoded = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid SSH key encoding") from exc

    if len(decoded) < 32:
        raise ValueError("SSH key is too short")

    return key.strip()


def create_cloud_init(
    name,
    seed_path,
    hostname=None,
    ssh_public_key=None,
):
    hostname = hostname if hostname is not None else name

    validate_hostname(hostname)

    if ssh_public_key is None:
        if not DEFAULT_SSH_KEY.is_file():
            raise ValueError("Default SSH public key not found")

        ssh_public_key = DEFAULT_SSH_KEY.read_text().strip()

    ssh_public_key = validate_ssh_key(ssh_public_key)

    user_data = (
        "#cloud-config\n"
        f"hostname: {json.dumps(hostname)}\n"
        "users:\n"
        "  - default\n"
        "ssh_authorized_keys:\n"
        f"  - {json.dumps(ssh_public_key)}\n"
        "ssh_pwauth: false\n"
    )

    metadata = (
        f"instance-id: {name}\n"
        f"local-hostname: {hostname}\n"
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        user_file = temp_path / "user-data"
        meta_file = temp_path / "meta-data"

        user_file.write_text(user_data)
        meta_file.write_text(metadata)

        subprocess.run(
            [
                "cloud-localds",
                str(seed_path),
                str(user_file),
                str(meta_file),
            ],
            check=True,
        )

    os.chmod(seed_path, 0o660)
