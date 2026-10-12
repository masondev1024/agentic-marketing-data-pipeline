#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: deploy.sh --stack-name llm-ops-lab-<suffix> --public-host <dns-name> [--expected-account 123456789012] [--profile develope-test] [--region eu-west-1] --apply

Creates or updates only the named CloudFormation stack from infra/aws-release/template.yaml.
The exact --apply flag is required; this script never runs as a dry-run by default.
EOF
}

stack_name=''
public_host=''
profile='develope-test'
region='eu-west-1'
expected_account="${AWS_EXPECTED_ACCOUNT:-}"
apply=0
while (($#)); do
  case "$1" in
    --stack-name) stack_name="${2:?missing stack name}"; shift 2 ;;
    --public-host) public_host="${2:?missing public host}"; shift 2 ;;
    --profile) profile="${2:?missing profile}"; shift 2 ;;
    --region) region="${2:?missing region}"; shift 2 ;;
    --expected-account) expected_account="${2:?missing expected account}"; shift 2 ;;
    --apply) apply=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown_argument=$1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$stack_name" =~ ^llm-ops-lab-[a-z0-9][a-z0-9-]{0,32}$ ]] || { echo 'stack_name_must_use_llm-ops-lab-prefix' >&2; exit 2; }
[[ "$public_host" =~ ^[a-z0-9][a-z0-9.-]{1,251}[a-z0-9]$ ]] || { echo 'public_host_must_be_lowercase_dns_name' >&2; exit 2; }
[[ "$region" == eu-west-1 ]] || { echo 'release_scope_is_eu-west-1' >&2; exit 2; }
[[ "$expected_account" =~ ^[0-9]{12}$ ]] || { echo 'set_AWS_EXPECTED_ACCOUNT_or_pass_--expected-account' >&2; exit 2; }
(( apply )) || { echo 'refusing_to_apply_without_--apply' >&2; exit 2; }

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
template="$repo_root/infra/aws-release/template.yaml"
account="$(aws sts get-caller-identity --profile "$profile" --query Account --output text)"
[[ "$account" == "$expected_account" ]] || { echo "unexpected_aws_account=$account" >&2; exit 3; }
if existing_stack="$(aws cloudformation describe-stacks --profile "$profile" --region "$region" --stack-name "$stack_name" --output json 2>/dev/null)"; then
  for expected_tag in 'Project=llm-ops-lab' 'Owner=aws-release-lab' 'Purpose=short-lived-release-test'; do
    tag_key="${expected_tag%%=*}"
    tag_value="${expected_tag#*=}"
    actual_tag="$(jq -r --arg key "$tag_key" '.Stacks[0].Tags[]? | select(.Key == $key) | .Value' <<<"$existing_stack" | head -n 1)"
    [[ "$actual_tag" == "$tag_value" ]] || { echo "existing_stack_tag_mismatch=$tag_key" >&2; exit 3; }
  done
fi
ami_id="$(aws ssm get-parameter --profile "$profile" --region "$region" --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 --query Parameter.Value --output text)"
[[ "$ami_id" =~ ^ami-[0-9a-f]+$ ]] || { echo 'could_not_resolve_al2023_arm64_ami' >&2; exit 3; }
parameter_prefix="/llm-ops-lab/aws-release/${stack_name#llm-ops-lab-}"

echo "scope_account=$account region=$region stack=$stack_name public_host=$public_host ami=$ami_id"
echo 'estimated_infrastructure_cost=about_USD_1.10_to_1.60_per_24h_before_data_transfer_logs_and_hosted_inference'
echo 'approved_budget_cap=USD_5; resource deletion remains an explicit separate step'
aws cloudformation deploy \
  --profile "$profile" \
  --region "$region" \
  --stack-name "$stack_name" \
  --template-file "$template" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides "PublicHost=$public_host" "ParameterPrefix=$parameter_prefix" "AmiId=$ami_id" \
  --tags Project=llm-ops-lab Owner=aws-release-lab Purpose=short-lived-release-test \
  --no-fail-on-empty-changeset
aws cloudformation describe-stacks --profile "$profile" --region "$region" --stack-name "$stack_name" \
  --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' --output table
