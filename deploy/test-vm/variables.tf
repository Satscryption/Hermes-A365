variable "name_prefix" {
  type        = string
  description = "Lowercase hyphenated prefix for resource names (3-20 characters)."
  validation {
    condition     = can(regex("^[a-z0-9-]{3,20}$", var.name_prefix))
    error_message = "name_prefix must match ^[a-z0-9-]{3,20}$."
  }
}
variable "location" {
  type        = string
  description = "Azure region for all resources."
  default     = "centralindia"
}
variable "tags" {
  type        = map(string)
  description = "Tags applied to every taggable resource."
  default = {
    purpose  = "example-walk"
    owner    = "example-team"
    deadline = "example-date"
  }
}
variable "address_space" {
  type        = string
  description = "Virtual network address space CIDR."
  default     = "10.103.0.0/24"
}
variable "subnet_prefix" {
  type        = string
  description = "Gateway subnet CIDR within the VNet."
  default     = "10.103.0.0/27"
}
variable "dns_label" {
  type        = string
  description = "DNS label for the public IP FQDN."
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,61}[a-z0-9]$", var.dns_label))
    error_message = "dns_label must match ^[a-z][a-z0-9-]{1,61}[a-z0-9]$."
  }
}