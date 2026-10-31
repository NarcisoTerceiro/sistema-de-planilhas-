"""Conector local de leitura do Google Sheets para Data Formulator 0.8.0b1.

As credenciais são lidas no servidor, nunca enviadas ao navegador.
"""
import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote

from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession
from data_formulator.data_loader.external_data_loader import (
    ExternalDataLoader, CatalogNode, CatalogCachePolicy, MAX_IMPORT_ROWS,
)

MEANINGS = {
    'EM_ANDAMENTO': 'Obras físicas. Consulte STATUS, não o nome da aba. Valor contratado, executado e pago são medidas distintas.',
    'PAVIMENTAÇÃO': 'Obras físicas de pavimentação. RUA identifica a via e SITUAÇÃO indica o status. Incluir nas obras físicas.',
    'EM_PROJETO': 'Projetos técnicos. Projeto concluído não significa obra física concluída.',
    'EM_LICITAÇÃO': 'Licitações. STATUS é a fase da contratação e OBSERVAÇÕES são notas. Não representam conclusão física.',
    'PENDÊNCIAS': 'Anotações de impedimentos. Não contar como novas obras nem presumir vínculos sem identificador.',
}


def number(value, decimal_dot=False):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    raw = str(value).strip().replace('R$', '').replace('\u00a0', '').replace(' ', '')
    percent = raw.endswith('%')
    raw = raw.removesuffix('%')
    if decimal_dot:
        raw = raw.replace(',', '.')
    elif ',' in raw:
        raw = raw.replace('.', '').replace(',', '.')
    elif re.fullmatch(r'-?\d{1,3}(\.\d{3})+', raw):
        raw = raw.replace('.', '')
    if not re.fullmatch(r'-?\d+(\.\d+)?', raw):
        raise ValueError('Não é um número simples')
    result = float(raw)
    return result / 100 if percent else result


def is_coordinate(label):
    """Coluna de latitude/longitude. Palavras completas por substring (cobre
    'LATITUDE (GRAUS)', 'LONGITUDE DA OBRA') e formas curtas por igualdade
    exata (evita casar 'PLATAFORMA', que contém 'LAT')."""
    return ('LATITUDE' in label or 'LONGITUDE' in label
            or label in ('LAT', 'LONG', 'LON', 'LNG'))


def prepare_rows(rows, title):
    """Padroniza cabeçalho e tipos, sem inventar zero para dado ausente."""
    import pandas as pd
    if not rows:
        return pd.DataFrame(), MEANINGS.get(title.upper(), '')
    width = max(map(len, rows))
    matrix = [[None if v is None or (isinstance(v, str) and not v.strip()) else v
               for v in row] + [None] * (width - len(row)) for row in rows]
    nonempty = [i for i, row in enumerate(matrix) if any(v is not None for v in row)]
    if not nonempty:
        return pd.DataFrame(), ''
    heading = nonempty[0]
    meaning = MEANINGS.get(title.upper(), 'Consulte as colunas e os valores. O nome da aba não comprova o status dos registros.')
    financial = any('VALORES DE RECURSOS PRÓPRIOS DAS OBRAS A SEREM PAGOS' in str(v)
                    for row in matrix[:20] for v in row)
    if financial:
        heading = next((i for i in nonempty if str(matrix[i][0]).strip().upper() == 'CONTRATO'), heading)
        count = 2
        meaning = 'Recursos próprios a pagar. Não são valores já pagos nem novas obras. Relacionar por contrato quando compatível.'
    else:
        count = 1
    headers, used = [], set()
    for col in range(width):
        parts = [str(matrix[i][col]).strip() for i in range(heading, min(heading + count, len(matrix))) if matrix[i][col] is not None]
        base = ' / '.join(dict.fromkeys(parts)) or f'Coluna_{col + 1}'
        name, suffix = base, 2
        while name in used:
            name = f'{base}_{suffix}'
            suffix += 1
        headers.append(name)
        used.add(name)
    data = []
    for row in matrix[heading + count:]:
        if not any(v is not None for v in row):
            continue
        # Exclui apenas linhas de fórmulas vazias/zero sem identificador.
        if row[0] is None and all(v is None or isinstance(v, (int, float)) and v == 0 for v in row):
            continue
        data.append(row)
    df = pd.DataFrame(data, columns=headers)
    for col in headers:
        values = df[col].dropna()
        if values.empty:
            continue
        label = col.upper()
        numeric = (is_coordinate(label)
                   or any(word in label for word in ('VALOR', 'ADITIVO', 'REAJUSTE', 'CONTRAPARTIDA', 'PAGO', 'SALDO', 'COMPRIMENTO', 'MEIO FIO', 'ÁREA PAVIMENTADA', '%')))
        # Datas formatadas pela API ficam como texto até esta conversão explícita.
        if 'DATA' in label:
            parsed = pd.to_datetime(df[col], dayfirst=True, errors='coerce', format='mixed')
            if parsed.notna().sum() == len(values):
                df[col] = parsed
                continue
        if numeric:
            try:
                converted = [None if pd.isna(v) else number(v, is_coordinate(label)) for v in df[col]]
                df[col] = pd.Series(converted, dtype='float64')
                continue
            except (ValueError, TypeError):
                pass  # texto não numérico é preservado, não convertido para zero
        # Arrow não aceita misturar textos e números no mesmo campo.
        if any(isinstance(v, str) for v in values) or len({type(v).__name__ for v in values}) > 1:
            df[col] = df[col].map(lambda v: None if pd.isna(v) else str(v))
    origin = 'aba_origem'
    while origin in df.columns:
        origin = '_' + origin
    df[origin] = title
    return df, meaning


