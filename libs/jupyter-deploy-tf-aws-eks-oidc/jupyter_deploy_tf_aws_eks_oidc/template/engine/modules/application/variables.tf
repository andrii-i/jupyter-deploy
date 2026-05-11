variable "name" {
  type = string
}

variable "resource_name_prefix" {
  type = string
}

variable "source_dir" {
  type = string
}

variable "image_name" {
  type = string
}

variable "image_build" {
  type = string
}

variable "build_bucket_name" {
  type = string
}

variable "build_bucket_arn" {
  type = string
}

variable "region" {
  type = string
}

variable "combined_tags" {
  type = map(string)
}
