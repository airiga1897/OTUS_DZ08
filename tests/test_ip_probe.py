"""Offline lifecycle checks. All state and cloud responses are isolated."""
from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from lab import Lab
from runtime import Terraform, admin_access, read, save
from yc import Cloud
spec = spec_from_file_location("ip_probe", ROOT / "functions/ip_probe.py")
probe = module_from_spec(spec)
spec.loader.exec_module(probe)
POLICY = read(ROOT / "config/lab.json")


class ProbeTest(TestCase):
    def test_source_is_platform_context_and_nonce_is_echoed(self):
        nonce = "a" * 32
        event = {"requestContext": {"identity": {"sourceIp": "95.31.25.30"}},
                 "queryStringParameters": {"nonce": nonce},
                 "headers": {"X-Forwarded-For": "8.8.8.8"}}
        response = probe.handler(event, None)
        self.assertEqual(json.loads(response["body"]), {"ip": "95.31.25.30", "nonce": nonce})
        for invalid in ("192.168.1.1", "224.0.0.1", "::1", "bad"):
            event["requestContext"]["identity"]["sourceIp"] = invalid
            self.assertEqual(probe.handler(event, None)["statusCode"], 400)

    def test_client_checks_nonce_ip_and_bounded_errors(self):
        cloud = Cloud.__new__(Cloud)
        cloud.token = "test-token"
        nonce = "a" * 32
        with patch("yc.uuid4", return_value=Mock(hex=nonce)), patch("yc.time.sleep"):
            for data in ({"ip": "95.31.25.30", "nonce": nonce},
                         {"ip": "95.31.25.30", "nonce": "wrong"},
                         {"ip": "10.0.0.1", "nonce": nonce}):
                with patch("yc.urlopen", return_value=BytesIO(json.dumps(data).encode())) as request:
                    if data["nonce"] == nonce and data["ip"] == "95.31.25.30":
                        self.assertEqual(cloud.public_ipv4("function1"), data["ip"])
                        self.assertIn("functions.yandexcloud.net/function1", request.call_args.args[0].full_url)
                        self.assertEqual(request.call_args.args[0].get_header("Authorization"), "Bearer test-token")
                    else:
                        with self.assertRaises(ValueError):
                            cloud.public_ipv4("function1")
            with patch("yc.urlopen", side_effect=URLError("offline")) as request:
                with self.assertRaisesRegex(RuntimeError, "state"):
                    cloud.public_ipv4("function1")
                self.assertEqual(request.call_count, 3)
            with patch("yc.urlopen", side_effect=HTTPError("url", 403, "denied", {}, None)) as request:
                with self.assertRaisesRegex(RuntimeError, "403"):
                    cloud.public_ipv4("function1")
                self.assertEqual(request.call_count, 1)

    def test_merge_preserves_previous_cidrs(self):
        self.assertEqual(admin_access(["95.31.25.30/32"], "135.106.162.122"),
                         ["95.31.25.30/32", "135.106.162.122/32"])
        self.assertEqual(admin_access(["95.31.25.30/32"], "95.31.25.30"), ["95.31.25.30/32"])
        with self.assertRaises(ValueError):
            admin_access(["0.0.0.0/0"], "95.31.25.30")

    def test_targeted_plan_cannot_create_vm(self):
        with TemporaryDirectory() as tmp:
            tf = Terraform(Path(tmp))
            change = {"type": "yandex_compute_instance", "address": "yandex_compute_instance.lab",
                      "change": {"actions": ["create"]}}
            with patch.object(tf, "call", side_effect=[None, json.dumps({"resource_changes": [change]})]) as call:
                with self.assertRaisesRegex(RuntimeError, "другие ресурсы"):
                    tf.plan_apply(token="test", probe_only=True)
                self.assertEqual(call.call_count, 2)


