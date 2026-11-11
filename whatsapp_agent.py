"""Usa o AnalystAgent original do Data Formulator, com workspace por número."""
import hashlib
import hmac
import json
import os
import shutil
import uuid
import re
import unicodedata
import logging
import time
import threading
from pathlib import Path

_GOOGLE_RESTORE_LOCK = threading.Lock()

RULES = '''Responda em português do Brasil, somente com fatos obtidos das tabelas disponíveis.
O canal é WhatsApp. Entregue texto legível e listas; não exponha Python, SQL, caminhos ou logs.
Não use tabelas Markdown, barras verticais, blocos de código ou títulos com #.
Organize listas com travessão (—), um item por bloco. Não use | como separador.
Comece pela resposta direta. Para listar registros, use um bloco por item, com nome
em destaque e campos em linhas separadas. Separe os itens com uma linha em branco.
Use negrito WhatsApp com um asterisco de cada lado. Inclua somente campos solicitados
ou indispensáveis para identificar o registro. Não termine oferecendo menus de opções.
Nomes de abas não comprovam status. Conte registros válidos consultados; linhas vazias,
cabeçalhos e totais não são novas obras. Antes de contar, exclua por código as linhas cujo
primeiro campo contenha TOTAL, SUBTOTAL, RESUMO ou variações (ex.: TOTAL GERAL, SUBTOTAL,
RESUMO GERAL); elas nunca entram na contagem. Não invente contagens, contratos ou sobreposições.
Ao mencionar a origem dos dados, escreva na própria frase “na aba/tabela *NOME*”, usando
o nome real da aba ou tabela consultada. Exemplo: “Na aba/tabela *EM_ANDAMENTO*, encontrei...”.
Ao contar obras físicas, use somente as abas EM_ANDAMENTO (consultar STATUS) e
PAVIMENTAÇÃO (RUA/SITUAÇÃO). EM_PROJETO, EM_LICITAÇÃO e PENDÊNCIAS nunca entram nessa
contagem. Não some projetos e licitações como obras físicas nem deduplique obras sem identificadores.
Consulte todas as tabelas relevantes antes de responder. Diferencie obra física, projeto e licitação.
Em EM_ANDAMENTO o nome da aba não determina o status: consulte STATUS.
PAVIMENTAÇÃO contém obras físicas; RUA identifica a obra e SITUAÇÃO é o status.
EM_PROJETO contém projetos técnicos: projeto concluído não é obra fisicamente concluída.
EM_LICITAÇÃO descreve fases da contratação, não conclusão física. STATUS e OBSERVAÇÕES são distintos.
PENDÊNCIAS e recursos próprios a pagar não são novas obras. A pagar não significa pago.
Valores contratado, executado e pago são diferentes. Vazio não significa zero.
REGRAS PARA CÁLCULOS:
- TODO número (contagem, soma, média, percentual, ranking) deve nascer da saída de
  código executado sobre os dados completos do recorte. É proibido calcular de cabeça,
  contar visualmente, usar preview/amostra ou repetir número do histórico sem recalcular
  por código. Se o código falhar, corrija-o e execute de novo; nunca responda com cálculo mental.
- Contagens: filtre por código, remova linhas de TOTAL/SUBTOTAL/RESUMO e linhas vazias,
  e use len(df_filtrado) ou df['coluna'].value_counts(). Havendo identificador de obra,
  conte obras distintas por ele; sem identificador, diga que a contagem é de registros,
  não de obras únicas.
- Conferência obrigatória em duas vias para toda resposta quantitativa: (1) calcule o
  número principal por código; (2) some os grupos (value_counts) e compare com o total
  do mesmo recorte. Se divergirem, investigue e corrija antes de responder. Sem
  confirmação, diga que não foi possível confirmar o número; nunca invente total "provável".
- Copie na resposta EXATAMENTE os números que apareceram na saída do código. Não
  recalcule nem "ajuste" números na redação; apenas formate (moeda, milhar) no texto final.
- Para dinheiro, preserve números já numéricos. Em textos brasileiros, remova R$,
  espaços e separadores de milhar e interprete a vírgula como decimal. Não aplique essa
  limpeza a valores já numéricos. Use Decimal a partir de strings ou centavos inteiros
  para somas monetárias; formate em reais só no fim.
- Mantenha ausentes e valores inválidos separados. Informe quantos registros possuem o
  valor solicitado e quantos não possuem; não preencha faltantes com zero.
- Confira filtros, status, abas, unidade e denominador. Se a pergunta tiver filtro
  (bairro, situação, mês, responsável, tipo...), conte SOMENTE as linhas que passam no
  filtro e escreva na resposta qual filtro foi aplicado. Não misture valor pago,
  executado e contratado. Não deduplique só pelo nome: contratos e etapas podem
  representar registros diferentes. Percentual agregado exige base explícita;
  não use média simples de percentuais para representar execução financeira total.
- Antes de concluir, confira por código o total contra a soma dos grupos que serão
  apresentados. Se os grupos puderem se sobrepor, explique isso; não force a soma a
  coincidir com a quantidade de obras distintas.
- Se a verificação falhar ou os dados forem insuficientes, explique a limitação e não
  apresente o total como confirmado.
Use o histórico para referências como "dessas obras". Não misture filtros de conversas distintas.
Se a pergunta for ambígua, faça uma pergunta curta de esclarecimento em texto.
Retorne todos os itens solicitados. Não invente dados ausentes nem corte listas silenciosamente.
Para listagens, materialize o conjunto em uma tabela de resultado quando necessário à continuidade.
Finalize com resposta textual explícita. Não encerre depois de apenas imprimir resultados em ferramentas.
Não busque dados na internet nem carregue outras fontes: utilize somente as tabelas publicadas neste workspace.
'''


