"""Create local-only credentials without printing or overwriting existing passwords."""

import os
import secrets
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    directory = root / 'work/olist/secrets'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    for role in ('admin', 'writer', 'reader'):
        path = directory / f'{role}.txt'
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, 'w') as stream:
            stream.write(secrets.token_urlsafe(32) + '\n')
    print('Local credentials ready in ignored work/olist/secrets; existing passwords preserved.')


if __name__ == '__main__':
    main()
