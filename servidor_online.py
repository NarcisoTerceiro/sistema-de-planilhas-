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

# Diagnostico: imprime os valores REAIS que o app recebe no boot. Ajuda a
# descobrir quando o host (ShardCloud) nao aplica uma troca de variavel.
print('BOOT env -> NVIDIA_ENABLED=%r NVIDIA_MODELS=%r WHATSAPP_MODEL_ID=%r GEMINI_MODELS=%r' % (
    os.environ.get('NVIDIA_ENABLED'), os.environ.get('NVIDIA_MODELS'),
    os.environ.get('WHATSAPP_MODEL_ID'), os.environ.get('GEMINI_MODELS')), flush=True)

# Diagnostico de PROVEDORES: lista TODA variavel _ENABLED / _MODELS / _API_KEY
# que o sistema enxerga no boot. O registro so cria um provedor quando existe
# {PREFIXO}_ENABLED=true + {PREFIXO}_API_KEY + {PREFIXO}_MODELS. Se o Claude nao
# entra, aqui mostra na hora o motivo (escrita errada tipo 'ANTROPIC', chave
# ausente, variavel que nao chegou, etc.). A chave aparece MASCARADA (so o
# tamanho e os primeiros caracteres), nunca o valor inteiro.
print('BOOT providers:', flush=True)
for _k in sorted(os.environ):
    _ku = _k.upper()
    if _ku.endswith('_ENABLED') or _ku.endswith('_MODELS') or _ku.endswith('_API_BASE') or _ku.endswith('_ENDPOINT'):
        print('   %s = %r' % (_k, os.environ.get(_k)), flush=True)
    elif _ku.endswith('_API_KEY'):
        _v = os.environ.get(_k) or ''
        print('   %s = <%d caracteres, começa com %r>' % (_k, len(_v), _v[:7]), flush=True)

# Contorna host (ShardCloud) que as vezes NAO aplica a troca de NVIDIA_MODELS,
# deixando um modelo antigo/morto preso. O WHATSAPP_MODEL_ID atualiza normal,
# entao ele vira a fonte da verdade: registra o modelo escolhido no provedor
# NVIDIA (descarta o modelo preso). Precisa rodar ANTES do Data Formulator ler
# o ambiente e montar o registro.
#
# A API da NVIDIA (integrate.api.nvidia.com) serve modelos de VARIOS provedores
# pelo MESMO endpoint. Entao qualquer id no formato 'org/modelo' vale aqui:
# 'nvidia/nemotron-...', 'zai/glm-5-3-flash', 'minimaxai/minimax-m3.1-flash',
# 'stepfun-ai/step-5' etc. Basta o '/' no id (modelos Gemini nao tem '/', entao
# nao sao afetados). Assim da pra testar qualquer modelo do build.nvidia.com
# trocando SO o WHATSAPP_MODEL_ID.
#
# IMPORTANTE: NAO sequestra o modelo se ele ja pertence a OUTRO provedor (ex.:
# um modelo listado em OPENROUTER_MODELS como 'google/gemma-...:free'). Sem essa
# guarda, o BOOT fix jogaria um modelo do OpenRouter no endpoint da NVIDIA -> 404.
# So forca NVIDIA_MODELS quando o modelo nao esta em nenhum outro *_MODELS.
_wmid = (os.environ.get('WHATSAPP_MODEL_ID') or '').strip()
if '/' in _wmid and (os.environ.get('NVIDIA_ENABLED') or '').strip().lower() == 'true':
    _em_outro = False
    for _k, _v in os.environ.items():
        if _k.upper().endswith('_MODELS') and _k.upper() != 'NVIDIA_MODELS':
            if _wmid in [m.strip() for m in (_v or '').split(',') if m.strip()]:
                _em_outro = True
                break
    _cur = [m.strip() for m in (os.environ.get('NVIDIA_MODELS') or '').split(',') if m.strip()]
    if not _em_outro and _wmid not in _cur:
        os.environ['NVIDIA_MODELS'] = _wmid
        print('BOOT fix -> NVIDIA_MODELS forcado para %r (via WHATSAPP_MODEL_ID)' % _wmid, flush=True)

if version('data_formulator') != '0.8.0b1':
    raise RuntimeError('Esta integração requer Data Formulator 0.8.0b1.')

# DF_PLUGIN_DIR = raiz do projeto: o Data Formulator carrega o
# google_sheets_data_loader.py como fonte de dados "Google Sheets" dentro da
# interface web. DATA_FORMULATOR_ENV_DIR resolve caminhos relativos do .env.
os.environ['DF_PLUGIN_DIR'] = str(ROOT)
os.environ['DATA_FORMULATOR_ENV_DIR'] = str(ROOT)
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
