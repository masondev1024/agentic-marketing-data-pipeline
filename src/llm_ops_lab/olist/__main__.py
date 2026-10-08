"""Local Olist lifecycle commands. No implicit database creation or cloud operations."""

import argparse
import json
from pathlib import Path

from .config import connection_info
from .pipeline import migrate, run_pipeline, save_report
from .validation import validate_sources


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        'command', choices=('validate', 'migrate', 'load', 'diagnose', 'investigate'))
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--intake', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--version', help='Pin diagnosis to an existing published version')
    parser.add_argument('--question', help='Bounded Olist investigation question')
    parser.add_argument('--engine', choices=('action-v5', 'intent-v1'), default='action-v5')
    parser.add_argument('--timeout', type=float, default=60.0)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == 'investigate' and (
        not args.question or not args.question.strip() or len(args.question) > 2048
        or not 0 < args.timeout <= 300
    ):
        parser.error('investigate requires a nonblank question of at most 2048 characters '
                     'and a timeout in (0, 300]')
    try:
        if args.command == 'validate':
            result = validate_sources(root, args.intake)
        elif args.command == 'migrate':
            migrate(connection_info(root))
            result = {'status': 'migrated'}
        elif args.command == 'load':
            result = run_pipeline(root, connection_info(root), intake_path=args.intake)
        elif args.command == 'diagnose':
            from .diagnostics import diagnose
            result = diagnose(connection_info(root, reader=True), version_id=args.version)
        else:
            from llm_ops_lab.marketing.offline_model import OfflineModel

            if args.engine == 'intent-v1':
                from .intent_agent import investigate
            else:
                from .agent import investigate
            # One owned inference process; no automatic downloads or fallback model.
            with OfflineModel() as model:
                result = investigate(
                    connection_info(root, reader=True), args.question, model.generate,
                    version_id=args.version, timeout_seconds=args.timeout,
                )
                result['model'] = model.identity
    except Exception as exc:
        # Never log the raw exception: drivers can include connection information.
        result = {'status': 'failed', 'error_code': type(exc).__name__}
    if args.output:
        save_report(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return int(result.get('status') in ('failed', 'busy', 'abstained'))


if __name__ == '__main__':
    raise SystemExit(main())