class ServiceOverloadedError(RuntimeError):
    """O provedor informou explicitamente sobrecarga temporária."""


class EmptyLLMResponseError(RuntimeError):
    """O provedor respondeu, mas sem conteúdo utilizável."""


def classify_api_error(diagnostic):
    text = str(diagnostic).casefold()
    if 'temporarily overloaded' in text or 'service overloaded' in text:
        return 'overloaded'
    if any(term in text for term in ('readtimeout', 'apitimeouterror', 'read operation timed out', 'request timed out', 'timed out')):
        return 'timeout'
    return 'other'


def retry_llm_stream(factory, retryable, attempts=None, wait=time.sleep):
    """Reabre apenas a chamada à IA; descarta deltas de tentativas incompletas."""
    if attempts is None:
        try:
            attempts = int(os.environ.get('WHATSAPP_LLM_ATTEMPTS', '4'))
        except ValueError:
            attempts = 4
        attempts = max(2, min(attempts, 8))
    for attempt in range(attempts):
        buffered = []
        try:
            stream = factory()
            try:
                while True:
                    try:
                        buffered.append(next(stream))
                    except StopIteration as done:
                        response = done.value
                        break
            finally:
                stream.close()
        except Exception as exc:
            if attempt + 1 >= attempts or not retryable(exc):
                raise
            delay = min(2 ** attempt, 8)
            logging.getLogger('whatsapp').warning(
                'API de IA: falha temporária (%s); nova tentativa %d/%d em %ds.',
                type(exc).__name__, attempt + 2, attempts, delay)
            wait(delay)
            continue
        yield from buffered
        return response


def phone_key(sender):
    secret = os.environ.get('FLASK_SECRET_KEY', '').strip()
    if not secret:
        raise ValueError('FLASK_SECRET_KEY não configurada; reinicie o servidor após preencher a variável.')
    return hmac.new(secret.encode(), sender.encode(), hashlib.sha256).hexdigest()


