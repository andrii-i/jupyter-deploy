variable "bucket_name_prefix" {
  type = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]*$", var.bucket_name_prefix))
    error_message = "bucket_name_prefix must be lowercase alphanumeric with hyphens, cannot start with a hyphen."
  }

  validation {
    condition     = length(var.bucket_name_prefix) >= 3 && length(var.bucket_name_prefix) <= 36
    error_message = "bucket_name_prefix must be between 3 and 36 characters."
  }
}

variable "combined_tags" {
  type = map(string)
}
