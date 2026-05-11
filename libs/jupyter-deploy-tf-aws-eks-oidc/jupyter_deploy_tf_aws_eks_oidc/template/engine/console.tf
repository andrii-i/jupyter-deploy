locals {
  console_name      = "console"
  console_namespace = var.workspace_router_namespace
  console_port      = 8080
  console_path      = "/get-started/"

  console_html = templatefile("${path.module}/../console/setup-config.html.tftpl", {})

  console_script = templatefile("${path.module}/../console/set-kubeconfig.sh.tftpl", {
    cluster_name     = module.eks_cluster.cluster_name
    cluster_endpoint = module.eks_cluster.cluster_endpoint
    cluster_ca       = module.eks_cluster.cluster_ca_certificate
    dex_url          = "https://${local.full_domain}/dex"
    client_secret    = random_password.dex_client_secret.result
    listen_port      = "9800"
  })

  console_nginx_conf = <<-NGINX
    server {
      listen ${local.console_port};
      root /usr/share/nginx/html;
      location /get-started/ {
        alias /usr/share/nginx/html/;
        default_type text/html;
      }
    }
  NGINX
}

resource "helm_release" "console" {
  name      = "console"
  chart     = "${path.module}/../charts/console"
  namespace = local.console_namespace

  values = [yamlencode({
    domain    = local.full_domain
    namespace = local.console_namespace
    name      = local.console_name
    port      = local.console_port
    path      = local.console_path
    html      = local.console_html
    script    = local.console_script
    nginxConf = local.console_nginx_conf
  })]

  depends_on = [helm_release.workspace_router]
}
