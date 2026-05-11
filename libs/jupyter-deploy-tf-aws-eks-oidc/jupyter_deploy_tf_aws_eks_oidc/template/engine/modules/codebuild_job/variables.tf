variable "project_name" {
  type = string
}

variable "ecr_repository_url" {
  type = string
}

variable "ecr_repository_arn" {
  type = string
}

variable "image_tag" {
  type = string
}

variable "buildspec" {
  type = string
}

variable "source_s3_location" {
  type = string
}

variable "source_s3_bucket_arn" {
  type = string
}

variable "combined_tags" {
  type = map(string)
}
