mock_provider "yandex" {
  mock_resource "yandex_vpc_route_table" {
    defaults = { id = "mock-nat-route" }
  }
}

variables {
  ip_probe_zip   = "../functions/ip_probe.py"
  cloud_id       = "mock-cloud"
  folder_id      = "mock-folder"
  name           = "otus-dz08-test"
  platform       = "standard-v3"
  image_id       = "mock-image"
  disk_type      = "network-hdd"
  admin_cidrs    = ["192.0.2.5/32", "192.0.2.6/32"]
  portal_key     = "mock-public-key"
  logs_keys      = "mock-public-key"
  kibana_port    = 5601
  wordpress_port = 8080
  nodes = {
    portal = { zone = "ru-central1-a", cidr = "10.84.8.0/24", cores = 2, memory = 4, disk = 20 }
    logs   = { zone = "ru-central1-a", cidr = "10.84.9.0/24", cores = 2, memory = 12, disk = 20 }
  }
}

run "two_nodes_one_zone" {
  command = apply

  assert {
    condition     = length(yandex_compute_instance.lab) == 2 && length(yandex_vpc_subnet.lab) == 2
    error_message = "There must be two VMs and two independent subnets in the base lab."
  }

  assert {
    condition = (
      toset([for rule in yandex_vpc_security_group.portal.ingress : rule.port]) ==
      toset([22, var.wordpress_port, var.kibana_port]) &&
      alltrue([
        for rule in yandex_vpc_security_group.portal.ingress :
        rule.protocol == "TCP" && toset(rule.v4_cidr_blocks) == toset(var.admin_cidrs)
      ])
    )
    error_message = "Portal SSH, WordPress and Kibana must be limited to the same administrative addresses."
  }

  assert {
    condition = (
      yandex_compute_instance.lab["portal"].network_interface[0].nat &&
      !yandex_compute_instance.lab["logs"].network_interface[0].nat &&
      yandex_vpc_subnet.lab["portal"].route_table_id != yandex_vpc_route_table.lab.id &&
      yandex_vpc_subnet.lab["logs"].route_table_id == yandex_vpc_route_table.lab.id
    )
    error_message = "Only logs uses the gateway route and only the portal has public NAT."
  }
}

run "two_nodes_two_zones" {
  command = plan

  variables {
    nodes = {
      portal = { zone = "ru-central1-a", cidr = "10.84.8.0/24", cores = 2, memory = 4, disk = 20 }
      logs   = { zone = "ru-central1-b", cidr = "10.84.9.0/24", cores = 2, memory = 12, disk = 20 }
    }
  }

  assert {
    condition = (
      yandex_compute_instance.lab["portal"].zone == yandex_vpc_subnet.lab["portal"].zone &&
      yandex_compute_instance.lab["logs"].zone == yandex_vpc_subnet.lab["logs"].zone &&
      yandex_vpc_subnet.lab["portal"].network_id == yandex_vpc_subnet.lab["logs"].network_id
    )
    error_message = "Each node needs a subnet in its zone and both subnets must use one VPC."
  }
}

run "probe_before_admin_address" {
  command = plan
  plan_options {
    target = [yandex_function.ip_probe]
  }
  variables {
    admin_cidrs = []
  }
  assert {
    condition     = yandex_function.ip_probe.runtime == "python312" && yandex_function.ip_probe.entrypoint == "index.handler"
    error_message = "The probe must be deployable before observing the operator address."
  }
}

run "reject_full_plan_without_admin_address" {
  command = plan
  variables {
    admin_cidrs = []
  }
  expect_failures = [yandex_vpc_security_group.portal]
}
