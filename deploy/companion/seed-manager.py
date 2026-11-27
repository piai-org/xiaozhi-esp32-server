#!/usr/bin/env python3
"""Generate a private, additive MySQL model seed; never connect to a database."""
import argparse
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent

def sql_string(value):
    return 'CONVERT(0x' + value.encode().hex() + ' USING utf8mb4)'

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=HERE / 'runtime/voice/data/.config.yaml')
    parser.add_argument('--output', type=Path, default=HERE / 'runtime/manager-models.sql')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    selected = config['selected_module']
    names = {key: selected[key] for key in ('ASR', 'TTS', 'LLM', 'Intent', 'Memory', 'VAD')}
    auxiliary = config['Intent'][names['Intent']]['llm']
    ids = {key: 'Companion_' + key for key in names}
    ids['Auxiliary'] = 'Companion_Auxiliary'
    statements = []
    for category, name in list(names.items()) + [('LLM', auxiliary)]:
        settings = dict(config[category][name])
        identifier = ids['Auxiliary'] if name == auxiliary else ids[category]
        if category == 'Intent':
            settings['llm'] = ids['Auxiliary']
        values = [sql_string(identifier), sql_string(category), sql_string(identifier),
                  sql_string(identifier), '0', '1', sql_string(json.dumps(settings, ensure_ascii=False)),
                  sql_string('Portable companion profile; operator-owned configuration'), '100', 'NOW()', 'NOW()']
        statements.append('INSERT INTO ai_model_config '
            '(id,model_type,model_code,model_name,is_default,is_enabled,config_json,remark,sort,create_date,update_date) '
            'SELECT ' + ','.join(values) + ' FROM DUAL WHERE NOT EXISTS '
            '(SELECT 1 FROM ai_model_config WHERE id=' + sql_string(identifier) + ');')
    text = 'SET NAMES utf8mb4;\nSTART TRANSACTION;\n' + '\n'.join(statements) + '\nCOMMIT;\n'
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(text)
    print('Wrote private model seed:', args.output)
    print('Model IDs:', ', '.join(ids.values()))

if __name__ == '__main__':
    main()
