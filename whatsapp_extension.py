"""Webhook oficial do WhatsApp, fila local e painel de configuração."""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path

import requests
from flask import request, Response, jsonify, render_template_string, session, redirect, url_for

from whatsapp_store import Store
from whatsapp_agent import ask, split_text, publish, read_config, phone_key, restore_from_google, model_is_provider

log = logging.getLogger('whatsapp')


def log_failure(stage, exc):
    """Localiza falhas sem registrar valores, perguntas, URLs ou credenciais."""
    import traceback
    frames = traceback.extract_tb(exc.__traceback__)
    location = ' > '.join(f'{Path(f.filename).name}:{f.lineno}:{f.name}' for f in frames[-6:])
    missing = ''
    if isinstance(exc, KeyError) and exc.args:
        field = str(exc.args[0])
        if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,70}', field):
            missing = f'; campo ausente={field}'
    # Mensagens de erro de importação citam apenas nomes de módulos/atributos,
    # nunca credenciais ou conteúdo de células: são seguras de registrar e
    # essenciais para diagnosticar falhas de dependência ou import parcial.
    if isinstance(exc, ImportError):
        missing += f'; detalhe={str(exc)[:200]}'
    log.error('%s: %s%s; local=%s', stage, type(exc).__name__, missing, location)


def env_true(name):
    return os.environ.get(name, '').lower() in ('true', '1', 'yes')


def same(left, right):
    return hmac.compare_digest(str(left).encode('utf-8'), str(right).encode('utf-8'))


def valid_signature(body, signature, secret):
    if not secret or not signature:
        return False
    expected = 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def incoming(payload, phone_id, now=None):
    now = time.time() if now is None else now
    items = []
    if payload.get('object') != 'whatsapp_business_account':
        return items
    for entry in payload.get('entry', []):
        for change in entry.get('changes', []):
            value = change.get('value') or {}
            if str((value.get('metadata') or {}).get('phone_number_id', '')) != phone_id:
                continue
            for msg in value.get('messages', []):
                sender = str(msg.get('from', ''))
                if not re.fullmatch(r'\d{8,20}', sender):
                    continue
                if msg.get('type') != 'text':
                    continue
                text = (msg.get('text') or {}).get('body', '')
                try:
                    received = float(msg.get('timestamp', 0))
                except (ValueError, TypeError):
                    continue
                message_id = msg.get('id')
                if (not isinstance(message_id, str) or not message_id or len(message_id) > 500
                        or not isinstance(text, str) or not text.strip() or len(text) > 8000
                        or received > now + 300 or now - received > 23 * 3600):
                    continue
                items.append({'id': message_id, 'sender': sender, 'body': text.strip(), 'received': received})
    return items


def send_text(sender, text, reply_to=None):
    token = os.environ['WHATSAPP_ACCESS_TOKEN']
    version = os.environ['WHATSAPP_GRAPH_VERSION']
    phone_id = os.environ['WHATSAPP_PHONE_NUMBER_ID']
    payload = {'messaging_product': 'whatsapp', 'to': sender, 'type': 'text',
               'text': {'body': text, 'preview_url': False}}
    if reply_to:
        payload['context'] = {'message_id': reply_to}
    response = requests.post(f'https://graph.facebook.com/{version}/{phone_id}/messages',
                             headers={'Authorization': f'Bearer {token}'}, json=payload, timeout=30)
    if response.status_code not in (200, 201):
        # Não inclui resposta bruta ou token em logs nem no painel.
        raise ValueError(f'Meta recusou o envio (HTTP {response.status_code}). Confira token, número e permissões.')
    data = response.json()
    messages = data.get('messages') or []
    if not messages or not messages[0].get('id'):
        raise requests.exceptions.RequestException('Meta não confirmou o ID da mensagem')
    return messages[0]['id']


