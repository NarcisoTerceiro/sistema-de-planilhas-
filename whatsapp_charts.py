"""Gera gráficos em PDF para o WhatsApp reaproveitando o motor do Data Formulator.

O agente analista já produz uma especificação de gráfico (``chart_spec``) e a
tabela de resultado quando decide visualizar. Este módulo apenas:

1. Detecta quando o usuário pediu o gráfico em PDF (``wants_pdf_chart``).
2. Converte a ``chart_spec`` do agente em uma especificação Vega-Lite usando a
   função já existente ``assemble_vegailte_chart`` e renderiza em PDF com
   ``vl_convert`` — as mesmas bibliotecas que o sistema usa para gerar gráficos.

Nada aqui chama a IA de novo: trabalhamos sobre o resultado que o agente já
calculou e verificou, preservando os números da análise.
"""
import logging
import re
import unicodedata
import uuid
from pathlib import Path

log = logging.getLogger('whatsapp')

# Importações pesadas protegidas: se o ambiente não tiver vl_convert ou o
# motor de gráficos, o chatbot continua respondendo em texto normalmente.
try:
    import pandas as pd
    from data_formulator.workflows.create_vl_plots import (
        assemble_vegailte_chart,
        field_metadata_to_semantic_types,
    )
    import vl_convert as vlc
    CHARTS_AVAILABLE = True
except Exception as exc:  # pragma: no cover - depende do ambiente de execução
    CHARTS_AVAILABLE = False
    log.warning('Geração de gráfico em PDF indisponível: %s', type(exc).__name__)


# Nomes oficiais dos modelos de gráfico (devem bater exatamente com os templates).
_TEMPLATE_NAMES = {
    'scatter plot', 'line chart', 'bar chart', 'grouped bar chart',
    'stacked bar chart', 'heatmap', 'area chart', 'streamgraph', 'boxplot',
    'histogram', 'pie chart', 'lollipop chart', 'strip plot', 'density plot',
    'bump chart', 'regression', 'ranged dot plot', 'candlestick chart',
    'waterfall chart', 'radar chart', 'rose chart', 'pyramid chart',
    'world map', 'us map',
}

# Apelidos comuns (inclusive em português) para o nome oficial do template.
_ALIASES = {
    'bar': 'Bar Chart', 'bars': 'Bar Chart', 'barra': 'Bar Chart',
    'barras': 'Bar Chart', 'coluna': 'Bar Chart', 'colunas': 'Bar Chart',
    'grouped bar': 'Grouped Bar Chart', 'stacked bar': 'Stacked Bar Chart',
    'line': 'Line Chart', 'linha': 'Line Chart', 'linhas': 'Line Chart',
    'area': 'Area Chart', 'área': 'Area Chart',
    'scatter': 'Scatter Plot', 'dispersão': 'Scatter Plot', 'dispersao': 'Scatter Plot',
    'pie': 'Pie Chart', 'pizza': 'Pie Chart', 'donut': 'Pie Chart', 'rosca': 'Pie Chart',
    'hist': 'Histogram', 'histograma': 'Histogram',
    'heat': 'Heatmap', 'mapa de calor': 'Heatmap',
    'box': 'Boxplot', 'boxplot': 'Boxplot', 'caixa': 'Boxplot',
    'lollipop': 'Lollipop Chart', 'pirulito': 'Lollipop Chart',
    'regression': 'Regression', 'regressão': 'Regression', 'regressao': 'Regression',
}

_CHART_WORDS = (
    'grafico', 'grafica', 'chart', 'visualiz', 'plot', 'plotar', 'diagrama',
    'histograma', 'dispersao', 'pizza', 'barras',
)


def _strip_accents(text):
    return ''.join(c for c in unicodedata.normalize('NFD', str(text).casefold())
                   if not unicodedata.combining(c))


