#!/bin/bash
# Update the IAM principal allowlist that gates access to the app through the auth sidecar.
#
#   sudo sh update-allowlist.sh roles [add|remove|set] Role1,Role2   # IAM role names (jd teams)
#   sudo sh update-allowlist.sh users [add|remove|set] alice,bob     # IAM user names (jd users)
#
# The persistent source of truth is /etc/AUTH_ALLOWLIST (two sections, [roles] and [users]),
# re-seeded from the terraform allowlist vars on every cloud-init run; because this script writes
# edits back into those vars, that re-seed is a no-op rather than a revert. This script
# edits that file, mirrors both sections into /opt/docker/.env (which docker-compose interpolates
# into the sidecar's ROLE_NAME_ALLOWLIST / USER_NAME_ALLOWLIST env), and recreates ONLY the
# auth-sidecar container — JupyterLab keeps running. On success it echoes the modified section's
# new content to stdout; the manifest runner writes that back into the matching terraform variable
# so `jd up` sees no diff (no split-brain between the live system and terraform state).
set -e

LOG_FILE="/var/log/jupyter-deploy/update-allowlist.log"
touch "$LOG_FILE"
exec 2> >(tee -a "$LOG_FILE" >&2)

ALLOWLIST_FILE="/etc/AUTH_ALLOWLIST"
ENV_FILE="/opt/docker/.env"
ENTITY_TYPE=$1
ACTION=$2
VALUES=$3

# Set to 1 if the sidecar never came back healthy. Checked only after the terraform write-back has
# been echoed, so a failed recreate is reported without costing the write-back.
RECREATE_FAILED=0

log_message() {
  echo "[$(date +"%Y-%m-%d %H:%M:%S")] $*" >> "$LOG_FILE"
}

# Ensure the file and both sections exist (in case it was manually deleted).
touch "$ALLOWLIST_FILE"
grep -q "^\[roles\]$" "$ALLOWLIST_FILE" || printf '\n[roles]\n' >> "$ALLOWLIST_FILE"
grep -q "^\[users\]$" "$ALLOWLIST_FILE" || printf '\n[users]\n' >> "$ALLOWLIST_FILE"

get_section_content() {
  local section=$1
  sed -n "/^\[$section\]$/,/^\[/p" "$ALLOWLIST_FILE" | grep -v "^\[$section\]$" | grep -v "^\[" | tr -d '\n' | tr -d ' '
}

update_section() {
  local section=$1
  local content=$2
  # Delete the section's current body lines, then insert the new single content line.
  sed -i "/^\[$section\]$/,/^\[/ {/^\[$section\]$/!{/^\[/!d}}" "$ALLOWLIST_FILE"
  if [ -n "$content" ]; then
    sed -i "/^\[$section\]$/a $content" "$ALLOWLIST_FILE"
  fi
}

# Replace or append KEY=value in the docker-compose env file.
set_env_var() {
  local key=$1
  local value=$2
  if grep -q "^$key=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s/^$key=.*/$key=$value/" "$ENV_FILE"
  else
    echo "$key=$value" >> "$ENV_FILE"
  fi
}

case "$ENTITY_TYPE" in
  roles|users) ;;
  *)
    echo "Error: invalid entity type '$ENTITY_TYPE'. Use 'roles' or 'users'."
    exit 1
    ;;
esac

# Defense-in-depth: this value normally arrives via SSM with an allowedPattern, but reject anything
# outside comma-separated bare IAM names here too (no whitespace or shell metacharacters).
if [ -n "$VALUES" ] && ! printf '%s' "$VALUES" | grep -Eq '^[a-zA-Z0-9+=,.@_-]+$'; then
  echo "Error: values must be comma-separated bare IAM names ([A-Za-z0-9+=,.@_-])."
  exit 1
fi

# IAM names are unique per account regardless of case, and the sidecar matches case-insensitively
# (it lower-cases both the allowlist and the caller name). Compare/dedup case-insensitively here too
# so add/remove/set agree with the sidecar — otherwise `remove datascience` would silently no-op
# against a stored `DataScience` while access stays granted. But PRESERVE the given casing in the
# stored list: main.tf re-adds the caller in its ARN casing on every `jd up`, so folding to lower
# case would make the write-back differ from terraform's rendered list and restart the whole stack
# each run. So key the set by lower-case name (dedup) but store the original casing as the value.
CURRENT=$(get_section_content "$ENTITY_TYPE")
IFS=',' read -ra CURRENT_ARR <<< "$CURRENT"
IFS=',' read -ra INPUT_ARR <<< "$VALUES"

