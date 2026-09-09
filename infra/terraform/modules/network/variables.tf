variable "name" { type = string }
variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "tags" { type = map(string) }
variable "public_http_enabled" { type = bool }
variable "allowed_http_cidrs" { type = list(string) }
