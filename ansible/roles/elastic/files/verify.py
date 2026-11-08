#!/usr/bin/python3
"""Check the lab without copying credentials or raw events off the VM."""
from base64 import b64encode
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import json
import os
import re
import ssl
import subprocess
import sys
import time

SETTINGS = json.loads(Path("/etc/otus-verify.json").read_text())
TOPICS = ("nginx", "wordpress")
ES = "http://127.0.0.1:9200"
KIBANA = SETTINGS["kibana"]
KIBANA_TLS = ssl.create_default_context(cafile=SETTINGS["kibana_certificate"])
AUTH = b64encode(f'elastic:{SETTINGS["password"]}'.encode()).decode()


def api(base, path, body=None):
    payload = json.dumps(body).encode() if body is not None else None
    request = Request(base + path, payload, {
        "Authorization": f"Basic {AUTH}", "Content-Type": "application/json", "kbn-xsrf": "true",
    })
    with urlopen(request, timeout=12, context=KIBANA_TLS if base == KIBANA else None) as response:
        return json.load(response)


def wait(callback, title, seconds=150):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if callback():
                return
        except HTTPError as error:
            if error.code not in (404, 429, 502, 503, 504):
                raise
        except (URLError, TimeoutError):
            pass
        time.sleep(4)
    raise RuntimeError(f"Timed out waiting for {title}.")


def kafka(tool, *args):
    return subprocess.run(
        (f'{SETTINGS["kafka_home"]}/bin/kafka-{tool}.sh', "--bootstrap-server",
         SETTINGS["bootstrap"], *args),
        env=dict(os.environ, KAFKA_HEAP_OPTS="-Xms128m -Xmx256m"),
        check=True, capture_output=True, text=True, encoding="utf-8", timeout=45,
    ).stdout


def view(topic, create=False):
    path = f"/api/data_views/data_view/otus-{topic}"
    try:
        result = api(KIBANA, path)
    except HTTPError as error:
        if error.code != 404 or not create:
            raise
        result = api(KIBANA, "/api/data_views/data_view", {
            "data_view": {"id": f"otus-{topic}", "title": f"logs-{topic}",
                          "name": f"ДЗ08 {topic}", "timeFieldName": "@timestamp"},
        })
    data = result["data_view"]
    return data["title"] == f"logs-{topic}" and data["timeFieldName"] == "@timestamp"


def setup():
    wait(lambda: api(KIBANA, "/api/status")["status"]["overall"]["level"] == "available",
         "Kibana", 300)
    if not all(view(topic, create=True) for topic in TOPICS):
        raise RuntimeError("Kibana data views have unexpected parameters.")


def baseline():
    for topic in TOPICS:
        description = kafka("topics", "--describe", "--topic", topic)
        if "PartitionCount: 2" not in description or "ReplicationFactor: 2" not in description:
            raise RuntimeError(f"{topic}: expected two partitions and replication factor 2.")
        partitions = [line for line in description.splitlines() if "\tPartition:" in line]
        if len(partitions) != 2 or any(
            len(set(line.split("Isr:", 1)[1].strip().split()[0].split(","))) != 2
            for line in partitions
        ):
            raise RuntimeError(f"{topic}: both replicas must be in sync.")
        if not view(topic):
            raise RuntimeError(f"{topic}: unexpected Kibana data view.")
        print(f"{topic}: two partitions, two in-sync replicas, data view ready.")


def offsets(path):
    result = {}
    for topic in TOPICS:
        lines = kafka("get-offsets", "--topic", topic, "--time", "-1").splitlines()
        result[topic] = {part: int(offset) for _, part, offset in
                         (line.split(":") for line in lines)}
        if set(result[topic]) != {"0", "1"}:
            raise RuntimeError(f"Cannot determine the two starting offsets for {topic}.")
    path.write_text(json.dumps(result), encoding="utf-8")


def partition_contains(topic, part, offset, marker):
    result = subprocess.run(
        (f'{SETTINGS["kafka_home"]}/bin/kafka-console-consumer.sh', "--bootstrap-server",
         SETTINGS["bootstrap"], "--topic", topic, "--partition", part, "--offset", str(offset),
         "--timeout-ms", "3500", "--consumer-property", "enable.auto.commit=false"),
        env=dict(os.environ, KAFKA_HEAP_OPTS="-Xms128m -Xmx256m"),
        capture_output=True, text=True, encoding="utf-8", timeout=20, check=False,
    )
    for line in result.stdout.splitlines():
        try:
            if json.loads(line).get("otus", {}).get("marker") == marker:
                return True
        except json.JSONDecodeError:
            continue
    return False


def queued(topic, marker, path):
    starts = json.loads(path.read_text(encoding="utf-8"))[topic]
    return any(partition_contains(topic, part, offset, marker) for part, offset in starts.items())


def indexed(topic, marker):
    query = {"query": {"term": {"otus.marker.keyword": marker}}}
    return api(ES, f"/logs-{topic}/_count", query)["count"] > 0


def main():
    operation = sys.argv[1]
    if operation in ("setup", "baseline"):
        globals()[operation]()
        return
    if operation not in ("offsets", "kafka", "elastic") or len(sys.argv) != 3:
        raise ValueError("Unknown verification operation.")
    marker = sys.argv[2]
    if not re.fullmatch("[a-f0-9]{32}", marker):
        raise ValueError("Invalid verification marker.")
    path = Path(f"/run/otus-dz08-{marker}.json")
    if operation == "offsets":
        offsets(path)
        return
    for topic in TOPICS:
        if operation == "kafka":
            wait(lambda: queued(topic, marker, path), f"{operation}/{topic}")
        else:
            wait(lambda: indexed(topic, marker), f"{operation}/{topic}")
        print(f"{operation}/{topic}: marked event found.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from error