# Build the resulting set of names for this section: lower-case key -> original-casing name.
declare -A RESULT_SET=()
case "$ACTION" in
  add)
    for v in "${CURRENT_ARR[@]}"; do [ -n "$v" ] && RESULT_SET["${v,,}"]="$v"; done
    # Keep the existing casing when a name is re-added; only genuinely new names bring their own.
    for v in "${INPUT_ARR[@]}"; do
      [ -n "$v" ] || continue
      k="${v,,}"
      [ -n "${RESULT_SET[$k]+x}" ] || RESULT_SET["$k"]="$v"
    done
    ;;
  remove)
    for v in "${CURRENT_ARR[@]}"; do [ -n "$v" ] && RESULT_SET["${v,,}"]="$v"; done
    for v in "${INPUT_ARR[@]}"; do
      [ -n "$v" ] || continue
      k="${v,,}"
      unset 'RESULT_SET[$k]'
    done
    ;;
  set)
    for v in "${INPUT_ARR[@]}"; do [ -n "$v" ] && RESULT_SET["${v,,}"]="$v"; done
    ;;
  *)
    echo "Error: invalid action '$ACTION'. Use 'add', 'remove' or 'set'."
    exit 1
    ;;
esac

# Emit the names sorted so the result is deterministic: an unchanged set always produces the same
# string, so the write-back keeps terraform state stable instead of reordering it on every edit.
FINAL=""
if [ ${#RESULT_SET[@]} -gt 0 ]; then
  FINAL=$(printf '%s\n' "${RESULT_SET[@]}" | sort | paste -sd, -)
fi

if [ "$CURRENT" != "$FINAL" ]; then
  log_message "Updating [$ENTITY_TYPE]: '$CURRENT' -> '$FINAL'"
  update_section "$ENTITY_TYPE" "$FINAL"

  set_env_var "ROLE_NAME_ALLOWLIST" "$(get_section_content roles)"
  set_env_var "USER_NAME_ALLOWLIST" "$(get_section_content users)"

  log_message "Recreating auth-sidecar to apply changes..."
  cd /opt/docker
  # Recreate only the sidecar so it picks up the new env from .env; JupyterLab keeps running.
  # --wait gates on the container's healthcheck, so this call does not return until the sidecar is
  # actually serving the new allowlist. That makes the CLI authoritative: `jd teams add` returning
  # 0 means the decision is live, so callers need no settle-poll of their own.
  if OUTPUT=$(docker compose up -d auth-sidecar --wait --wait-timeout 60 2>&1); then
    log_message "auth-sidecar recreate complete: $OUTPUT"
  else
    # Roll the host edit back, because this command is about to fail and a failed command gets NO
    # terraform write-back: aws_ssm_runner raises on SSM Status=Failed before returning results, and
    # the handlers only call update_variables() on success. Leaving the edit in place would put the
    # host ahead of terraform, and the direction that costs is `remove` — the host would have
    # revoked the entry while the terraform variable still lists it, so the next `jd up` re-seeds
    # /etc/AUTH_ALLOWLIST and silently restores the access the operator just revoked.
    # Rolling back keeps both sides at the pre-command state, so a non-zero exit means exactly
    # "nothing changed, retry once the sidecar is healthy".
    log_message "ERROR: auth-sidecar did not become healthy within 60s: $OUTPUT"
    log_message "Rolling back [$ENTITY_TYPE] to '$CURRENT' so the host still matches terraform."
    update_section "$ENTITY_TYPE" "$CURRENT"
    set_env_var "ROLE_NAME_ALLOWLIST" "$(get_section_content roles)"
    set_env_var "USER_NAME_ALLOWLIST" "$(get_section_content users)"
    # Best-effort, deliberately NOT gated on health: the running container still holds the
    # abandoned edit in its environment, so recreate it against the rolled-back .env. If it stays
    # unhealthy the files are still consistent, and its restart policy converges it later.
    docker compose up -d auth-sidecar --force-recreate >/dev/null 2>&1 || true
    RECREATE_FAILED=1
  fi
else
  log_message "No change to [$ENTITY_TYPE]; auth-sidecar left running."
fi

# Echo the modified section's new content. The manifest runner writes this back into the matching
# terraform variable (iam_role_names_allowlist / iam_user_names_allowlist), keeping state in sync.
echo "$(get_section_content "$ENTITY_TYPE")"

# Non-zero when the sidecar never became healthy. The write-back above is emitted either way, but
# the CLI only consumes it on success, so the failure path rolled the host edit back rather than
# relying on it: whichever path ran, host and terraform agree.
exit "$RECREATE_FAILED"
