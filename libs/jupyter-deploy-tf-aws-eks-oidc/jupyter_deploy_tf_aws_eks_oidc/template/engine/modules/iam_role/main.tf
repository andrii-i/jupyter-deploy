resource "aws_iam_role" "this" {
  name               = var.role_name
  assume_role_policy = var.assume_role_policy
  tags               = var.combined_tags
}

resource "aws_iam_role_policy_attachment" "policies" {
  for_each   = { for idx, arn in var.policy_arns : tostring(idx) => arn }
  role       = aws_iam_role.this.name
  policy_arn = each.value
}
