from importlib.util import module_from_spec, spec_from_file_location
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
import json
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lab import Lab
from remote import Remote
from runtime import Terraform
from yc import Cloud, choose, requirements

POLICY = json.loads((ROOT / "config/lab.json").read_text())
ZONES = POLICY["zones"][:2]
spec = spec_from_file_location("materials_import", ROOT / "ansible/roles/wordpress/files/import-content.py")
importer = module_from_spec(spec)
# The importer runs on Ubuntu. The tests exercise its pure logic on Windows.
with patch.dict(sys.modules, {"fcntl": object()}):
    spec.loader.exec_module(importer)


class AuthorizationTest(TestCase):
    def test_cloud_reads_the_clone_profile(self):
        with (
            patch("yc.Path.is_file", return_value=True),
            patch("yc.which") as which,
            patch("yc.subprocess.run") as run,
        ):
            run.side_effect = [subprocess.CompletedProcess([], 0, value)
                               for value in ("cloud\n", "folder\n", "profiletoken\n")]
            with patch.dict(os.environ, {"YC_TOKEN": "oldtoken", "YC_FOLDER_ID": "oldfolder"}):
                cloud = Cloud()
                self.assertEqual(os.environ["YC_TOKEN"], "oldtoken")
            self.assertEqual((cloud.cloud, cloud.folder, cloud.token), ("cloud", "folder", "profiletoken"))
            which.assert_not_called()
            config = [str(ROOT / ".tools/bin/yc.exe"), "--config", str(ROOT / ".tools/yc-config.yaml")]
            self.assertEqual([call.args[0] for call in run.call_args_list], [
                [*config, "config", "get", "cloud-id"],
                [*config, "config", "get", "folder-id"],
                [*config, "iam", "create-token", "--no-browser"],
            ])

    def test_missing_profile_stops_before_cloud_or_terraform_calls(self):
        with (
            TemporaryDirectory() as path,
            patch("yc.Path.is_file", return_value=False),
            patch("yc.which", return_value="yc"),
            patch("yc.subprocess.run", side_effect=subprocess.CalledProcessError(1, ["yc"])) as run,
            patch.object(Cloud, "confirm_folder") as api,
            patch.object(Terraform, "init") as init,
        ):
            lab = Lab(Path(path))
            with self.assertRaisesRegex(RuntimeError, r"yc-config\.yaml init"):
                lab.deploy()
            self.assertEqual(run.call_args_list[0].args[0], [
                "yc", "--config", str(ROOT / ".tools/yc-config.yaml"), "config", "get", "cloud-id",
            ])
            run.assert_called_once()
            api.assert_not_called()
            init.assert_not_called()
            self.assertFalse(lab.context.exists())

    def test_terraform_plan_and_apply_receive_the_profile_token(self):
        with TemporaryDirectory() as path, patch("runtime.run", side_effect=[None, "{}", None]) as run:
            with patch.dict(os.environ, {"YC_TOKEN": "oldtoken"}):
                Terraform(Path(path)).plan_apply(token="profiletoken")
                self.assertEqual(os.environ["YC_TOKEN"], "oldtoken")
            plan, show, apply = run.call_args_list
            self.assertEqual([call.args[2] for call in (plan, show, apply)], ["plan", "show", "apply"])
            self.assertEqual(plan.kwargs["env"]["YC_TOKEN"], "profiletoken")
            self.assertEqual(apply.kwargs["env"]["YC_TOKEN"], "profiletoken")


