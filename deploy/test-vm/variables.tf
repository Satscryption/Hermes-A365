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
variable "vm_size" {
  type        = string
  description = "Azure VM size."
  default     = "Standard_B2s_v2"
}
variable "admin_username" {
  type        = string
  description = "Admin username for SSH access."
  default     = "azureops"
}
variable "admin_ssh_public_key" {
  type        = string
  description = "SSH public key for the admin user (no private key material)."
}
variable "image_version" {
  type        = string
  description = "Canonical Ubuntu 24.04 LTS image version (not latest)."
  default     = "24.04.202611170"
  validation {
    condition     = var.image_version != "latest"
    error_message = "image_version must be an explicit version, not latest."
  }
}
variable "shutdown_time_utc" {
  type        = string
  description = "Daily VM shutdown time in UTC as HHMM."
  default     = "1900"
  validation {
    condition     = can(regex("^([01][0-9]|2[0-3])[0-5][0-9]$", var.shutdown_time_utc))
    error_message = "shutdown_time_utc must be a valid UTC time in HHMM form."
  }
}
variable "hermes_a365_ref" {
  type        = string
  description = "Git commit (40-char lowercase hex) for Hermes-A365."
  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.hermes_a365_ref))
    error_message = "hermes_a365_ref must be a 40-char lowercase hex commit."
  }
}
variable "hermes_agent_ref" {
  type        = string
  description = "Git commit (40-char lowercase hex) for hermes-agent."
  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.hermes_agent_ref))
    error_message = "hermes_agent_ref must be a 40-char lowercase hex commit."
  }
}
variable "uv_version" {
  type        = string
  description = "uv release to install on the VM."
  default     = "0.13.9"
}
