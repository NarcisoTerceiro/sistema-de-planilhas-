# Data Formulator completo + WhatsApp (versão para testes)

Mantém o sistema web da Microsoft, acrescentando um botão WhatsApp, um painel administrativo e um webhook da API oficial da Meta. A integração usa a mesma classe AnalystAgent da versão 0.8.0b1, não uma IA separada. As respostas dependem do modelo configurado e continuam precisando de conferência.

## Arquivos no Windows

Pare a execução anterior com Ctrl+C. Coloque os arquivos deste pacote em `C:\Users\Narci\data-formulator-teste`, ao lado de `.venv`. Substitua `iniciar_data_formulator.py` e `google_sheets_data_loader.py` se já existirem. Os outros são adições. Preserve seu `.env` preenchido: o arquivo `configuracao-whatsapp.env.example` é somente referência e não o substitui.

O `.env` anterior com NVIDIA_API_KEY, NVIDIA_MODELS e GOOGLE_SERVICE_ACCOUNT_FILE continua sendo lido. Para conferir a interface local, mantenha WHATSAPP_ENABLED=false.

Execute no PowerShell:

```powershell
cd "$HOME\data-formulator-teste"
.\.venv\Scripts\python.exe iniciar_data_formulator.py
```

Abra http://localhost:5567 e, para a integração, http://localhost:5567/integracoes/whatsapp. Se faltar uma dependência na instalação existente, envie a mensagem do terminal; o pacote não reinstala o Data Formulator automaticamente.

## Deixe a planilha pronta

1. Configure sua API no `.env`, abra o sistema e selecione o modelo global da NVIDIA.
2. Importe o Excel ou escolha uma planilha na fonte Google Sheets. Para o Google, as APIs Drive e Sheets devem estar ativadas e a planilha compartilhada como leitor com a conta de serviço.
3. Confira as abas, os registros e as respostas no chat web.
4. Abra o botão WhatsApp. Escolha a sessão/workspace que contém as tabelas importadas.
5. Marque somente as tabelas que os usuários podem consultar, selecione o modelo e publique.

A publicação cria um retrato dos dados, incluindo as descrições das tabelas e colunas. Não altera a planilha. Atualizações no Excel ou Google Sheets não são propagadas automaticamente ao WhatsApp: atualize as tabelas no sistema e publique novamente. Uma nova publicação reinicia o contexto dos números no próximo contato. A conversa do navegador e cada número têm históricos separados, com o mesmo agente e os dados publicados. Nenhum usuário do WhatsApp recebe as credenciais.

Depois de publicar, use **Testar o agente sem enviar WhatsApp**. Essa pergunta consulta sua IA com os dados publicados, em uma conversa de teste própria. Não é preciso configurar a Meta para esse teste, e nenhuma mensagem é enviada para outro número. A chamada à IA usa sua quota normal.

## Render

Use um repositório seu contendo estes arquivos (sem .env nem JSON). Não precisa copiar todo o repositório Microsoft: o build instala a versão fixada do pacote, que inclui o aplicativo web completo.

O `render.yaml` usa o plano Free, sem disco pago. Conversas e arquivos locais são temporários. A planilha original permanece no Google Sheets. Não foi realizado deploy.

Se preferir a criação manual:

- Build Command: `pip install -r requirements-render.txt`
- Start Command: `gunicorn --workers 1 --threads 4 --timeout 300 --bind 0.0.0.0:$PORT servidor_online:app`
- Python: 3.11.9
- HOST: 0.0.0.0
- Plano: Free
- DATA_FORMULATOR_HOME: /tmp/data_formulator
- Sem disco persistente
- WORKSPACE_BACKEND: local
- DF_ALLOW_PLUGINS: 1
- FLASK_SECRET_KEY: valor forte e estável
- WEB_ADMIN_USER: seu nome administrativo
- WEB_ADMIN_PASSWORD: senha de pelo menos 12 caracteres
- Configure a NVIDIA usando as mesmas variáveis do exemplo.

No Render, envie o JSON em **Secret Files** com nome `google-service-account.json`. Configure GOOGLE_SERVICE_ACCOUNT_FILE=/etc/secrets/google-service-account.json. Não coloque esse JSON no GitHub. Os arquivos que já estão no seu PC não são enviados automaticamente ao Render: importe a planilha novamente ou conecte o Google Sheets no servidor.

Use um único worker Gunicorn e uma instância. A fila SQLite e o worker embutido foram feitos para esse arranjo. Não use múltiplos processos, réplicas ou autoreload. Sem disco persistente os dados e a fila somem ao reiniciar/reimplantar. O serviço web permanece protegido por usuário e senha; apenas a rota de saúde e o webhook da Meta ficam fora desse login. O webhook valida sua própria assinatura.

## Conectar a Meta / WhatsApp

É necessário um aplicativo na Meta com WhatsApp Business Platform / Cloud API, um número de teste ou empresarial e suas credenciais. Não há conexão por QR code do WhatsApp pessoal.

No Render configure:

- WHATSAPP_ACCESS_TOKEN: token da Meta com permissão para mensagens do número.
- WHATSAPP_PHONE_NUMBER_ID: ID do número mostrado pela Meta, não o telefone escrito com DDD.
- WHATSAPP_GRAPH_VERSION: versão da Graph API indicada no exemplo do seu painel da Meta, no formato vXX.0. Não use literalmente vXX.0.
- META_APP_SECRET: segredo do aplicativo da Meta.
- WHATSAPP_VERIFY_TOKEN: texto secreto criado por você para verificar o webhook.
- WHATSAPP_ALLOWED_NUMBERS: números de teste que podem perguntar, como aparecem no campo `from` do WhatsApp, só dígitos. Use números autorizados no painel da Meta quando estiver usando o número de teste.
- WHATSAPP_MEMORY_HOURS: 24, ou outro período desejado.
- WHATSAPP_ENABLED: true, apenas depois de preencher tudo.

