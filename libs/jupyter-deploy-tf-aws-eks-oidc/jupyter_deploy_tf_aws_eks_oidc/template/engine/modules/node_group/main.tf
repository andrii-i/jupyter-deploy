data "aws_ec2_instance_type" "this" {
  instance_type = var.instance_type
}

locals {
  has_gpu    = try(length(data.aws_ec2_instance_type.this.gpus) > 0, false)
  has_neuron = try(length(data.aws_ec2_instance_type.this.neuron_devices) > 0, false)

  supported_architectures = try(data.aws_ec2_instance_type.this.supported_architectures, ["x86_64"])
  architecture            = contains(local.supported_architectures, "x86_64") ? "x86_64" : "arm64"

  # Map instance capabilities to EKS AL2023 ami_type values
  resolved_ami_type = (
    local.has_gpu && local.architecture == "x86_64" ? "AL2023_x86_64_NVIDIA" :
    local.has_neuron ? "AL2023_x86_64_NEURON" :
    local.architecture == "arm64" ? "AL2023_ARM_64_STANDARD" :
    "AL2023_x86_64_STANDARD"
  )

  ami_type = var.ami_type == "default" ? local.resolved_ami_type : var.ami_type
}

resource "aws_eks_node_group" "this" {
  cluster_name    = var.cluster_name
  node_group_name = var.node_group_name
  node_role_arn   = var.node_role_arn
  subnet_ids      = var.subnet_ids
  ami_type        = local.ami_type

  instance_types = [var.instance_type]
  disk_size      = var.disk_size_gb

  labels = {
    "jupyter-deploy/role" = var.role_label
  }

  scaling_config {
    min_size     = var.min_size
    max_size     = var.max_size
    desired_size = var.desired_size
  }

  tags = var.combined_tags
}
