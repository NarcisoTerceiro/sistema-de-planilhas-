"""WSGI do Data Formulator completo, com extensão de WhatsApp."""
import os
from pathlib import Path
from importlib.metadata import version
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
if version('data_formulator') != '0.8.0b1':
    raise RuntimeError('Esta integração requer Data Formulator 0.8.0b1.')
os.environ['DF_PLUGIN_DIR'] = str(ROOT)
os.environ['DATA_FORMULATOR_ENV_DIR'] = str(ROOT)
if os.environ.get('RENDER'):
    os.environ.setdefault('HOST', '0.0.0.0')
if os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE', '').strip():
    os.environ.setdefault('DF_SOURCES__google_planilhas__type', 'google_sheets')
    os.environ.setdefault('DF_SOURCES__google_planilhas__name', 'Google Sheets — conta de serviço')
if not os.environ.get('FLASK_SECRET_KEY'):
    if os.environ.get('RENDER') or os.environ.get('WHATSAPP_ENABLED', '').lower() == 'true':
        raise RuntimeError('Defina uma FLASK_SECRET_KEY estável nas variáveis do servidor.')
    # Apenas para teste local sem WhatsApp, mantém compatibilidade com o fluxo existente.
    import secrets
    os.environ['FLASK_SECRET_KEY'] = secrets.token_hex(32)

from data_formulator.app import app, configure_logging
configure_logging()
from whatsapp_extension import install
install(app)


if __name__ == '__main__':
    host = os.environ.get('HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', '5567'))
    print(f'Data Formulator: http://{host}:{port}')
    print(f'Integração WhatsApp: http://{host}:{port}/integracoes/whatsapp')
    app.run(host=host, port=port, threaded=True, use_reloader=False)
