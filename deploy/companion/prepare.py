#!/usr/bin/env python3
"""Render a private standalone config using only the Python standard library."""
import argparse
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent

def read_env(path):
    result = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key.strip()):
            raise ValueError('Invalid environment entry')
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        result[key.strip()] = value
    return result

def render(value, env):
    if isinstance(value, dict):
        return {key: render(item, env) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, env) for item in value]
    if isinstance(value, str):
        def replace(match):
            key = match[1]
            if not env.get(key):
                raise ValueError('Missing environment setting: ' + key)
            return env[key]
        return re.sub(r'\$\{([A-Z][A-Z0-9_]*)\}', replace, value)
    return value

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', type=Path, default=HERE / '.env')
    parser.add_argument('--output', type=Path, default=HERE / 'runtime/voice/data/.config.yaml')
    parser.add_argument('--overwrite', action='store_true')
    args = parser.parse_args()
    env = read_env(args.env)
    # Explicit environment variables override the private file.
    env.update({key: value for key, value in os.environ.items() if key in env})
    if env.get('DASHSCOPE_API_KEY', '').startswith('replace-'):
        parser.error('Set DASHSCOPE_API_KEY in your private .env first')
    for key in ('PUBLIC_WS_URL', 'PUBLIC_HTTP_URL'):
        parsed = urlsplit(env.get(key, ''))
        if not parsed.hostname or parsed.hostname.endswith('.example'):
            parser.error('Set a reachable ' + key)
    template = '\n'.join((HERE / 'profile.template.yaml').read_text().splitlines()[1:])
    config = render(json.loads(template), env)
    config['prompt'] = (HERE / 'role.txt').read_text()
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Exclusive creation protects an existing operator config unless explicitly requested.
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if args.overwrite else os.O_EXCL)
    fd = os.open(args.output, flags, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
    args.output.chmod(0o600)
    print('Wrote private configuration:', args.output)

if __name__ == '__main__':
    main()