def wants_pdf_chart(question):
    """Usuário pediu um gráfico em PDF? Exige menção a PDF e a um termo de gráfico."""
    text = _strip_accents(question)
    if 'pdf' not in text:
        return False
    return any(word in text for word in _CHART_WORDS)


def normalize_chart_type(chart_type):
    """Converte o tipo informado pelo agente no nome exato de um template."""
    raw = str(chart_type or '').strip()
    if not raw:
        return 'Bar Chart'
    low = raw.casefold()
    if low in _TEMPLATE_NAMES:
        # Recupera a capitalização oficial.
        return {n: n.title().replace(' Us ', ' US ') for n in _TEMPLATE_NAMES}.get(low, raw.title())
    titled = raw.title()
    if titled.casefold() in _TEMPLATE_NAMES:
        return titled
    if low in _ALIASES:
        return _ALIASES[low]
    for alias, name in _ALIASES.items():
        if alias in low:
            return name
    return 'Bar Chart'


def normalize_encodings(encodings):
    """Garante o formato {canal: {'field': nome, ...}} aceito pelo montador.

    O agente pode emitir o campo como string ("x": "coluna") ou como objeto
    ("x": {"field": "coluna", "aggregate": "sum"}). Tratamos ambos.
    """
    result = {}
    for channel, value in (encodings or {}).items():
        if isinstance(value, str):
            if value:
                result[channel] = {'field': value}
        elif isinstance(value, dict):
            field = value.get('field') or value.get('value') or value.get('name')
            if field:
                clean = {'field': field}
                if value.get('aggregate'):
                    clean['aggregate'] = value['aggregate']
                result[channel] = clean
    return result


def _safe_filename(title):
    base = _strip_accents(title or 'grafico')
    base = re.sub(r'[^a-z0-9]+', '-', base).strip('-') or 'grafico'
    return base[:50] + '.pdf'


def build_chart_pdf(df, chart_spec, out_dir, field_metadata=None, title='', subtitle=''):
    """Monta o gráfico e grava um PDF. Retorna (caminho, nome_arquivo) ou None.

    Levanta exceção só em falhas inesperadas; casos previsíveis (sem dados,
    encodings inválidos, motor ausente) retornam None para o chamador decidir.
    """
    if not CHARTS_AVAILABLE:
        return None
    if df is None or len(df) == 0:
        return None

    chart_spec = chart_spec or {}
    chart_type = normalize_chart_type(
        chart_spec.get('chart_type') or chart_spec.get('type'))
    encodings = normalize_encodings(chart_spec.get('encodings'))
    if not encodings:
        return None
    # Mantém apenas canais cujo campo exista na tabela de resultado.
    encodings = {ch: enc for ch, enc in encodings.items() if enc['field'] in df.columns}
    if not encodings:
        return None

    config = chart_spec.get('config') if isinstance(chart_spec.get('config'), dict) else None
    semantic_types = field_metadata_to_semantic_types(field_metadata) if field_metadata else None

    try:
        spec = assemble_vegailte_chart(
            df, chart_type, encodings, config=config, semantic_types=semantic_types)
    except Exception as exc:
        log.warning('Montagem do gráfico falhou (%s); tentando Bar Chart simples.', type(exc).__name__)
        try:
            spec = assemble_vegailte_chart(df, 'Bar Chart', encodings)
        except Exception:
            return None

    if title:
        spec['title'] = {'text': str(title)[:200]}
        if subtitle:
            spec['title']['subtitle'] = str(subtitle)[:200]

    pdf_bytes = vlc.vegalite_to_pdf(spec)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = _safe_filename(title)
    path = out_dir / (uuid.uuid4().hex + '.pdf')
    path.write_bytes(pdf_bytes)
    return str(path), filename


def dataframe_from_rows(rows):
    """Reconstrói um DataFrame a partir dos registros já calculados pelo agente."""
    if not CHARTS_AVAILABLE or not rows:
        return None
    try:
        return pd.DataFrame(list(rows))
    except Exception:
        return None
