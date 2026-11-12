"""Teste rápido da API. Rode com:  python teste_api.py"""
import os
import tempfile

os.environ["BANCO_DE_DADOS"] = os.path.join(tempfile.mkdtemp(), "teste.db")

from app import app  # noqa: E402

c = app.test_client()


def ok(condicao, nome):
    print(("OK   " if condicao else "FALHOU ") + nome)
    assert condicao, nome


r = c.get("/")
ok(r.status_code == 200, "API no ar")

ok(c.get("/sincronizar").status_code == 401, "sem login não entra")
ok(c.post("/cadastro", json={"email": "a@teste.com", "senha": "123"}).status_code == 400, "senha curta é recusada")

r = c.post("/cadastro", json={"email": "A@Teste.com", "senha": "segredo1"})
ok(r.status_code == 201, "cadastro")
H = {"Authorization": "Bearer " + r.get_json()["token"]}
ok(c.post("/cadastro", json={"email": "a@teste.com", "senha": "segredo1"}).status_code == 409, "e-mail repetido é recusado")
ok(c.post("/login", json={"email": "a@teste.com", "senha": "errada"}).status_code == 401, "senha errada é recusada")
ok(c.post("/login", json={"email": "a@teste.com", "senha": "segredo1"}).status_code == 200, "login")

r = c.get("/sincronizar", headers=H).get_json()
ok(r["items"] == [] and r["iniciado"] is False, "conta nova começa vazia")

r = c.post("/bancos", headers=H, json={"nome": "Banco A", "saldo": 150000, "limite": 300000, "usado": 50000,
                                      "chave": "nubank", "cor": "#820AD1", "logo": "data:image/png;base64,AAAA"})
ok(r.status_code == 201, "cria banco")
ok(c.get("/bancos", headers=H).get_json()[0]["cor"] == "#820AD1", "guarda a cor e o banco escolhido")
ok(c.post("/bancos", headers=H, json={"nome": "X", "cor": "roxo"}).status_code == 400, "cor inválida é recusada")
banco = r.get_json()["id"]

c.post("/lancamentos", headers=H, json={"tipo": "entrada", "desc": "Salário", "valor": 320000, "data": "2026-06-05",
                                        "cat": "Salário", "forma": "Transferência", "fixo": True, "quinto": True, "banco": banco})
c.post("/lancamentos", headers=H, json={"tipo": "saida", "desc": "Luz", "valor": 18000, "data": "2026-06-10",
                                        "cat": "Luz", "forma": "Boleto", "fixo": True, "aPagar": True})
r = c.post("/lancamentos", headers=H, json={"tipo": "saida", "desc": "Celular", "valor": 100000, "data": "2026-10-18",
                                            "cat": "Compras", "forma": "Crédito", "parcelas": 3})
cel = r.get_json()["id"]
c.post("/lancamentos", headers=H, json={"tipo": "saida", "valor": 62000, "data": "2026-11-06", "cat": "Mercado", "forma": "Pix"})
c.post("/lancamentos", headers=H, json={"tipo": "saida", "valor": 20000, "data": "2026-11-07", "cat": "Reserva", "forma": "Pix"})
ok(c.post("/lancamentos", headers=H, json={"tipo": "saida", "valor": -5, "data": "2026-11-07", "cat": "X", "forma": "Pix"}).status_code == 400,
   "valor negativo é recusado")
c.put("/meta", headers=H, json={"meta": 30000})

r = c.get("/resumo?mes=2026-11", headers=H).get_json()
# parcelas de 100000 em 3x: 33334 + 33333 + 33333 -> novembro é a 2ª = 33333
ok(r["entrou"] == 320000, "resumo: entrou")
ok(r["saiu"] == 18000 + 33333 + 62000, "resumo: saiu (fixa + parcela + mercado)")
ok(r["guardado"] == 20000, "resumo: guardado")
ok(r["livre_para_gastar"] == 320000 - 113333 - 30000, "resumo: livre para gastar desconta a meta")
ok(r["falta_pagar"] == 18000 + 33333, "resumo: falta pagar")
ok(r["mais_gastou"] == "Mercado" and r["menos_gastou"] == "Luz", "resumo: mais e menos gastou")

r = c.get("/lancamentos?mes=2026-10", headers=H).get_json()
ok(next(i for i in r if i["id"] == cel)["valorNoMes"] == 33334, "1ª parcela leva os centavos que sobram")
ok(c.get("/lancamentos?mes=2027-01", headers=H).get_json().__len__() == 2, "depois da última parcela só ficam os fixos")

r = c.put("/lancamentos/" + cel, headers=H, json={"tipo": "saida", "desc": "Celular novo", "valor": 120000, "data": "2026-10-18",
                                                  "cat": "Compras", "forma": "Crédito", "parcelas": 4})
