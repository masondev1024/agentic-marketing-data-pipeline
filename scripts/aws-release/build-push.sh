#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: build-push.sh --stack-name llm-ops-lab-<suffix> --source-revision <7-to-40-hex> [--expected-account 123456789012] [--profile develope-test] [--region eu-west-1] --push

Builds one linux/arm64 immutable image and pushes it to the ECR repository owned by
the named stack. The exact --push flag is required; builds can still consume local disk.
EOF
}

stack_name=''
revision=''
profile='develope-test'
region='eu-west-1'
expected_account="${AWS_EXPECTED_ACCOUNT:-}"
push=0
while (($#)); do
  case "$1" in
    --stack-name) stack_name="${2:?missing stack name}"; shift 2 ;;
    --source-revision) revision="${2:?missing source revision}"; shift 2 ;;
    --profile) profile="${2:?missing profile}"; shift 2 ;;
    --region) region="${2:?missing region}"; shift 2 ;;
    --expected-account) expected_account="${2:?missing expected account}"; shift 2 ;;
    --push) push=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown_argument=$1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$stack_name" =~ ^llm-ops-lab-[a-z0-9][a-z0-9-]{0,32}$ ]] || { echo 'stack_name_must_use_llm-ops-lab-prefix' >&2; exit 2; }
[[ "$revision" =~ ^[0-9a-fA-F]{7,40}$ ]] || { echo 'source_revision_must_be_git_sha' >&2; exit 2; }
[[ "$region" == eu-west-1 ]] || { echo 'release_scope_is_eu-west-1' >&2; exit 2; }
[[ "$expected_account" =~ ^[0-9]{12}$ ]] || { echo 'set_AWS_EXPECTED_ACCOUNT_or_pass_--expected-account' >&2; exit 2; }
(( push )) || { echo 'refusing_to_push_without_--push' >&2; exit 2; }

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
inputs=(
  src
  data
  pyproject.toml
  uv.lock
  infra/aws-release/Dockerfile
  infra/aws-release/Dockerfile.dockerignore
)
head_revision="$(git -C "$repo_root" rev-parse --verify 'HEAD^{commit}')"
resolved_revision="$(git -C "$repo_root" rev-parse --verify "${revision}^{commit}")" || {
  echo 'source_revision_does_not_resolve_to_commit' >&2
  exit 3
}
[[ "$resolved_revision" == "$head_revision" ]] || {
  echo 'source_revision_must_be_current_HEAD' >&2
  exit 3
}
if ! git -C "$repo_root" diff --quiet HEAD -- "${inputs[@]}"; then
  echo 'docker_build_inputs_differ_from_committed_HEAD' >&2
  git -C "$repo_root" diff --name-only HEAD -- "${inputs[@]}" >&2
  exit 3
fi
untracked_inputs="$(git -C "$repo_root" ls-files --others --exclude-standard -- "${inputs[@]}")"
if [[ -n "$untracked_inputs" ]]; then
  echo 'untracked_docker_build_inputs_present' >&2
  printf '%s\n' "$untracked_inputs" >&2
  exit 3
fi

# Build only from committed contents. This excludes ignored caches and any other
# worktree files, while the checks above ensure the labeled revision is exact.
build_context="$(mktemp -d "${TMPDIR:-/tmp}/llm-ops-build-context.XXXXXX")"
docker_config=''
cleanup() {
  [[ -z "$docker_config" ]] || rm -rf "$docker_config"
  [[ -z "$build_context" ]] || rm -rf "$build_context"
}
trap cleanup EXIT
git -C "$repo_root" archive --format=tar "$head_revision" | tar -xf - -C "$build_context"

account="$(aws sts get-caller-identity --profile "$profile" --query Account --output text)"
[[ "$account" == "$expected_account" ]] || { echo "unexpected_aws_account=$account" >&2; exit 3; }
stack_info="$(aws cloudformation describe-stacks --profile "$profile" --region "$region" --stack-name "$stack_name" --output json)"
for expected_tag in 'Project=llm-ops-lab' 'Owner=aws-release-lab' 'Purpose=short-lived-release-test'; do
  tag_key="${expected_tag%%=*}"
  tag_value="${expected_tag#*=}"
  actual_tag="$(jq -r --arg key "$tag_key" '.Stacks[0].Tags[]? | select(.Key == $key) | .Value' <<<"$stack_info" | head -n 1)"
  [[ "$actual_tag" == "$tag_value" ]] || { echo "stack_tag_mismatch=$tag_key" >&2; exit 3; }
done
repository="$(aws cloudformation describe-stacks --profile "$profile" --region "$region" --stack-name "$stack_name" --query "Stacks[0].Outputs[?OutputKey=='EcrRepositoryName'].OutputValue | [0]" --output text)"
[[ "$repository" == "llm-ops-lab-$stack_name" ]] || { echo 'ecr_repository_did_not_match_expected_stack_scope' >&2; exit 3; }
registry="$account.dkr.ecr.$region.amazonaws.com"
revision_lower="$(printf '%s' "$revision" | tr '[:upper:]' '[:lower:]')"
tag="release-$revision_lower"

docker_config="$(mktemp -d "${TMPDIR:-/tmp}/llm-ops-ecr-config.XXXXXX")"
chmod 0700 "$docker_config"
export DOCKER_CONFIG="$docker_config"
aws ecr get-login-password --profile "$profile" --region "$region" |
  docker login --username AWS --password-stdin "$registry" >/dev/null
docker buildx build --platform linux/arm64 --provenance=false \
  --file "$build_context/infra/aws-release/Dockerfile" \
  --tag "$registry/$repository:$tag" \
  --push "$build_context"
unset DOCKER_CONFIG
cleanup
trap - EXIT
echo "pushed_image=$registry/$repository:$tag"