class PlacementTest(TestCase):
    def test_standard_hdd_catalog_may_omit_zone_ids(self):
        cloud = Cloud.__new__(Cloud)
        image = {"status": "READY", "id": "image"}
        with patch.object(cloud, "get", side_effect=[image, {"id": "network-hdd"}]), patch.object(
            cloud, "pages", return_value=[{"id": ZONES[0], "status": "UP"}]
        ), patch.object(cloud, "resources", return_value=[]):
            self.assertEqual(cloud.catalog(POLICY), (image, {ZONES[0]}, []))

    def test_prefers_one_zone(self):
        zone = ZONES[0]
        free = requirements(POLICY, zone, zone)
        actual = choose(POLICY, set(ZONES), [], free)
        self.assertEqual({zone}, {node["zone"] for node in actual.values()})
        self.assertNotEqual(actual["portal"]["cidr"], actual["logs"]["cidr"])

    def test_separate_zones_with_two_available_cores_each(self):
        free = requirements(POLICY, *ZONES)
        actual = choose(POLICY, set(ZONES), [], free)
        self.assertEqual(set(ZONES), {node["zone"] for node in actual.values()})
        self.assertNotEqual(actual["portal"]["cidr"], actual["logs"]["cidr"])

    def test_unknown_quota_stops_selection(self):
        zone = ZONES[0]
        free = requirements(POLICY, zone, zone)
        del free[f"compute.instanceCores.count.{zone}"]
        with self.assertRaisesRegex(RuntimeError, "instanceCores"):
            choose(POLICY, {zone}, [], free)

    def test_conflicting_subnet_uses_the_other_allowed_subnet(self):
        zone = ZONES[0]
        policy = dict(POLICY, subnets=[*POLICY["subnets"], "10.84.10.0/24"])
        free = requirements(policy, zone, zone)
        actual = choose(policy, {zone}, [POLICY["subnets"][0]], free)
        self.assertEqual(actual["portal"]["cidr"], POLICY["subnets"][1])

    def test_no_compatible_disk_zone_or_network(self):
        zone = ZONES[0]
        free = requirements(POLICY, zone, zone)
        with self.assertRaisesRegex(RuntimeError, "disk type unavailable"):
            choose(POLICY, set(), [], free)
        with self.assertRaisesRegex(RuntimeError, "non-overlapping"):
            choose(POLICY, {zone}, POLICY["subnets"], free)


class ImportTest(TestCase):
    def setUp(self):
        self.records = importer.materials(ROOT / "portal/materials.xml")

    def test_wxr_contains_all_eight_materials(self):
        self.assertEqual([str(number) for number in range(1, 9)],
                         [record["lesson"] for record in self.records])

    def test_existing_edited_post_is_preserved(self):
        record = self.records[0]
        post = {"ID": 12, "post_name": record["slug"], "post_status": "publish"}
        with patch.object(importer, "find", return_value=post), patch.object(
            importer, "wp", return_value='[{"meta_value":"1"}]'
        ) as wp:
            self.assertEqual(importer.apply(record, False), "preserved")
            wp.assert_called_once_with("post", "meta", "list", "12", "--keys=otus_lesson",
                                       "--format=json")

    def test_partial_import_repairs_meta_without_creating_a_post(self):
        record = self.records[0]
        post = {"ID": 12, "post_name": record["slug"], "post_status": "publish"}
        with patch.object(importer, "find", return_value=post), patch.object(
            importer, "wp", side_effect=["[]", "Success"]
        ) as wp:
            self.assertEqual(importer.apply(record, False), "preserved")
            self.assertEqual(wp.call_args.args, ("post", "meta", "add", "12", "otus_lesson", "1"))

    def test_verification_never_imports_missing_material(self):
        with patch.object(importer, "find", return_value=None), patch.object(importer, "wp") as wp:
            with self.assertRaisesRegex(RuntimeError, "Missing material"):
                importer.apply(self.records[0], True)
            wp.assert_not_called()


