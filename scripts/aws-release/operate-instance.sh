#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: operate-instance.sh --stack-name llm-ops-lab-<suffix> --action <install-image|backup|verify-backup|status> [--image-tag release-<sha>] [--backup-key backups/sqlite/<UTC-stamp>/investigations.sqlite] [--expected-account 123456789012] [--profile develope-test] [--region eu-west-1] --run

Runs one allowlisted operation on the stack-owned instance through SSM Run Command.
The exact --run flag is required; arbitrary remote shell commands are not accepted.
EOF
}

stack_name=''
action=''
image_tag=''
backup_key=''
profile='develope-test'
region='eu-west-1'
expected_account="${AWS_EXPECTED_ACCOUNT:-}"
run=0
while (($#)); do
  case "$1" in
    --stack-name) stack_name="${2:?missing stack name}"; shift 2 ;;
    --action) action="${2:?missing action}"; shift 2 ;;
    --image-tag) image_tag="${2:?missing image tag}"; shift 2 ;;
    --backup-key) backup_key="${2:?missing backup key}"; shift 2 ;;
    --profile) profile="${2:?missing profile}"; shift 2 ;;
    --region) region="${2:?missing region}"; shift 2 ;;
    --expected-account) expected_account="${2:?missing expected account}"; shift 2 ;;
    --run) run=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown_argument=$1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$stack_name" =~ ^llm-ops-lab-[a-z0-9][a-z0-9-]{0,32}$ ]] || { echo 'stack_name_must_use_llm-ops-lab-prefix' >&2; exit 2; }
[[ "$region" == eu-west-1 ]] || { echo 'release_scope_is_eu-west-1' >&2; exit 2; }
[[ "$expected_account" =~ ^[0-9]{12}$ ]] || { echo 'set_AWS_EXPECTED_ACCOUNT_or_pass_--expected-account' >&2; exit 2; }
(( run )) || { echo 'refusing_to_run_without_--run' >&2; exit 2; }
account="$(aws sts get-caller-identity --profile "$profile" --query Account --output text)"
[[ "$account" == "$expected_account" ]] || { echo "unexpected_aws_account=$account" >&2; exit 3; }
stack_info="$(aws cloudformation describe-stacks --profile "$profile" --region "$region" --stack-name "$stack_name" --output json)"
for expected_tag in 'Project=llm-ops-lab' 'Owner=aws-release-lab' 'Purpose=short-lived-release-test'; do
  tag_key="${expected_tag%%=*}"
  tag_value="${expected_tag#*=}"
  actual_tag="$(jq -r --arg key "$tag_key" '.Stacks[0].Tags[]? | select(.Key == $key) | .Value' <<<"$stack_info" | head -n 1)"
  [[ "$actual_tag" == "$tag_value" ]] || { echo "stack_tag_mismatch=$tag_key" >&2; exit 3; }
done
instance_id="$(jq -r '.Stacks[0].Outputs[]? | select(.OutputKey == "AppInstanceId") | .OutputValue' <<<"$stack_info" | head -n 1)"
[[ "$instance_id" =~ ^i-[0-9a-f]+$ ]] || { echo 'stack_owned_instance_id_unavailable' >&2; exit 3; }

case "$action" in
  install-image)
    [[ "$image_tag" =~ ^release-[0-9a-fA-F]{7,40}$ ]] || { echo 'image_tag_must_be_release_git_sha' >&2; exit 2; }
    image_revision="$(printf '%s' "${image_tag#release-}" | tr '[:upper:]' '[:lower:]')"
    command="sudo /usr/local/sbin/llm-ops-install-image release-$image_revision"
    ;;
  backup)
    [[ -z "$image_tag" && -z "$backup_key" ]] || { echo 'backup_action_takes_no_extra_arguments' >&2; exit 2; }
    command='sudo /usr/local/sbin/llm-ops-backup-sqlite'
    ;;
  verify-backup)
    [[ "$backup_key" =~ ^backups/sqlite/[0-9]{8}T[0-9]{6}Z/investigations\.sqlite$ ]] || { echo 'backup_key_invalid' >&2; exit 2; }
    [[ -z "$image_tag" ]] || { echo 'verify-backup_does_not_accept_image_tag' >&2; exit 2; }
    command="sudo /usr/local/sbin/llm-ops-verify-sqlite-backup '$backup_key'"
    ;;
  status)
    [[ -z "$image_tag" && -z "$backup_key" ]] || { echo 'status_action_takes_no_extra_arguments' >&2; exit 2; }
    command='systemctl --no-pager --full status llm-ops-lab-api.service llm-ops-lab-caddy.service'
    ;;
  *) echo 'action_not_allowlisted' >&2; usage >&2; exit 2 ;;
esac

parameters="$(jq -cn --arg command "$command" '{commands:[$command]}')"
command_id="$(aws ssm send-command --profile "$profile" --region "$region" \
  --instance-ids "$instance_id" --document-name AWS-RunShellScript \
  --parameters "$parameters" --query 'Command.CommandId' --output text)"
echo "ssm_command_id=$command_id instance_id=$instance_id action=$action"
if ! aws ssm wait command-executed --profile "$profile" --region "$region" --command-id "$command_id" --instance-id "$instance_id"; then
  aws ssm get-command-invocation --profile "$profile" --region "$region" --command-id "$command_id" \
    --instance-id "$instance_id" --query '{Status:Status,ResponseCode:ResponseCode,StandardOutputContent:StandardOutputContent,StandardErrorContent:StandardErrorContent}' --output json
  exit 4
fi
aws ssm get-command-invocation --profile "$profile" --region "$region" --command-id "$command_id" \
  --instance-id "$instance_id" --query '{Status:Status,ResponseCode:ResponseCode,StandardOutputContent:StandardOutputContent,StandardErrorContent:StandardErrorContent}' --output json