class Worker:
    def __init__(self, app, root, store):
        self.app, self.root, self.store = app, Path(root), store
        self.wake = threading.Event()

    def start(self):
        # O mesmo bloqueio funciona em Windows e Linux e é liberado pelo SO
        # até em encerramento abrupto. Adquirir antes de recuperar a fila.
        from filelock import FileLock, Timeout
        self._guard = FileLock(str(getattr(self, '_lock_path', str(self.store.path) + '.worker.lock')))
        try:
            self._guard.acquire(timeout=0)
        except Timeout:
            raise RuntimeError('Já existe um servidor consumindo esta fila WhatsApp. Feche a outra instância; use um worker no Render.') from None
        self.store.recover()
        threading.Thread(target=self.run, name='whatsapp-worker', daemon=True).start()

    def run(self):
        while True:
            job = None
            try:
                job = self.store.next_job()
                if job:
                    self.process(job)
                    continue
            except Exception as exc:
                log_failure('Fila WhatsApp', exc)
                if job:
                    self.store.mark_job(job['id'], 'failed', 'Falha interna da fila; confira o diagnóstico no terminal.')
            self.wake.wait(2)
            self.wake.clear()

    def process(self, job):
        if time.time() - job['received'] > 23 * 3600:
            self.store.mark_job(job['id'], 'expired', 'Mensagem fora da janela de resposta')
            return
        if job['status'] != 'ready':
            try:
                if env_true('WHATSAPP_RESTORE_FROM_GOOGLE'):
                    restore_from_google(self.root)
                answer, history, key, revision = ask(self.app, self.root, self.store, job['sender'], job['body'])
                self.store.ready(job['id'], split_text(answer), key, revision, history)
            except Exception as exc:
                log_failure('Análise WhatsApp interrompida', exc)
                # Nunca manda uma resposta de dados parcial ou inventada.
                timeout = isinstance(exc, TimeoutError) or 'timeout' in type(exc).__name__.casefold()
                overloaded = type(exc).__name__ == 'ServiceOverloadedError'
                notice = ('O serviço da IA está temporariamente sobrecarregado e não conseguiu concluir a consulta. Tente novamente em instantes. Sua pergunta não precisa ser alterada.' if overloaded else
                          'A IA demorou além do tempo permitido e a consulta foi interrompida. Tente novamente em instantes. Sua pergunta não precisa ser reformulada.' if timeout else
                          'Não consegui concluir essa consulta agora. Tente novamente em instantes. Se persistir, informe o administrador.')
                created = self.store.error_reply(job['id'], notice)
                if created:
                    job['error'] = 'A análise não concluiu; enviada uma orientação ao usuário.'
        for part in self.store.pending_parts(job['id']):
            self.store.mark_part(job['id'], part['seq'], 'sending')
            try:
                meta_id = send_text(job['sender'], part['body'], reply_to=job['id'] if part['seq'] == 0 else None)
            except requests.exceptions.RequestException:
                self.store.mark_part(job['id'], part['seq'], 'uncertain', error='Sem confirmação do envio; não reenviado para evitar duplicidade.')
                self.store.mark_job(job['id'], 'uncertain', 'Verifique no WhatsApp se a mensagem chegou.')
                return
            except ValueError as exc:
                self.store.mark_part(job['id'], part['seq'], 'failed', error=str(exc))
                self.store.mark_job(job['id'], 'failed', str(exc))
                return
            self.store.mark_part(job['id'], part['seq'], 'accepted', meta_id=meta_id)
        self.store.mark_job(job['id'], 'analysis_failed' if job.get('error') else 'accepted', job.get('error'))