ok(r.status_code == 200 and r.get_json()["desc"] == "Celular novo", "altera lançamento")
ok(c.delete("/lancamentos/" + cel, headers=H).status_code == 200, "apaga lançamento")
ok(c.delete("/lancamentos/" + cel, headers=H).status_code == 404, "apagar de novo dá 404")

tudo = c.get("/sincronizar", headers=H).get_json()
ok(len(tudo["items"]) == 4 and len(tudo["bancos"]) == 1 and tudo["iniciado"], "baixa tudo")
tudo["perfil"] = {"nome": "Teste", "foto": "data:image/jpeg;base64,AAAA"}
tudo["items"][0]["pagos"] = {"24322": 1794350000000}
ok(c.put("/sincronizar", headers=H, json=tudo).status_code == 200, "envia tudo")
de_volta = c.get("/sincronizar", headers=H).get_json()
ok(de_volta["perfil"]["nome"] == "Teste" and len(de_volta["items"]) == 4, "o que foi enviado volta igual")
ok(any(i["pagos"] == {"24322": 1794350000000} for i in de_volta["items"]), "contas pagas são guardadas")

r2 = c.post("/cadastro", json={"email": "b@teste.com", "senha": "segredo2"}).get_json()
H2 = {"Authorization": "Bearer " + r2["token"]}
ok(c.get("/sincronizar", headers=H2).get_json()["items"] == [], "um usuário não vê os dados do outro")
ok(c.delete("/bancos/" + banco, headers=H2).status_code == 404, "um usuário não apaga o banco do outro")

r = c.post("/bancos", headers=H, json={"nome": "Cartão", "saldo": 0, "limite": 300000, "usado": 0, "fech": 3, "venc": 10})
cartao = r.get_json()["id"]
c.post("/lancamentos", headers=H, json={"tipo": "saida", "desc": "Tênis", "valor": 30000, "data": "2026-11-11",
                                        "cat": "Compras", "forma": "Crédito", "banco": cartao})
nov = c.get("/lancamentos?mes=2026-11", headers=H).get_json()
dez = c.get("/lancamentos?mes=2026-12", headers=H).get_json()
ok(not any(i["desc"] == "Tênis" for i in nov) and any(i["desc"] == "Tênis" for i in dez),
   "compra no crédito depois do fechamento cai na fatura do mês seguinte")
ok(c.get("/resumo?mes=2026-12", headers=H).get_json()["falta_pagar"] >= 30000, "compra no crédito entra em falta pagar")

import clt  # noqa: E402
ok(clt.inss(847555) == 98809 and clt.inss(2000000) == 98809, "CLT: INSS máximo de R$ 988,09 (teto 2026)")
ok(clt.inss(162100) == 12158 and clt.inss(300000) == 24860, "CLT: INSS progressivo")
ir = clt.irrf(600000, 0, 0)
ok((ir["tabela"], ir["reducao"], ir["valor"]) == (57429, 17975, 39454), "CLT: exemplo da Receita (R$ 6.000 -> IR R$ 394,54)")
ok(clt.irrf(500000, clt.inss(500000))["valor"] == 0, "CLT: até R$ 5.000 não paga IR")
ok(clt.irrf(800000, clt.inss(800000))["reducao"] == 0, "CLT: acima de R$ 7.350 sem redução")
d = clt.decimo_terceiro(300000, 12, 0)
ok((d["parcela1"], d["inss"], d["irrf"], d["parcela2"]) == (150000, 24860, 0, 125140), "CLT: 13º de R$ 3.000")
d = clt.decimo_terceiro(600000, 12, 0)
ok((d["inss"], d["irrf"], d["liquido"]) == (64151, 38510, 497339), "CLT: 13º de R$ 6.000 com redução parcial")
f = clt.ferias(300000, 30, True, 0)
ok((f["dias_gozados"], f["abono"] + f["abono_terco"], f["inss"], f["liquido"]) == (20, 133333, 21569, 378431), "CLT: férias vendendo 10 dias")
tudo = c.get("/sincronizar", headers=H).get_json()
tudo["clt"] = {"bruto": 600000, "meses": 12, "dias": 30, "abono": False, "dep": 0, "extras": 0}
c.put("/sincronizar", headers=H, json=tudo)
r = c.get("/clt", headers=H).get_json()
ok(r["decimo_terceiro"]["liquido"] == 497339 and r["ferias"]["liquido"] == 604064, "rota /clt usa o salário bruto salvo")
r = c.get("/clt?bruto=300000&vender=1", headers=H).get_json()
ok(r["ferias"]["liquido"] == 378431, "rota /clt aceita valores na URL")

ok(c.post("/sair", headers=H).status_code == 200, "sair")
ok(c.get("/sincronizar", headers=H).status_code == 401, "token antigo deixa de valer")
print("\nTudo certo.")
