terraform {
  required_version = ">= 1.6, < 2.0"
  backend "local" {}

  required_providers {
    yandex = {
      source  = "yandex-cloud/yandex"
      version = "0.244.0"
    }
  }
}

provider "yandex" {
  cloud_id  = var.cloud_id
  folder_id = var.folder_id
}

resource "yandex_vpc_network" "lab" {
  name = var.name
}

resource "yandex_vpc_gateway" "lab" {
  name = "${var.name}-nat"
  shared_egress_gateway {}
}

resource "yandex_vpc_route_table" "lab" {
  name       = "${var.name}-egress"
  network_id = yandex_vpc_network.lab.id

  static_route {
    destination_prefix = "0.0.0.0/0"
    gateway_id         = yandex_vpc_gateway.lab.id
  }
}

resource "yandex_vpc_subnet" "lab" {
  for_each       = var.nodes
  name           = "${var.name}-${each.key}"
  zone           = each.value.zone
  network_id     = yandex_vpc_network.lab.id
  route_table_id = each.key == "logs" ? yandex_vpc_route_table.lab.id : null
  v4_cidr_blocks = [each.value.cidr]
}

resource "yandex_vpc_security_group" "portal" {
  name       = "${var.name}-portal"
  network_id = yandex_vpc_network.lab.id

  ingress {
    protocol       = "TCP"
    port           = 22
    v4_cidr_blocks = var.admin_cidrs
  }

  ingress {
    protocol       = "TCP"
    port           = var.wordpress_port
    v4_cidr_blocks = var.admin_cidrs
  }

  ingress {
    protocol       = "TCP"
    port           = var.kibana_port
    v4_cidr_blocks = var.admin_cidrs
  }

  egress {
    protocol       = "ANY"
    v4_cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "yandex_vpc_security_group" "logs" {
  name       = "${var.name}-logs"
  network_id = yandex_vpc_network.lab.id

  ingress {
    protocol          = "TCP"
    port              = 22
    security_group_id = yandex_vpc_security_group.portal.id
  }

  ingress {
    protocol          = "TCP"
    from_port         = 9092
    to_port           = 9093
    security_group_id = yandex_vpc_security_group.portal.id
  }

  ingress {
    protocol          = "TCP"
    port              = var.kibana_port
    security_group_id = yandex_vpc_security_group.portal.id
  }

  egress {
    protocol       = "ANY"
    v4_cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "yandex_compute_instance" "lab" {
  for_each    = var.nodes
  name        = "${var.name}-${each.key}"
  zone        = each.value.zone
  platform_id = var.platform
  labels      = { otus_lab = var.name }

  resources {
    cores         = each.value.cores
    memory        = each.value.memory
    core_fraction = 100
  }

  boot_disk {
    initialize_params {
      name     = "${var.name}-${each.key}"
      size     = each.value.disk
      type     = var.disk_type
      image_id = var.image_id
    }
    auto_delete = true
  }

  network_interface {
    subnet_id = yandex_vpc_subnet.lab[each.key].id
    nat       = each.key == "portal"
    security_group_ids = [
      each.key == "portal" ? yandex_vpc_security_group.portal.id : yandex_vpc_security_group.logs.id
    ]
  }

  metadata = {
    ssh-keys = "ubuntu:${each.key == "logs" ? var.logs_keys : var.portal_key}"
  }
}
