variable "region" {
  description = "The AWS region where to deploy the resources."
  type        = string
}

variable "combined_tags" {
  description = "The full set of tags to add to resources."
  type        = map(string)
}

variable "postfix" {
  description = "The deployment-specific postfix to add to resource names."
  type        = string
}

variable "ami_id" {
  description = "The Amazon machine image ID for the EC2 instance."
  type        = string
}

variable "instance_type" {
  description = "The instance type of the EC2 instance."
  type        = string
}

variable "vpc_id" {
  description = "ID of the VPC the subnets belong to, used to resolve availability_zone to a subnet."
  type        = string
}

variable "subnet_ids" {
  description = "Candidate subnet IDs to place the instance in, one per availability zone."
  type        = list(string)
}

variable "availability_zone" {
  description = <<-EOT
    Availability zone to place the instance in. "any" selects the first of var.subnet_ids.

    Placement is resolved here rather than by the caller because it is not free: an instance type
    is not offered in every zone, and the EBS volumes must land in the same zone as the instance.
  EOT
  type        = string
}

variable "security_group_id" {
  description = "The ID of the security group to use to control network traffic to/from the EC2 instance."
  type        = string
}

variable "min_root_volume_size_gb" {
  description = "The minimum size in gigabytes of the root EBS volume. Actual size is max(this, max(round(AMI_size × 1.33), AMI_size + 10))."
  type        = number
  nullable    = true
}

variable "instance_profile_name" {
  description = "Name of the instance profile to assign to the EC2 instance."
  type        = string
}
