variable "name" { type = string }
variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "tags" { type = map(string) }
variable "subnet_id" { type = string }
variable "node_nic_ids" { type = map(string) }
variable "redis_nic_id" { type = string }
variable "public_http_enabled" { type = bool }