Após reiniciar o serviço, configure no painel da Meta:

- Callback URL: `https://SEU-SERVICO.onrender.com/webhooks/whatsapp`
- Verify token: o mesmo valor de WHATSAPP_VERIFY_TOKEN.
- Assine o campo/evento `messages` do WhatsApp.

O número de telefone é vinculado no painel oficial da Meta. Esta extensão usa o Phone Number ID correspondente. O sistema não registra, transfere nem verifica a propriedade de um número por conta própria.

Envie uma pergunta a partir de um número autorizado para o número de teste da Meta. A assinatura é verificada, a mensagem entra na fila e o agente consulta apenas a cópia publicada. Respostas longas são divididas em partes. `/reiniciar` limpa o contexto daquela conversa. Se a análise falhar, é enviada uma orientação, sem resultado de dados inventado.

Esta versão recebe apenas texto e retorna texto/listas. Quando o usuário pede um gráfico em PDF, também envia o gráfico como documento PDF anexado (ver abaixo). Não envia áudio nem outros arquivos. Não compartilha o histórico do chat web com o cidadão. O upload e a navegação do aplicativo web continuam disponíveis.

## Gráfico em PDF pelo WhatsApp

Quando o usuário pede um gráfico em PDF (a mensagem menciona "pdf" junto de um termo como "gráfico", "grafico", "visualização" ou "plot"), o sistema:

1. Orienta o agente a montar a visualização adequada com a ação `visualize`, usando os dados já calculados e verificados da planilha — não inventa números novos.
2. Converte a especificação do gráfico (tipo + eixos) em Vega-Lite com a função `assemble_vegailte_chart` do próprio Data Formulator e renderiza o PDF com `vl_convert` — as mesmas bibliotecas de gráficos que o sistema já usa.
3. Responde em texto com os principais números e envia o PDF como documento anexado pela Cloud API da Meta (upload da mídia + mensagem do tipo `document`).

Observações:
- Se o agente não conseguir produzir um gráfico (dados não adequados, por exemplo), o usuário recebe a resposta em texto e um aviso curto pedindo para detalhar eixos e categorias; nenhum PDF é enviado.
- O PDF é gravado em arquivo temporário da conversa. No Render Free, um reinício entre gerar e enviar pode perder o arquivo; nesse caso a parte de mídia é marcada como falha, sem reenvio automático.
- Não exige novas variáveis de ambiente: o `vl_convert` já acompanha o `data_formulator`.

## Limites do teste

- Somente os números autorizados em WHATSAPP_ALLOWED_NUMBERS são atendidos. Não abra para público irrestrito sem revisar isolamento de execução, limites de uso e custos da API.
- O agente original executa código no backend. O modo local não deve ser tratado como um ambiente seguro para usuários desconhecidos; a configuração entregue é para seu painel privado e números de teste confiáveis.
- Uma análise lenta ocupa o único worker da fila; o painel web continua separado. Há timeout por chamada à IA, mas não uma garantia de duração total da análise.
- `accepted` significa aceito pela API da Meta, não confirmação de entrega. Eventos de status não criam novas consultas.
- Webhooks repetidos com o mesmo ID não refazem a resposta. Envios sem confirmação de rede são marcados `uncertain`, sem reenvio automático para reduzir duplicidades. Confira o WhatsApp e peça uma nova consulta se necessário.
- A memória de texto guarda as últimas 12 etapas dentro do período configurado; as tabelas derivadas ficam no workspace daquele número. Isso dá contexto, mas não garante a interpretação correta de toda referência.
- Republicar dados muda a versão e inicia novas conversas; a sincronização com o Google é feita manualmente no painel.
- Não houve teste real com sua NVIDIA nem com sua conta Meta. A integração foi exercitada com respostas simuladas e requer teste de ponta a ponta após configuração.

O projeto original é da Microsoft e usa licença MIT: https://github.com/microsoft/data-formulator. Mantenha os avisos do projeto original se redistribuir seu código.

## Render gratuito: reconstrução dos dados

Configure GOOGLE_SPREADSHEET_ID com o ID da planilha e WHATSAPP_RESTORE_FROM_GOOGLE=true. O JSON deve ser um Secret File no Render, apontado por GOOGLE_SERVICE_ACCOUNT_FILE. WHATSAPP_SHEET_NAMES seleciona nomes exatos separados por vírgula (vazio = todas as abas). WHATSAPP_RULES guarda orientações próprias. WHATSAPP_MODEL_ID é opcional se só há um modelo global.

Após perder o disco temporário, o sistema reconstrói somente os dados publicados, consultando o Google Sheets. Conversas, filas e histórico web não são restaurados. Alterações feitas somente na tela de publicação não viram variáveis do Render: mantenha ID, abas e regras nas variáveis para preservar a escolha após reinícios. A planilha precisa continuar acessível à conta de serviço.

O Render Free pode suspender o serviço após 15 minutos sem tráfego e levar cerca de um minuto para acordar, além do tempo de leitura das abas. Mensagens aguardando na fila e o controle de duplicatas antigo também podem ser perdidos ao reiniciar. Esta configuração é para testes.

Para persistir tudo futuramente, será necessário armazenamento externo ou um disco persistente de um serviço pago.
