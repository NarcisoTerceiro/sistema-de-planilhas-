# Sistema completo na Oracle Cloud (grátis) com Docker

Roda o **Data Formulator completo + WhatsApp** numa máquina gratuita da Oracle
Cloud (Always Free), com HTTPS automático. Ao contrário do Render Free (512 MB,
que derruba o agente por falta de memória), a Oracle Free dá **até 12 GB de RAM**
— sobra de folga para a análise do agente.

É **gratuito e permanente** (não é teste de 30 dias). Pede cartão no cadastro,
mas o nível Always Free não cobra enquanto você ficar dentro dele.

## O que você vai ter no fim

- `https://seu-dominio/` → interface completa do Data Formulator (protegida por senha).
- `https://seu-dominio/chatbot-whatsapp` → painel do chatbot.
- `https://seu-dominio/webhooks/whatsapp` → webhook para a Meta.
- Dados e fila que **sobrevivem a reinícios** (volume Docker), sem hibernação.

---

## 1. Criar a máquina na Oracle (uma vez)

1. Crie conta em https://www.oracle.com/cloud/free/ (escolha a região mais perto).
2. No menu → **Compute → Instances → Create instance**.
3. **Image and shape → Change shape → Ampere (ARM)**. Escolha **VM.Standard.A1.Flex**
   com **2 OCPUs e 12 GB de memória** (dentro do Always Free).
   - Se aparecer "out of capacity", tente outra região ou outro horário.
4. Em **Image**, escolha **Canonical Ubuntu 22.04**.
5. Em **Add SSH keys**, salve a chave privada (você vai usar para entrar).
6. Crie a instância e anote o **IP público**.

### Abrir as portas 80 e 443

7. Na página da instância → **Virtual Cloud Network** (clique na sub-rede) →
   **Security Lists** → **Default Security List** → **Add Ingress Rules**:
   - Source `0.0.0.0/0`, protocolo TCP, porta **80**.
   - Source `0.0.0.0/0`, protocolo TCP, porta **443**.

---

## 2. Domínio HTTPS grátis (DuckDNS)

A Meta exige HTTPS com certificado válido, e isso precisa de um domínio (um IP
puro não serve). O DuckDNS dá um subdomínio grátis:

1. Entre em https://www.duckdns.org (login com Google/GitHub).
2. Crie um subdomínio, ex.: `obras-pb.duckdns.org`.
3. No campo **current ip**, coloque o **IP público da sua VM** e salve.

---

## 3. Instalar o Docker na VM

Entre por SSH (`ssh -i sua-chave ubuntu@IP_DA_VM`) e rode:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
# Dentro da Oracle, libere as portas também no firewall da própria VM:
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

Saia e entre de novo no SSH (para o grupo `docker` valer).

---

## 4. Baixar o projeto e configurar

```bash
git clone https://github.com/NarcisoTerceiro/sistema-de-planilhas-.git
cd sistema-de-planilhas-

# Arquivo de variáveis
cp .env.oracle.example .env
nano .env        # preencha DOMAIN, senhas, chave NVIDIA, GOOGLE_SPREADSHEET_ID...

# JSON da conta de serviço do Google (NÃO vai para o GitHub)
mkdir -p secrets
nano secrets/google-service-account.json   # cole o conteúdo do JSON e salve
```

No `.env`, preencha com atenção:
- `DOMAIN` = seu subdomínio do DuckDNS (ex.: `obras-pb.duckdns.org`).
- `FLASK_SECRET_KEY` = gere com `python3 -c "import secrets; print(secrets.token_hex(32))"`.
- `WEB_ADMIN_PASSWORD` = senha com 12+ caracteres.
- `NVIDIA_API_KEY`, `GOOGLE_SPREADSHEET_ID` = os seus valores.
- A planilha deve estar **compartilhada** com o e-mail da conta de serviço.

---

## 5. Subir o sistema

```bash
docker compose up -d --build
```

A primeira vez demora alguns minutos (baixa e instala as dependências). O Caddy
pega o certificado HTTPS sozinho assim que o DOMAIN estiver apontando para o IP.

Ver os logs:
```bash
docker compose logs -f app
```
Procure por `Conector Google e módulos do Data Formulator carregados no startup.`

Abra `https://seu-dominio.duckdns.org/chatbot-whatsapp`, entre com o usuário e
senha de admin, e teste o agente. **Agora a análise tem 12 GB — não vai estourar
memória como no Render.**

---

## 6. Conectar o WhatsApp (quando quiser)

1. No `.env`, preencha as variáveis `WHATSAPP_*` e `META_APP_SECRET`, e mude
   `WHATSAPP_ENABLED=true`.
2. `docker compose up -d` (recria só o que mudou).
3. No painel da Meta, configure o webhook:
   - Callback URL: `https://seu-dominio.duckdns.org/webhooks/whatsapp`
   - Verify token: o mesmo valor de `WHATSAPP_VERIFY_TOKEN`.
   - Assine o evento `messages`.

---

## Comandos do dia a dia

```bash
docker compose logs -f app      # ver logs
docker compose restart app      # reiniciar
docker compose down             # parar tudo
git pull && docker compose up -d --build   # atualizar após mudanças no código
```

## Observações

- **Um único processo** (1 worker gunicorn): a fila SQLite e o worker do WhatsApp
  foram feitos para isso. Não aumente `--workers`.
- Os dados ficam no volume `df_data` e persistem entre reinícios. Como são
  persistentes, a reimportação automática do Google só roda quando não há dados
  publicados ainda — diferente do Render, onde rodava a cada reinício.
- O sistema inteiro roda dentro do container, isolado do resto da VM.
- Custo: **R$ 0** enquanto a instância ficar no formato Always Free (2 OCPU / 12 GB).
