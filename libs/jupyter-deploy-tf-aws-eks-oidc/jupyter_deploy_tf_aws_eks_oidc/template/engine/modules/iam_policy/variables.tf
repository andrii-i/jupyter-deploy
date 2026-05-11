variable "policy_name" {
  type = string
}

variable "statements" {
  type = list(object({
    actions   = list(string)
    resources = list(string)
  }))
}

variable "combined_tags" {
  type = map(string)
}
