# Python-MTA-API-Gateway

API Gateway Stateless em FastAPI para operar SMTP (Postfix) e IMAP (Dovecot) com autenticação de dupla camada:

1. **Gateway Auth** via `X-API-KEY` (comparada com `GATEWAY_API_KEY` do `.env`).
2. **Mail Auth** via `user_auth/pass_auth` no body **ou** HTTP Basic Auth, validados em tempo real no servidor de e-mail.

## Requisitos

- Python 3.12+
- Acesso aos servidores de e-mail

## Configuração

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Variáveis de ambiente por serviço

Você pode configurar **host, porta, segurança e autenticação separadamente** para SMTP e IMAP.

### Fallback

- `MAIL_SERVER_HOST`: host global usado se `SMTP_HOST`/`IMAP_HOST` não estiverem definidos.
- `MAIL_TIMEOUT_SECONDS`: timeout para conexões.

### SMTP

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_SECURITY_MODE`: `AUTODETECT`, `TLS`, `SSL`, `NONE`
- `SMTP_AUTH_METHOD`: `AUTODETECT`, `PLAIN`, `MD5`, `NONE`
- `SMTP_DEFAULT_FROM`: remetente padrão quando `SMTP_AUTH_METHOD=NONE`

### IMAP

- `IMAP_HOST`
- `IMAP_PORT`
- `IMAP_SECURITY_MODE`: `AUTODETECT`, `TLS`, `SSL`, `NONE`
- `IMAP_AUTH_METHOD`: `AUTODETECT`, `PLAIN`, `MD5`, `NONE`

### Como funciona o `AUTODETECT`

- SMTP: porta `465` => `SSL`, outras => `TLS`.
- IMAP: porta `993` => `SSL`, outras => `TLS`.
- Auth `AUTODETECT` usa `PLAIN` (`login`) por padrão.

## Execução local

```bash
uvicorn app.main:app --reload --port 8000
```

## Endpoints

- `GET /healthz`
- `POST /v1/send` (**alias:** `POST /send`)
- `POST /v1/inbox` (**alias:** `POST /fetch`)

### POST `/v1/send`

```json
{
  "to": "destino@dominio.com",
  "subject": "Assunto",
  "body": "Texto da mensagem",
  "user_auth": "usuario@dominio.com",
  "pass_auth": "senha"
}
```

> `user_auth`/`pass_auth` podem ser omitidos no body se enviados via Basic Auth (exceto quando `*_AUTH_METHOD=NONE`).

### POST `/v1/inbox`

```json
{
  "user_auth": "usuario@dominio.com",
  "pass_auth": "senha",
  "limit": 10
}
```

## Segurança e logs

- Sem banco de dados e sem persistência de arquivos temporários.
- Logs estruturados com `timestamp`, `client_ip`, `action` e `status`.
- Nunca registra senha ou conteúdo de e-mail nos logs.

## Mapeamento de erros (gateway)

- `500`: configuração inválida no ambiente (ex.: porta fora de faixa, modo inválido).
- `504`: timeout ao conectar/autenticar/enviar/buscar no serviço de e-mail.
- `502`: falha de conexão, TLS/SSL ou erro upstream SMTP/IMAP.
- `400`: credenciais de e-mail ausentes quando o método de auth exige autenticação.

## Deploy (Ubuntu 24.04)

- Unit file systemd: `deploy/python-mta-api-gateway.service`
- Arquivo pedido no objetivo: `deploy/service.systemd`
- Exemplo de reverse proxy TLS: `deploy/nginx.conf`
- Manual completo passo a passo: `DEPLOY_UBUNTU_24.04.md`

## Manual de uso da API (formato para outra IA)

- Arquivo: `API_USAGE_MANUAL.txt`
