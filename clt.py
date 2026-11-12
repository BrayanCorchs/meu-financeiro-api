"""
Cálculos trabalhistas (CLT) com as tabelas de 2026. Todos os valores em centavos.

- INSS do empregado: Portaria Interministerial MPS/MF nº 13/2026, progressivo por faixas,
  teto de R$ 8.475,55 (desconto máximo de R$ 988,09).
- IRRF: tabela mensal vigente, dedução por dependente de R$ 189,59 ou desconto simplificado
  de R$ 607,20 (o que for melhor) e a redução da Lei nº 15.270/2025:
    rendimento até R$ 5.000,00 ......... imposto zerado (redução de até R$ 312,89)
    de R$ 5.000,01 a R$ 7.350,00 ....... redução = 978,62 − 0,133145 × rendimento
    acima de R$ 7.350,00 ............... sem redução
- 13º salário e férias têm o IR calculado separado do salário do mês.
- Abono pecuniário (venda de férias) + 1/3: sem INSS e sem IR.
"""
import math

ANO = 2026
FAIXAS_INSS = [(162100, 0.075), (290284, 0.09), (435427, 0.12), (847555, 0.14)]
FAIXAS_IR = [(242880, 0.0, 0), (282665, 0.075, 18216), (375105, 0.15, 39416), (466468, 0.225, 67549), (math.inf, 0.275, 90873)]
DEDUCAO_DEPENDENTE = 18959
DESCONTO_SIMPLIFICADO = 60720
LIMITE_ISENCAO = 500000
LIMITE_REDUCAO = 735000
REDUCAO_MAXIMA = 31289
REDUCAO_A, REDUCAO_B = 97862, 0.133145
DIAS_DE_DIREITO = (30, 24, 18, 12)  # art. 130 da CLT, conforme as faltas


def arredonda(valor):
    """Arredonda centavos para o inteiro mais próximo (0,5 sobe), igual ao app."""
    return int(math.floor(valor + 0.5))


def inss(base):
    total, anterior = 0.0, 0
    for limite, aliquota in FAIXAS_INSS:
        if base > anterior:
            total += (min(base, limite) - anterior) * aliquota
        anterior = limite
    return arredonda(total)


def irrf(rendimento, inss_pago, dependentes=0):
    legal = inss_pago + dependentes * DEDUCAO_DEPENDENTE
    base = max(0, rendimento - max(legal, DESCONTO_SIMPLIFICADO))
    aliquota, deduzir = next((a, d) for limite, a, d in FAIXAS_IR if base <= limite)
    imposto = max(0, arredonda(base * aliquota - deduzir))
    if rendimento <= LIMITE_ISENCAO:
        reducao = min(imposto, REDUCAO_MAXIMA)
    elif rendimento <= LIMITE_REDUCAO:
        reducao = max(0, arredonda(REDUCAO_A - REDUCAO_B * rendimento))
    else:
        reducao = 0
    reducao = min(imposto, reducao)
    return {"base": base, "tabela": imposto, "reducao": reducao, "valor": imposto - reducao,
            "desconto_simplificado": DESCONTO_SIMPLIFICADO > legal}


def decimo_terceiro(remuneracao, meses=12, dependentes=0):
    total = arredonda(remuneracao * meses / 12)
    p1 = arredonda(total / 2)
    i = inss(total)
    ir = irrf(total, i, dependentes)
    return {"bruto": total, "parcela1": p1, "inss": i, "irrf": ir["valor"], "irrf_detalhe": ir,
            "parcela2": total - p1 - i - ir["valor"], "liquido": total - i - ir["valor"]}


def ferias(remuneracao, dias=30, vender=False, dependentes=0):
    vendidos = dias // 3 if vender else 0
    gozados = dias - vendidos
    valor = arredonda(remuneracao * gozados / 30)
    terco = arredonda(valor / 3)
    i = inss(valor + terco)
    ir = irrf(valor + terco, i, dependentes)
    abono = arredonda(remuneracao * vendidos / 30)
    abono_terco = arredonda(abono / 3)
    bruto = valor + terco + abono + abono_terco
    return {"dias_gozados": gozados, "dias_vendidos": vendidos, "ferias": valor, "terco": terco,
            "abono": abono, "abono_terco": abono_terco, "inss": i, "irrf": ir["valor"], "irrf_detalhe": ir,
            "bruto": bruto, "liquido": bruto - i - ir["valor"]}