class GoogleSheetsDataLoader(ExternalDataLoader):
    DISPLAY_NAME = 'Google Sheets — conta de serviço'
    CATALOG_CACHE_POLICY = CatalogCachePolicy(listing_ttl_seconds=300, metadata_ttl_seconds=300,
                                              refresh_cost='cheap', automatic_refresh='while_connected')

    @staticmethod
    def list_params():
        return []

    @staticmethod
    def auth_config():
        # Não pede credenciais no navegador; usa o JSON configurado pelo dono do servidor.
        return {'mode': 'none'}

    @staticmethod
    def catalog_hierarchy():
        return [{'key': 'spreadsheet', 'label': 'Planilha'}, {'key': 'table', 'label': 'Aba'}]

    def __init__(self, params=None):
        self.params = params or {}
        path = Path(os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE', '')).expanduser()
        if not path.is_absolute():
            path = Path(os.environ.get('DATA_FORMULATOR_ENV_DIR', '.')) / path
        try:
            info = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError) as exc:
            raise ValueError('Confira GOOGLE_SERVICE_ACCOUNT_FILE no .env: não consegui ler a chave JSON.') from None
        if info.get('type') != 'service_account' or info.get('token_uri') != 'https://oauth2.googleapis.com/token':
            raise ValueError('O JSON não é uma chave de conta de serviço Google válida.')
        scopes = ['https://www.googleapis.com/auth/spreadsheets.readonly']
        # O painel pode listar toda a conta sem alterar variáveis globais.
        self.pinned_id = str(self.params.get('spreadsheet_id', os.environ.get('GOOGLE_SPREADSHEET_ID', ''))).strip()
        if self.pinned_id and not re.fullmatch(r'[A-Za-z0-9_-]+', self.pinned_id):
            raise ValueError('GOOGLE_SPREADSHEET_ID deve conter apenas o ID, não o link completo.')
        if not self.pinned_id:
            scopes.append('https://www.googleapis.com/auth/drive.metadata.readonly')
        self.session = AuthorizedSession(Credentials.from_service_account_info(info, scopes=scopes), refresh_timeout=30)
        self._cache = {}
        self._lock = threading.RLock()

    def _get(self, url, params=None):
        try:
            response = self.session.get(url, params=params, timeout=30)
        except Exception:
            raise ValueError('Falha de conexão com o Google. Tente novamente.') from None
        if response.status_code == 403:
            raise ValueError('Google recusou o acesso. Confira o compartilhamento e se as APIs Sheets e Drive estão ativadas.')
        if response.status_code == 404:
            raise ValueError('Planilha não encontrada ou não compartilhada com a conta de serviço.')
        if response.status_code == 429:
            raise ValueError('Limite do Google atingido. Aguarde antes de atualizar.')
        if response.status_code != 200:
            raise ValueError(f'Google retornou erro HTTP {response.status_code}.')
        return response.json()

    def _cached(self, key, callback):
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < 60:
                return hit[1]
            value = callback()
            self._cache[key] = (time.monotonic(), value)
            return value

    def _book(self, book_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', book_id) or self.pinned_id and book_id != self.pinned_id:
            raise ValueError('ID de planilha inválido.')
        return self._cached(('book', book_id), lambda: self._get(
            f'https://sheets.googleapis.com/v4/spreadsheets/{book_id}',
            {'fields': 'spreadsheetId,properties.title,sheets.properties(sheetId,title)'}))

    def _books(self):
        if self.pinned_id:
            b = self._book(self.pinned_id)
            return [{'id': self.pinned_id, 'name': b['properties']['title']}]
        def fetch():
            files, page = [], None
            while True:
                params = {'q': "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false",
                          'fields': 'nextPageToken,files(id,name)', 'pageSize': 100,
                          'includeItemsFromAllDrives': 'true', 'supportsAllDrives': 'true'}
                if page:
                    params['pageToken'] = page
                result = self._get('https://www.googleapis.com/drive/v3/files', params)
                files.extend(result.get('files', []))
                page = result.get('nextPageToken')
                if not page:
                    return sorted(files, key=lambda x: (x['name'].casefold(), x['id']))
        return self._cached('books', fetch)

    def test_connection(self):
        self._books()
        return True

    def _path_book_id(self, part):
        match = re.search(r'\[([A-Za-z0-9_-]+)\]$', part)
        return match.group(1) if match else part

    def ls(self, path=None, filter=None):
        path = path or []
        needle = (filter or '').casefold()
        if not path:
            return [CatalogNode(name=f"{b['name']} [{b['id']}]", node_type='namespace',
                                path=[f"{b['name']} [{b['id']}]"])
                    for b in self._books() if needle in b['name'].casefold()]
        if len(path) != 1:
            return []
        book_id = self._path_book_id(path[0])
        nodes = []
        for item in self._book(book_id).get('sheets', []):
            p = item['properties']; title = p['title']
            if needle not in title.casefold():
                continue
            source_id = f"{book_id}.{p['sheetId']}"
            nodes.append(CatalogNode(name=title, node_type='table', path=path + [title],
                                    metadata={'_source_name': source_id, 'table_key': source_id,
                                              'description': MEANINGS.get(title.upper(), f'Aba {title} do Google Sheets.')}))
        return nodes

    def list_tables(self, table_filter=None):
        result = []
        for parent in self.ls():
            for node in self.ls(parent.path, filter=table_filter):
                result.append({'name': node.metadata['_source_name'], 'path': node.path, 'metadata': node.metadata})
        return result

    def _resolve(self, source_table):
        if isinstance(source_table, list):
            book_id = self._path_book_id(source_table[0]); sheet = '.'.join(source_table[1:])
        else:
            book, sep, sheet = str(source_table).partition('.')
            if not sep:
                raise ValueError('Selecione uma planilha e uma aba.')
            book_id = self._path_book_id(book)
        for item in self._book(book_id).get('sheets', []):
            p = item['properties']
            if str(p['sheetId']) == sheet or p['title'] == sheet:
                return book_id, p['title']
        raise ValueError('Aba não encontrada. Atualize a lista de planilhas.')

    def _frame(self, source_table):
        book_id, title = self._resolve(source_table)
        def fetch():
            escaped = "'" + title.replace("'", "''") + "'"
            result = self._get(f'https://sheets.googleapis.com/v4/spreadsheets/{book_id}/values/{quote(escaped, safe="")}',
                               {'valueRenderOption': 'UNFORMATTED_VALUE', 'dateTimeRenderOption': 'FORMATTED_STRING'})
            return prepare_rows(result.get('values', []), title)
        return self._cached(('frame', book_id, title), fetch)

    def get_metadata(self, path):
        df, meaning = self._frame(path)
        return {'row_count': len(df), 'columns': [{'name': c, 'type': str(df[c].dtype)} for c in df],
                'description': meaning}

    def get_column_types(self, source_table):
        return self.get_metadata(source_table)

    def fetch_data_as_arrow(self, source_table, import_options=None):
        import pyarrow as pa
        opts = import_options or {}
        if opts.get('filters') or opts.get('source_filters') or opts.get('sort_columns'):
            raise ValueError('Importe a aba completa e peça ao agente para filtrar e ordenar depois.')
        df, _ = self._frame(source_table)
        self._last_total_rows = len(df)
        size = max(0, min(int(opts.get('size', MAX_IMPORT_ROWS)), MAX_IMPORT_ROWS))
        columns = opts.get('columns') or list(df.columns)
        return pa.Table.from_pandas(df.loc[:, columns].iloc[:size], preserve_index=False)

    def probe(self, path, query):
        from data_formulator.data_loader.probe_utils import run_probe_on_duckdb
        book_id, title = self._resolve(path)
        sheet_id = next(p['properties']['sheetId'] for p in self._book(book_id)['sheets'] if p['properties']['title'] == title)
        loader = self
        class FullScan:
            def fetch_data_as_arrow(self, source_table, import_options):
                # A consulta de inspeção aplica os filtros no DuckDB depois da leitura.
                return loader.fetch_data_as_arrow(source_table, {'size': import_options['size']})
        return run_probe_on_duckdb(FullScan(), path, query,
                                  source_table=f'{book_id}.{sheet_id}', scan_size=MAX_IMPORT_ROWS)
