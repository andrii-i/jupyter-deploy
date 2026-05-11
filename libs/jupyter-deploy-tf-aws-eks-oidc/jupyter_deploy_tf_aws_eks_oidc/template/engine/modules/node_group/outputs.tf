output "node_group_name" {
  value = aws_eks_node_group.this.node_group_name
}

output "ami_type" {
  value = local.ami_type
}

output "instance_category" {
  description = "Instance category: cpu, gpu, or neuron"
  value       = local.has_neuron ? "neuron" : (local.has_gpu ? "gpu" : "cpu")
}