PANEL = '''<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chatbot WhatsApp — Data Formulator</title><style>
body{font:16px system-ui;max-width:960px;margin:35px auto;padding:0 20px;color:#17324d;background:#f6f8fb}section{background:white;padding:24px;margin:18px 0;border-radius:12px}label{display:block;margin:12px 0}select,textarea,button{font:inherit;padding:10px;max-width:100%;box-sizing:border-box}textarea{width:100%}button{background:#1463a6;color:white;border:0;border-radius:6px;cursor:pointer}td,th{text-align:left;padding:9px;border-bottom:1px solid #ddd}small{color:#53677d}.error{color:#a52222}</style>
<a href="/chatbot-whatsapp">Voltar ao Chatbot WhatsApp</a><h1>Configurações do Chatbot WhatsApp</h1>
<p>Prepare e confira os dados no sistema. Depois publique somente as tabelas que o WhatsApp poderá consultar.</p>
<section><h2>Origem da planilha</h2><form method="get"><label>De onde deseja importar?<select name="source" onchange="this.form.submit()"><option value="sessions" selected>Sessões do sistema</option><option value="google">Conta de serviço Google</option></select></label><noscript><button>Selecionar origem</button></noscript></form></section>
{% if message %}<section class="error">{{message}}</section>{% endif %}
<section><h2>1. Escolha a sessão com a planilha pronta</h2>
{% if not workspaces %}<p class="error">Nenhuma sessão local encontrada. Este servidor usa a <a href="/integracoes/whatsapp?source=google">conta de serviço Google</a> como origem dos dados.</p>{% endif %}
<form method="get"><select name="workspace" required><option value="">Selecione</option>{% for w in workspaces %}<option value="{{w.id}}" {% if w.id==selected %}selected{% endif %}>{{w.name}}</option>{% endfor %}</select> <button>Abrir tabelas</button></form>
{% if tables %}<form method="post"><input type="hidden" name="csrf" value="{{csrf}}"><input type="hidden" name="workspace" value="{{selected}}">
<h2>2. Tabelas que serão usadas</h2>{% for t in tables %}<label><input type="checkbox" name="tables" value="{{t.name}}" checked> {{t.name}} <small>({{t.rows}} registros)</small></label>{% endfor %}
<label>Modelo NVIDIA do Chatbot <select name="model_id" required>{% for m in models %}<option value="{{m.id}}">{{m.model}}</option>{% endfor %}</select></label>
{% if not models %}<p class="error">Cadastre um modelo NVIDIA no Data Formulator para publicar o Chatbot WhatsApp.</p>{% endif %}
<label>Orientações adicionais para responder sobre os dados<textarea name="rules" rows="5" maxlength="10000" placeholder="Explique aqui regras próprias da sua planilha.">{{config.get('rules','')}}</textarea></label>
<p>Publicar cria uma cópia para consulta. Não modifica sua planilha. Se atualizar os dados no sistema, publique novamente. Uma nova publicação reinicia a memória do WhatsApp no próximo contato.</p>
<button>Publicar dados para o WhatsApp</button></form>{% endif %}</section>
{% if config.get('revision') %}<section><h2>Teste antes de conectar o número</h2><form method="post" action="/integracoes/whatsapp/testar"><input type="hidden" name="csrf" value="{{csrf}}"><label>Pergunta de teste<textarea name="question" maxlength="8000" rows="3" required></textarea></label><button>Testar o agente sem enviar WhatsApp</button></form><small>Usa sua API de IA e uma conversa de teste separada. Não envia mensagem para ninguém.</small></section>{% endif %}
<section><h2>Estado das conexões</h2><p>WhatsApp/Meta: <strong>{{'configurado' if enabled else 'não configurado'}}</strong></p>
<p>Conta de serviço Google: <strong>{{google_status}}</strong></p><p>IA do Chatbot: <strong>{{nvidia_status}}</strong></p>
<p>Data Formulator original: Groq — <strong>{{groq_status}}</strong></p>
<p>Tabelas publicadas: {{config.get('tables',[])|join(', ') or 'nenhuma'}}</p>
<p>Webhook da Meta: <code>{{webhook}}</code></p><p>As chaves e tokens ficam nas variáveis do servidor. Não cole credenciais nesta tela.</p>
<p>Mensagens: {{stats}}</p><small>accepted = aceito pela API da Meta; não confirma entrega. uncertain = envio sem confirmação, sem reenvio automático.</small>
<table><tr><th>Horário</th><th>Estado</th><th>Detalhe</th></tr>{% for job in jobs %}<tr><td>{{job.when}}</td><td>{{job.status}}</td><td>{{job.error or ''}}</td></tr>{% endfor %}</table>
<p>Envie <code>/reiniciar</code> no WhatsApp para começar outra conversa. Esta versão recebe mensagens de texto.</p></section></html>'''


