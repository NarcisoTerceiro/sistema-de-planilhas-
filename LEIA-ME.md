# Publicar Data Formulator completo no Render

1. Crie um repositório no GitHub e envie os arquivos deste pacote na raiz. Não envie o ZIP como arquivo único. A dependência data_formulator==0.8.0b1 instala o aplicativo web completo.
2. No Render, crie um Web Service Python ligado ao repositório (ou use o Blueprint render.yaml).
3. Build: `pip install -r requirements-render.txt`
4. Start: `gunicorn --workers 1 --threads 4 --timeout 300 --bind 0.0.0.0:$PORT servidor_online:app`
5. Em Environment, importe seu .env. Confira as variáveis do arquivo render.env.example. Configure PYTHON_VERSION=3.11.9, HOST=0.0.0.0, DF_ALLOW_PLUGINS=1, WORKSPACE_BACKEND=local e DATA_FORMULATOR_HOME=/tmp/data_formulator.
6. Configure WEB_ADMIN_USER e WEB_ADMIN_PASSWORD (12 caracteres ou mais). Configure FLASK_SECRET_KEY com um valor aleatório estável (o Blueprint gera esse valor).
7. Em Secret Files, adicione google-service-account.json com o conteúdo do JSON da conta de serviço. Configure GOOGLE_SERVICE_ACCOUNT_FILE=/etc/secrets/google-service-account.json. Compartilhe a planilha como leitor com o client_email dessa conta. Habilite Google Sheets API e Google Drive API no projeto Google.
8. Coloque manualmente GOOGLE_SPREADSHEET_ID nas variáveis do Render. Use só o ID entre /d/ e /edit da URL. WHATSAPP_RESTORE_FROM_GOOGLE=true. O servidor restaura todas as abas com registros; abas vazias não são publicadas. WHATSAPP_SHEET_NAMES fica vazio.
9. Configure Groq com GROQ_ENABLED=true, GROQ_ENDPOINT=openai, GROQ_API_BASE=https://api.groq.com/openai/v1, GROQ_MODELS=llama-3.3-70b-versatile e GROQ_API_KEY. Selecione esse modelo Groq no Data Formulator. O endereço explícito evita enviar a chave Groq para a OpenAI.
10. Configure NVIDIA_ENABLED=true, NVIDIA_ENDPOINT=openai, NVIDIA_API_BASE=https://integrate.api.nvidia.com/v1, NVIDIA_MODELS com o modelo disponível em sua conta e NVIDIA_API_KEY. O chatbot permite apenas modelos NVIDIA. WHATSAPP_MODEL_ID é opcional se há um único modelo NVIDIA.
11. Faça o deploy inicialmente com WHATSAPP_ENABLED=false. Abra o site, entre com usuário/senha, confira o card Chatbot WhatsApp, os status e teste o agente pelo painel.
12. Configure WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_GRAPH_VERSION (versão válida do seu painel Meta), META_APP_SECRET e WHATSAPP_VERIFY_TOKEN. Ative WHATSAPP_ENABLED=true e faça novo deploy.
13. No painel Meta, configure https://SEU-SERVICO.onrender.com/webhooks/whatsapp como callback e o mesmo WHATSAPP_VERIFY_TOKEN. Assine messages. O aplicativo aceita texto de qualquer número; as restrições do número de teste da Meta continuam dependendo da configuração da sua conta.

## Planilha e memória
O ID é alterado manualmente no Render. Ao trocar o ID, faça novo deploy para reconstruir os dados. Escolhas feitas apenas no painel não atualizam as variáveis do Render. A restauração lê todas as abas pela conta de serviço. Uma publicação é uma cópia dos dados: alterações posteriores na planilha exigem nova publicação/recarga.
Cada número tem memória e workspace separados. Mantidos 12 passos e prazo de 24 horas desde a última atividade; /reiniciar limpa o contexto. A memória usa SQLite temporário e pode desaparecer quando o serviço reinicia. A planilha é restaurada pelo Google, mas histórico e fila não são restaurados. Nenhum banco externo é necessário nesta etapa.
Use um worker e uma instância. O painel administrativo tem senha; o webhook continua validando a assinatura da Meta. O agente executa análises no servidor; a implantação não foi testada com credenciais reais e não deve ser tratada como sandbox de execução endurecido.

## Arquivos
O pacote não inclui .venv, chaves ou JSON de credenciais. As dependências serão instaladas no Render. O projeto Microsoft Data Formulator usa licença MIT. Teste real das APIs e deploy dependem das suas contas.
