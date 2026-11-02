"""Local API secret storage. Run `python -m webui.api_token` to initialize."""
import argparse
import os
from pathlib import Path
import secrets
import tempfile

TOKEN_FILE = Path(__file__).resolve().parent / '.api_token'


def read_token():
    """An explicitly configured environment variable overrides local storage."""
    if 'KRONOS_API_TOKEN' in os.environ:
        return os.environ['KRONOS_API_TOKEN']
    try:
        token = TOKEN_FILE.read_text(encoding='utf-8').strip()
    except (OSError, UnicodeError):
        return ''
    if not token or any(char.isspace() for char in token):
        return ''
    return token


def initialize_token(rotate=False):
    """Create an owner-only secret without exposing it in terminal output."""
    if not rotate:
        # Exclusive creation prevents accidentally overwriting an existing secret.
        try:
            fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(secrets.token_urlsafe(48) + '\n')
        return True
    # Atomic replacement avoids serving a partially written token during rotation.
    fd, name = tempfile.mkstemp(prefix='.api_token-', dir=TOKEN_FILE.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(secrets.token_urlsafe(48) + '\n')
        os.replace(name, TOKEN_FILE)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return True


def main():
    parser = argparse.ArgumentParser(description='Create or rotate the local forecast API token')
    parser.add_argument('--rotate', action='store_true', help='Replace the current local token')
    args = parser.parse_args()
    changed = initialize_token(rotate=args.rotate)
    print(f'Token {"saved" if changed else "already exists"}: {TOKEN_FILE}')
    print('The secret is not printed. KRONOS_API_TOKEN, when set, overrides this file.')


if __name__ == '__main__':
    main()
