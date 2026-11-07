mock_provider "yandex" {
  mock_resource "yandex_vpc_route_table" {
    defaults = { id = "mock-nat-route" }
  }
}

variables {
  cloud_id    = "mock-cloud"
  folder_id   = "mock-folder"
  name        = "otus-dz08-test"
  platform    = "standard-v3"
  image_id    = "mock-image"
  disk_type   = "network-hdd"
  admin_cidrs = ["192.0.2.5/32"]
  portal_key  = "mock-public-key"
  logs_keys   = "mock-public-key"
  kibana_port = 5601
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
