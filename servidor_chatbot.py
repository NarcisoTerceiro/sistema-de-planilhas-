"""Servidor reduzido: painel e webhook do chatbot, sem iniciar o Data Formulator web."""
import os
import logging
from importlib.metadata import version
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, redirect

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')

if version('data_formulator') != '0.8.0b1':
    raise RuntimeError('Esta integração requer Data Formulator 0.8.0b1.')

# Mantém os caminhos de configuração usados pelo leitor do Google e pelo núcleo
# de análise do Data Formulator, sem importar data_formulator.app e suas rotas/UI.
# O chatbot importa o conector diretamente. Evita executá-lo novamente
# durante a descoberta automática de plugins do Data Formulator.
os.environ['DF_PLUGIN_DIR'] = str(ROOT / '_chatbot_plugins')
os.environ['DATA_FORMULATOR_ENV_DIR'] = str(ROOT)
if os.environ.get('RENDER'):
    os.environ.setdefault('HOST', '0.0.0.0')

# Conta de serviço Google via variável de ambiente (p/ hosts sem "arquivo
# secreto", como a Hyze). Se GOOGLE_SERVICE_ACCOUNT_JSON existir e
# GOOGLE_SERVICE_ACCOUNT_FILE não, grava o JSON em disco e aponta o arquivo.
# Aceita o conteúdo do JSON puro ou o mesmo conteúdo em base64.
_sa_json = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON', '').strip()
if _sa_json and not os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE', '').strip():
    import json as _json
    import base64 as _base64
    _raw = _sa_json
    if not _raw.lstrip().startswith('{'):
        try:
            _raw = _base64.b64decode(_sa_json).decode('utf-8')
        except Exception:
            _raw = _sa_json
    try:
        _json.loads(_raw)
    except ValueError:
        raise RuntimeError(
            'GOOGLE_SERVICE_ACCOUNT_JSON inválido: cole o conteúdo do JSON da '
            'chave da conta de serviço (ou o mesmo conteúdo em base64).'
        )
    _home = os.environ.get('DATA_FORMULATOR_HOME', str(ROOT))
    os.makedirs(_home, exist_ok=True)
    _sa_path = os.path.join(_home, 'google-service-account.json')
    with open(_sa_path, 'w', encoding='utf-8') as _fh:
        _fh.write(_raw)
    os.environ['GOOGLE_SERVICE_ACCOUNT_FILE'] = _sa_path

host = os.environ.get('HOST', '127.0.0.1')
port = int(os.environ.get('PORT', '5567'))
online = host not in ('127.0.0.1', 'localhost', '::1')
if not os.environ.get('FLASK_SECRET_KEY'):
    if online or os.environ.get('WHATSAPP_ENABLED', '').lower() == 'true':
        raise RuntimeError('Defina FLASK_SECRET_KEY estável nas variáveis do servidor.')
    import secrets
    os.environ['FLASK_SECRET_KEY'] = secrets.token_hex(32)

app = Flask('chatbot_whatsapp')
app.secret_key = os.environ['FLASK_SECRET_KEY']
app.json.ensure_ascii = False
app.json.sort_keys = False
app.config.update(
    MAX_CONTENT_LENGTH=500 * 1024 * 1024,
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 365,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=bool(os.environ.get('RENDER')),
    # O AnalystAgent e Workspace usam somente estas opções do Flask.
    CLI_ARGS={
        'host': host,
        'sandbox': 'local',
        'data_dir': os.environ.get('DATA_FORMULATOR_HOME'),
        'max_display_rows': int(os.environ.get('MAX_DISPLAY_ROWS', '10000')),
        'scratch_max_bytes': int(os.environ.get('SCRATCH_MAX_SIZE_MB', '1024')) * 1024 * 1024,
        'scratch_max_file_bytes': int(os.environ.get('SCRATCH_MAX_FILE_SIZE_MB', '20')) * 1024 * 1024,
    },
)

logging.basicConfig(
    level=os.environ.get('LOG_LEVEL', 'INFO').upper(),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)

from whatsapp_extension import install  # noqa: E402

install(app)


@app.get('/')
def home():
    return redirect('/chatbot-whatsapp')


if __name__ == '__main__':
    print(f'Chatbot WhatsApp: http://{host}:{port}/chatbot-whatsapp')
    app.run(host=host, port=port, threaded=True, use_reloader=False)
