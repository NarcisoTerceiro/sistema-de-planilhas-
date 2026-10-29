"""Testes sem enviar mensagens reais nem consultar IA. python -m unittest test_integracao_whatsapp -v"""
import base64
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from whatsapp_store import Store
import whatsapp_extension as ext
from whatsapp_agent import split_text, phone_key


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            'DATA_FORMULATOR_HOME': self.tmp.name,
            'HOST': '0.0.0.0', 'WEB_ADMIN_USER': 'admin', 'WEB_ADMIN_PASSWORD': 'senha-de-teste-123',
            'FLASK_SECRET_KEY': 'segredo-estavel-de-teste', 'WHATSAPP_ENABLED': 'true',
            'WHATSAPP_START_WORKER': 'false', 'WHATSAPP_ACCESS_TOKEN': 'token-falso',
            'WHATSAPP_PHONE_NUMBER_ID': '123', 'WHATSAPP_GRAPH_VERSION': 'v99.0',
            'META_APP_SECRET': 'segredo-meta-de-teste', 'WHATSAPP_VERIFY_TOKEN': 'verificar-teste',
            'WHATSAPP_ALLOWED_NUMBERS': '5583999999999',
        }, clear=True)
        self.environment.start()
        self.app = Flask(__name__)
        self.app.secret_key = os.environ['FLASK_SECRET_KEY']
        self.app.add_url_rule('/', 'fake_home', lambda: '<html><body>Data Formulator</body></html>')
        self.worker = ext.install(self.app)
        self.client = self.app.test_client()

    def tearDown(self):
        self.environment.stop()
        self.tmp.cleanup()

    def payload(self, sender='5583999999999', msg_id='wamid-teste', phone_id='123'):
        return {'object': 'whatsapp_business_account', 'entry': [{'changes': [{'value': {
            'metadata': {'phone_number_id': phone_id},
            'messages': [{'from': sender, 'id': msg_id, 'timestamp': str(int(time.time())),
                          'type': 'text', 'text': {'body': 'Quais obras estão concluídas?'}}]
        }}]}]}

    def post_webhook(self, payload, signature=True):
        raw = json.dumps(payload).encode()
        headers = {'Content-Type': 'application/json'}
        if signature:
            headers['X-Hub-Signature-256'] = 'sha256=' + hmac.new(os.environ['META_APP_SECRET'].encode(), raw, hashlib.sha256).hexdigest()
        return self.client.post('/webhooks/whatsapp', data=raw, headers=headers)

    def test_admin_protection_and_public_health(self):
        self.assertEqual(self.client.get('/').status_code, 401)
        self.assertEqual(self.client.get('/healthz').status_code, 200)
        auth = 'Basic ' + base64.b64encode(b'admin:senha-de-teste-123').decode()
        response = self.client.get('/', headers={'Authorization': auth})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'/integracoes/whatsapp', response.data)

    def test_webhook_verification(self):
        query = {'hub.mode': 'subscribe', 'hub.verify_token': 'verificar-teste', 'hub.challenge': '12345'}
        response = self.client.get('/webhooks/whatsapp', query_string=query)
        self.assertEqual(response.data, b'12345')
        query['hub.verify_token'] = 'errado'
        self.assertEqual(self.client.get('/webhooks/whatsapp', query_string=query).status_code, 403)

    def test_invalid_signature_does_not_enqueue(self):
        self.assertEqual(self.post_webhook(self.payload(), signature=False).status_code, 403)
        self.assertEqual(self.worker.store.stats(), {})

    def test_deduplication_and_allowlist(self):
        self.assertEqual(self.post_webhook(self.payload()).status_code, 200)
        self.assertEqual(self.post_webhook(self.payload()).status_code, 200)
        self.assertEqual(self.worker.store.stats(), {'queued': 1})
        self.post_webhook(self.payload(sender='5583888888888', msg_id='outro'))
        self.post_webhook(self.payload(phone_id='999', msg_id='outro-id'))
        self.assertEqual(self.worker.store.stats(), {'queued': 1})

    def test_status_events_do_not_create_questions(self):
        self.assertEqual(self.post_webhook({'object': 'whatsapp_business_account', 'entry': [{'changes': [{'value': {'statuses': [{'status': 'delivered'}]}}]}]}).status_code, 200)
        self.assertEqual(self.worker.store.stats(), {})

    def test_expired_messages_ignored(self):
        payload = self.payload()
        payload['entry'][0]['changes'][0]['value']['messages'][0]['timestamp'] = str(time.time() - 86400)
        self.post_webhook(payload)
        self.assertEqual(self.worker.store.stats(), {})

    def test_long_answer_no_truncation(self):
        source = '\n'.join(f'{i}. Obra exemplo para teste' for i in range(1000))
        parts = split_text(source)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) < 4096 for p in parts))
        recovered = '\n'.join(p.split('\n', 1)[1] for p in parts)
        self.assertEqual(recovered, source)

    def test_history_isolated_and_revision_reset(self):
        store = self.worker.store
        first, second = phone_key('5583999999999'), phone_key('5583888888888')
        store.enqueue([{'id': 'a', 'sender': '5583999999999', 'body': 'pergunta', 'received': time.time()}])
        store.ready('a', ['resposta'], first, 'rev1', [{'user_question': 'pergunta', 'agent_response': 'resposta'}])
        self.assertEqual(len(store.history(first, 'rev1')), 1)
        self.assertEqual(store.history(second, 'rev1'), [])
        self.assertEqual(store.history(first, 'rev2'), [])
        self.assertNotEqual(first, second)

    def test_worker_mocked_sends_only_generated_response(self):
        self.post_webhook(self.payload())
        job = self.worker.store.next_job()
        with patch.object(ext, 'ask', return_value=('Resposta calculada', [], phone_key(job['sender']), 'rev1')), patch.object(ext, 'send_text', return_value='meta-aceitou') as send:
            self.worker.process(job)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[1], 'Resposta calculada')
        self.assertEqual(self.worker.store.stats(), {'accepted': 1})

    def test_network_uncertainty_is_not_auto_resent(self):
        self.post_webhook(self.payload())
        job = self.worker.store.next_job()
        with patch.object(ext, 'ask', return_value=('Resposta calculada', [], phone_key(job['sender']), 'rev1')), patch.object(ext, 'send_text', side_effect=ext.requests.Timeout()) as send:
            self.worker.process(job)
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self.worker.store.stats(), {'uncertain': 1})
        self.worker.store.recover()
        self.assertIsNone(self.worker.store.next_job())

    def test_failed_analysis_replies_without_fake_data(self):
        self.post_webhook(self.payload())
        job = self.worker.store.next_job()
        with patch.object(ext, 'ask', side_effect=RuntimeError('sem resposta')), patch.object(ext, 'send_text', return_value='meta-erro-aceito') as send:
            self.worker.process(job)
            self.assertIn('Não consegui concluir', send.call_args.args[1])
        self.assertEqual(self.worker.store.stats(), {'analysis_failed': 1})
        self.assertEqual(self.worker.store.history(phone_key(job['sender']), 'rev1'), [])

    def test_recovery_does_not_resend_inflight_part(self):
        store = self.worker.store
        store.enqueue([{'id': 'a', 'sender': '5583999999999', 'body': 'q', 'received': time.time()}])
        store.ready('a', ['resposta'], 'k', 'r', [])
        store.mark_part('a', 0, 'sending')
        store.recover()
        self.assertIsNone(store.next_job())
        self.assertEqual(store.stats(), {'uncertain': 1})

    def test_test_panel_rejects_request_without_csrf(self):
        auth = 'Basic ' + base64.b64encode(b'admin:senha-de-teste-123').decode()
        response = self.client.post('/integracoes/whatsapp/testar', data={'question': 'Quantas obras?'}, headers={'Authorization': auth})
        self.assertEqual(response.status_code, 403)

    def test_local_preview_does_not_send_messages(self):
        auth = 'Basic ' + base64.b64encode(b'admin:senha-de-teste-123').decode()
        with self.client.session_transaction() as s:
            s['wa_csrf'] = 'csrf-teste'
        with patch.object(ext, 'ask', return_value=('Há 27 obras.', [], 'painel-chave', 'rev1')), patch.object(ext, 'send_text') as send:
            response = self.client.post('/integracoes/whatsapp/testar', data={'question': 'Quantas obras?', 'csrf': 'csrf-teste'}, headers={'Authorization': auth})
            self.assertEqual(response.status_code, 200)
            self.assertIn('Há 27 obras.', response.get_data(as_text=True))
            send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
