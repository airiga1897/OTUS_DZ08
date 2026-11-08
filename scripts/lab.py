"""Operator entry point for the two-VM lab."""
from argparse import ArgumentParser
from ipaddress import ip_address, ip_network
from pathlib import Path
from urllib.error import URLError
import logging
import shutil
import subprocess
import sys
import time
from uuid import uuid4

from remote import Remote
from runtime import ROOT, Terraform, admin_access, locked, read, run, save
from yc import Cloud, GIB, choose

LOG = logging.getLogger(__name__)


class Lab:
    def __init__(self, local, *, plugin_dir=None):
        self.local = local
        self.context = local / "context.json"
        self.variables = local / "terraform.tfvars.json"
        self.tf = Terraform(local, plugin_dir=plugin_dir)
        self.policy = read(ROOT / "config/lab.json")

    def state_context(self):
        if not self.context.exists():
            raise RuntimeError("No local deployment context. Run deploy first.")
        return read(self.context)

    def cloud_context(self, cloud):
        context = self.state_context()
        if (context["provider"], context["cloud"], context["folder"]) != (
            "yc", cloud.cloud, cloud.folder
        ):
            raise RuntimeError("The current YC credentials point to a different cloud or folder.")
        return context

    def new_context(self, cloud):
        if self.tf.state.exists() or self.variables.exists():
            raise RuntimeError("Terraform files exist without context; recover them before deploy.")
        if self.local.name != ".local-test":
            existing = cloud.lab_names()
            if existing:
                raise RuntimeError(
                    f"DZ08 stand already exists in the selected YC folder: {', '.join(existing)}. "
                    "This clone has no local deployment context. "
                    "Use the original clone with its Terraform state and SSH keys to resume or destroy it."
                )
        image, zones, used = cloud.catalog(self.policy)
        if any(node["disk"] * GIB < int(image["minDiskSize"])
               for node in self.policy["nodes"].values()):
            raise RuntimeError("A configured boot disk is smaller than the Ubuntu image minimum.")
        cidrs = self.confirm_admin_access()
        nodes = choose(self.policy, zones, used, cloud.quota())
        context = {
            "provider": "yc", "cloud": cloud.cloud, "folder": cloud.folder,
            "name": "otus-dz08-" + uuid4().hex[:16], "nodes": nodes,
            "admin_cidrs": cidrs, "platform": self.policy["platform"],
            "disk_type": self.policy["disk_type"], "image_id": image["id"],
            "wordpress_port": self.policy["wordpress_port"],
            "kibana_port": self.policy["kibana_port"],
        }
        if cloud.inventory(context["name"]):
            raise RuntimeError("The generated namespace is already in use.")
        save(self.context, context)
        return context

    def confirm_admin_access(self, saved=()):
        access_file = ROOT / "config/lab.local.json"
        configured = (read(access_file) if access_file.exists() else self.policy)["admin_cidrs"]
        return admin_access(list(dict.fromkeys([*saved, *configured])))

    def key(self, name):
        key = self.local / name
        if not key.exists():
            run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key))
        return key.with_suffix(".pub").read_text(encoding="utf-8").strip()

    def prepare_variables(self, context):
        settings = {key: context[key] for key in (
            "name", "nodes", "admin_cidrs", "platform", "disk_type", "image_id",
            "wordpress_port", "kibana_port",
        )}
        if self.variables.exists():
            previous = read(self.variables)
            updated = dict(previous, wordpress_port=settings["wordpress_port"],
                           admin_cidrs=settings["admin_cidrs"])
            if previous != updated:
                save(self.variables, updated)
            return
        operator = self.key("operator")
        internal = self.key("ansible")
        save(self.variables, {
            "cloud_id": context["cloud"], "folder_id": context["folder"],
            **settings,
            "portal_key": operator, "logs_keys": f"{operator}\nubuntu:{internal}",
        })

    def deploy(self):
        cloud = Cloud()
        cloud.confirm_folder()
        if self.context.exists():
            context = self.cloud_context(cloud)
            context["admin_cidrs"] = self.confirm_admin_access(context["admin_cidrs"])
        else:
            context = self.new_context(cloud)
        existing = self.tf.resources(context)
        if context.get("lineage") and not existing:
            raise RuntimeError("Previously deployed infrastructure is missing from local state.")
        if cloud.inventory(context["name"]) != existing:
            raise RuntimeError("YC and state disagree; keep the files for ownership diagnosis.")
        if existing:
            LOG.info("Reusing existing DZ08 stand %s.", context["name"])
        self.prepare_variables(context)
        self.tf.init()
        self.tf.plan_apply(token=cloud.token)
        context["lineage"] = read(self.tf.state)["lineage"]
        save(self.context, context)
        if cloud.inventory(context["name"]) != self.tf.resources(context):
            raise RuntimeError("YC resources do not match the applied state.")
        nodes = self.tf.nodes(context)
        for name, node in nodes.items():
            if node["zone"] != context["nodes"][name]["zone"]:
                raise RuntimeError("Actual placement differs from the selected zone.")
            address = ip_address(node["private"])
            if address not in ip_network(context["nodes"][name]["cidr"]):
                raise RuntimeError("A node has an unexpected private address.")
        if not nodes["portal"]["public"] or nodes["logs"]["public"]:
            raise RuntimeError("Only the portal may have a public address.")
        self.remote().configure()

    def remote(self):
        context = self.state_context()
        return Remote(self.local, context, self.tf.nodes(context))

    def destroy(self):
        cloud = Cloud()
        cloud.confirm_folder()
        context = self.cloud_context(cloud)
        owned = self.tf.resources(context)
        found = cloud.inventory(context["name"])
        if found - owned:
            raise RuntimeError("Untracked lab resources found; local recovery files were retained.")
        cloud.confirm_ownership(context["name"], owned, found)
        if owned:
            self.prepare_variables(context)
            self.tf.init()
            self.tf.plan_apply(token=cloud.token, destroy=True)
        for attempt in range(10):
            if not self.tf.resources(context) and not cloud.inventory(context["name"]):
                self.clean()
                LOG.info("Removal verified in Terraform state and YC.")
                return
            if attempt < 9:
                time.sleep(3)
        raise RuntimeError("Removal is incomplete; local recovery files were retained.")

    def clean(self):
        if self.local.resolve().parent != ROOT or not self.local.name.startswith(".local"):
            raise RuntimeError("Refusing cleanup outside this clone.")
        temp = ROOT / ".tools/temp"
        if temp.resolve() != temp:
            raise RuntimeError("Refusing cleanup of a redirected .tools/temp directory.")
        if temp.exists():
            self.clean_directory(temp)
        self.clean_directory(self.local, keep=("operation.lock",))

    @staticmethod
    def clean_directory(directory, *, keep=()):
        for entry in directory.iterdir():
            if entry.name in keep:
                continue
            if entry.is_symlink() or (sys.platform == "win32" and entry.is_junction()):
                raise RuntimeError(f"Refusing cleanup of linked path: {entry.name}")
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()

    def test_cycle(self):
        try:
            self.deploy()
            self.remote().command("portal", "sudo /usr/local/lib/otus-dz08/import-content")
            self.remote().verify()
        finally:
            if self.context.exists():
                self.destroy()


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--test-context", action="store_true",
                        help="recover an interrupted test-cycle with destroy")
    parser.add_argument("--plugin-dir", type=Path,
                        help="install Terraform providers only from this directory for deploy or destroy")
    parser.add_argument("command", choices=[
        "deploy", "import-content", "verify", "open", "destroy", "test-cycle",
    ])
    args = parser.parse_args()
    if args.test_context and args.command != "destroy":
        parser.error("--test-context is only available with destroy")
    plugin_dir = args.plugin_dir
    if plugin_dir is not None:
        if args.command not in {"deploy", "destroy"}:
            parser.error("--plugin-dir is only available with deploy or destroy")
        plugin_dir = plugin_dir.resolve()
        if not plugin_dir.is_dir():
            parser.error(f"Terraform plugin directory does not exist: {plugin_dir}")
    test = args.command == "test-cycle" or args.test_context
    lab = Lab(ROOT / (".local-test" if test else ".local"), plugin_dir=plugin_dir)
    try:
        with locked(lab.local):
            if args.command == "test-cycle" and (lab.context.exists() or lab.tf.state.exists()):
                raise RuntimeError("Previous test cycle needs cleanup before a new run.")
            if args.command in {"deploy", "destroy", "test-cycle"}:
                getattr(lab, args.command.replace("-", "_"))()
            elif args.command == "import-content":
                lab.remote().command("portal", "sudo /usr/local/lib/otus-dz08/import-content")
            else:
                getattr(lab.remote(), args.command)()
    except (RuntimeError, ValueError, KeyError, OSError, URLError, subprocess.SubprocessError) as error:
        LOG.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
