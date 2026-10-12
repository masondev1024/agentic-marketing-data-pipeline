#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: delete-stack.sh --stack-name llm-ops-lab-<suffix> [--expected-account 123456789012] [--profile develope-test] [--region eu-west-1] --apply

Purges every version and delete marker in this stack's backup bucket, then deletes
only the named CloudFormation stack and waits for stack deletion to complete.
EOF
}

stack_name=''
profile='develope-test'
region='eu-west-1'
expected_account="${AWS_EXPECTED_ACCOUNT:-}"
apply=0
while (($#)); do
  case "$1" in
    --stack-name) stack_name="${2:?missing stack name}"; shift 2 ;;
    --profile) profile="${2:?missing profile}"; shift 2 ;;
    --region) region="${2:?missing region}"; shift 2 ;;
    --expected-account) expected_account="${2:?missing expected account}"; shift 2 ;;
    --apply) apply=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown_argument=$1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$stack_name" =~ ^llm-ops-lab-[a-z0-9][a-z0-9-]{0,32}$ ]] || { echo 'stack_name_must_use_llm-ops-lab-prefix' >&2; exit 2; }
[[ "$region" == eu-west-1 ]] || { echo 'release_scope_is_eu-west-1' >&2; exit 2; }
[[ "$expected_account" =~ ^[0-9]{12}$ ]] || { echo 'set_AWS_EXPECTED_ACCOUNT_or_pass_--expected-account' >&2; exit 2; }
(( apply )) || { echo 'refusing_to_delete_without_--apply' >&2; exit 2; }
account="$(aws sts get-caller-identity --profile "$profile" --query Account --output text)"
[[ "$account" == "$expected_account" ]] || { echo "unexpected_aws_account=$account" >&2; exit 3; }

stack_info="$(aws cloudformation describe-stacks --profile "$profile" --region "$region" --stack-name "$stack_name" --output json)"
for expected_tag in 'Project=llm-ops-lab' 'Owner=aws-release-lab' 'Purpose=short-lived-release-test'; do
  tag_key="${expected_tag%%=*}"
  tag_value="${expected_tag#*=}"
  actual_tag="$(jq -r --arg key "$tag_key" '.Stacks[0].Tags[]? | select(.Key == $key) | .Value' <<<"$stack_info" | head -n 1)"
  [[ "$actual_tag" == "$tag_value" ]] || { echo "stack_tag_mismatch=$tag_key; refusing_delete" >&2; exit 3; }
done
bucket="$(jq -r '.Stacks[0].Outputs[]? | select(.OutputKey == "BackupBucketName") | .OutputValue' <<<"$stack_info" | head -n 1)"
parameter_prefix="$(jq -r '.Stacks[0].Outputs[]? | select(.OutputKey == "ParameterPrefix") | .OutputValue' <<<"$stack_info" | head -n 1)"
if [[ -z "$bucket" || "$bucket" == None ]]; then
  bucket="$(aws cloudformation describe-stack-resources --profile "$profile" --region "$region" --stack-name "$stack_name" --query "StackResources[?LogicalResourceId=='BackupBucket'].PhysicalResourceId | [0]" --output text)"
fi
if [[ -z "$bucket" || "$bucket" == None ]]; then
  bucket=''
else
  [[ "$bucket" == "$stack_name"* && "$bucket" == *[Bb][Aa][Cc][Kk][Uu][Pp]* ]] || {
    echo 'backup_bucket_name_did_not_match_expected_stack_scope; refusing_delete' >&2
    exit 4
  }
fi
echo "scope_account=$account region=$region stack=$stack_name backup_bucket=$bucket"
if [[ -n "$bucket" ]]; then
  echo 'purging_all_current_objects_versions_and_delete_markers_from_stack_bucket'

  while :; do
    page="$(aws s3api list-object-versions --profile "$profile" --region "$region" --bucket "$bucket" --max-items 1000 --output json)"
    objects="$(jq -c '[.Versions[]?, .DeleteMarkers[]?] | map({Key,VersionId})' <<<"$page")"
    count="$(jq 'length' <<<"$objects")"
    (( count > 0 )) || break
    (( count <= 1000 )) || { echo "s3_delete_batch_exceeds_limit=$count" >&2; exit 5; }
    deleted="$(aws s3api delete-objects --profile "$profile" --region "$region" --bucket "$bucket" --delete "$(jq -cn --argjson objects "$objects" '{Objects:$objects,Quiet:true}')" --output json)"
    errors="$(jq '(.Errors // []) | length' <<<"$deleted")"
    (( errors == 0 )) || { echo "s3_delete_errors=$errors" >&2; exit 5; }
  done

  remaining_page="$(aws s3api list-object-versions --profile "$profile" --region "$region" --bucket "$bucket" --max-items 1000 --output json)"
  remaining="$(jq '(.Versions // [] | length) + (.DeleteMarkers // [] | length)' <<<"$remaining_page")"
  [[ "$remaining" == 0 ]] || { echo "bucket_versions_remain=$remaining" >&2; exit 5; }
fi
aws cloudformation delete-stack --profile "$profile" --region "$region" --stack-name "$stack_name"
aws cloudformation wait stack-delete-complete --profile "$profile" --region "$region" --stack-name "$stack_name"
if aws cloudformation describe-stacks --profile "$profile" --region "$region" --stack-name "$stack_name" >/dev/null 2>&1; then
  echo 'stack_still_present_after_delete_wait' >&2
  exit 6
fi
expected_prefix="/llm-ops-lab/aws-release/${stack_name#llm-ops-lab-}"
if [[ -z "$parameter_prefix" || "$parameter_prefix" == None ]]; then parameter_prefix="$expected_prefix"; fi
[[ "$parameter_prefix" == "$expected_prefix" ]] || { echo 'ssm_parameter_prefix_did_not_match_stack_scope' >&2; exit 7; }
aws ssm delete-parameters --profile "$profile" --region "$region" \
  --names "$parameter_prefix/nvidia-api-key" "$parameter_prefix/olist-reader-dsn" "$parameter_prefix/public-api-token" \
  --output json >/dev/null
remaining_parameters="$(aws ssm get-parameters-by-path --profile "$profile" --region "$region" --path "$parameter_prefix" --recursive --query 'Parameters[].Name' --output json | jq 'length')"
[[ "$remaining_parameters" == 0 ]] || { echo "runtime_ssm_parameters_remain=$remaining_parameters" >&2; exit 8; }
echo 'stack_deletion_verified=true'
echo 'runtime_ssm_parameter_deletion_verified=true'