class LifecycleTest(TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        for folder in ("config", "functions", ".local", ".tools"):
            (self.root / folder).mkdir()
        save(self.root / "config/lab.json", POLICY)
        (self.root / "functions/ip_probe.py").write_bytes((ROOT / "functions/ip_probe.py").read_bytes())
        (self.root / ".tools/yc-config.yaml").write_text("test-profile")
        self.enterContext(patch("lab.ROOT", self.root))
        self.enterContext(patch("runtime.ROOT", self.root))
        self.enterContext(patch("lab.wp_cli"))
        self.cloud = self.enterContext(patch("lab.Cloud")).return_value
        self.cloud.cloud, self.cloud.folder, self.cloud.token = "cloud", "folder", "test"
        self.cloud.lab_names.return_value = []
        self.cloud.catalog.return_value = ({"id": "image", "minDiskSize": 0}, POLICY["zones"], [])
        self.cloud.public_ipv4.return_value = "95.31.25.30"
        self.cloud.inventory.side_effect = lambda name: set(self.inventory)
        self.inventory = set()
        self.nodes = {
            "portal": dict(POLICY["nodes"]["portal"], zone="ru-central1-d", cidr="10.84.8.0/24"),
            "logs": dict(POLICY["nodes"]["logs"], zone="ru-central1-d", cidr="10.84.9.0/24"),
        }
        self.enterContext(patch("lab.choose", return_value=self.nodes))
        self.lab = Lab(self.root / ".local")
        self.enterContext(patch.object(self.lab, "key", return_value="test-public-key"))
        self.enterContext(patch.object(self.lab.tf, "init"))
        self.remote = self.enterContext(patch.object(self.lab, "remote")).return_value
        self.applies = self.enterContext(patch.object(self.lab.tf, "plan_apply", side_effect=self.apply))

    def apply(self, *, token, probe_only=False, destroy=False):
        context = read(self.lab.context)
        name = context["name"]
        resources = []
        outputs = {}
        self.inventory.clear()
        if not destroy:
            resources.append({"mode": "managed", "type": "yandex_function", "name": "ip_probe",
                              "instances": [{"attributes": {"name": name + "-ip-probe",
                                             "folder_id": "folder", "id": "probe1"}}]})
            self.inventory.add(("yandex_function", "probe1"))
            if not probe_only:
                self.assertTrue(read(self.lab.variables)["admin_cidrs"])
                nodes = {}
                for role, private in (("portal", "10.84.8.2"), ("logs", "10.84.9.2")):
                    attrs = {"name": name + "-" + role, "folder_id": "folder", "id": role,
                             "boot_disk": [{"disk_id": role + "-disk"}]}
                    resources.append({"mode": "managed", "type": "yandex_compute_instance",
                                      "instances": [{"attributes": attrs}]})
                    self.inventory.update({("yandex_compute_instance", role), ("disk", role + "-disk")})
                    nodes[role] = {"id": role, "zone": "ru-central1-d", "private": private,
                                   "public": "51.250.43.252" if role == "portal" else ""}
                outputs = {"nodes": {"value": nodes}}
        save(self.lab.tf.state, {"lineage": name, "resources": resources, "outputs": outputs})

    def test_deploy_destroy_deploy_and_new_ip(self):
        self.lab.deploy()
        first = read(self.lab.context)
        self.assertEqual(self.lab.tf.probe_id(first), "probe1")
        archive = (self.lab.local / "ip-probe.zip").read_bytes()
        self.lab.tf.prepare_probe()
        self.assertEqual(archive, (self.lab.local / "ip-probe.zip").read_bytes())
        self.cloud.public_ipv4.return_value = "135.106.162.122"
        self.lab.deploy()
        context = read(self.lab.context)
        self.assertEqual(context["nodes"], first["nodes"])
        self.assertEqual(context["admin_cidrs"], ["95.31.25.30/32", "135.106.162.122/32"])
        self.lab.destroy()
        self.assertFalse(self.inventory)
        self.assertFalse(self.lab.context.exists())
        self.assertTrue((self.root / ".tools/yc-config.yaml").exists())
        self.lab.deploy()
        context = read(self.lab.context)
        self.assertNotEqual(context["name"], first["name"])
        self.assertEqual(context["admin_cidrs"], ["95.31.25.30/32", "135.106.162.122/32"])
        self.assertEqual(sum(bool(call.kwargs.get("probe_only")) for call in self.applies.call_args_list), 2)

    def test_ip_failure_preserves_function_then_retry_completes(self):
        self.cloud.public_ipv4.side_effect = RuntimeError("IP unavailable")
        with self.assertRaisesRegex(RuntimeError, "IP unavailable"):
            self.lab.deploy()
        self.assertEqual(self.inventory, {("yandex_function", "probe1")})
        self.assertTrue(self.lab.context.exists())
        self.remote.configure.assert_not_called()
        self.cloud.public_ipv4.side_effect = None
        self.lab.deploy()
        self.assertEqual(sum(bool(call.kwargs.get("probe_only")) for call in self.applies.call_args_list), 1)
        self.remote.configure.assert_called_once()

    def test_destroy_function_only_does_not_need_ip(self):
        self.cloud.public_ipv4.side_effect = RuntimeError("IP unavailable")
        with self.assertRaises(RuntimeError):
            self.lab.deploy()
        self.lab.destroy()
        self.assertEqual(self.cloud.public_ipv4.call_count, 1)
        self.assertFalse(self.inventory)
        self.assertFalse(self.lab.tf.state.exists())

    def test_failed_function_deletion_retains_recovery_data(self):
        self.cloud.public_ipv4.side_effect = RuntimeError("IP unavailable")
        with self.assertRaises(RuntimeError):
            self.lab.deploy()
        self.applies.side_effect = RuntimeError("delete failed")
        with self.assertRaisesRegex(RuntimeError, "delete failed"):
            self.lab.destroy()
        self.assertTrue(self.lab.context.exists())
        self.assertTrue(self.lab.tf.state.exists())
        self.assertTrue((self.lab.local / "ip-probe.zip").exists())

    def test_partial_function_creation_can_be_destroyed(self):
        def failed_create(**kwargs):
            self.apply(**kwargs)
            raise RuntimeError("version creation failed")
        self.applies.side_effect = failed_create
        with self.assertRaisesRegex(RuntimeError, "version creation failed"):
            self.lab.deploy()
        self.applies.side_effect = self.apply
        self.lab.destroy()
        self.assertFalse(self.inventory)
        self.assertFalse(self.lab.context.exists())

    def test_existing_stand_without_function_is_upgraded(self):
        self.lab.deploy()
        state = read(self.lab.tf.state)
        state["resources"] = [r for r in state["resources"] if r["type"] != "yandex_function"]
        save(self.lab.tf.state, state)
        self.inventory.remove(("yandex_function", "probe1"))
        original_name = read(self.lab.context)["name"]
        original_apply = self.apply

        def preserve_vms(**kwargs):
            if kwargs.get("probe_only"):
                state["resources"].append({
                    "mode": "managed", "type": "yandex_function",
                    "instances": [{"attributes": {"name": original_name + "-ip-probe",
                                   "folder_id": "folder", "id": "probe1"}}],
                })
                save(self.lab.tf.state, state)
                self.inventory.add(("yandex_function", "probe1"))
            else:
                original_apply(**kwargs)
        self.applies.side_effect = preserve_vms
        self.cloud.public_ipv4.return_value = "135.106.162.122"
        self.lab.deploy()
        self.assertEqual(read(self.lab.context)["name"], original_name)
        self.assertEqual(read(self.lab.context)["admin_cidrs"],
                         ["95.31.25.30/32", "135.106.162.122/32"])

    def test_unconfirmed_function_removal_preserves_files(self):
        self.cloud.public_ipv4.side_effect = RuntimeError("IP unavailable")
        with self.assertRaises(RuntimeError):
            self.lab.deploy()
        def still_present(**kwargs):
            save(self.lab.tf.state, {"resources": [], "lineage": read(self.lab.context)["lineage"]})
        self.applies.side_effect = still_present
        with patch("lab.time.sleep"), self.assertRaisesRegex(RuntimeError, "Removal is incomplete"):
            self.lab.destroy()
        self.assertTrue(self.lab.context.exists())
        self.assertTrue(self.lab.tf.state.exists())

    def test_address_history_is_scoped_and_explicit_addresses_survive(self):
        save(self.root / ".tools/admin-cidrs.json", {"other/folder": ["8.8.8.8/32"]})
        save(self.root / "config/lab.local.json", {"admin_cidrs": ["135.106.162.122/32"]})
        self.lab.deploy()
        self.assertEqual(read(self.lab.context)["admin_cidrs"],
                         ["135.106.162.122/32", "95.31.25.30/32"])
        history = read(self.root / ".tools/admin-cidrs.json")
        self.assertEqual(history["other/folder"], ["8.8.8.8/32"])
        self.assertEqual(history["cloud/folder"], ["95.31.25.30/32"])
        self.assertEqual(read(self.root / "config/lab.local.json"),
                         {"admin_cidrs": ["135.106.162.122/32"]})