class StateTest(TestCase):
    def test_clean_deploy_reports_existing_yc_stand_before_placement(self):
        stand = "otus-dz08-0123456789abcdef"
        cloud = Cloud.__new__(Cloud)
        with (
            TemporaryDirectory() as path,
            patch("lab.Cloud", return_value=cloud),
            patch.object(cloud, "confirm_folder"),
            patch.object(cloud, "resources", return_value=[{"name": "default"}, {"name": stand}]) as resources,
            patch.object(cloud, "catalog") as catalog,
        ):
            lab = Lab(Path(path) / ".local")
            with self.assertRaisesRegex(RuntimeError, f"DZ08 stand already exists.*{stand}"):
                lab.deploy()
            resources.assert_called_once_with("yandex_vpc_network")
            catalog.assert_not_called()
            self.assertFalse(lab.context.exists())

    def test_new_context_prefers_local_access_policy_when_present(self):
        with TemporaryDirectory() as path:
            root = Path(path)
            config = root / "config"
            config.mkdir()
            (config / "lab.json").write_text(json.dumps(POLICY), encoding="utf-8")
            cloud = Cloud.__new__(Cloud)
            cloud.cloud, cloud.folder = "cloud", "folder"
            with patch("lab.ROOT", root), patch.object(
                cloud, "catalog", return_value=({"id": "image", "minDiskSize": 0}, ZONES, [])
            ), patch.object(cloud, "lab_names", return_value=[]), patch.object(cloud, "quota"), patch.object(
                cloud, "inventory", return_value=set()
            ), patch(
                "lab.choose", return_value={}
            ), patch("lab.admin_access", side_effect=list) as access:
                lab = Lab(root)
                self.assertEqual(lab.new_context(cloud)["admin_cidrs"], POLICY["admin_cidrs"])
                access.assert_called_once_with(POLICY["admin_cidrs"])
                cidrs = ["192.0.2.5/32"]
                (config / "lab.local.json").write_text(json.dumps({"admin_cidrs": cidrs}), encoding="utf-8")
                self.assertEqual(lab.new_context(cloud)["admin_cidrs"], cidrs)
                access.assert_called_with(cidrs)

    def test_replacement_plan_is_rejected_before_apply(self):
        with TemporaryDirectory() as path:
            tf = Terraform(Path(path))
            plan = {"resource_changes": [{
                "type": "yandex_compute_instance", "address": "instance",
                "change": {"actions": ["delete", "create"]},
            }]}
            with patch.object(tf, "call", side_effect=[None, json.dumps(plan)]) as call:
                with self.assertRaisesRegex(RuntimeError, "unexpected Terraform action"):
                    tf.plan_apply(token="profiletoken")
                self.assertEqual(call.call_count, 2)

    def test_existing_portal_security_group_accepts_only_ingress_updates(self):
        with TemporaryDirectory() as path:
            tf = Terraform(Path(path))
            before = {"name": "portal", "ingress": [{"port": 22}]}
            after = dict(before, ingress=[{"port": 22}, {"port": 8080}, {"port": 5601}])
            resource = {"type": "yandex_vpc_security_group", "address": "yandex_vpc_security_group.portal",
                        "change": {"actions": ["update"], "before": before, "after": after}}
            with patch.object(tf, "call", side_effect=[None, json.dumps({"resource_changes": [resource]}), None]) as call:
                tf.plan_apply(token="profiletoken")
                self.assertEqual(call.call_args.args[0], "apply")
            after["name"] = "unexpected"
            with patch.object(tf, "call", side_effect=[None, json.dumps({"resource_changes": [resource]})]) as call:
                with self.assertRaisesRegex(RuntimeError, "Nothing was applied"):
                    tf.plan_apply(token="profiletoken")
                self.assertEqual(call.call_count, 2)

    def test_destroy_rejects_resources_in_another_folder(self):
        with TemporaryDirectory() as path:
            tf = Terraform(Path(path))
            tf.state.write_text(json.dumps({"lineage": "one", "resources": [{
                "mode": "managed", "type": "yandex_vpc_network",
                "instances": [{"attributes": {
                    "name": "lab", "folder_id": "other", "id": "network",
                }}],
            }]}))
            with self.assertRaisesRegex(RuntimeError, "another YC folder"):
                tf.resources({"name": "lab", "folder": "mine", "lineage": "one"})

    def test_cleanup_rejects_a_directory_outside_the_clone(self):
        with TemporaryDirectory() as path:
            directory = Path(path) / ".local"
            directory.mkdir()
            sentinel = directory / "state"
            sentinel.touch()
            with self.assertRaisesRegex(RuntimeError, "outside this clone"):
                Lab(directory).clean()
            self.assertTrue(sentinel.exists())


