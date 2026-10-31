# Chatbot de obras — versão reduzida para teste

Esta versão inicia o painel do chatbot e o webhook do WhatsApp sem carregar a aplicação web completa do Data Formulator. Ela preserva os arquivos originais do leitor Google Sheets, do agente de respostas e da integração WhatsApp.

## O que continua no sistema

- Leitura de todas as abas não vazias pela conta de serviço do Google.
- Mesmo `AnalystAgent`, prompt/regras e fluxo de respostas do chatbot atual.
- Painel protegido, teste de perguntas, fila e webhook do WhatsApp.
- Restauração da planilha após reinício quando `WHATSAPP_RESTORE_FROM_GOOGLE=true`.

## O que foi retirado do início do servidor

- A interface principal do Data Formulator, gráficos, rotas de demonstração e inicialização do servidor completo.
- O processo web agora parte de `servidor_chatbot.py`.

O pacote Python `data_formulator==0.8.0b1` continua instalado porque o leitor e as respostas reutilizam o `Workspace`, o `AnalystAgent` e o registro de modelos originais. Esses módulos são importados quando necessários; `data_formulator.app` não é iniciado.

## Render

Este pacote é para testar no Render como um **segundo serviço**, sem substituir o Data Formulator que já está publicado.

1. Coloque a pasta `chatbot-render-enxuto` dentro do repositório GitHub ligado ao Render e envie essa alteração para o GitHub. Não envie `.env`, o JSON da conta de serviço nem chaves.
2. No Render, clique em **New + → Web Service** e escolha o mesmo repositório e a branch atual.
3. Em **Root Directory**, informe `chatbot-render-enxuto`.
4. Use estas configurações:
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements-render.txt`
   - **Start Command:** `gunicorn --workers 1 --threads 2 --timeout 300 --bind 0.0.0.0:$PORT servidor_chatbot:app`
   - **Health Check Path:** `/healthz`
   - **Instance Type:** Free
5. Crie as variáveis de ambiente a partir dos campos de `render.yaml`. Preencha com as mesmas credenciais corretas que já usa no serviço atual. `WEB_ADMIN_PASSWORD` precisa ter pelo menos 12 caracteres.
6. Em **Environment → Secret Files**, adicione o JSON da conta de serviço com o nome `google-service-account.json`. A variável `GOOGLE_SERVICE_ACCOUNT_FILE` já aponta para `/etc/secrets/google-service-account.json`.
7. Defina `GOOGLE_SPREADSHEET_ID` com o ID da planilha. Mantenha `WHATSAPP_ENABLED=false` no primeiro deploy para verificar a inicialização e a conexão com o Sheets sem receber mensagens.
8. Depois do deploy, abra `https://ENDERECO-DO-SERVICO/chatbot-whatsapp`, entre no painel e teste a planilha e as perguntas. Veja **Logs** se o deploy falhar.

Quando o teste do painel e da planilha estiver funcionando, configure as credenciais da Meta e só então mude `WHATSAPP_ENABLED=true`. O callback continua sendo `/webhooks/whatsapp`.

O novo serviço usa o plano Free, mas os limites de memória e o repouso por inatividade continuam valendo. O teste mede se essa versão menor inicia e responde melhor; não garante disponibilidade contínua nem elimina os limites do plano.

Use `GOOGLE_SPREADSHEET_ID` no Render para escolher a planilha; `WHATSAPP_SHEET_NAMES` vazio significa todas as abas. A restauração e a memória continuam temporárias no Render Free; a memória das conversas pode sumir após reinício.

Este pacote não inclui `.env`, JSON de serviço, tokens ou chaves de API. Não altere nem apague o serviço atual até terminar os testes no serviço separado.
