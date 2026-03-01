"""
Signals para recálculo automático do saldo diário quando houver
inclusão, alteração ou exclusão de baixas, transferências ou lançamentos.
"""
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from .models import ContaPagarBaixa, ContaReceberBaixa, Lancamento, Transferencia
from .saldo_service import recalcular_saldo_apos_movimento


def _recalcular_baixa_receber(sender, instance, **kwargs):
    recalcular_saldo_apos_movimento(
        conta_financeira_id=instance.conta_financeira_id,
        data=instance.data_pagamento,
    )


def _recalcular_baixa_pagar(sender, instance, **kwargs):
    recalcular_saldo_apos_movimento(
        conta_financeira_id=instance.conta_financeira_id,
        data=instance.data_pagamento,
    )


def _recalcular_lancamento(sender, instance, **kwargs):
    recalcular_saldo_apos_movimento(
        conta_financeira_id=instance.conta_financeira_id,
        data=instance.data,
    )


def _recalcular_transferencia(sender, instance, **kwargs):
    recalcular_saldo_apos_movimento(
        data=instance.data,
        conta_origem_id=instance.conta_origem_id,
        conta_destino_id=instance.conta_destino_id,
    )


@receiver(post_save, sender=ContaReceberBaixa)
def saldo_pos_save_baixa_receber(sender, instance, created, **kwargs):
    _recalcular_baixa_receber(sender, instance, **kwargs)


@receiver(post_delete, sender=ContaReceberBaixa)
def saldo_pos_delete_baixa_receber(sender, instance, **kwargs):
    _recalcular_baixa_receber(sender, instance, **kwargs)


@receiver(post_save, sender=ContaPagarBaixa)
def saldo_pos_save_baixa_pagar(sender, instance, created, **kwargs):
    _recalcular_baixa_pagar(sender, instance, **kwargs)


@receiver(post_delete, sender=ContaPagarBaixa)
def saldo_pos_delete_baixa_pagar(sender, instance, **kwargs):
    _recalcular_baixa_pagar(sender, instance, **kwargs)


@receiver(post_save, sender=Lancamento)
def saldo_pos_save_lancamento(sender, instance, created, **kwargs):
    _recalcular_lancamento(sender, instance, **kwargs)


@receiver(post_delete, sender=Lancamento)
def saldo_pos_delete_lancamento(sender, instance, **kwargs):
    _recalcular_lancamento(sender, instance, **kwargs)


@receiver(post_save, sender=Transferencia)
def saldo_pos_save_transferencia(sender, instance, created, **kwargs):
    _recalcular_transferencia(sender, instance, **kwargs)


@receiver(post_delete, sender=Transferencia)
def saldo_pos_delete_transferencia(sender, instance, **kwargs):
    _recalcular_transferencia(sender, instance, **kwargs)
