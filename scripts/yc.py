"""Read-only Yandex Cloud API and deterministic placement."""
from collections import defaultdict
from ipaddress import IPv4Network, ip_network
from itertools import product
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

GIB = 1024**3
COMPUTE = "https://compute.api.cloud.yandex.net/compute/v1"
VPC = "https://vpc.api.cloud.yandex.net/vpc/v1"
QUOTA = "https://quota-manager.api.cloud.yandex.net/quota-manager/v1"
IAM_RESOURCES = "https://resource-manager.api.cloud.yandex.net/resource-manager/v1"
TYPES = {
    "yandex_compute_instance": (COMPUTE, "instances"),
    "disk": (COMPUTE, "disks"),
    "yandex_vpc_network": (VPC, "networks"),
    "yandex_vpc_subnet": (VPC, "subnets"),
    "yandex_vpc_route_table": (VPC, "routeTables"),
    "yandex_vpc_gateway": (VPC, "gateways"),
    "yandex_vpc_security_group": (VPC, "securityGroups"),
}


class Cloud:
    def __init__(self):
        self.cloud = os.environ.get("YC_CLOUD_ID")
        self.folder = os.environ.get("YC_FOLDER_ID")
        self.token = os.environ.get("YC_TOKEN")
        if not all((self.cloud, self.folder, self.token)):
            raise ValueError("Set YC_CLOUD_ID, YC_FOLDER_ID and YC_TOKEN using the yc commands in README.md.")

    def get(self, base, path, *, missing_ok=False, **query):
        url = f"{base}/{path}?{urlencode(query)}"
        request = Request(url, headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urlopen(request, timeout=20) as response:
                return json.load(response)
        except HTTPError as error:
            if missing_ok and error.code == 404:
                return None
            raise RuntimeError(f"YC API {path}: HTTP {error.code}") from error
        except (URLError, TimeoutError) as error:
            raise RuntimeError(f"YC API {path}: connection failed") from error

    def pages(self, base, path, key, **query):
        while True:
            page = self.get(base, path, pageSize=100, **query)
            if not isinstance(page, dict) or set(page) - {key, "nextPageToken", "resource"}:
                raise RuntimeError(f"YC API returned an unexpected list response for {path}.")
            yield from page.get(key, [])
            token = page.get("nextPageToken")
            if not token:
                return
            query["pageToken"] = token

    def resources(self, kind):
        base, path = TYPES[kind]
        return self.pages(base, path, path, folderId=self.folder)

    def inventory(self, prefix):
        return {
            (kind, item["id"])
            for kind in TYPES
            for item in self.resources(kind)
            if item["name"] == prefix or item["name"].startswith(prefix + "-")
        }

    def confirm_folder(self):
        folder = self.get(IAM_RESOURCES, f"folders/{self.folder}")
        if folder["cloudId"] != self.cloud or folder["status"] != "ACTIVE":
            raise RuntimeError("The YC folder is unavailable or belongs to another cloud.")

    def confirm_ownership(self, prefix, owned, found):
        for kind, identifier in owned - found:
            base, path = TYPES[kind]
            item = self.get(base, f"{path}/{identifier}", missing_ok=True)
            if item is not None:
                raise RuntimeError(f"YC resource {kind}/{identifier} no longer matches {prefix}.")

    def quota(self):
        result = {}
        for service in ("compute", "vpc"):
            for quota in self.pages(
                QUOTA, "quotaLimits", "quotaLimits", service=service,
                **{"resource.id": self.cloud, "resource.type": "resource-manager.cloud"},
            ):
                result[quota["quotaId"]] = int(quota["limit"]) - int(quota.get("usage", 0))
        return result

    def catalog(self, policy):
        check_platform(policy)
        image = self.get(COMPUTE, "images:latestByFamily", folderId="standard-images",
                         family=policy["image_family"])
        if image.get("status") != "READY" or image.get("architecture") not in (None, "X86_64"):
            raise RuntimeError("The selected Ubuntu x86-64 image is unavailable.")
        disk = self.get(COMPUTE, f'diskTypes/{policy["disk_type"]}')
        zones = {zone["id"] for zone in self.pages(COMPUTE, "zones", "zones")
                 if zone["status"] == "UP"}
        used = [cidr for subnet in self.resources("yandex_vpc_subnet")
                for cidr in subnet["v4CidrBlocks"]]
        disk_zones = disk.get("zoneIds")
        if disk.get("id") != policy["disk_type"]:
            raise RuntimeError("YC returned an unexpected disk type.")
        if disk_zones is None and disk["id"] in {"network-hdd", "network-ssd"}:
            # Standard network disks may omit zoneIds. Quota Manager still
            # verifies availability of the requested disk size per zone.
            disk_zones = zones
        if not isinstance(disk_zones, (list, set)):
            raise RuntimeError("YC did not confirm the disk type's placement.")
        return image, zones & set(disk_zones), used


def choose(policy, available_zones, used, free):
    occupied = [ip_network(cidr) for cidr in used]
    networks = [ip_network(cidr) for cidr in policy["subnets"]]
    if (not all(isinstance(cidr, IPv4Network) and cidr.prefixlen == 24 for cidr in networks)
            or len(set(networks)) != len(networks)):
        raise ValueError("Placement requires distinct IPv4 /24 networks.")
    networks = [cidr for cidr in networks if not any(cidr.overlaps(other) for other in occupied)]
    candidates = sorted(product(policy["zones"], repeat=2), key=lambda pair: pair[0] != pair[1])
    failures = []
    for portal, logs in candidates:
        label = f"{portal}/{logs}"
        if not {portal, logs} <= available_zones:
            failures.append(f"{label}: zone or disk type unavailable")
            continue
        if len(networks) < 2:
            failures.append(f"{label}: insufficient non-overlapping subnets")
            continue
        needs = requirements(policy, portal, logs)
        missing = [key for key, count in needs.items() if key not in free or free[key] < count]
        if missing:
            failures.append(f'{label}: missing or insufficient quotas {", ".join(missing)}')
            continue
        return {name: dict(policy["nodes"][name], zone=zone, cidr=str(cidr))
                for (name, zone), cidr in zip((("portal", portal), ("logs", logs)), networks)}
    raise RuntimeError("No compatible YC placement:\n" + "\n".join(failures))


def requirements(policy, portal, logs):
    needs = defaultdict(int, {
        "compute.instances.count": 2, "compute.disks.count": 2,
        "vpc.networks.count": 1, "vpc.subnets.count": 2,
        "vpc.routeTables.count": 1, "vpc.securityGroups.count": 3,
        "vpc.externalAddresses.count": 1, "vpc.staticRoutes.count": 1,
        "vpc.gateways.count": 1, "vpc.sharedEgressGateways.count": 1,
    })
    disk_quota = {"network-hdd": "hddDisks", "network-ssd": "ssdDisks"}[policy["disk_type"]]
    for name, zone in (("portal", portal), ("logs", logs)):
        node = policy["nodes"][name]
        needs[f"compute.instanceCores.count.{zone}"] += node["cores"]
        needs[f"compute.instanceMemory.size.{zone}"] += node["memory"] * GIB
        needs[f"compute.{disk_quota}.size.{zone}"] += node["disk"] * GIB
    return {key: count for key, count in needs.items() if count}


def check_platform(policy):
    if policy["platform"] != "standard-v3":
        raise ValueError("This lab supports the YC standard-v3 platform.")
    for name, node in policy["nodes"].items():
        cores = node["cores"]
        if (cores not in {2, 4, 6, 8, 10, 12, 14, 16, 20, 24, 28, 32} or
                node["memory"] % cores != 0 or not 1 <= node["memory"] / cores <= 16):
            raise ValueError(f"{name}: unsupported vCPU/RAM pair for the configured platform.")