class DestroyCleanupTest(TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.enterContext(patch("lab.ROOT", root))
        config = root / "config"
        config.mkdir()
        (config / "lab.json").write_text(json.dumps(POLICY), encoding="utf-8")
        self.local_policy = config / "lab.local.json"
        self.local_policy.touch()
        local = root / ".local"
        local.mkdir()
        self.lock = local / "operation.lock"
        self.lock.touch()
        self.lab = Lab(local)
        context = {"provider": "yc", "cloud": "cloud", "folder": "folder", "name": "lab"}
        self.lab.context.write_text(json.dumps(context), encoding="utf-8")
        self.lab.tf.state.write_text('{"resources": []}', encoding="utf-8")
        self.temp = root / ".tools/temp"
        nested = self.temp / "logs"
        nested.mkdir(parents=True)
        (nested / "trace.log").touch()
        (self.temp / "provider.log").touch()
        self.yc_config = self.temp.parent / "yc-config.yaml"
        self.yc_config.touch()
        self.tool = self.temp.parent / "bin/terraform.exe"
        self.tool.parent.mkdir()
        self.tool.touch()
        self.cloud = self.enterContext(patch("lab.Cloud")).return_value
        self.cloud.cloud, self.cloud.folder = "cloud", "folder"

    def test_confirmed_destroy_clears_temp_and_local_state(self):
        self.cloud.inventory.return_value = set()
        self.lab.destroy()
        self.assertEqual(list(self.temp.iterdir()), [])
        self.assertEqual(list(self.lab.local.iterdir()), [self.lock])
        self.assertTrue(self.tool.exists())
        self.assertTrue(self.yc_config.exists())
        self.assertTrue(self.local_policy.exists())

    def test_unconfirmed_destroy_preserves_temp_and_local_state(self):
        self.cloud.inventory.side_effect = [set()] + [{("disk", "disk")}] * 10
        with patch("lab.time.sleep"), self.assertRaisesRegex(RuntimeError, "Removal is incomplete"):
            self.lab.destroy()
        self.assertTrue((self.temp / "logs/trace.log").exists())
        self.assertTrue(self.yc_config.exists())
        self.assertTrue(self.lab.context.exists())
        self.assertTrue(self.lab.tf.state.exists())


class PublicAccessTest(TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.remote = Remote(Path(folder), {"wordpress_port": 8080, "kibana_port": 5601}, {
            "portal": {"public": "192.0.2.5", "zone": "ru-central1-a"},
            "logs": {"private": "10.84.9.5", "zone": "ru-central1-a"},
        })

    def test_open_prints_public_urls_without_starting_ssh(self):
        self.remote.kibana_certificate.write_text(
            "-----BEGIN CERTIFICATE-----\nY2VydA==\n-----END CERTIFICATE-----\n", encoding="ascii"
        )
        with redirect_stdout(StringIO()) as output, patch("remote.run") as run:
            self.remote.open()
        self.assertIn("http://192.0.2.5:8080/", output.getvalue())
        self.assertIn("https://192.0.2.5:5601/", output.getvalue())
        self.assertIn("SHA-256:", output.getvalue())
        run.assert_not_called()

    def test_verify_preserves_the_two_log_checks_and_uses_the_public_wordpress_host(self):
        with patch("remote.HTTPConnection") as connect, patch.object(self.remote, "command") as command:
            response = connect.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'class="lesson-card"' * 8
            self.remote.verify()
        connect.assert_called_once_with("192.0.2.5", 8080, timeout=20)
        commands = [call.args[1] for call in command.call_args_list]
        self.assertTrue(any("--resolve '192.0.2.5:8080:127.0.0.1'" in item for item in commands))
        for phase in ("kafka", "elastic"):
            self.assertTrue(any(f"/verify {phase} " in item for item in commands))

    def test_public_wordpress_redirect_fails_verification(self):
        with patch("remote.HTTPConnection", return_value=Mock()) as connect:
            connect.return_value.getresponse.return_value.status = 301
            with self.assertRaisesRegex(RuntimeError, "HTTP 301"):
                self.remote.verify_portal()
