variable "secret_prefix" {
  type = string
}

variable "secret_value" {
  type      = string
  sensitive = true
}

variable "region" {
  type = string
}

variable "combined_tags" {
  type = map(string)
}