def ai_timeout():
    try:
        value = int(os.environ.get('WHATSAPP_AI_TIMEOUT_SECONDS', '90'))
    except ValueError:
        raise ValueError('WHATSAPP_AI_TIMEOUT_SECONDS deve ser um número inteiro entre 10 e 300.') from None
    if not 10 <= value <= 300:
        raise ValueError('WHATSAPP_AI_TIMEOUT_SECONDS deve ficar entre 10 e 300.')
    return value


def split_text(text, limit=3500):
    """Mantém a lista inteira e divide abaixo do limite de mensagem do WhatsApp."""
    text = format_whatsapp(str(text))
    if not text:
        text = 'A análise terminou sem uma resposta utilizável. Por favor, reformule a pergunta.'
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind('\n\n', 0, limit)
        if cut < limit // 2:
            cut = text.rfind('\n', 0, limit)
        if cut < limit // 2:
            cut = text.rfind(' ', 0, limit)
        if cut < 1:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    if len(chunks) > 1:
        chunks = [f'({i}/{len(chunks)})\n{body}' for i, body in enumerate(chunks, 1)]
    return chunks


def format_whatsapp(text):
    """Converte tabelas Markdown em itens sem pedir outra chamada à IA."""
    text = re.sub(r'\*\*([^\n]+?)\*\*', r'*\1*', str(text))
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.M)
    lines, out, i = text.splitlines(), [], 0

    def cells(line):
        return [c.strip() for c in re.split(r'(?<!\\)\|', line.strip().strip('|'))]

    while i < len(lines):
        if i + 1 < len(lines) and '|' in lines[i]:
            headers, separator = cells(lines[i]), cells(lines[i + 1])
            if len(headers) == len(separator) and all(re.fullmatch(r':?-{3,}:?', c.replace(' ', '')) for c in separator):
                i += 2
                out.append('')
                number = 0
                while i < len(lines) and '|' in lines[i] and lines[i].strip():
                    values = cells(lines[i])
                    if len(values) != len(headers):
                        break
                    number += 1
                    out.append(f'— {values[0]}')
                    out.extend(f'{h.strip("*")}: {v}' for h, v in zip(headers[1:], values[1:]))
                    out.append('')
                    i += 1
                continue
        out.append(lines[i])
        i += 1
    result = '\n'.join(out)
    # Também trata respostas sem o cabeçalho completo de uma tabela Markdown.
    result = re.sub(r'(?m)^[ \t]*[|:\- \t]{3,}[ \t]*$', '', result)
    result = re.sub(r'(?m)^[ \t]*\|[ \t]*|[ \t]*\|[ \t]*$', '', result)
    result = re.sub(r'[ \t]*\\?\|[ \t]*', '\n— ', result)
    result = re.sub(r'(?m)^([ \t]*)[•\-*][ \t]+', r'\1— ', result)
    return re.sub(r'\n{3,}', '\n\n', result).strip()


def catalog_question(question):
    text = ''.join(c for c in unicodedata.normalize('NFD', question.casefold()) if not unicodedata.combining(c))
    return bool(re.search(r'\babas?\b', text) and re.search(r'\b(quais|liste|listar|disponiveis|disponivel)\b', text)
                and not re.search(r'\b(obras|concluidas|engenheiros|valores|ranking)\b', text))


def sheet_label(name):
    normalized = ''.join(c for c in unicodedata.normalize('NFD', name.casefold()) if not unicodedata.combining(c))
    for suffix, label in [('em_andamento', 'Em andamento'), ('em_licitacao', 'Em licitação'),
                           ('em_projeto', 'Em projeto'), ('pavimentacao', 'Pavimentação'),
                           ('pendencias', 'Pendências'), ('sheet6', 'Sheet6')]:
        if normalized == suffix or normalized.endswith('_' + suffix):
            return label
    return name.replace('_', ' ')


