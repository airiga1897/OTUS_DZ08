#!/usr/bin/python3
"""Initialize the local Elasticsearch credentials and exact log indices."""
from base64 import b64encode
from json import dumps
from os import environ
from urllib.error import HTTPError
from urllib.request import Request, urlopen

password = environ["ES_PASSWORD"]
auth = b64encode(f"elastic:{password}".encode()).decode()


def request(method, path, body=None):
    data = dumps(body).encode() if body is not None else None
    req = Request(f"http://127.0.0.1:9200{path}", data, {
        "Authorization": f"Basic {auth}", "Content-Type": "application/json",
    }, method=method)
    with urlopen(req, timeout=15) as response:
        return response.read()


request("POST", "/_security/user/elastic/_password", {"password": password})
request("POST", "/_security/user/kibana_system/_password",
        {"password": environ["KIBANA_PASSWORD"]})
request("PUT", "/_security/role/otus_ingest", {
    "cluster": ["monitor"],
    "indices": [{"names": ["logs-nginx", "logs-wordpress"], "privileges": ["write"]}],
})
request("PUT", "/_security/user/otus_logstash",
        {"password": environ["LOGSTASH_PASSWORD"], "roles": ["otus_ingest"]})
for index in ("logs-nginx", "logs-wordpress"):
    try:
        request("HEAD", f"/{index}")
    except HTTPError as error:
        if error.code != 404:
            raise
        request("PUT", f"/{index}", {"settings": {"number_of_replicas": 0},
                                     "mappings": {"properties": {"@timestamp": {"type": "date"}}}})
        print(f"updated {index}")
