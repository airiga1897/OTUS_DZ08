variable "cloud_id" {
  type = string
}

variable "folder_id" {
  type = string
}

variable "name" {
  type = string
}

variable "platform" {
  type = string
}

variable "image_id" {
  type = string
}

variable "disk_type" {
  type = string
}

variable "kibana_port" {
  type = number
}

variable "admin_cidrs" {
  type = list(string)
}

variable "portal_key" {
  type = string
}

variable "logs_keys" {
  type = string
}

variable "nodes" {
  type = map(object({
    zone   = string
    cidr   = string
    cores  = number
    memory = number
    disk   = number
  }))
}

output "nodes" {
  value = {
    for name, node in yandex_compute_instance.lab : name => {
      id      = node.id
      zone    = node.zone
      private = node.network_interface[0].ip_address
      public  = node.network_interface[0].nat_ip_address
    }
  }
}
