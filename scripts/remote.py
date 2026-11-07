"""Deliver the configuration and run service checks over OpenSSH."""
from pathlib import Path
from tempfile import TemporaryDirectory
import re
import secrets
import shlex
import subprocess
import tarfile
import time
from uuid import uuid4

from runtime import ROOT, read, run, save


class Remote:
    def __init__(self, local, context, nodes):
        self.local = local
        self.context = context
        self.nodes = nodes
        self.config = local / "ssh-config"
        self.config.write_text(self.ssh_config(), encoding="utf-8")

    def ssh_config(self):
        known = (self.local / "known_hosts").as_posix()
        key = (self.local / "operator").as_posix()
        return (
            f'Host *\n User ubuntu\n BatchMode yes\n ConnectTimeout 8\n'
            f' IdentitiesOnly yes\n IdentityFile "{key}"\n'
            f' StrictHostKeyChecking accept-new\n UserKnownHostsFile "{known}"\n'
            f'Host portal\n HostName {self.nodes["portal"]["public"]}\n'
            f'Host logs\n HostName {self.nodes["logs"]["private"]}\n ProxyJump portal\n'
        )

    def command(self, node, command, *, capture=False, timeout=900):
        return run("ssh", "-F", str(self.config), node, command,
                   capture=capture, timeout=timeout)

    def wait(self):
        for attempt in range(24):
            try:
                self.command("portal", "true", capture=True, timeout=12)
                self.command("logs", "true", capture=True, timeout=15)
                return
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                if attempt == 23:
                    raise
                time.sleep(5)

    def settings(self):
        path = self.local / "secrets.json"
        if not path.exists():
            save(path, {key: secrets.token_hex(24) for key in
                        ("wordpress_admin", "wordpress_db", "elastic_password",
                         "kibana_password", "kibana_key", "logstash_password")})
        return dict(
            read(path), logs_address=self.nodes["logs"]["private"],
            wordpress_port=self.context["wordpress_port"], kibana_port=self.context["kibana_port"],
            kafka_cluster_id=read(self.local / "kafka.json")["id"],
        )

    def authorize_ansible(self):
        # YC image metadata may install only the first key for an OS user.
        key = " ".join((self.local / "ansible.pub").read_text(encoding="utf-8").split()[:2])
        if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}", key):
            raise ValueError("Invalid Ansible public key.")
        public_key = shlex.quote(key)
        self.command(
            "logs",
            'set -eu; umask 077; mkdir -p "$HOME/.ssh"; chmod 700 "$HOME/.ssh"; '
            'keys="$HOME/.ssh/authorized_keys"; touch "$keys"; chmod 600 "$keys"; '
            f'grep -Fq -- {public_key} "$keys" || echo {public_key} >> "$keys"',
        )

    def configure(self):
        self.wait()
        self.authorize_ansible()
        kafka = self.local / "kafka.json"
        if not kafka.exists():
            save(kafka, {"id": secrets.token_urlsafe(16)})
        with TemporaryDirectory(dir=self.local) as tmp:
            bundle = Path(tmp) / "bundle.tar.gz"
            self.bundle(bundle)
            remote = self.command("portal", "mktemp -d /tmp/otus-dz08.XXXXXXXX", capture=True).strip()
            if not re.fullmatch(r"/tmp/otus-dz08\.[A-Za-z0-9]{8}", remote):
                raise RuntimeError("Unexpected remote staging directory.")
            try:
                run("scp", "-F", str(self.config), str(bundle), f"portal:{remote}/bundle.tar.gz")
                self.command("portal", f"cd {remote} && tar -xzf bundle.tar.gz && "
                             "chmod 600 ansible-key settings.json && bash bootstrap.sh",
                             timeout=2400)
            finally:
                self.command("portal", f"rm -r -- {remote}")

    def bundle(self, path):
        settings = path.parent / "settings.json"
        save(settings, self.settings())
        with tarfile.open(path, "w:gz") as archive:
            for directory in ("ansible", "portal"):
                archive.add(ROOT / directory, arcname=directory,
                            filter=lambda item: None if "__pycache__" in Path(item.name).parts else item)
            for source, name in (
                (ROOT / "scripts/bootstrap.sh", "bootstrap.sh"),
                (ROOT / "config/stack.yml", "stack.yml"),
                (settings, "settings.json"),
                (self.local / "ansible", "ansible-key"),
            ):
                archive.add(source, arcname=name)

    def open(self):
        for name, node in self.nodes.items():
            print(f'{name}: {node["zone"]}')
        wordpress = self.context["wordpress_port"]
        kibana = self.context["kibana_port"]
        print(f'WordPress: http://localhost:{wordpress}  Kibana: http://localhost:{kibana}')
        print("Keep this SSH session open. Ctrl+C closes both tunnels.")
        run("ssh", "-F", str(self.config), "-o", "ExitOnForwardFailure=yes", "-N",
            "-L", f"127.0.0.1:{wordpress}:127.0.0.1:{wordpress}",
            "-L", f"127.0.0.1:{kibana}:127.0.0.1:{kibana}", "portal", timeout=None)

    def verify(self):
        self.command("portal", "sudo /usr/local/lib/otus-dz08/import-content --check")
        port = self.context["wordpress_port"]
        self.command("portal", f"test \"$(curl -fsS 'http://localhost:{port}/' | "
                     "grep -c 'class=\"lesson-card\"')\" -eq 8")
        self.command("logs", "sudo /usr/local/lib/otus-dz08/verify baseline")
        marker = uuid4().hex
        try:
            self.command("logs", "sudo systemctl stop logstash")
            self.command("logs", f"sudo /usr/local/lib/otus-dz08/verify offsets {marker}")
            self.command("portal", f"curl -fsS -H 'X-OTUS-Check: {marker}' "
                         f"'http://localhost:{port}/' >/dev/null")
            self.command("logs", f"sudo /usr/local/lib/otus-dz08/verify kafka {marker}")
        finally:
            self.command("logs", f"sudo rm -f /run/otus-dz08-{marker}.json; "
                         "sudo systemctl start logstash")
        self.command("logs", f"sudo /usr/local/lib/otus-dz08/verify elastic {marker}")
