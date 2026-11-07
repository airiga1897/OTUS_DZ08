from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import json
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lab import Lab
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
    def test_cloud_reads_the_active_profile(self):
        with patch("yc.which", return_value="yc"), patch("yc.subprocess.run") as run:
            run.side_effect = [subprocess.CompletedProcess([], 0, value)
                               for value in ("cloud\n", "folder\n", "profiletoken\n")]
            with patch.dict(os.environ, {"YC_TOKEN": "oldtoken", "YC_FOLDER_ID": "oldfolder"}):
                cloud = Cloud()
                self.assertEqual(os.environ["YC_TOKEN"], "oldtoken")
            self.assertEqual((cloud.cloud, cloud.folder, cloud.token), ("cloud", "folder", "profiletoken"))
            self.assertEqual([call.args[0] for call in run.call_args_list], [
                ["yc", "config", "get", "cloud-id"],
                ["yc", "config", "get", "folder-id"],
                ["yc", "iam", "create-token", "--no-browser"],
            ])

    def test_missing_profile_stops_before_cloud_or_terraform_calls(self):
        with TemporaryDirectory() as path, patch("yc.which", return_value="yc"), patch(
            "yc.subprocess.run", side_effect=subprocess.CalledProcessError(1, ["yc"])
        ) as run, patch.object(Cloud, "confirm_folder") as api, patch.object(Terraform, "init") as init:
            lab = Lab(Path(path))
            with self.assertRaisesRegex(RuntimeError, "yc init --no-browser"):
                lab.deploy()
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
            ), patch.object(cloud, "quota"), patch.object(cloud, "inventory", return_value=set()), patch(
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