GOOGLE_PANEL = '''<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Google Sheets</title>
<style>body{font:16px system-ui;max-width:850px;margin:30px auto;padding:20px;background:#f6f8fb}section{background:white;padding:20px;margin:18px 0;border-radius:10px}label{display:block;margin:12px 0}input,select,button,textarea{font:inherit;padding:10px;max-width:100%;box-sizing:border-box}textarea{width:100%}button{cursor:pointer}p{line-height:1.5}</style>
<a href="/chatbot-whatsapp">Voltar ao Chatbot WhatsApp</a><h1>Configurações do Chatbot WhatsApp</h1>
<section><h2>Origem da planilha</h2><form method="get" action="/integracoes/whatsapp"><label>De onde deseja importar?<select name="source" onchange="this.form.submit()"><option value="sessions">Sessões do sistema</option><option value="google" selected>Conta de serviço Google</option></select></label><noscript><button>Selecionar origem</button></noscript></form></section>
<p>Usa o JSON configurado no servidor. Não é necessário enviar a chave nesta tela.</p>
{% if message %}<section role="alert">{{message}}</section>{% endif %}
<section><h2>1. Escolha a planilha</h2>
<form method="post" action="/integracoes/whatsapp?source=google"><input type="hidden" name="csrf" value="{{csrf}}"><button name="action" value="books">Atualizar lista de planilhas</button></form>
{% if books %}<form method="post" action="/integracoes/whatsapp?source=google"><input type="hidden" name="csrf" value="{{csrf}}"><label>Planilha<select name="book_id" required>{% for b in books %}<option value="{{b.id}}">{{b.name}}</option>{% endfor %}</select></label><button name="action" value="open">Abrir abas</button></form>{% endif %}
<p>Ou abra diretamente pelo ID, sem precisar listar no Drive:</p><form method="post" action="/integracoes/whatsapp?source=google"><input type="hidden" name="csrf" value="{{csrf}}"><label>ID da planilha<input name="book_id" value="{{book_id}}" required maxlength="200"></label><button name="action" value="open">Abrir pelo ID</button></form>
<p>Para a busca, habilite as APIs Google Drive e Google Sheets. A planilha deve estar compartilhada com o client_email do JSON.</p></section>
{% if tabs %}<section><h2>2. Selecione as abas</h2><form method="post" action="/integracoes/whatsapp?source=google"><input type="hidden" name="csrf" value="{{csrf}}"><input type="hidden" name="book_id" value="{{book_id}}">
<label><input type="checkbox" checked onchange="this.form.querySelectorAll('input[name=tabs]').forEach(x=>x.checked=this.checked)"> Todas as abas</label>
{% for t in tabs %}<label><input type="checkbox" name="tabs" value="{{t.id}}" checked> {{t.name}}</label>{% endfor %}
<label>Modelo NVIDIA do Chatbot<select name="model_id" required>{% for m in models %}<option value="{{m.id}}">{{m.model}}</option>{% endfor %}</select></label>
{% if not models %}<p role="alert">Nenhum modelo NVIDIA está configurado. Configure a API NVIDIA no Data Formulator antes de publicar o Chatbot.</p>{% endif %}
<label>Orientações adicionais<textarea name="rules" maxlength="10000" rows="4"></textarea></label><button name="action" value="publish">Importar e publicar para WhatsApp</button></form></section>{% endif %}
<p>A importação cria uma cópia para consulta; não altera o Google Sheets. Para atualizar os dados, importe novamente. Abas vazias são ignoradas.</p>
<p>No Render gratuito, esta escolha pode se perder ao reiniciar. Para recarga automática, configure GOOGLE_SPREADSHEET_ID, WHATSAPP_SHEET_NAMES (vazio = todas) e WHATSAPP_RESTORE_FROM_GOOGLE=true nas variáveis do servidor.</p></html>'''


CHATBOT_HOME = '''<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chatbot WhatsApp</title><style>
body{font:16px system-ui;max-width:900px;margin:36px auto;padding:0 20px;color:#17324d;background:#f6f8fb}a{color:inherit;text-decoration:none}.card{display:block;background:white;padding:26px;border-radius:16px;border:1px solid #dce5ec;box-shadow:0 8px 24px #16334d12;margin-top:24px}.card:hover{border-color:#176943}.pill{display:inline-block;padding:5px 10px;border-radius:20px;background:#e8f5ed;color:#176943;font-weight:600}.muted{color:#53677d}</style>
<a href="/">Voltar ao Data Formulator</a><h1>Chatbot WhatsApp</h1><p class="muted">Conexões, planilha e configurações do chatbot.</p>
<a class="card" href="/integracoes/whatsapp"><span class="pill">{{'Conectado' if connected else 'Configurar conexão'}}</span><h2>Chatbot conectado</h2><p>Ver o status do WhatsApp/Meta, Google Sheets e NVIDIA, além de abrir ou trocar a planilha da conta de serviço.</p><strong>Abrir configurações →</strong></a></html>'''


