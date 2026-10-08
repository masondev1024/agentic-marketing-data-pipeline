# GitHub Actions workflow

The `CI` workflow runs on pushes and pull requests targeting `main` or `dev`, and can also be started manually. It uses Python from `.python-version`, installs the lockfile with `uv sync --locked --all-groups`, runs the full pytest suite, and checks application code, tests, and maintained scripts with Ruff. Archived run evidence is not treated as maintained source code.

CI starts a disposable PostgreSQL 17 service on `127.0.0.1:55432`. The job creates fresh, random writer and reader credentials and stores them only in the ignored `work/olist/secrets/` directory for the opt-in PostgreSQL integration tests. The administrator connection points only to that job's service. It does not use a personal database, repository secret, or cloud database.

The workflow does not install the optional `local` extra or download model weights. MLX inference is specific to Apple Silicon macOS and is not exercised on the Linux runner; model behavior must be verified separately on the supported local runtime. Tests that exercise application logic use controlled substitutes where needed.

GitHub Actions and the PostgreSQL service image are pinned to immutable commit or image digests. To update a pin, verify the release and immutable identifier from its upstream source, update the version comment with it, and run the workflow before merging. The uv executable is also pinned to an explicit version.

## 브랜치 사용

`main`은 검증된 기준, `dev`는 다음 변경을 통합하는 브랜치다. 최초 게시에서는 두 브랜치를
동일한 검증 커밋으로 만들고 로컬 작업은 `dev`에서 계속한다. 이후 `dev`의 변경을 검토하고
CI 결과를 확인한 뒤 `main`에 반영한다. 현재 브랜치 보호 정책을 설정했다는 뜻은 아니다.

CI는 Python 코드와 격리된 PostgreSQL을 검증한다. NVIDIA 실제 호출·MLX 실제 추론·
AWS 배포는 실행하지 않으며, 로컬 평가 증거와 GitHub Actions 결과를 구분한다.
Ruff는 유지보수 소스를 검사하며 `evidence/`의 해시 고정 실험 스크립트는 설정에서 제외한다.
