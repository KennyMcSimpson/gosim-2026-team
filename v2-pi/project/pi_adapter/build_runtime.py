"""Install pinned Linux Node; the Pi dependencies are bundled in the source ZIP."""
from __future__ import annotations

import hashlib
import platform
import subprocess
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NODE_RELEASE = "node-v22.20.0-linux-x64"
NODE_SHA256 = "eeaccb0378b79406f2208e8b37a62479c70595e20be6b659125eb77dd1ab2a29"
NODE_URL = f"https://nodejs.org/dist/v22.20.0/{NODE_RELEASE}.tar.gz"


def install_node():
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise RuntimeError("platform build requires Linux x86_64; install Node >=22.19 locally")
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    archive = runtime / "node.tar.gz"
    digest = hashlib.sha256()
    try:
        # urllib uses the platform's HTTPS_PROXY environment automatically.
        with urllib.request.urlopen(NODE_URL, timeout=60) as response, archive.open("wb") as out:
            for block in iter(lambda: response.read(1024 * 1024), b""):
                digest.update(block)
                out.write(block)
        if digest.hexdigest() != NODE_SHA256:
            raise RuntimeError("Node archive checksum mismatch")
        with tarfile.open(archive, "r:gz") as bundle:
            member = bundle.getmember(f"{NODE_RELEASE}/bin/node")
            if not member.isfile():
                raise RuntimeError("Node binary is not a regular archive member")
            target = runtime / NODE_RELEASE / "bin/node"
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.extractfile(member) as incoming, target.open("wb") as output:
                for block in iter(lambda: incoming.read(1024 * 1024), b""):
                    output.write(block)
            target.chmod(0o755)
    finally:
        archive.unlink(missing_ok=True)
    return runtime / NODE_RELEASE / "bin/node"


def main():
    expected = (ROOT / "worker.bundle.sha256").read_text(encoding="ascii").split()[0]
    if hashlib.sha256((ROOT / "worker.bundle.mjs").read_bytes()).hexdigest() != expected:
        raise RuntimeError("Pi bundle checksum mismatch")
    node = install_node()
    subprocess.run([str(node), "--check", "worker.bundle.mjs"], cwd=ROOT, check=True)
    print("Pinned Node and verified Pi bundle ready")


if __name__ == "__main__":
    main()
