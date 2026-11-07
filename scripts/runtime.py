"""Local files, command execution and Terraform ownership checks."""
from contextlib import contextmanager
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from urllib.request import urlopen
import csv
import json
import os
import subprocess

from yc import TYPES

ROOT = Path(__file__).resolve().parents[1]


def run(*args, capture=False, cwd=ROOT, timeout=1200, env=None):
    return subprocess.run(
        args, cwd=cwd, check=True, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE if capture else None, timeout=timeout, env=env,
    ).stdout


def save(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def public_ipv4():
    addresses = []
    for url in ("https://api.ipify.org", "https://checkip.amazonaws.com"):
        with urlopen(url, timeout=10) as response:
            addresses.append(IPv4Address(response.read(80).decode().strip()))
    if addresses[0] != addresses[1] or not addresses[0].is_global:
        raise ValueError("The two HTTPS checks did not confirm the same public IPv4.")
    return addresses[0]


def admin_access(configured):
    address = public_ipv4()
    if not configured:
        return [f"{address}/32"]
    networks = [IPv4Network(cidr) for cidr in configured]
    if any(net.prefixlen != 32 or not net.network_address.is_global for net in networks):
        raise ValueError("admin_cidrs must contain only public IPv4 /32 entries.")
    if not any(address in net for net in networks):
        raise ValueError("The confirmed IPv4 is absent from admin_cidrs.")
    return [str(net) for net in networks]


def protect(directory):
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or (os.name == "nt" and directory.is_junction()):
        raise RuntimeError("The local data directory must not be a link.")
    if os.name == "nt":
        row = next(csv.reader([run("whoami", "/user", "/fo", "csv", "/nh", capture=True).strip()]))
        run("icacls", str(directory), "/inheritance:r", "/grant:r", f"*{row[1]}:(OI)(CI)F",
            capture=True)
    else:
        directory.chmod(0o700)


@contextmanager
def locked(directory):
    protect(directory)
    with (directory / "operation.lock").open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class Terraform:
    def __init__(self, local):
        self.local = local
        self.state = local / "terraform.tfstate"
        self.plan = local / "saved.tfplan"

    def call(self, *args, capture=False, token=None):
        env = dict(os.environ, TF_DATA_DIR=str(self.local / "terraform-data"),
                   TF_IN_AUTOMATION="true", TF_INPUT="false")
        if token is not None:
            env["YC_TOKEN"] = token
        return run("terraform", f"-chdir={ROOT / 'terraform'}", *args, capture=capture, env=env)

    def init(self):
        self.call("init", "-input=false", "-lockfile=readonly")

    def plan_apply(self, *, token, destroy=False):
        mode = ["-destroy"] if destroy else []
        self.call("plan", *mode, "-input=false", f"-state={self.state}",
                  f"-var-file={self.local / 'terraform.tfvars.json'}", f"-out={self.plan}", token=token)
        data = json.loads(self.call("show", "-json", str(self.plan), capture=True))
        allowed = {("no-op",), ("delete",) if destroy else ("create",)}
        for change in data.get("resource_changes", []):
            if change["type"] not in TYPES or tuple(change["change"]["actions"]) not in allowed:
                raise RuntimeError(f"Refusing unexpected Terraform action for {change['address']}.")
        self.call("apply", "-input=false", f"-state={self.state}", str(self.plan), token=token)

    def resources(self, context):
        if not self.state.exists():
            return set()
        document = read(self.state)
        if context.get("lineage") and document["lineage"] != context["lineage"]:
            raise RuntimeError("Terraform state lineage differs from the saved context.")
        owned = set()
        for resource in document["resources"]:
            if resource["mode"] != "managed" or resource["type"] not in TYPES:
                raise RuntimeError("Terraform state contains an unsupported resource.")
            for instance in resource["instances"]:
                record = instance["attributes"]
                name = record["name"]
                prefix = context["name"]
                if name != prefix and not name.startswith(prefix + "-"):
                    raise RuntimeError("Terraform state contains a resource outside the lab.")
                if record.get("folder_id") != context["folder"]:
                    raise RuntimeError("Terraform state resource belongs to another YC folder.")
                owned.add((resource["type"], record["id"]))
                if resource["type"] == "yandex_compute_instance":
                    disk_id = record["boot_disk"][0]["disk_id"]
                    owned.add(("disk", disk_id))
        return owned

    def nodes(self, context):
        if not self.resources(context):
            raise RuntimeError("The lab has no managed infrastructure.")
        nodes = read(self.state)["outputs"]["nodes"]["value"]
        ids = self.resources(context)
        if set(nodes) != {"portal", "logs"} or any(
            ("yandex_compute_instance", node["id"]) not in ids for node in nodes.values()
        ):
            raise RuntimeError("Terraform outputs do not match the managed nodes.")
        return nodes
