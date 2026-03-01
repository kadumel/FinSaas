# FinSaaS

Aplicação de gestão financeira (contas a pagar/receber, lançamentos, transferências, cadastros).

## Tecnologias

- Python 3.12
- Django 6
- PostgreSQL
- Tailwind CSS (templates)

## Desenvolvimento local

1. Clone o repositório e crie um ambiente virtual:

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

2. Crie o arquivo `.env` na raiz (use `.env.example` como referência):

```env
DJANGO_SECRET_KEY=sua-chave-secreta
DB_NAME=finsaas
DB_USER=postgres
DB_PASSWORD=sua-senha
DB_HOST=localhost
DB_PORT=5432
```

3. Execute as migrações e suba o servidor:

```bash
python manage.py migrate
python manage.py runserver
```

Acesse `http://127.0.0.1:8000`.

## Git – enviar para o repositório

1. Inicialize o Git (se ainda não tiver):

```bash
git init
```

2. Adicione o remote (substitua pela URL do seu repositório):

```bash
git remote add origin https://github.com/SEU_USUARIO/FinSaas.git
```

3. Adicione os arquivos, faça commit e envie:

```bash
git add .
git commit -m "Configuração inicial para deploy"
git branch -M main
git push -u origin main
```

O arquivo `.env` não será enviado (está no `.gitignore`). No Railway você configura as variáveis pelo painel.

## Deploy no Railway

1. **Crie uma conta** em [railway.app](https://railway.app) e faça login.

2. **Novo projeto**  
   - Clique em **New Project**.  
   - Escolha **Deploy from GitHub repo** e selecione o repositório do FinSaas (conecte o GitHub se precisar).

3. **Adicione o PostgreSQL**  
   - No projeto, clique em **+ New** → **Database** → **PostgreSQL**.  
   - O Railway cria o serviço e define automaticamente a variável **DATABASE_URL** no projeto.  
   - Vincule essa variável ao **serviço da aplicação**: no serviço do app, em **Variables**, use **Add variable** → **Add a variable reference** e referencie a variável do Postgres (ou deixe as variáveis compartilhadas no mesmo projeto).

4. **Variáveis de ambiente do serviço da aplicação**  
   No serviço da sua aplicação (não no banco), em **Variables**, configure:

   | Variável            | Valor / Observação |
   |---------------------|--------------------|
   | `DJANGO_SECRET_KEY` | Chave secreta forte (ex.: gerada com `python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"`) |
   | `DATABASE_URL`      | Se o Postgres está no mesmo projeto, o Railway já pode injetar; caso contrário, use a URL de conexão fornecida pelo serviço PostgreSQL. |

   O projeto já está preparado para usar `DATABASE_URL` (via `dj-database-url`). Não é obrigatório configurar `PGHOST`, `PGPORT`, etc., se `DATABASE_URL` estiver definido.

5. **Domínio público**  
   - No serviço da aplicação: **Settings** → **Networking** → **Generate Domain**.  
   - Anote a URL (ex.: `https://seu-app.up.railway.app`).  
   - Se o login/CSRF falhar, em **Variables** adicione:  
     `CSRF_TRUSTED_ORIGINS` = `https://seu-app.up.railway.app`  
     (ou a URL exata que o Railway mostrar).

6. **Deploy**  
   - O Railway detecta o Django pelo `manage.py` e usa o **Procfile**:
     - **release**: `migrate` e `collectstatic`
     - **web**: `gunicorn finsaas.wsgi`
   - A cada push na branch conectada, um novo deploy é disparado.

7. **Criar superusuário (opcional)**  
   No dashboard do serviço da aplicação, abra **Settings** → **Deploy** e em **Custom start command** deixe em branco (para usar o Procfile).  
   Para criar um superusuário, use o **Railway CLI** ou um job one-off (se disponível), ou rode localmente apontando para o `DATABASE_URL` de produção (com cuidado).

### Resumo das variáveis no Railway

- **Obrigatório:** `DJANGO_SECRET_KEY`, `DATABASE_URL` (ou conexão Postgres fornecida pelo Railway).
- **Recomendado após gerar domínio:** `CSRF_TRUSTED_ORIGINS` = sua URL pública (ex.: `https://seu-app.up.railway.app`).

## Estrutura do projeto

- `finsaas/` – configurações do Django (settings, urls, wsgi).
- `core/` – app base (dashboard, tenant, empresas).
- `accounts/` – usuários e autenticação.
- `finance/` – módulo financeiro (cadastros, movimentos, relatórios).
- `templates/` – templates globais.
- `Procfile` – comandos de release e web para Railway (e similares).

## Licença

Uso interno / proprietário.
