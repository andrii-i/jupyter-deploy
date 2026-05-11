variable "role_name" {
  type = string
}

variable "assume_role_policy" {
  type = string
}

variable "policy_arns" {
  type    = list(string)
  default = []
}

variable "combined_tags" {
  type = map(string)
}
