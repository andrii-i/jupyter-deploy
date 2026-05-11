data "aws_iam_policy_document" "codebuild_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codebuild.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "codebuild" {
  name               = "${var.project_name}-codebuild"
  assume_role_policy = data.aws_iam_policy_document.codebuild_trust.json
  tags               = var.combined_tags
}

data "aws_iam_policy_document" "codebuild_permissions" {
  statement {
    sid = "ECRPush"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [var.ecr_repository_arn]
  }

  statement {
    sid       = "ECRAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "S3Source"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
    ]
    resources = ["${var.source_s3_bucket_arn}/*"]
  }

  statement {
    sid       = "S3BucketList"
    actions   = ["s3:ListBucket"]
    resources = [var.source_s3_bucket_arn]
  }

  statement {
    sid = "CloudWatchLogs"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "codebuild" {
  name   = "${var.project_name}-permissions"
  role   = aws_iam_role.codebuild.id
  policy = data.aws_iam_policy_document.codebuild_permissions.json
}

resource "aws_codebuild_project" "this" {
  name         = var.project_name
  service_role = aws_iam_role.codebuild.arn

  source {
    type      = "S3"
    location  = var.source_s3_location
    buildspec = var.buildspec
  }

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/standard:7.0"
    type            = "LINUX_CONTAINER"
    privileged_mode = true

    environment_variable {
      name  = "ECR_REPO_URL"
      value = var.ecr_repository_url
    }

    environment_variable {
      name  = "IMAGE_TAG"
      value = var.image_tag
    }
  }

  tags = var.combined_tags
}
