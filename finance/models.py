from django.db import models

from core.models import BaseModel, Empresa, Estado, Cidade


class Pessoa(BaseModel):
    TIPO_FISICA = "F"
    TIPO_JURIDICA = "J"
    TIPOS = (
        (TIPO_FISICA, "Pessoa Física"),
        (TIPO_JURIDICA, "Pessoa Jurídica"),
    )
    CLIENTE = "C"
    FORNECEDOR = "F"
    AMBOS = "A"
    TIPO_CADASTRO_CHOICES = (
        (CLIENTE, "Cliente"),
        (FORNECEDOR, "Fornecedor"),
        (AMBOS, "Cliente e Fornecedor"),
    )

    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="pessoas")
    tipo = models.CharField(max_length=1, choices=TIPOS)
    nome_razao = models.CharField(max_length=255)
    cpf_cnpj = models.CharField(max_length=18)
    tipo_cadastro = models.CharField(
        max_length=1,
        choices=TIPO_CADASTRO_CHOICES,
        default=AMBOS,
    )
    # Localização
    endereco = models.CharField(max_length=255, blank=True)
    numero = models.CharField(max_length=20, blank=True)
    complemento = models.CharField(max_length=100, blank=True)
    bairro = models.CharField(max_length=100, blank=True)
    estado = models.ForeignKey(
        Estado,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pessoas",
    )
    cidade = models.ForeignKey(
        Cidade,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pessoas",
    )
    # Contato
    telefone = models.CharField(max_length=20, blank=True)
    celular = models.CharField(max_length=20, blank=True)
    whatsapp = models.BooleanField(default=False)
    email = models.EmailField(blank=True)

    class Meta:
        db_table = "fin_pessoa"


class CentroResultado(BaseModel):
    TIPO_SINTETICO = "S"
    TIPO_ANALITICO = "A"
    TIPOS = (
        (TIPO_SINTETICO, "Sintético"),
        (TIPO_ANALITICO, "Analítico"),
    )

    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="centros_resultado")
    codigo = models.CharField(max_length=50)
    descricao = models.CharField(max_length=255)
    centro_contabil = models.CharField(max_length=30, blank=True)
    nivel = models.PositiveIntegerField(default=1)
    tipo = models.CharField(max_length=1, choices=TIPOS, default=TIPO_ANALITICO)
    centro_pai = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="filhos",
    )

    class Meta:
        db_table = "fin_centro_resultado"


class PlanoConta(BaseModel):
    TIPO_SINTETICO = "S"
    TIPO_ANALITICO = "A"
    TIPOS = (
        (TIPO_SINTETICO, "Sintético"),
        (TIPO_ANALITICO, "Analítico"),
    )

    NATUREZA_RECEITA = "R"
    NATUREZA_DESPESA = "D"
    NATUREZAS = (
        (NATUREZA_RECEITA, "Receita"),
        (NATUREZA_DESPESA, "Despesa"),
    )

    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="planos_conta")
    codigo = models.CharField(max_length=50)
    descricao = models.CharField(max_length=255)
    conta_pai = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="filhos",
    )
    conta_contabil = models.CharField(max_length=30, blank=True)
    tipo = models.CharField(max_length=1, choices=TIPOS, default=TIPO_ANALITICO)
    natureza = models.CharField(max_length=1, choices=NATUREZAS, default=NATUREZA_DESPESA)
    nivel = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "fin_plano_conta"


class Banco(BaseModel):
    codigo = models.CharField(max_length=10, unique=True)
    nome = models.CharField(max_length=100)

    class Meta:
        db_table = "fin_banco"


class ContaFinanceira(BaseModel):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="contas_financeiras")
    banco = models.ForeignKey(Banco, on_delete=models.PROTECT, related_name="contas")
    agencia = models.CharField(max_length=20)
    conta = models.CharField(max_length=20)
    saldo_inicial = models.DecimalField(max_digits=15, decimal_places=2, default=0)

    class Meta:
        db_table = "fin_conta_financeira"


class TipoDocumento(BaseModel):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="tipos_documento")
    nome = models.CharField(max_length=100)

    class Meta:
        db_table = "fin_tipo_documento"


class ContaPagar(BaseModel):
    STATUS_ABERTO = "A"
    STATUS_PAGO = "P"
    STATUS_CANCELADO = "C"
    STATUS_PARCIAL = "R"
    STATUS_CHOICES = (
        (STATUS_ABERTO, "Aberto"),
        (STATUS_PARCIAL, "Pago parcialmente"),
        (STATUS_PAGO, "Pago"),
        (STATUS_CANCELADO, "Cancelado"),
    )

    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="contas_pagar")
    pessoa = models.ForeignKey(Pessoa, on_delete=models.PROTECT, related_name="contas_pagar")
    descricao = models.CharField(max_length=255)
    documento = models.CharField(max_length=30)
    valor_total = models.DecimalField(max_digits=15, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ABERTO)
    tipo_documento = models.ForeignKey(
        "TipoDocumento",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="contas_pagar",
    )
    competencia = models.CharField(max_length=7, blank=True)
    data_emissao = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "fin_conta_pagar"


class ContaPagarVenc(BaseModel):
    conta_pagar = models.ForeignKey(ContaPagar, on_delete=models.CASCADE, related_name="vencimentos")
    sequencial_vencimento = models.PositiveIntegerField()
    data_vencimento = models.DateField()
    valor = models.DecimalField(max_digits=15, decimal_places=2)
    data_cancelamento = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "fin_conta_pagar_venc"