def install(app):
    # No Windows, o sandbox usa multiprocessing/spawn, que reimporta a entrada
    # do servidor. Um processo de análise não deve criar rotas, cache ou worker.
    import multiprocessing
    if multiprocessing.current_process().name != 'MainProcess':
        return None
    if app.extensions.get('whatsapp_bridge'):
        return app.extensions['whatsapp_bridge']
    for canonical, alias in [('WHATSAPP_ACCESS_TOKEN', 'WHATSAPP_TOKEN'),
                             ('WHATSAPP_GRAPH_VERSION', 'GRAPH_API_VERSION')]:
        if not os.environ.get(canonical) and os.environ.get(alias):
            os.environ[canonical] = os.environ[alias]
    home = Path(os.environ.get('DATA_FORMULATOR_HOME', str(Path.home() / '.data_formulator'))).resolve()
    home.mkdir(parents=True, exist_ok=True)
    root = home / 'whatsapp'
    if env_true('WHATSAPP_CACHE_ONLY'):
        import tempfile
        import atexit
        import shutil
        root = Path(tempfile.mkdtemp(prefix='df-whatsapp-cache-'))
        atexit.register(shutil.rmtree, root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    store = Store(root / 'messages.sqlite3')
    online = os.environ.get('HOST', '127.0.0.1') not in ('127.0.0.1', 'localhost', '::1')
    user, password = os.environ.get('WEB_ADMIN_USER', ''), os.environ.get('WEB_ADMIN_PASSWORD', '')
    if online and (not user or len(password) < 12):
        raise ValueError('Defina WEB_ADMIN_USER e WEB_ADMIN_PASSWORD com ao menos 12 caracteres antes de abrir online.')
    enabled = env_true('WHATSAPP_ENABLED')
    if enabled:
        required = ('WHATSAPP_ACCESS_TOKEN', 'WHATSAPP_PHONE_NUMBER_ID', 'WHATSAPP_GRAPH_VERSION', 'WHATSAPP_VERIFY_TOKEN', 'META_APP_SECRET', 'FLASK_SECRET_KEY')
        if any(not os.environ.get(name, '').strip() for name in required):
            missing = [name for name in required if not os.environ.get(name, '').strip()]
            raise ValueError('Faltam configurações do WhatsApp: ' + ', '.join(missing))
        if not re.fullmatch(r'v\d+\.\d+', os.environ['WHATSAPP_GRAPH_VERSION']) or not re.fullmatch(r'\d+', os.environ['WHATSAPP_PHONE_NUMBER_ID']):
            raise ValueError('Confira WHATSAPP_GRAPH_VERSION (ex.: vXX.0) e WHATSAPP_PHONE_NUMBER_ID (ID numérico da Meta).')
    @app.before_request
    def protect_panel():
        if request.path in ('/healthz', '/webhooks/whatsapp', '/api/chat'):
            return None
        if user and password:
            auth = request.authorization
            if not auth or not same(auth.username or '', user) or not same(auth.password or '', password):
                return Response('Acesso administrativo protegido.', 401, {'WWW-Authenticate': 'Basic realm="Data Formulator"'})

    @app.after_request
    def integration_link(response):
        if request.path == '/' and response.status_code == 200 and response.mimetype == 'text/html':
            # O index original vem de send_from_directory (FileWrapper/passthrough).
            response.direct_passthrough = False
            html = response.get_data(as_text=True)
            link = '<a href="/chatbot-whatsapp" style="position:fixed;bottom:16px;left:48px;z-index:9999;background:#176943;color:white;padding:9px 14px;border-radius:7px;text-decoration:none;font:14px system-ui">Chatbot WhatsApp</a>'
            response.set_data(html.replace('</body>', link + '</body>'))
            response.headers.pop('ETag', None)
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/healthz')
    def wa_health():
        return jsonify(status='ok')

    @app.route('/webhooks/whatsapp', methods=['GET', 'POST'])
    def wa_webhook():
        if not enabled:
            return Response('WhatsApp desativado', 503)
        if request.method == 'GET':
            valid = request.args.get('hub.mode') == 'subscribe' and same(
                request.args.get('hub.verify_token', ''), os.environ['WHATSAPP_VERIFY_TOKEN'])
            return Response(request.args.get('hub.challenge', '') if valid else 'Não autorizado', 200 if valid else 403)
        if request.content_length and request.content_length > 1024 * 1024:
            return Response('Payload grande demais', 413)
        raw = request.get_data()
        if len(raw) > 1024 * 1024:
            return Response('Payload grande demais', 413)
        if not valid_signature(raw, request.headers.get('X-Hub-Signature-256', ''), os.environ['META_APP_SECRET']):
            return Response('Assinatura inválida', 403)
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError()
            items = incoming(payload, os.environ['WHATSAPP_PHONE_NUMBER_ID'])
            store.enqueue(items)
        except (ValueError, TypeError, AttributeError):
            return Response('Payload inválido', 400)
        except OverflowError:
            return Response('Fila cheia, tente novamente', 503)
        worker.wake.set()
        return jsonify(status='received')

    def available_workspaces():
        found = {}
        users = home / 'users'
        if users.exists():
            for manifest in users.glob('*/workspaces/*/workspace.yaml'):
                path = manifest.parent.resolve()
                if path.is_relative_to(users):
                    key = str(path.relative_to(home)).replace('\\', '/')
                    found[key] = path
        return found

    def provider_models(registry, provider):
        models = []
        for model in registry.list_public():
            config = registry.get_config(model['id'])
            if config and model_is_provider(config, provider):
                models.append(model)
        return models

    def google_account_status():
        configured = os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE', '').strip()
        if not configured:
            return 'não configurada'
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = Path(os.environ.get('DATA_FORMULATOR_ENV_DIR', '.')) / path
        return 'arquivo configurado' if path.is_file() else 'arquivo não encontrado'

    def connection_status(config, registry):
        model_config = registry.get_config(config.get('model_id', '')) if config.get('model_id') else None
        nvidia = 'modelo NVIDIA publicado' if model_config and model_is_provider(model_config, 'nvidia') else 'aguardando modelo NVIDIA'
        groq = 'modelo Groq disponível no Data Formulator' if provider_models(registry, 'groq') else 'Groq não localizado no registro de modelos'
        return google_account_status(), nvidia, groq

    @app.get('/chatbot-whatsapp')
    def wa_home():
        try:
            config = read_config(root)
        except ValueError:
            config = {}
        connected = bool(enabled and config.get('revision'))
        return render_template_string(CHATBOT_HOME, connected=connected)

    @app.route('/integracoes/whatsapp', methods=['GET', 'POST'])
    def wa_panel():
        from data_formulator.model_registry import model_registry
        workspaces = available_workspaces()
        # Este servidor não inicia a UI web do Data Formulator, então não há
        # "sessões do sistema" para importar. A conta de serviço Google é o
        # único caminho. Só mostra o seletor de sessões se ?source=sessions
        # for pedido explicitamente (instalação local que já tenha sessões).
        if request.values.get('source') == 'google' or (
                request.values.get('source') != 'sessions' and not workspaces):
            return wa_google()
        selected = request.values.get('workspace', '')
        message = ''
        config = {}
        try:
            config = read_config(root)
        except ValueError:
            pass
        csrf = session.setdefault('wa_csrf', secrets.token_urlsafe(32))
        tables = []
        if selected in workspaces:
            try:
                # O painel inicial e o teste da planilha publicada não precisam
                # importar o Workspace do editor de sessões locais.
                from data_formulator.datalake.workspace import Workspace
                workspace = Workspace(identity_id='whatsapp:editor', workspace_path=workspaces[selected])
                tables = [{'name': name, 'rows': workspace.get_table_metadata(name).row_count} for name in workspace.list_tables()]
            except Exception as exc:
                log_failure('Leitura da sessão no painel', exc)
                message = 'Não foi possível abrir essa sessão. Consulte os logs. A planilha já publicada continua configurada.'
                if request.method == 'POST':
                    return Response('Não foi possível abrir a sessão selecionada. Reabra o painel e consulte os logs.', 503)
        if request.method == 'POST':
            if not same(request.form.get('csrf', ''), csrf):
                return Response('Reabra a página e tente novamente.', 403)
            model_id = request.form.get('model_id', '')
            chosen = request.form.getlist('tables')
            model_config = model_registry.get_config(model_id)
            if selected not in workspaces or not model_config or not model_is_provider(model_config, 'nvidia') or len(request.form.get('rules', '')) > 10000:
                return Response('Confira a sessão, o modelo e as orientações.', 400)
            try:
                config = publish(root, workspaces[selected], chosen, model_id, request.form.get('rules', ''))
                message = 'Dados publicados. Somente essas tabelas serão copiadas para cada conversa do WhatsApp.'
            except Exception as exc:
                log.warning('Publicação WhatsApp falhou: %s', type(exc).__name__)
                message = 'Não foi possível publicar. Confira as tabelas e os logs do sistema.'
        from datetime import datetime
        jobs = store.latest()
        for job in jobs:
            job['when'] = datetime.fromtimestamp(job['received']).strftime('%d/%m %H:%M')
        google_status, nvidia_status, groq_status = connection_status(config, model_registry)
        return render_template_string(PANEL, message=message, workspaces=[{'id': k, 'name': k} for k in sorted(workspaces)],
                                      selected=selected, tables=tables, models=provider_models(model_registry, 'nvidia'), config=config,
                                      csrf=csrf, enabled=enabled, webhook=request.url_root.rstrip('/') + '/webhooks/whatsapp',
                                      stats=store.stats(), jobs=jobs, google_status=google_status,
                                      nvidia_status=nvidia_status, groq_status=groq_status)

    @app.route('/integracoes/whatsapp/google', methods=['GET', 'POST'])
    def wa_google():
        from data_formulator.model_registry import model_registry
        csrf = session.setdefault('wa_csrf', secrets.token_urlsafe(32))
        books, tabs, message = [], [], ''
        book_id = request.form.get('book_id', '').strip()
        if request.method == 'POST' and not same(request.form.get('csrf', ''), csrf):
            return Response('Reabra a página e tente novamente.', 403)
        if request.method in ('GET', 'POST'):
            try:
                from google_sheets_data_loader import GoogleSheetsDataLoader
                action = request.form.get('action') if request.method == 'POST' else 'books'
                if action == 'books':
                    # Não prende a listagem ao GOOGLE_SPREADSHEET_ID do .env.
                    loader = GoogleSheetsDataLoader({'spreadsheet_id': ''})
                    for node in loader.ls():
                        match = re.search(r'\[([A-Za-z0-9_-]+)\]$', node.path[0])
                        if match:
                            books.append({'id': match.group(1), 'name': node.name})
                    if not books:
                        message = 'Nenhuma planilha encontrada. Compartilhe uma planilha com o client_email da conta de serviço.'
                elif action in ('open', 'publish'):
                    if not re.fullmatch(r'[A-Za-z0-9_-]{5,200}', book_id):
                        raise ValueError('Informe somente o ID da planilha, não o link completo.')
                    loader = GoogleSheetsDataLoader({'spreadsheet_id': book_id})
                    parents = loader.ls()
                    if not parents:
                        raise ValueError('Planilha não encontrada para essa conta de serviço.')
                    nodes = loader.ls(parents[0].path)
                    tabs = [{'id': n.metadata['_source_name'], 'name': n.name} for n in nodes]
                    if action == 'publish':
                        model_id = request.form.get('model_id', '')
                        rules = request.form.get('rules', '')
                        selected = set(request.form.getlist('tabs'))
                        if not selected or selected - {t['id'] for t in tabs}:
                            raise ValueError('Selecione ao menos uma aba disponível.')
                        model_config = model_registry.get_config(model_id)
                        if not model_config or not model_is_provider(model_config, 'nvidia') or len(rules) > 10000:
                            raise ValueError('Selecione um modelo NVIDIA para o Chatbot e confira as orientações.')
                        import tempfile
                        from data_formulator.datalake.workspace import Workspace
                        with tempfile.TemporaryDirectory(prefix='df-google-import-') as staging:
                            workspace = Workspace(identity_id='whatsapp:google-editor', workspace_path=Path(staging))
                            imported = []
                            for node in nodes:
                                if node.metadata['_source_name'] not in selected:
                                    continue
                                table = loader.fetch_data_as_arrow(node.metadata['_source_name'])
                                if table.num_columns == 0 or table.num_rows == 0:
                                    continue
                                meta = workspace.write_parquet_from_arrow(table, node.name)
                                meta.description = node.metadata.get('description', '')
                                workspace.add_table_metadata(meta)
                                imported.append(meta.name)
                            if not imported:
                                raise ValueError('As abas selecionadas estão vazias.')
                            publish(root, Path(staging), imported, model_id, rules)
                        return redirect('/integracoes/whatsapp')
                else:
                    raise ValueError('Escolha buscar, abrir ou importar a planilha.')
            except Exception as exc:
                log_failure('Importação Google no painel', exc)
                # Mensagens do conector são próprias, sem chaves ou conteúdo de células.
                safe_errors = ('Informe somente', 'Selecione ao menos', 'Confira o modelo', 'As abas selecionadas',
                               'Planilha não encontrada', 'Confira GOOGLE_SERVICE_ACCOUNT_FILE', 'O JSON não é',
                               'Google recusou', 'Falha de conexão')
                message = str(exc) if isinstance(exc, ValueError) and str(exc).startswith(safe_errors) else 'Não foi possível importar. Confira o JSON, compartilhamento e as APIs Google Sheets e Drive; veja o diagnóstico no terminal.'
        return render_template_string(GOOGLE_PANEL, csrf=csrf, books=books, tabs=tabs,
                                      book_id=book_id, message=message, models=provider_models(model_registry, 'nvidia'))

    @app.post('/integracoes/whatsapp/testar')
    def wa_test_agent():
        if not same(request.form.get('csrf', ''), session.get('wa_csrf', '')) or not session.get('wa_csrf'):
            return Response('Reabra o painel de integração e tente novamente.', 403)
        question = request.form.get('question', '').strip()
        if not question or len(question) > 8000:
            return Response('Digite uma pergunta de até 8000 caracteres.', 400)
        # A conversa de teste também é serializada; duas abas do painel não misturam resultados.
        with test_lock:
            try:
                answer, history, key, revision = ask(app, root, store, 'painel-admin-teste', question)
                store.save_history(key, revision, history)
            except Exception as exc:
                log_failure('Teste do agente falhou', exc)
                answer = 'O agente não concluiu a análise. Confira os logs do Data Formulator. Nenhuma mensagem foi enviada.'
        return render_template_string('''<!doctype html><html lang="pt-BR"><meta charset="utf-8"><title>Teste do agente</title><body style="font:16px system-ui;max-width:900px;margin:30px auto;padding:20px"><a href="/integracoes/whatsapp">Voltar à integração</a><h1>Teste do agente</h1><h2>Sua pergunta</h2><p>{{question}}</p><h2>Resposta</h2><div style="white-space:pre-wrap">{{answer}}</div><p>Nenhuma mensagem foi enviada ao WhatsApp.</p></body></html>''', question=question, answer=answer)

    @app.route('/api/chat', methods=['GET', 'POST'])
    def api_chat():
        # Cliente de terminal (ex.: PowerShell): manda a pergunta com um token
        # Bearer e recebe a resposta em JSON. Usa o MESMO motor do painel/WhatsApp
        # (a função ask()), então responde igual ao Data Formulator.
        token = (os.environ.get('API_CHAT_TOKEN', '').strip()
                 or os.environ.get('ADMIN_TOKEN', '').strip()
                 or os.environ.get('WHATSAPP_VERIFY_TOKEN', '').strip())
        if not token:
            return jsonify(erro='Defina API_CHAT_TOKEN no servidor.'), 503
        header = request.headers.get('Authorization', '')
        enviado = header[7:].strip() if header[:7].lower() == 'bearer ' else (
            request.args.get('token', '') or request.form.get('token', ''))
        if not same(enviado, token):
            return jsonify(erro='Não autorizado. Cabeçalho Authorization: Bearer <token>.'), 401
        corpo = request.get_json(silent=True) or {}
        pergunta = (request.args.get('q') or corpo.get('q') or corpo.get('pergunta')
                    or request.form.get('q') or '').strip()
        if not pergunta:
            return jsonify(erro='Envie a pergunta no parâmetro q.'), 400
        if len(pergunta) > 8000:
            return jsonify(erro='Pergunta de até 8000 caracteres.'), 400
        # No plano gratuito o disco é efêmero: se ainda não há publicação,
        # reconstrói a planilha a partir do Google antes de responder.
        try:
            if not (root / 'config.json').exists() and env_true('WHATSAPP_RESTORE_FROM_GOOGLE'):
                restore_from_google(root)
        except Exception as exc:
            log_failure('api/chat: restauração da planilha falhou', exc)
            return jsonify(erro='Não foi possível carregar a planilha. Confira os logs.', resposta=''), 500
        with test_lock:
            try:
                answer, history, key, revision = ask(app, root, store, 'api-terminal', pergunta)
                store.save_history(key, revision, history)
            except Exception as exc:
                log_failure('api/chat: agente falhou', exc)
                return jsonify(erro=str(exc), resposta=''), 500
        return jsonify(pergunta=pergunta, resposta=answer)

    test_lock = threading.Lock()
    worker = Worker(app, root, store)
    worker._lock_path = home / '.whatsapp-worker.lock'
    app.extensions['whatsapp_bridge'] = worker
    # Pré-carrega, uma única vez e no processo principal, todos os módulos
    # pesados usados sob demanda pelo painel, pelo agente e pela restauração.
    # Sem isto, a thread de restauração do Google e a primeira requisição do
    # painel importam os mesmos módulos do Data Formulator ao mesmo tempo no
    # startup e uma delas recebe um módulo parcialmente inicializado
    # (ImportError 'cannot import name ... circular import'), virando erro 500.
    # Importar aqui, antes de abrir requisições e antes da thread de
    # restauração, garante que todos já estejam completos em sys.modules.
    try:
        import google_sheets_data_loader  # noqa: F401
        from data_formulator.model_registry import model_registry  # noqa: F401
        from data_formulator.datalake.workspace import Workspace  # noqa: F401
        from data_formulator.analyst.agent import AnalystAgent  # noqa: F401
        from data_formulator.agents.client_utils import Client  # noqa: F401
        log.info('Conector Google e módulos do Data Formulator carregados no startup.')
    except Exception:
        log.exception('Falha ao pré-carregar módulos no startup')
    if env_true('WHATSAPP_RESTORE_FROM_GOOGLE'):
        def restore_on_startup():
            try:
                restore_from_google(root)
                log.info('Dados do WhatsApp disponíveis a partir do Google Sheets.')
            except Exception as exc:
                log_failure('Restauração do Google pendente', exc)

        # Não bloqueia o início do Gunicorn enquanto baixa e publica as abas.
        # A rotina continua lendo as mesmas abas e usando os mesmos dados.
        threading.Thread(target=restore_on_startup, name='whatsapp-google-restore', daemon=True).start()
    if enabled and os.environ.get('WHATSAPP_START_WORKER', 'true').lower() == 'true':
        worker.start()
    return worker