def table_text(df):
    import pandas as pd
    if df.empty:
        return 'Nenhum registro encontrado.'
    lines = []
    for i, (_, row) in enumerate(df.iterrows(), 1):
        fields = []
        for col, value in row.items():
            if isinstance(value, (list, dict, tuple)):
                value = json.dumps(value, ensure_ascii=False, default=str)
            elif pd.isna(value):
                continue
            label = str(col).replace('_', ' ').strip().capitalize()
            fields.append(f'{label}: {value}')
        if fields:
            lines.append('— ' + '\n'.join(fields))
    return '\n\n'.join(lines)


def read_config(root):
    try:
        config = json.loads((Path(root) / 'config.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise ValueError('Escolha as tabelas e salve a configuração em /integracoes/whatsapp.') from None
    if (not isinstance(config, dict)
            or not all(isinstance(config.get(k), str) and config[k].strip() for k in ('revision', 'model_id'))):
        raise ValueError('Publicação incompleta. Publique novamente as tabelas no painel WhatsApp.')
    import re
    if not re.fullmatch(r'[0-9a-f]{32}', config['revision']):
        raise ValueError('Identificador da publicação inválido. Publique novamente no painel.')
    return config


def model_is_provider(model_config, provider):
    """Identifica o provedor pelo endpoint ou pelo nome do modelo configurado."""
    model_config = model_config if isinstance(model_config, dict) else {}
    endpoint = str(model_config.get('endpoint') or model_config.get('api_base') or '').casefold()
    model = str(model_config.get('model') or '').casefold()
    provider_name = str(model_config.get('provider') or model_config.get('api_type')
                        or model_config.get('provider_display') or '').casefold()
    if provider == 'nvidia':
        return ('nvidia' in endpoint or model.startswith('nvidia/')
                or provider_name in ('nvidia', 'nvidia_nim', 'nvidia-nim'))
    if provider == 'groq':
        return ('groq' in endpoint or provider_name == 'groq')
    return provider_name == provider.casefold() or provider.casefold() in endpoint


def fallback_model(registry, current_id):
    """Escolhe o modelo de reserva para quando a IA principal falhar.

    Usa WHATSAPP_FALLBACK_MODEL_ID se definido; senão, o primeiro modelo Groq
    registrado. Devolve None quando não há reserva configurada."""
    requested = os.environ.get('WHATSAPP_FALLBACK_MODEL_ID', '').strip()
    candidates = [requested] if requested else []
    for item in registry.list_public():
        cfg = registry.get_config(item['id'])
        if cfg and str(cfg.get('provider_display') or '').casefold() == 'groq':
            candidates.append(item['id'])
    for model_id in candidates:
        if model_id == current_id:
            continue
        cfg = registry.get_config(model_id)
        if cfg and all(cfg.get(k) for k in ('endpoint', 'model')) and str(cfg.get('api_key') or '').strip():
            return cfg
    return None


def resolve_primary_model(registry, wanted):
    """Resolve o modelo principal de forma tolerante.

    Aceita o ID interno OU o nome do modelo (ex.:
    'nvidia/nemotron-3-ultra-550b-a55b' ou 'meta/llama-3.3-70b-instruct').
    Se nada casar (ID antigo/órfão depois de reiniciar), cai no primeiro
    modelo NVIDIA registrado com credenciais válidas. Assim o painel nunca
    precisa ser republicado só porque o servidor reiniciou.

    Devolve (config, motivo). motivo = 'id' | 'nome' | 'fallback-nvidia' |
    'nenhum'."""
    wanted = (wanted or '').strip()

    def usable(cfg):
        return bool(cfg) and all(cfg.get(k) for k in ('endpoint', 'model'))

    # 1) tentativa direta pelo ID interno publicado/configurado
    if wanted:
        cfg = registry.get_config(wanted)
        if usable(cfg):
            return cfg, 'id'

    # 2) varre os modelos registrados: casa pelo nome do modelo ou pelo ID,
    #    e guarda os NVIDIA válidos para o fallback.
    nvidia_valid = []
    for item in registry.list_public():
        cfg = registry.get_config(item.get('id'))
        if not usable(cfg):
            continue
        if wanted and (str(cfg.get('model') or '').strip() == wanted or item.get('id') == wanted):
            return cfg, 'nome'
        if model_is_provider(cfg, 'nvidia') and str(cfg.get('api_key') or '').strip():
            nvidia_valid.append(cfg)

    # 3) fallback: primeiro modelo NVIDIA válido registrado
    if nvidia_valid:
        return nvidia_valid[0], 'fallback-nvidia'
    return None, 'nenhum'


def prepare_conversation(root, key, revision, expired=False, reset=False):
    """Troca a geração da conversa sem precisar excluir pastas em uso."""
    parent = Path(root) / 'conversations'
    parent.mkdir(parents=True, exist_ok=True)
    pointer = parent / (key + '.current')
    current = parent / key  # Compatibilidade com conversas anteriores.
    try:
        name = pointer.read_text(encoding='utf-8').strip()
        if re.fullmatch(re.escape(key) + r'-[0-9a-f]{32}', name):
            current = parent / name
    except FileNotFoundError:
        pass
    try:
        matches = (current / 'published_revision.txt').read_text(encoding='utf-8') == revision
    except FileNotFoundError:
        matches = False
    if matches and not expired and not reset:
        return current
    target = parent / (key + '-' + uuid.uuid4().hex)
    temporary = parent / (key + '-' + uuid.uuid4().hex + '.tmp')
    try:
        shutil.copytree(Path(root) / 'snapshots' / revision, target)
        (target / 'published_revision.txt').write_text(revision, encoding='utf-8')
        temporary.write_text(target.name, encoding='utf-8')
        os.replace(temporary, pointer)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        temporary.unlink(missing_ok=True)
        raise
    # A geração antiga pode continuar aberta no sandbox do Windows. Sua remoção
    # não é necessária para usar a nova; o cache temporário é limpo pelo host.
    return target


def run_agent_attempt(app, workspace, chat_dir, identity, key, question, history, config, client):
    """Executa a análise completa com um modelo; devolve (resposta, passo de histórico).

    Levanta ServiceOverloadedError, TimeoutError ou EmptyLLMResponseError quando
    o problema é do provedor, permitindo ao chamador tentar o modelo de reserva."""
    from data_formulator.analyst.agent import AnalystAgent
    # Código gerado deve ter um destino temporário existente no próprio workspace.
    (chat_dir / 'tmp').mkdir(exist_ok=True)
    inputs = [{'name': name} for name in workspace.list_tables()]
    summary, report, result_tables, interact, freetext = '', [], [], '', []
    terminal_error = False
    timed_out = False
    overloaded = False
    llm_error_seen = False
    with app.test_request_context('/internal/whatsapp', headers={'Accept-Language': 'pt-BR'}):
        class WhatsAppAnalystAgent(AnalystAgent):
            # Mantém as tentativas do principal, incluindo falhas durante
            # a leitura do stream. Um único laço evita multiplicar 3 por 3.
            _MAX_LLM_RETRIES = 1
            _last_llm_error = None

            def _stream_llm(self, messages, tools):
                base = super()._stream_llm
                def transient(exc):
                    return classify_api_error(str(exc)) == 'overloaded' or self._is_transient_error(exc)
                try:
                    return (yield from retry_llm_stream(lambda: base(messages, tools), transient))
                except Exception as exc:
                    # O evento 'error' do agente só carrega mensagem genérica;
                    # guarda o erro real do provedor para o chamador conseguir
                    # classificar sobrecarga/timeout.
                    self._last_llm_error = exc
                    raise

        agent = WhatsAppAnalystAgent(client=client, workspace=workspace,
                             agent_exploration_rules=RULES + '\n' + config.get('rules', ''),
                             language_instruction='Responda em português do Brasil.',
                             max_iterations=6, max_repair_attempts=2, identity_id=identity)
        for event in agent.run(input_tables=inputs, user_question=question,
                               focused_thread=history, conversation_id=key):
            kind = event.get('type')
            content = event.get('content')
            if kind == 'completion':
                data = content or {}
                # Alguns modelos (ex.: Nemotron) devolvem o texto final em
                # 'text'/'message'/'content' em vez de 'summary'.
                summary = str(data.get('summary') or data.get('text')
                              or data.get('message') or data.get('content') or '').strip()
                terminal_error = event.get('status') not in ('success', None)
                llm_error_seen = llm_error_seen or event.get('status') == 'llm_error'
            elif kind == 'text_delta':
                if isinstance(content, str):
                    if event.get('channel') == 'report':
                        report.append(content)
                    else:
                        # Texto final "solto" do modelo (resposta sem acao
                        # commitada). Usado quando nao ha summary/report/resultado.
                        freetext.append(content)
            elif kind == 'result':
                result = (content or {}).get('result') or {}
                name = (result.get('virtual') or {}).get('table_name') or result.get('table_name') or result.get('output_table') or result.get('name')
                if name and name in workspace.list_tables():
                    result_tables.append(name)
            elif kind == 'interact':
                # O agente pode pedir esclarecimento via widget; converte para texto.
                payload = content or event.get('action_data') or {}
                questions = payload.get('questions', []) if isinstance(payload, dict) else []
                texts = [q if isinstance(q, str) else q.get('question', q.get('text', '')) for q in questions]
                interact = '\n'.join(filter(None, texts)) or 'Pode detalhar sua pergunta para eu consultar a planilha corretamente?'
            elif kind == 'error':
                terminal_error = True
                # O evento traz só mensagem genérica; o erro real fica em
                # _last_llm_error (setado quando as tentativas se esgotam).
                raw = getattr(agent, '_last_llm_error', None)
                diagnostic = ' '.join(filter(None, (
                    str(raw) if raw is not None else '',
                    str(content or event.get('message') or ''),
                ))).casefold()
                category = classify_api_error(diagnostic)
                overloaded = overloaded or category == 'overloaded'
                timed_out = timed_out or category == 'timeout'

    answer = summary or ''.join(report).strip() or ''.join(freetext).strip() or interact
    # Só apresenta resultados explicitamente emitidos, nunca uma tabela arbitrária antiga.
    narrative = answer
    if result_tables and not terminal_error:
        details = '\n\n'.join(table_text(workspace.read_data_as_df(name)) for name in dict.fromkeys(result_tables))
        answer = (answer + '\n\nDetalhes:\n' if answer else '') + details
    if terminal_error and overloaded:
        raise ServiceOverloadedError('O serviço de IA informou sobrecarga temporária.')
    if terminal_error and timed_out:
        raise TimeoutError('A API de IA excedeu o tempo de espera.')
    if not answer:
        # Resposta vazia do provedor (ou do agente): trata como falha do
        # provedor para o chamador tentar o modelo de reserva.
        raise EmptyLLMResponseError('O modelo de IA não devolveu conteúdo utilizável.')
    if terminal_error:
        raise RuntimeError('O agente não concluiu a análise com uma resposta utilizável.')
    answer = format_whatsapp(answer)
    step = {'user_question': question, 'agent_response': answer}
    if result_tables:
        last = result_tables[-1]
        frame = workspace.read_data_as_df(last)
        step.update(table_name=last, columns=list(frame.columns), row_count=len(frame))
    return answer, step


def ask(app, root, store, sender, question):
    from data_formulator.datalake.workspace import Workspace
    from data_formulator.analyst.agent import AnalystAgent
    from data_formulator.agents.client_utils import Client
    from data_formulator.model_registry import model_registry

    root = Path(root)
    config = read_config(root)
    key = phone_key(sender)
    revision = config['revision']
    identity = 'whatsapp:' + key
    previous = store.conversation_revision(key)
    ttl = int(os.environ.get('WHATSAPP_MEMORY_HOURS', '24')) * 3600
    import time
    expired = previous is None or time.time() - previous['updated'] > ttl
    reset = question.strip().casefold() in ('/reiniciar', 'nova conversa', '/reset')
    chat_dir = prepare_conversation(root, key, revision, expired=expired, reset=reset)
    history = [] if reset else store.history(key, revision, ttl)
    if reset:
        return ('Conversa reiniciada. Pode enviar sua pergunta sobre a planilha.', [], key, revision)

    workspace = Workspace(identity_id=identity, workspace_path=chat_dir)
    (chat_dir / 'tmp').mkdir(exist_ok=True)
    if catalog_question(question):
        lines = ['*Abas disponíveis para consulta*']
        for name in config.get('tables', []):
            if name in workspace.list_tables():
                meta = workspace.get_table_metadata(name)
                lines.append(f'— Na aba/tabela *{name}* ({sheet_label(name)}): {meta.row_count} linhas importadas')
        lines.append('As quantidades são linhas importadas; não representam necessariamente obras válidas.')
        answer = '\n\n'.join(lines)
        history.append({'user_question': question, 'agent_response': answer})
        return answer, history[-12:], key, revision

    # WHATSAPP_MODEL_ID (se definido) vira o modelo principal; senão, usa o
    # modelo publicado no painel. O outro provedor vira reserva automática.
    primary_id = os.environ.get('WHATSAPP_MODEL_ID', '').strip() or config['model_id']
    full_model, motivo = resolve_primary_model(model_registry, primary_id)
    if not full_model:
        raise ValueError('Nenhum modelo NVIDIA válido está registrado no servidor. '
                         'Confira NVIDIA_ENABLED, NVIDIA_API_KEY e NVIDIA_MODELS.')
    if motivo != 'id':
        logging.getLogger('whatsapp').info(
            'Modelo principal resolvido por %s: %s (pedido: %r).',
            motivo, full_model.get('model'), primary_id or '(painel)')

    def build_client(model_config):
        local_client = Client(model_config['endpoint'], model_config['model'], model_config.get('api_key'),
                              model_config.get('api_base'), model_config.get('api_version'))
        local_client.params.update(timeout=ai_timeout(), num_retries=0, max_retries=0)
        return local_client

    # Tentativas: modelo principal (WHATSAPP_MODEL_ID ou o publicado) primeiro;
    # se indisponível (sobrecarga, timeout ou resposta vazia), o modelo de
    # outro provedor assume.
    attempts = [(full_model, None)]
    reserve = fallback_model(model_registry, primary_id)
    if reserve:
        reserve_tag = str(reserve.get('provider_display') or 'reserva').casefold()
        attempts.append((reserve, reserve_tag))

    chat_logger = logging.getLogger('whatsapp')
    for model_config, tag in attempts:
        client = build_client(model_config)
        try:
            answer, step = run_agent_attempt(app, workspace, chat_dir, identity, key,
                                             question, history, config, client)
        except (ServiceOverloadedError, TimeoutError, EmptyLLMResponseError) as exc:
            if tag is None and len(attempts) > 1:
                chat_logger.warning('IA principal falhou (%s); assumindo o modelo de reserva (%s).',
                                    type(exc).__name__, reserve['model'])
                continue
            raise
        if tag:
            chat_logger.info('Análise concluída pelo modelo de reserva (%s).', tag)
        history.append(step)
        # A memória de texto é limitada; as tabelas de resultados continuam no workspace da conversa.
        return answer, history[-12:], key, revision
    raise RuntimeError('O agente não concluiu a análise com uma resposta utilizável.')


def publish(root, workspace_path, selected_tables, model_id, rules):
    """Cria um conjunto imutável; só publica a referência depois de terminar."""
    from data_formulator.datalake.workspace import Workspace
    root = Path(root)
    revision = uuid.uuid4().hex
    target = root / 'snapshots' / revision
    source = Workspace(identity_id='whatsapp:editor', workspace_path=workspace_path)
    if not selected_tables or any(name not in source.list_tables() for name in selected_tables):
        raise ValueError('Selecione ao menos uma tabela existente.')
    try:
        snapshot = Workspace(identity_id='whatsapp:template', workspace_path=target)
        for name in selected_tables:
            snapshot.write_parquet(source.read_data_as_df(name), name)
            source_meta = source.get_table_metadata(name)
            meta = snapshot.get_table_metadata(name)
            meta.description = source_meta.description
            by_name = {c.name: c for c in source_meta.columns}
            for col in meta.columns:
                if col.name in by_name:
                    col.description = by_name[col.name].description
            snapshot.add_table_metadata(meta)
        config = {'revision': revision, 'tables': selected_tables, 'model_id': model_id, 'rules': rules}
        temporary = root / ('config-' + revision + '.tmp')
        temporary.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, root / 'config.json')
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return config


def restore_from_google(root):
    """Reconstrói os dados publicados após perda do disco temporário no Render Free."""
    # A restauração automática de inicialização e o primeiro trabalho do WhatsApp
    # podem coincidir. Serializa as chamadas sem alterar quais abas são lidas.
    with _GOOGLE_RESTORE_LOCK:
        return _restore_from_google_locked(root)


def _restore_from_google_locked(root):
    from google_sheets_data_loader import GoogleSheetsDataLoader
    from data_formulator.datalake.workspace import Workspace
    from data_formulator.model_registry import model_registry
    root = Path(root)
    if (root / 'config.json').exists():
        return
    if not os.environ.get('GOOGLE_SPREADSHEET_ID', '').strip():
        raise ValueError('Configure GOOGLE_SPREADSHEET_ID para restaurar a planilha no plano gratuito.')
    models = model_registry.list_public()
    requested_model = os.environ.get('WHATSAPP_MODEL_ID', '').strip()
    available_models = [m for m in models if model_registry.get_config(m['id'])]
    model_id = requested_model or (available_models[0]['id'] if available_models else '')
    model_config = model_registry.get_config(model_id) if model_id else None
    if not model_config or not all(model_config.get(k) for k in ('endpoint', 'model')):
        raise ValueError('Configure um modelo de IA no painel do Data Formulator ou em WHATSAPP_MODEL_ID para restaurar o WhatsApp.')
    loader = GoogleSheetsDataLoader()
    requested = {v.strip() for v in os.environ.get('WHATSAPP_SHEET_NAMES', '').split(',') if v.strip()}
    parent = loader.ls()[0]
    nodes = loader.ls(parent.path)
    if requested - {node.name for node in nodes}:
        raise ValueError('Alguma aba de WHATSAPP_SHEET_NAMES não existe na planilha.')
    selected = [node for node in nodes if not requested or node.name in requested]
    if not selected:
        raise ValueError('Nenhuma aba disponível para restaurar.')
    staging = root / ('restore-' + uuid.uuid4().hex)
    workspace = Workspace(identity_id='whatsapp:restore', workspace_path=staging)
    imported = []
    try:
        for node in selected:
            table = loader.fetch_data_as_arrow(node.metadata['_source_name'])
            if table.num_columns == 0 or table.num_rows == 0:
                continue
            meta = workspace.write_parquet_from_arrow(table, node.name)
            meta.description = node.metadata.get('description', '')
            workspace.add_table_metadata(meta)
            imported.append(meta.name)
        if not imported:
            raise ValueError('As abas selecionadas não contêm registros.')
        publish(root, staging, imported, model_id, os.environ.get('WHATSAPP_RULES', ''))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
