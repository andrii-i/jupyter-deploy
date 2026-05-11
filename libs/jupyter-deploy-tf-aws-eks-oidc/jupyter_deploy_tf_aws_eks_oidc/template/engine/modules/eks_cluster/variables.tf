variable "cluster_name" {
  type = string
}

variable "kubernetes_version" {
  type = string
}

variable "cluster_role_arn" {
  type = string
}

variable "cluster_log_retention_days" {
  type = number
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "combined_tags" {
  type = map(string)
}
