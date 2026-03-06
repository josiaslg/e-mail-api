# Manual de Deploy — Ubuntu 24.04

Este guia cobre deploy de produção do **Python-MTA-API-Gateway** com:

- FastAPI
- Gunicorn + UvicornWorker
- systemd
- Nginx (reverse proxy)
- TLS com Let's Encrypt

> Exemplo assume domínio `api.example.com` e diretório `/opt/python-mta-api-gateway`.

---

## 1) Pré-requisitos

No servidor Ubuntu 24.04, como usuário com sudo:

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3-pip nginx certbot python3-certbot-nginx
```

Opcional (ver diagnóstico):

```bash
sudo apt install -y curl jq
```

---

## 2) Criar usuário de serviço (opcional, recomendado)

```bash
sudo useradd --system --home /opt/python-mta-api-gateway --shell /usr/sbin/nologin mailgateway || true
```

---

## 3) Publicar o código

```bash
sudo mkdir -p /opt/python-mta-api-gateway
sudo chown -R $USER:$USER /opt/python-mta-api-gateway
cd /opt/python-mta-api-gateway

# opção A: clone
git clone <SEU_REPOSITORIO_GIT> .

# opção B: upload rsync/scp do projeto para esta pasta
```

---

## 4) Ambiente Python + dependências

```bash
cd /opt/python-mta-api-gateway
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

---

## 5) Configurar variáveis de ambiente

```bash
cp .env.example .env
nano .env
```

Exemplo mínimo recomendado:

```env
GATEWAY_API_KEY=troque-por-uma-chave-forte
MAIL_SERVER_HOST=mail.example.com
MAIL_TIMEOUT_SECONDS=20

SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_SECURITY_MODE=TLS
SMTP_AUTH_METHOD=PLAIN
SMTP_DEFAULT_FROM=no-reply@example.com

IMAP_HOST=imap.example.com
IMAP_PORT=993
IMAP_SECURITY_MODE=SSL
IMAP_AUTH_METHOD=PLAIN
```

> Dica: gere uma chave forte com `openssl rand -hex 32`.

Permissões do `.env`:

```bash
chmod 600 .env
```

---

## 6) Instalar unit do systemd

O projeto já possui `deploy/python-mta-api-gateway.service` e `deploy/service.systemd`.

Copie o unit file para o systemd:

```bash
sudo cp deploy/python-mta-api-gateway.service /etc/systemd/system/python-mta-api-gateway.service
```

Ajuste usuário/grupo no arquivo se necessário (`www-data` ou `mailgateway`):

```bash
sudo nano /etc/systemd/system/python-mta-api-gateway.service
```

Recarregue e habilite:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now python-mta-api-gateway.service
sudo systemctl status python-mta-api-gateway.service --no-pager
```

Logs do serviço:

```bash
journalctl -u python-mta-api-gateway.service -f
```

---

## 7) Configurar Nginx (reverse proxy)

Copie o arquivo de exemplo:

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/python-mta-api-gateway.conf
```

Edite `server_name` para seu domínio:

```bash
sudo nano /etc/nginx/sites-available/python-mta-api-gateway.conf
```

Ative o site e valide:

```bash
sudo ln -sf /etc/nginx/sites-available/python-mta-api-gateway.conf /etc/nginx/sites-enabled/python-mta-api-gateway.conf
sudo nginx -t
sudo systemctl reload nginx
```

---

## 8) TLS com Let's Encrypt

Com DNS do domínio apontando para o servidor:

```bash
sudo certbot --nginx -d api.example.com
```

Teste renovação automática:

```bash
sudo certbot renew --dry-run
```

---

## 9) Firewall (opcional, recomendado)

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
sudo ufw status
```

---

## 10) Validação pós-deploy

Healthcheck:

```bash
curl -sS https://api.example.com/healthz | jq .
```

Teste autenticação gateway (deve falhar sem chave):

```bash
curl -i https://api.example.com/v1/inbox
```

Teste com `X-API-KEY`:

```bash
curl -sS -X POST "https://api.example.com/v1/inbox" \
  -H "Content-Type: application/json" \
  -H "X-API-KEY: SUA_CHAVE" \
  -d '{"user_auth":"usuario@example.com","pass_auth":"senha","limit":3}'
```

---

## 11) Troubleshooting rápido

### Serviço não sobe

```bash
sudo systemctl status python-mta-api-gateway.service --no-pager
journalctl -u python-mta-api-gateway.service -n 200 --no-pager
```

### Nginx com erro de config

```bash
sudo nginx -t
```

### Erro 500 no `/healthz`

- Verifique `.env` (portas, modos `*_SECURITY_MODE`, `*_AUTH_METHOD`, variáveis obrigatórias).
- Verifique permissões do `.env` e `EnvironmentFile` no unit file.

### Erro 502/504 nos endpoints

- Validar conectividade do servidor API com SMTP/IMAP (`host`, `porta`, firewall, DNS).
- Validar modo TLS/SSL configurado no `.env`.
- Validar credenciais do mailbox e método de auth (`PLAIN`, `MD5`, etc).

---

## 12) Operação diária

Reiniciar serviço:

```bash
sudo systemctl restart python-mta-api-gateway.service
```

Atualizar versão:

```bash
cd /opt/python-mta-api-gateway
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart python-mta-api-gateway.service
```

---

## 13) Hardening recomendado

- Manter `GATEWAY_API_KEY` forte e rotacionar periodicamente.
- Restringir origem no Nginx (allowlist IP) se possível.
- Rodar serviço com usuário não privilegiado.
- Ativar monitoramento de logs e alertas para excesso de `401`, `502`, `504`.
- Manter pacotes e dependências atualizados.
