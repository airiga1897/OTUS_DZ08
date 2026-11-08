#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
sudo cloud-init status --wait
sudo apt-get -o DPkg::Lock::Timeout=120 update -qq
sudo apt-get -o DPkg::Lock::Timeout=120 install -y ansible-core
export ANSIBLE_HOST_KEY_CHECKING=True
export ANSIBLE_SSH_ARGS="-o StrictHostKeyChecking=accept-new"
cat > inventory.py <<'PY'
#!/usr/bin/env python3
import json
from pathlib import Path
settings = json.loads(Path("settings.json").read_text())
print(json.dumps({"_meta": {"hostvars": {
    "portal": {"ansible_connection": "local"},
    "logs": {"ansible_host": settings["logs_address"], "ansible_user": "ubuntu",
             "ansible_ssh_private_key_file": str(Path("ansible-key").resolve())},
}}, "all": {"hosts": ["portal", "logs"]}}))
PY
chmod 700 inventory.py
ansible-playbook -i ./inventory.py ansible/site.yml -e @stack.yml -e @settings.json "$@"
