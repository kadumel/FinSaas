"""
Serviço de recálculo do saldo diário por conta financeira.

Sempre que houver inclusão ou alteração em baixas (contas a pagar/receber),
transferências ou lançamentos manuais, o saldo da conta afetada deve ser
recalculado a partir da data do movimento.
"""
from decimal import Decimal
from django.db.models import F, Sum
from django.db import transaction

from .models import (
    ContaFinanceira,
    ContaPagarBaixa,
    ContaReceberBaixa,
    Lancamento,
    Saldo,
    Transferencia,
)


def _todas_datas_com_movimento(conta: ContaFinanceira):
    """Retorna set de todas as datas que possuem algum movimento na conta."""
    from django.db.models import Value
    from django.db.models.functions import Cast
    from django.db.models import DateField

    datas = set()

    # Datas das baixas a receber
    qs_receber = ContaReceberBaixa.objects.filter(conta_financeira=conta).values_list(
        "data_pagamento", flat=True
    )
    datas.update(qs_receber.distinct())

    # Datas das baixas a pagar
    qs_pagar = ContaPagarBaixa.objects.filter(conta_financeira=conta).values_list(
        "data_pagamento", flat=True
    )
    datas.update(qs_pagar.distinct())

    # Datas dos lançamentos (crédito/débito manuais e estornos)
    qs_lanc = Lancamento.objects.filter(conta_financeira=conta).values_list(
        "data", flat=True
    )
    datas.update(qs_lanc.distinct())

    # Datas das transferências (origem = saída, destino = entrada)
    qs_trans_origem = Transferencia.objects.filter(conta_origem=conta).values_list(
        "data", flat=True
    )
    qs_trans_destino = Transferencia.objects.filter(conta_destino=conta).values_list(
        "data", flat=True
    )
    datas.update(qs_trans_origem.distinct())
    datas.update(qs_trans_destino.distinct())

    return sorted(datas)


def _credito_do_dia(conta: ContaFinanceira, data):
    """
    Somatório diário: baixas a receber + lançamentos de crédito.
    Transferências de entrada já entram como lançamentos de crédito na conta destino.
    """
    credito = Decimal("0")

    # Baixas de contas a receber (valor_pago + juros + multa = total que entra na conta)
    total_receber = (
        ContaReceberBaixa.objects.filter(
            conta_financeira=conta, data_pagamento=data
        ).aggregate(s=Sum(F("valor_pago") + F("juros") + F("multa")))["s"]
        or Decimal("0")
    )
    credito += total_receber

    # Lançamentos de crédito (inclui transferências entrada, que geram Lancamento TIPO_CREDITO)
    total_lanc_credito = (
        Lancamento.objects.filter(
            conta_financeira=conta,
            data=data,
            tipo=Lancamento.TIPO_CREDITO,
        ).aggregate(s=Sum("valor"))["s"]
        or Decimal("0")
    )
    credito += total_lanc_credito

    return credito


def _debito_do_dia(conta: ContaFinanceira, data):
    """
    Somatório diário: baixas a pagar + lançamentos de débito.
    Transferências de saída já entram como lançamentos de débito na conta origem.
    """
    debito = Decimal("0")

    # Baixas de contas a pagar (valor_pago + juros + multa = total que sai da conta)
    total_pagar = (
        ContaPagarBaixa.objects.filter(
            conta_financeira=conta, data_pagamento=data
        ).aggregate(s=Sum(F("valor_pago") + F("juros") + F("multa")))["s"]
        or Decimal("0")
    )
    debito += total_pagar

    # Lançamentos de débito (inclui transferências saída, que geram Lancamento TIPO_DEBITO)
    total_lanc_debito = (
        Lancamento.objects.filter(
            conta_financeira=conta,
            data=data,
            tipo=Lancamento.TIPO_DEBITO,
        ).aggregate(s=Sum("valor"))["s"]
        or Decimal("0")
    )
    debito += total_lanc_debito

    return debito


@transaction.atomic
def recalcular_saldo_conta(conta_financeira: ContaFinanceira, data_a_partir_de=None):
    """
    Recalcula os registros de Saldo (diário) para a conta financeira.

    Se data_a_partir_de for None, recalcula a partir da primeira data com movimento.
    Caso contrário, recalcula a partir de data_a_partir_de (inclusive).
    O saldo acumulado usa saldo_inicial da ContaFinanceira para dias anteriores ao primeiro movimento.
    """
    todas_datas = sorted(_todas_datas_com_movimento(conta_financeira))
    if not todas_datas:
        Saldo.objects.filter(conta_financeira=conta_financeira).delete()
        return

    if data_a_partir_de is not None:
        datas = [d for d in todas_datas if d >= data_a_partir_de]
        if not datas:
            return
        primeira_data = datas[0]
        # Saldo ao final do dia anterior à primeira data a processar
        anterior_row = (
            Saldo.objects.filter(
                conta_financeira=conta_financeira, data__lt=primeira_data
            )
            .order_by("-data")
            .first()
        )
        if anterior_row is not None:
            saldo_anterior = anterior_row.saldo
        else:
            # Não existe saldo calculado antes; recalcular desde o início
            datas = todas_datas
            saldo_anterior = conta_financeira.saldo_inicial
    else:
        datas = todas_datas
        saldo_anterior = conta_financeira.saldo_inicial

    for data in datas:
        credito = _credito_do_dia(conta_financeira, data)
        debito = _debito_do_dia(conta_financeira, data)
        saldo_anterior = saldo_anterior + credito - debito

        Saldo.objects.update_or_create(
            conta_financeira=conta_financeira,
            data=data,
            defaults={"credito": credito, "debito": debito, "saldo": saldo_anterior},
        )


def recalcular_saldo_apos_movimento(
    conta_financeira_id=None,
    data=None,
    conta_origem_id=None,
    conta_destino_id=None,
):
    """
    Chamada após inclusão/alteração/exclusão de baixa, transferência ou lançamento.

    Para baixa ou lançamento: passar conta_financeira_id e data.
    Para transferência: passar conta_origem_id e conta_destino_id e data (recalcula as duas contas).
    """
    from .models import ContaFinanceira

    contas_ids = set()
    if conta_financeira_id:
        contas_ids.add(conta_financeira_id)
    if conta_origem_id:
        contas_ids.add(conta_origem_id)
    if conta_destino_id:
        contas_ids.add(conta_destino_id)

    for cid in contas_ids:
        conta = ContaFinanceira.objects.filter(pk=cid).first()
        if not conta:
            continue
        recalcular_saldo_conta(conta, data_a_partir_de=data)
