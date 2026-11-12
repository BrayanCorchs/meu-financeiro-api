"""
Meu Financeiro - API em Python (Flask + SQLite).

Rodar no computador:
    pip install -r requirements.txt
    python app.py
A API sobe em http://localhost:5000

Variáveis de ambiente (opcionais):
    BANCO_DE_DADOS       caminho do arquivo SQLite (padrão: financeiro.db nesta pasta)
    ORIGENS_PERMITIDAS   sites que podem chamar a API, separados por vírgula
                         (ex.: https://meu-financeiro.vercel.app). Padrão: * (qualquer site)
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import date
from functools import wraps

from flask import Flask, g, jsonify, request

import clt

PASTA = os.path.dirname(os.path.abspath(__file__))
BANCO_DE_DADOS = os.environ.get("BANCO_DE_DADOS", os.path.join(PASTA, "financeiro.db"))
ORIGENS = [o.strip() for o in os.environ.get("ORIGENS_PERMITIDAS", "*").split(",") if o.strip()]
RESERVA = "Reserva"            # categoria usada para dinheiro guardado
VALIDADE_SESSAO = 90 * 24 * 3600  # 90 dias, em segundos
ITERACOES_SENHA = 200_000

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2 MB por requisição
app.json.ensure_ascii = False


# ----------------------------------------------------------------------------
# Banco de dados
# ----------------------------------------------------------------------------
ESQUEMA = """
CREATE TABLE IF NOT EXISTS usuarios (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT NOT NULL UNIQUE,
    senha_hash TEXT NOT NULL,
    sal        TEXT NOT NULL,
    nome       TEXT NOT NULL DEFAULT '',
    foto       TEXT NOT NULL DEFAULT '',
    meta       INTEGER NOT NULL DEFAULT 0,      -- centavos por mês
    iniciado   INTEGER NOT NULL DEFAULT 0,
    clt        TEXT NOT NULL DEFAULT '{}',      -- dados para 13º e férias
    criado_em  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessoes (
    token_hash TEXT PRIMARY KEY,
    usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    criado_em  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bancos (
    usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    id         TEXT NOT NULL,
    nome       TEXT NOT NULL,
    saldo      INTEGER NOT NULL DEFAULT 0,      -- centavos
    limite     INTEGER NOT NULL DEFAULT 0,
    usado      INTEGER NOT NULL DEFAULT 0,
    em         INTEGER NOT NULL DEFAULT 0,      -- quando o saldo foi informado (ms)
    chave      TEXT NOT NULL DEFAULT '',        -- banco escolhido na lista (ex.: nubank)
    cor        TEXT NOT NULL DEFAULT '',        -- cor do cartão (#rrggbb)
    logo       TEXT NOT NULL DEFAULT '',        -- imagem enviada pelo usuário (data:image/...)
    fech       INTEGER NOT NULL DEFAULT 0,      -- dia em que a fatura fecha (0 = não informado)
    venc       INTEGER NOT NULL DEFAULT 0,      -- dia em que a fatura vence (0 = não informado)
    PRIMARY KEY (usuario_id, id)
);
CREATE TABLE IF NOT EXISTS lancamentos (
    usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    id         TEXT NOT NULL,
    tipo       TEXT NOT NULL,                   -- entrada | saida
    descricao  TEXT NOT NULL,
    valor      INTEGER NOT NULL,                -- centavos (total, se parcelado)
    data       TEXT NOT NULL,                   -- AAAA-MM-DD
    categoria  TEXT NOT NULL,
    forma      TEXT NOT NULL,
    parcelas   INTEGER NOT NULL DEFAULT 1,
    fixo       INTEGER NOT NULL DEFAULT 0,
    a_pagar    INTEGER NOT NULL DEFAULT 0,
    quinto     INTEGER NOT NULL DEFAULT 0,      -- cai no 5º dia útil
    banco_id   TEXT,
    criado     INTEGER,                         -- quando foi lançado (ms)
    pagos      TEXT NOT NULL DEFAULT '{}',      -- {"indice_do_mes": quando_pagou}
    PRIMARY KEY (usuario_id, id)
);
"""


def conectar():
    con = sqlite3.connect(BANCO_DE_DADOS)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def db():
    if "db" not in g:
        g.db = conectar()
    return g.db


@app.teardown_appcontext
def fechar_db(_erro):
    con = g.pop("db", None)
    if con is not None:
        con.close()


def criar_tabelas():
    con = conectar()
    con.executescript(ESQUEMA)
    colunas = [c[1] for c in con.execute("PRAGMA table_info(usuarios)")]
    if "clt" not in colunas:  # bancos criados por versões anteriores
        con.execute("ALTER TABLE usuarios ADD COLUMN clt TEXT NOT NULL DEFAULT '{}'")
    colunas = [c[1] for c in con.execute("PRAGMA table_info(bancos)")]
    for coluna, tipo in (("chave", "TEXT NOT NULL DEFAULT ''"), ("cor", "TEXT NOT NULL DEFAULT ''"), ("logo", "TEXT NOT NULL DEFAULT ''"),
                         ("fech", "INTEGER NOT NULL DEFAULT 0"), ("venc", "INTEGER NOT NULL DEFAULT 0")):
        if coluna not in colunas:
            con.execute(f"ALTER TABLE bancos ADD COLUMN {coluna} {tipo}")
    con.commit()
    con.close()


criar_tabelas()


# ----------------------------------------------------------------------------
# Erros e CORS
# ----------------------------------------------------------------------------
class Erro(Exception):
    def __init__(self, mensagem, codigo=400):
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.codigo = codigo


@app.errorhandler(Erro)
def tratar_erro(e):
    return jsonify(detail=e.mensagem), e.codigo


@app.errorhandler(404)
def nao_encontrado(_e):
    return jsonify(detail="Endereço não encontrado."), 404


@app.errorhandler(405)
def metodo_errado(_e):
    return jsonify(detail="Método não permitido neste endereço."), 405


@app.errorhandler(413)
def grande_demais(_e):
    return jsonify(detail="Dados grandes demais. Use uma foto menor."), 413


@app.before_request
def responder_preflight():
    if request.method == "OPTIONS":
        return "", 204


@app.after_request
def cabecalhos_cors(resposta):
    origem = request.headers.get("Origin", "")
    if "*" in ORIGENS:
        resposta.headers["Access-Control-Allow-Origin"] = "*"
    elif origem in ORIGENS:
        resposta.headers["Access-Control-Allow-Origin"] = origem
        resposta.headers["Vary"] = "Origin"
    resposta.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    resposta.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return resposta


# ----------------------------------------------------------------------------
# Senhas e sessões
# ----------------------------------------------------------------------------
def gerar_hash(senha, sal):
    return hashlib.pbkdf2_hmac("sha256", senha.encode(), bytes.fromhex(sal), ITERACOES_SENHA).hex()


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def abrir_sessao(usuario_id):
    token = secrets.token_urlsafe(32)
    db().execute("INSERT INTO sessoes (token_hash, usuario_id, criado_em) VALUES (?, ?, ?)",
                 (hash_token(token), usuario_id, int(time.time())))
    db().commit()
    return token


def logado(funcao):
    """Exige o cabeçalho  Authorization: Bearer <token>  e coloca o usuário em g.uid."""
    @wraps(funcao)
    def interna(*args, **kwargs):
        cabecalho = request.headers.get("Authorization", "")
        if not cabecalho.startswith("Bearer "):
            raise Erro("Entre na sua conta para continuar.", 401)
        linha = db().execute("SELECT usuario_id, criado_em FROM sessoes WHERE token_hash = ?",
                             (hash_token(cabecalho[7:]),)).fetchone()
        if linha is None or time.time() - linha["criado_em"] > VALIDADE_SESSAO:
            raise Erro("Sua sessão expirou. Entre de novo.", 401)
        g.uid = linha["usuario_id"]
        return funcao(*args, **kwargs)
    return interna


def corpo_json():
    dados = request.get_json(silent=True)
    if not isinstance(dados, dict):
        raise Erro("Envie os dados em JSON.")
    return dados


# ----------------------------------------------------------------------------
# Validação
# ----------------------------------------------------------------------------
RE_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def inteiro(valor, nome, minimo=None, maximo=None, padrao=None):
    if valor is None and padrao is not None:
        return padrao
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or int(valor) != valor:
        raise Erro(f"O campo {nome} precisa ser um número inteiro.")
    valor = int(valor)
    if (minimo is not None and valor < minimo) or (maximo is not None and valor > maximo):
        raise Erro(f"O campo {nome} está fora do intervalo permitido.")
    return valor


def texto(valor, nome, maximo, obrigatorio=True):
    if valor is None:
        valor = ""
    if not isinstance(valor, str):
        raise Erro(f"O campo {nome} precisa ser um texto.")
    valor = valor.strip()[:maximo]
    if obrigatorio and not valor:
        raise Erro(f"Preencha o campo {nome}.")
    return valor


def novo_id(prefixo):
    return prefixo + secrets.token_hex(6)


def limpar_lancamento(d):
    if not isinstance(d, dict):
        raise Erro("Lançamento inválido.")
    ident = d.get("id") or novo_id("l")
    if not isinstance(ident, str) or not RE_ID.match(ident):
        raise Erro("Identificador de lançamento inválido.")
    tipo = d.get("tipo")
    if tipo not in ("entrada", "saida"):
        raise Erro("O tipo precisa ser 'entrada' ou 'saida'.")
    data = texto(d.get("data"), "data", 10)
    try:
        date.fromisoformat(data)
    except ValueError:
        raise Erro("A data precisa estar no formato AAAA-MM-DD.")
    pagos = d.get("pagos") or {}
    if not isinstance(pagos, dict) or len(pagos) > 600:
        raise Erro("Campo pagos inválido.")
    pagos_limpos = {}
    for chave, quando in pagos.items():
        if not str(chave).isdigit():
            raise Erro("Campo pagos inválido.")
        pagos_limpos[str(chave)] = quando if isinstance(quando, (int, float)) and not isinstance(quando, bool) else True
    fixo = bool(d.get("fixo"))
    saida = tipo == "saida"
    banco = d.get("banco")
    if banco is not None and (not isinstance(banco, str) or not RE_ID.match(banco)):
        raise Erro("Banco inválido.")
    categoria = texto(d.get("cat"), "categoria", 40)
    return {
        "id": ident,
        "tipo": tipo,
        "desc": texto(d.get("desc"), "descrição", 60, obrigatorio=False) or categoria,
        "valor": inteiro(d.get("valor"), "valor", minimo=1, maximo=10**12),
        "data": data,
        "cat": categoria,
        "forma": texto(d.get("forma"), "forma de pagamento", 30),
        "parcelas": inteiro(d.get("parcelas"), "parcelas", 1, 48, padrao=1) if saida and not fixo else 1,
        "fixo": fixo,
        "aPagar": bool(d.get("aPagar")) and saida,
        "quinto": bool(d.get("quinto")) and not saida,
        "banco": banco,
        "criado": inteiro(d.get("criado"), "criado", 0) if d.get("criado") is not None else None,
        "pagos": pagos_limpos,
    }


def limpar_banco(d):
    if not isinstance(d, dict):
        raise Erro("Banco inválido.")
    ident = d.get("id") or novo_id("b")
    if not isinstance(ident, str) or not RE_ID.match(ident):
        raise Erro("Identificador de banco inválido.")
    limite = inteiro(d.get("limite"), "limite", 0, 10**12, padrao=0)
    chave = d.get("chave") or ""
    cor = d.get("cor") or ""
    logo = d.get("logo") or ""
    if not isinstance(chave, str) or not re.match(r"^[a-z0-9-]{0,30}$", chave):
        raise Erro("Banco da lista inválido.")
    if not isinstance(cor, str) or (cor and not re.match(r"^#[0-9a-fA-F]{6}$", cor)):
        raise Erro("Cor do cartão inválida.")
    if not isinstance(logo, str) or (logo and (not logo.startswith("data:image/") or len(logo) > 60000)):
        raise Erro("Logo inválida ou grande demais.")
    return {
        "chave": chave, "cor": cor, "logo": logo,
        "fech": inteiro(d.get("fech"), "dia de fechamento", 0, 31, padrao=0) if limite else 0,
        "venc": inteiro(d.get("venc"), "dia de vencimento", 0, 31, padrao=0) if limite else 0,
        "id": ident,
        "nome": texto(d.get("nome"), "nome do banco", 30),
        "saldo": inteiro(d.get("saldo"), "saldo", -10**12, 10**12, padrao=0),
        "limite": limite,
        "usado": inteiro(d.get("usado"), "limite usado", -10**12, 10**12, padrao=0),
        "em": inteiro(d.get("em"), "em", 0, padrao=int(time.time() * 1000)),
    }


def limpar_clt(c):
    c = c if isinstance(c, dict) else {}

    def numero(v, minimo, maximo, padrao):
        return v if isinstance(v, int) and not isinstance(v, bool) and minimo <= v <= maximo else padrao

    dias = numero(c.get("dias"), 1, 30, 30)
    return {"meses": numero(c.get("meses"), 1, 12, 12), "dias": dias if dias in clt.DIAS_DE_DIREITO else 30,
            "abono": bool(c.get("abono")), "bruto": numero(c.get("bruto"), 0, 10**12, 0),
            "extras": numero(c.get("extras"), 0, 10**12, 0), "dep": numero(c.get("dep"), 0, 20, 0)}


def limpar_perfil(p):
    p = p if isinstance(p, dict) else {}
    foto = p.get("foto") or ""
    if not isinstance(foto, str) or (foto and not foto.startswith("data:image/")) or len(foto) > 200_000:
        raise Erro("Foto inválida ou grande demais.")
    return {"nome": texto(p.get("nome"), "nome", 30, obrigatorio=False), "foto": foto}


# ----------------------------------------------------------------------------
# Leitura e gravação
# ----------------------------------------------------------------------------
def linha_para_lancamento(l):
    item = {
        "id": l["id"], "tipo": l["tipo"], "desc": l["descricao"], "valor": l["valor"], "data": l["data"],
        "cat": l["categoria"], "forma": l["forma"], "parcelas": l["parcelas"], "fixo": bool(l["fixo"]),
        "aPagar": bool(l["a_pagar"]), "pagos": json.loads(l["pagos"] or "{}"),
    }
    if l["quinto"]:
        item["quinto"] = True
    if l["banco_id"]:
        item["banco"] = l["banco_id"]
    if l["criado"]:
        item["criado"] = l["criado"]
    return item


def linha_para_banco(b):
    return {"id": b["id"], "nome": b["nome"], "saldo": b["saldo"], "limite": b["limite"], "usado": b["usado"], "em": b["em"],
            "chave": b["chave"], "cor": b["cor"], "logo": b["logo"], "fech": b["fech"], "venc": b["venc"]}


def listar_lancamentos(uid):
    linhas = db().execute("SELECT * FROM lancamentos WHERE usuario_id = ? ORDER BY data DESC, criado DESC", (uid,))
    return [linha_para_lancamento(l) for l in linhas]


def listar_bancos(uid):
    linhas = db().execute("SELECT * FROM bancos WHERE usuario_id = ? ORDER BY rowid", (uid,))
    return [linha_para_banco(b) for b in linhas]


def gravar_lancamento(uid, it):
    db().execute(
        """INSERT OR REPLACE INTO lancamentos
           (usuario_id, id, tipo, descricao, valor, data, categoria, forma, parcelas, fixo, a_pagar, quinto, banco_id, criado, pagos)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (uid, it["id"], it["tipo"], it["desc"], it["valor"], it["data"], it["cat"], it["forma"], it["parcelas"],
         int(it["fixo"]), int(it["aPagar"]), int(it["quinto"]), it["banco"], it["criado"], json.dumps(it["pagos"])))


def gravar_banco(uid, b):
    db().execute(
        """INSERT OR REPLACE INTO bancos (usuario_id, id, nome, saldo, limite, usado, em, chave, cor, logo, fech, venc)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (uid, b["id"], b["nome"], b["saldo"], b["limite"], b["usado"], b["em"], b["chave"], b["cor"], b["logo"], b["fech"], b["venc"]))


def marcar_iniciado(uid):
    db().execute("UPDATE usuarios SET iniciado = 1 WHERE id = ?", (uid,))


# ----------------------------------------------------------------------------
# Regras do mês (as mesmas contas que o app faz na tela)
# ----------------------------------------------------------------------------
def indice_do_mes(texto_mes):
    """'2026-11' ou '2026-11-10' -> número do mês contado desde o ano 0."""
    ano, mes = texto_mes.split("-")[:2]
    return int(ano) * 12 + int(mes) - 1


def deslocamento_fatura(it, bancos):
    """Compra no crédito com o vencimento do cartão cadastrado: em quantos meses ela cai na fatura.
    Comprou no dia do fechamento ou depois, vai para a fatura seguinte. Sem fechamento, considera 7 dias antes do vencimento."""
    if it["tipo"] != "saida" or it["forma"] != "Crédito" or not bancos:
        return 0
    b = bancos.get(it.get("banco"))
    if not b or not b.get("venc"):
        return 0
    venc = b["venc"]
    fech = b.get("fech") or (venc - 7 if venc > 7 else venc + 23)
    dia = int(it["data"][8:10])
    return (1 if dia >= fech else 0) + (0 if venc > fech else 1)


def ocorre(it, idx, bancos=None):
    """Quanto um lançamento pesa em um mês. Parcelado: total dividido; os centavos que sobram vão na 1ª parcela."""
    inicio = indice_do_mes(it["data"]) + deslocamento_fatura(it, bancos)
    if it["fixo"]:
        return {"valor": it["valor"]} if idx >= inicio else None
    n = it["parcelas"] or 1
    k = idx - inicio
    if k < 0 or k >= n:
        return None
    base = it["valor"] // n
    return {"valor": base + (it["valor"] - base * n if k == 0 else 0), "parcela": k + 1, "de": n}


def eh_conta(it):
    return it["tipo"] == "saida" and (it["aPagar"] or it["fixo"] or it["parcelas"] > 1 or it["forma"] == "Crédito")


def calcular_resumo(itens, idx, meta, bancos=None):
    entrou = saiu = guardado = falta_pagar = 0
    por_categoria, por_forma = {}, {}
    for it in itens:
        o = ocorre(it, idx, bancos)
        if not o:
            continue
        v = o["valor"]
        if it["tipo"] == "entrada":
            entrou += v
        elif it["cat"] == RESERVA:
            guardado += v
        else:
            saiu += v
            por_categoria[it["cat"]] = por_categoria.get(it["cat"], 0) + v
            por_forma[it["forma"]] = por_forma.get(it["forma"], 0) + v
        if eh_conta(it) and not it["pagos"].get(str(idx)):
            falta_pagar += v
    ranking = sorted(por_categoria.items(), key=lambda par: -par[1])
    reserva = max(meta, guardado)
    return {
        "entrou": entrou, "saiu": saiu, "guardado": guardado, "meta": meta,
        "sobra": entrou - saiu - guardado,
        "livre_para_gastar": entrou - saiu - reserva,
        "falta_pagar": falta_pagar,
        "por_categoria": [{"categoria": c, "valor": v} for c, v in ranking],
        "por_forma": [{"forma": f, "valor": v} for f, v in sorted(por_forma.items(), key=lambda par: -par[1])],
        "mais_gastou": ranking[0][0] if ranking else None,
        "menos_gastou": ranking[-1][0] if len(ranking) > 1 else None,
    }


def ler_mes(obrigatorio=False):
    mes = request.args.get("mes", "")
    if not mes:
        if obrigatorio:
            hoje = date.today()
            return hoje.year * 12 + hoje.month - 1
        return None
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", mes):
        raise Erro("Use o mês no formato AAAA-MM, por exemplo 2026-11.")
    return indice_do_mes(mes)


# ----------------------------------------------------------------------------
# Rotas: conta
# ----------------------------------------------------------------------------
@app.get("/")
def inicio():
    return jsonify(app="Meu Financeiro", status="no ar")


@app.post("/cadastro")
def cadastro():
    d = corpo_json()
    email = texto(d.get("email"), "e-mail", 120).lower()
    senha = d.get("senha")
    if not RE_EMAIL.match(email):
        raise Erro("Digite um e-mail válido.")
    if not isinstance(senha, str) or len(senha) < 6 or len(senha) > 200:
        raise Erro("A senha precisa ter pelo menos 6 caracteres.")
    sal = secrets.token_hex(16)
    try:
        cur = db().execute("INSERT INTO usuarios (email, senha_hash, sal, criado_em) VALUES (?, ?, ?, ?)",
                           (email, gerar_hash(senha, sal), sal, int(time.time())))
    except sqlite3.IntegrityError:
        raise Erro("Já existe uma conta com esse e-mail. Toque em Entrar.", 409)
    return jsonify(token=abrir_sessao(cur.lastrowid)), 201


@app.post("/login")
def login():
    d = corpo_json()
    email = texto(d.get("email"), "e-mail", 120).lower()
    senha = d.get("senha") if isinstance(d.get("senha"), str) else ""
    u = db().execute("SELECT id, senha_hash, sal FROM usuarios WHERE email = ?", (email,)).fetchone()
    # compara mesmo quando o e-mail não existe, para não revelar quais e-mails têm conta
    esperado = u["senha_hash"] if u else "0" * 64
    sal = u["sal"] if u else "00" * 16
    if not hmac.compare_digest(gerar_hash(senha[:200], sal), esperado) or u is None:
        raise Erro("E-mail ou senha incorretos.", 401)
    return jsonify(token=abrir_sessao(u["id"]))


@app.post("/sair")
@logado
def sair():
    db().execute("DELETE FROM sessoes WHERE token_hash = ?", (hash_token(request.headers["Authorization"][7:]),))
    db().commit()
    return jsonify(ok=True)


@app.get("/eu")
@logado
def eu():
    u = db().execute("SELECT email, nome, foto, meta FROM usuarios WHERE id = ?", (g.uid,)).fetchone()
    return jsonify(email=u["email"], nome=u["nome"], foto=u["foto"], meta=u["meta"])


# ----------------------------------------------------------------------------
# Rotas: sincronização (o que o app usa para salvar e carregar tudo)
# ----------------------------------------------------------------------------
@app.get("/sincronizar")
@logado
def baixar_tudo():
    u = db().execute("SELECT nome, foto, meta, iniciado, clt FROM usuarios WHERE id = ?", (g.uid,)).fetchone()
    return jsonify(items=listar_lancamentos(g.uid), bancos=listar_bancos(g.uid), meta=u["meta"],
                   perfil={"nome": u["nome"], "foto": u["foto"]}, iniciado=bool(u["iniciado"]),
                   clt=limpar_clt(json.loads(u["clt"] or "{}")))


@app.put("/sincronizar")
@logado
def enviar_tudo():
    d = corpo_json()
    itens, bancos = d.get("items") or [], d.get("bancos") or []
    if not isinstance(itens, list) or not isinstance(bancos, list) or len(itens) > 5000 or len(bancos) > 30:
        raise Erro("Quantidade de dados inválida.")
    itens = [limpar_lancamento(i) for i in itens]
    bancos = [limpar_banco(b) for b in bancos]
    perfil = limpar_perfil(d.get("perfil"))
    meta = inteiro(d.get("meta"), "meta", 0, 10**12, padrao=0)
    dados_clt = limpar_clt(d.get("clt"))
    con = db()
    try:
        con.execute("DELETE FROM lancamentos WHERE usuario_id = ?", (g.uid,))
        con.execute("DELETE FROM bancos WHERE usuario_id = ?", (g.uid,))
        for b in bancos:
            gravar_banco(g.uid, b)
        for it in itens:
            gravar_lancamento(g.uid, it)
        con.execute("UPDATE usuarios SET nome = ?, foto = ?, meta = ?, iniciado = ?, clt = ? WHERE id = ?",
                    (perfil["nome"], perfil["foto"], meta, int(bool(d.get("iniciado"))), json.dumps(dados_clt), g.uid))
        con.commit()
    except Exception:
        con.rollback()
        raise
    return jsonify(ok=True, lancamentos=len(itens), bancos=len(bancos))


# ----------------------------------------------------------------------------
# Rotas: lançamentos
# ----------------------------------------------------------------------------
@app.get("/lancamentos")
@logado
def ver_lancamentos():
    itens = listar_lancamentos(g.uid)
    idx = ler_mes()
    if idx is None:
        return jsonify(itens)
    bancos = {b["id"]: b for b in listar_bancos(g.uid)}
    do_mes = []
    for it in itens:
        o = ocorre(it, idx, bancos)
        if o:
            do_mes.append({**it, "valorNoMes": o["valor"], "parcela": o.get("parcela"), "de": o.get("de")})
    return jsonify(do_mes)


@app.post("/lancamentos")
@logado
def criar_lancamento():
    it = limpar_lancamento({**corpo_json(), "id": None, "criado": int(time.time() * 1000)})
    gravar_lancamento(g.uid, it)
    marcar_iniciado(g.uid)
    db().commit()
    return jsonify(it), 201


@app.put("/lancamentos/<ident>")
@logado
def alterar_lancamento(ident):
    atual = db().execute("SELECT * FROM lancamentos WHERE usuario_id = ? AND id = ?", (g.uid, ident)).fetchone()
    if atual is None:
        raise Erro("Lançamento não encontrado.", 404)
    antigo = linha_para_lancamento(atual)
    it = limpar_lancamento({"criado": antigo.get("criado"), "pagos": antigo["pagos"], **corpo_json(), "id": ident})
    gravar_lancamento(g.uid, it)
    db().commit()
    return jsonify(it)


@app.delete("/lancamentos/<ident>")
@logado
def apagar_lancamento(ident):
    cur = db().execute("DELETE FROM lancamentos WHERE usuario_id = ? AND id = ?", (g.uid, ident))
    db().commit()
    if cur.rowcount == 0:
        raise Erro("Lançamento não encontrado.", 404)
    return jsonify(ok=True)


# ----------------------------------------------------------------------------
# Rotas: bancos
# ----------------------------------------------------------------------------
@app.get("/bancos")
@logado
def ver_bancos():
    return jsonify(listar_bancos(g.uid))


@app.post("/bancos")
@logado
def criar_banco():
    b = limpar_banco({**corpo_json(), "id": None})
    gravar_banco(g.uid, b)
    marcar_iniciado(g.uid)
    db().commit()
    return jsonify(b), 201


@app.put("/bancos/<ident>")
@logado
def alterar_banco(ident):
    if db().execute("SELECT 1 FROM bancos WHERE usuario_id = ? AND id = ?", (g.uid, ident)).fetchone() is None:
        raise Erro("Banco não encontrado.", 404)
    b = limpar_banco({**corpo_json(), "id": ident})
    gravar_banco(g.uid, b)
    db().commit()
    return jsonify(b)


@app.delete("/bancos/<ident>")
@logado
def apagar_banco(ident):
    cur = db().execute("DELETE FROM bancos WHERE usuario_id = ? AND id = ?", (g.uid, ident))
    db().execute("UPDATE lancamentos SET banco_id = NULL WHERE usuario_id = ? AND banco_id = ?", (g.uid, ident))
    db().commit()
    if cur.rowcount == 0:
        raise Erro("Banco não encontrado.", 404)
    return jsonify(ok=True)


# ----------------------------------------------------------------------------
# Rotas: meta e resumo
# ----------------------------------------------------------------------------
@app.put("/meta")
@logado
def definir_meta():
    meta = inteiro(corpo_json().get("meta"), "meta", 0, 10**12)
    db().execute("UPDATE usuarios SET meta = ?, iniciado = 1 WHERE id = ?", (meta, g.uid))
    db().commit()
    return jsonify(meta=meta)


@app.get("/resumo")
@logado
def resumo():
    idx = ler_mes(obrigatorio=True)
    meta = db().execute("SELECT meta FROM usuarios WHERE id = ?", (g.uid,)).fetchone()["meta"]
    bancos = {b["id"]: b for b in listar_bancos(g.uid)}
    dados = calcular_resumo(listar_lancamentos(g.uid), idx, meta, bancos)
    dados["mes"] = f"{idx // 12:04d}-{idx % 12 + 1:02d}"
    return jsonify(dados)


# ----------------------------------------------------------------------------
# Rotas: 13º e férias (CLT, tabelas de 2026)
# ----------------------------------------------------------------------------
def salario_cadastrado(itens, idx):
    return sum(it["valor"] for it in itens if it["tipo"] == "entrada" and it["fixo"]
               and it["cat"] in ("Salário", "Adiantamento") and indice_do_mes(it["data"]) <= idx)


@app.get("/clt")
@logado
def calculos_clt():
    """13º e férias líquidos. Parâmetros opcionais na URL substituem o que está salvo:
    bruto, extras (centavos), dependentes, meses, dias (30/24/18/12), vender (1/0)."""
    u = db().execute("SELECT clt FROM usuarios WHERE id = ?", (g.uid,)).fetchone()
    c = limpar_clt(json.loads(u["clt"] or "{}"))
    a = request.args
    try:
        for chave, campo in (("bruto", "bruto"), ("extras", "extras"), ("dependentes", "dep"), ("meses", "meses"), ("dias", "dias")):
            if chave in a:
                c[campo] = int(a[chave])
    except ValueError:
        raise Erro("Os parâmetros precisam ser números inteiros (valores em centavos).")
    if "vender" in a:
        c["abono"] = a["vender"] in ("1", "true", "sim")
    c = limpar_clt(c)
    hoje = date.today()
    cadastrado = salario_cadastrado(listar_lancamentos(g.uid), hoje.year * 12 + hoje.month - 1)
    remuneracao = (c["bruto"] or cadastrado) + c["extras"]
    if remuneracao <= 0:
        raise Erro("Informe o salário bruto ou cadastre seu salário fixo no app.")
    return jsonify(ano_das_tabelas=clt.ANO, remuneracao=remuneracao, usando_salario_cadastrado=not c["bruto"],
                   parametros=c, decimo_terceiro=clt.decimo_terceiro(remuneracao, c["meses"], c["dep"]),
                   ferias=clt.ferias(remuneracao, c["dias"], c["abono"], c["dep"]))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