class ContaPagarRateio(BaseModel):
    conta_pagar = models.ForeignKey(ContaPagar, on_delete=models.CASCADE, related_name="rateios")
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="rateios_pagar")
    centro_resultado = models.ForeignKey(CentroResultado, on_delete=models.PROTECT)
    plano_conta = models.ForeignKey(PlanoConta, on_delete=models.PROTECT)
    valor = models.DecimalField(max_digits=15, decimal_places=2)

    class Meta:
        db_table = "fin_conta_pagar_rateio"


class ContaPagarBaixa(BaseModel):
    conta_pagar_venc = models.ForeignKey(ContaPagarVenc, on_delete=models.CASCADE, related_name="baixas")
    conta_financeira = models.ForeignKey(
        ContaFinanceira,
        on_delete=models.PROTECT,
        related_name="baixas_pagar",
    )
    data_pagamento = models.DateField()
    valor_pago = models.DecimalField(max_digits=15, decimal_places=2)
    juros = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    multa = models.DecimalField(max_digits=15, decimal_places=2, default=0)

    class Meta:
        db_table = "fin_conta_pagar_baixa"


class ContaReceber(BaseModel):
    STATUS_ABERTO = "A"
    STATUS_PAGO = "P"
    STATUS_CANCELADO = "C"
    STATUS_PARCIAL = "R"
    STATUS_CHOICES = (
        (STATUS_ABERTO, "Aberto"),
        (STATUS_PARCIAL, "Recebido parcialmente"),
        (STATUS_PAGO, "Recebido"),
        (STATUS_CANCELADO, "Cancelado"),
    )

    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="contas_receber")
    pessoa = models.ForeignKey(Pessoa, on_delete=models.PROTECT, related_name="contas_receber")
    descricao = models.CharField(max_length=255)
    valor_total = models.DecimalField(max_digits=15, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ABERTO)
    tipo_documento = models.ForeignKey(
        "TipoDocumento",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="contas_receber",
    )
    competencia = models.CharField(max_length=7, blank=True)
    data_emissao = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "fin_conta_receber"


class ContaReceberVenc(BaseModel):
    conta_receber = models.ForeignKey(ContaReceber, on_delete=models.CASCADE, related_name="vencimentos")
    sequencial_vencimento = models.PositiveIntegerField()
    data_vencimento = models.DateField()
    valor = models.DecimalField(max_digits=15, decimal_places=2)
    data_cancelamento = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "fin_conta_receber_venc"


class ContaReceberRateio(BaseModel):
    conta_receber = models.ForeignKey(ContaReceber, on_delete=models.CASCADE, related_name="rateios")
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="rateios_receber")
    centro_resultado = models.ForeignKey(CentroResultado, on_delete=models.PROTECT)
    plano_conta = models.ForeignKey(PlanoConta, on_delete=models.PROTECT)
    valor = models.DecimalField(max_digits=15, decimal_places=2)

    class Meta:
        db_table = "fin_conta_receber_rateio"


class ContaReceberBaixa(BaseModel):
    conta_receber_venc = models.ForeignKey(ContaReceberVenc, on_delete=models.CASCADE, related_name="baixas")
    conta_financeira = models.ForeignKey(
        ContaFinanceira,
        on_delete=models.PROTECT,
        related_name="baixas_receber",
    )
    data_pagamento = models.DateField()
    valor_pago = models.DecimalField(max_digits=15, decimal_places=2)
    juros = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    multa = models.DecimalField(max_digits=15, decimal_places=2, default=0)

    class Meta:
        db_table = "fin_conta_receber_baixa"


class Lancamento(BaseModel):
    TIPO_CREDITO = "C"
    TIPO_DEBITO = "D"
    TIPOS = (
        (TIPO_CREDITO, "Crédito"),
        (TIPO_DEBITO, "Débito"),
    )

    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="lancamentos")
    conta_financeira = models.ForeignKey(
        ContaFinanceira,
        on_delete=models.CASCADE,
        related_name="lancamentos",
    )
    tipo = models.CharField(max_length=1, choices=TIPOS)
    valor = models.DecimalField(max_digits=15, decimal_places=2)
    data = models.DateField()
    origem = models.CharField(max_length=50)
    centro_resultado = models.ForeignKey(
        CentroResultado,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="lancamentos",
    )
    plano_conta = models.ForeignKey(
        PlanoConta,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="lancamentos",
    )
    baixa_conta_pagar = models.ForeignKey(
        ContaPagarBaixa,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="lancamentos",
    )
    baixa_conta_receber = models.ForeignKey(
        ContaReceberBaixa,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="lancamentos",
    )
    transferencia = models.ForeignKey(
        "Transferencia",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="lancamentos",
    )
    observacao = models.TextField(blank=True)

    class Meta:
        db_table = "fin_lancamento"


class Transferencia(BaseModel):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="transferencias")
    conta_origem = models.ForeignKey(
        ContaFinanceira,
        on_delete=models.CASCADE,
        related_name="transferencias_origem",
    )
    conta_destino = models.ForeignKey(
        ContaFinanceira,
        on_delete=models.CASCADE,
        related_name="transferencias_destino",
    )
    valor = models.DecimalField(max_digits=15, decimal_places=2)
    data = models.DateField()

    class Meta:
        db_table = "fin_transferencia"


class Saldo(BaseModel):
    conta_financeira = models.OneToOneField(
        ContaFinanceira,
        on_delete=models.CASCADE,
        related_name="saldo",
    )
    saldo_atual = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    data_atualizacao = models.DateTimeField()

    class Meta:
        db_table = "fin_saldo"
