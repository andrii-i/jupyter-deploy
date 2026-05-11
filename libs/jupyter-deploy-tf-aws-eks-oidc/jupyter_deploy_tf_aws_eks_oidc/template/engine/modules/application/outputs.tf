output "image_uri" {
  value = local.image_uri
}

output "repository_url" {
  value = module.ecr.repository_url
}
