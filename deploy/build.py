#!/usr/bin/env python3
"""Builds deploy/cloud-init.yaml (paste as Custom data when creating the VM) and
deploy/update.sh (paste into the VM's Run command later to ship new code)."""
import base64, io, sys, tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".venv", "__pycache__", ".pytest_cache", "deploy", ".git", "tests"}
ENV = Path(sys.argv[1]).read_text() if len(sys.argv) > 1 else "NTFY_TOPIC=\n"


def bundle() -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=9) as tar:
        for p in sorted(ROOT.rglob("*")):
            rel = p.relative_to(ROOT)
            if p.is_file() and not (set(rel.parts) & SKIP) and p.suffix not in (".db", ".pyc") \
                    and p.name not in ("status.html", "status.json", ".env", ".env.example", ".gitignore", "README.md"):
                tar.add(p, arcname=str(rel))
    return base64.b64encode(buf.getvalue()).decode()


def indent(text, n=6):
    return "\n".join(" " * n + line for line in text.splitlines())


INSTALL = r"""#!/bin/bash
# Installs/updates psn-watch from /opt/psn-watch.tar.gz.b64. Safe to re-run.
set -euxo pipefail
id psnwatch >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin psnwatch
mkdir -p /opt/psn-watch /var/lib/psn-watch /opt/pw-browsers
base64 -d /opt/psn-watch.tar.gz.b64 | tar -xz -C /opt/psn-watch
cd /opt/psn-watch
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
[ -f /opt/pw-browsers/.installed ] || { .venv/bin/python -m playwright install --with-deps chromium && touch /opt/pw-browsers/.installed; }
chown -R psnwatch:psnwatch /opt/psn-watch /var/lib/psn-watch /opt/pw-browsers
chmod 640 /etc/psn-watch.env; chgrp psnwatch /etc/psn-watch.env
cp /opt/psn-watch/deploy-files/psn-watch.service /etc/systemd/system/psn-watch.service
systemctl daemon-reload
systemctl enable psn-watch
systemctl restart psn-watch
"""

STATUS = r"""#!/bin/bash
# psn-status: what the watcher sees right now + recent log
cd /opt/psn-watch && sudo -u psnwatch PSN_DATA=/var/lib/psn-watch .venv/bin/python watch.py --status
echo; systemctl --no-pager --lines=0 status psn-watch | head -5
echo; journalctl -u psn-watch --no-pager -n ${1:-25} -o cat
"""


def main():
    b64 = bundle()
    service = (ROOT / "deploy" / "psn-watch.service").read_text()
    # ship the service file inside the bundle too, so updates can change it
    files = {
        "/opt/psn-watch.tar.gz.b64": (b64, "0600"),
        "/usr/local/sbin/psn-install": (INSTALL, "0755"),
        "/usr/local/bin/psn-status": (STATUS, "0755"),
        "/etc/psn-watch.env": (ENV, "0640"),
    }
    out = ["#cloud-config", "package_update: true", "packages: [python3-venv, python3-pip]",
           "swap: {filename: /swapfile, size: 2147483648}", "write_files:"]
    for path, (content, mode) in files.items():
        out += [f"  - path: {path}", f"    permissions: '{mode}'", "    content: |", indent(content)]
    out += ["runcmd:",
            "  - mkdir -p /opt/psn-watch/deploy-files",
            "  - " + repr(f"echo '{base64.b64encode(service.encode()).decode()}' | base64 -d > /opt/psn-watch/deploy-files/psn-watch.service"),
            "  - /usr/local/sbin/psn-install > /var/log/psn-install.log 2>&1"]
    ci = "\n".join(out) + "\n"
    (ROOT / "deploy" / "cloud-init.yaml").write_text(ci)

    upd = ("#!/bin/bash\n# Paste into Azure portal > VM > Run command > RunShellScript to ship new code\nset -e\n"
           f"echo '{b64}' > /opt/psn-watch.tar.gz.b64\n"
           "mkdir -p /opt/psn-watch/deploy-files\n"
           f"echo '{base64.b64encode(service.encode()).decode()}' | base64 -d > /opt/psn-watch/deploy-files/psn-watch.service\n"
           "/usr/local/sbin/psn-install > /var/log/psn-install.log 2>&1 && echo INSTALL_OK || { tail -20 /var/log/psn-install.log; exit 1; }\n"
           "sleep 20; /usr/local/bin/psn-status 15\n")
    (ROOT / "deploy" / "update.sh").write_text(upd)
    # Self-contained installer: run as root on a fresh Ubuntu VM (scp it over, then `sudo bash deploy.sh`)
    parts = ["#!/bin/bash", "# psn-watch self-contained deploy/update. Run: sudo bash psn-watch-deploy.sh", "set -euo pipefail"]
    for path, (content, mode) in files.items():
        if path == "/etc/psn-watch.env":
            # never overwrite alert settings you may have edited on the VM
            parts.append(f"[ -f {path} ] || cat > {path} <<'PSNEOF'\n{content.rstrip()}\nPSNEOF")
        else:
            parts.append(f"cat > {path} <<'PSNEOF'\n{content.rstrip()}\nPSNEOF")
        parts.append(f"chmod {mode} {path}")
    parts += ["mkdir -p /opt/psn-watch/deploy-files",
              f"cat > /opt/psn-watch/deploy-files/psn-watch.service <<'PSNEOF'\n{service.rstrip()}\nPSNEOF",
              "if [ ! -f /swapfile ]; then fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab; fi",
              "command -v python3 >/dev/null && python3 -m venv --help >/dev/null 2>&1 || { apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -yq python3-venv python3-pip; }",
              "dpkg -s python3-venv >/dev/null 2>&1 || { apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -yq python3-venv python3-pip; }",
              "echo 'installing (takes ~5 min the first time)...'",
              "/usr/local/sbin/psn-install > /var/log/psn-install.log 2>&1 && echo INSTALL_OK || { tail -30 /var/log/psn-install.log; echo INSTALL_FAILED; exit 1; }",
              "sleep 25; /usr/local/bin/psn-status 20"]
    dep = "\n".join(parts) + "\n"
    (ROOT / "deploy" / "psn-watch-deploy.sh").write_text(dep)
    print(f"psn-watch-deploy.sh: {len(dep):,} bytes")
    print(f"cloud-init.yaml: {len(ci):,} bytes (Azure limit 64 KB after base64 ≈ 48 KB raw)")
    print(f"update.sh:       {len(upd):,} bytes")


main()
