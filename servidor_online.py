"""Data Formulator completo (interface web) + extensão de WhatsApp.

Entrypoint para rodar o sistema inteiro: a aplicação web da Microsoft com
gráficos, upload e análise visual, MAIS o painel do chatbot, o webhook do
WhatsApp e a importação do Google Sheets.

Use este módulo (servidor_online:app) para o deploy completo na Oracle/VPS.
O servidor_chatbot:app continua disponível para a versão enxuta só de chatbot.
"""
import os
from pathlib import Path
from importlib.metadata import version

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')

if version('data_formulator') != '0.8.0b1':
    raise RuntimeError('Esta integração requer Data Formulator 0.8.0b1.')

# DF_PLUGIN_DIR = raiz do projeto: o Data Formulator carrega o
# google_sheets_data_loader.py como fonte de dados "Google Sheets" dentro da
# interface web. DATA_FORMULATOR_ENV_DIR resolve caminhos relativos do .env.
os.environ['DF_PLUGIN_DIR'] = str(ROOT)
os.environ['DATA_FORMULATOR_ENV_DIR'] = str(ROOT)
os.environ.setdefault('HOST', '0.0.0.0')

# Expõe a fonte Google Sheets na interface quando há conta de serviço.
if os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE', '').strip():
    os.environ.setdefault('DF_SOURCES__google_planilhas__type', 'google_sheets')
    os.environ.setdefault('DF_SOURCES__google_planilhas__name', 'Google Sheets — conta de serviço')

host = os.environ.get('HOST', '127.0.0.1')
online = host not in ('127.0.0.1', 'localhost', '::1')
if not os.environ.get('FLASK_SECRET_KEY'):
    if online or os.environ.get('WHATSAPP_ENABLED', '').lower() == 'true':
        raise RuntimeError('Defina uma FLASK_SECRET_KEY estável nas variáveis do servidor.')
    # Apenas para teste local sem WhatsApp.
    import secrets
    os.environ['FLASK_SECRET_KEY'] = secrets.token_hex(32)

from data_formulator.app import app, configure_logging  # noqa: E402
configure_logging()

from whatsapp_extension import install  # noqa: E402
install(app)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5567'))
    print(f'Data Formulator: http://{host}:{port}')
    print(f'Chatbot WhatsApp: http://{host}:{port}/chatbot-whatsapp')
    app.run(host=host, port=port, threaded=True, use_reloader=False)
