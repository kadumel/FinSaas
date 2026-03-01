from django.conf import settings
from django.db import models
from django.utils import timezone


class BaseModel(models.Model):
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="%(class)s_created",
        on_delete=models.SET_NULL,
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="%(class)s_updated",
        on_delete=models.SET_NULL,
        )

    class Meta:
        abstract = True


class Tenant(BaseModel):
    razao_social = models.CharField(max_length=255)
    nome_fantasia = models.CharField(max_length=255, blank=True)
    cnpj = models.CharField(max_length=18, unique=True)
    qtd_empresas_contratadas = models.PositiveIntegerField(default=1)
    qtd_usuarios_contratados = models.PositiveIntegerField(default=1)
    ativo = models.BooleanField(default=True)

    class Meta:
        db_table = "sis_tenant"

    def __str__(self) -> str:
        return self.nome_fantasia or self.razao_social


class Empresa(BaseModel):
    TIPO_MATRIZ = "M"
    TIPO_FILIAL = "F"
    TIPOS = (
        (TIPO_MATRIZ, "Matriz"),
        (TIPO_FILIAL, "Filial"),
    )

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="empresas")
    empresa_matriz = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="filiais",
    )
    razao_social = models.CharField(max_length=255)
    cnpj = models.CharField(max_length=18)
    tipo = models.CharField(max_length=1, choices=TIPOS, default=TIPO_MATRIZ)
    ativo = models.BooleanField(default=True)

    class Meta:
        db_table = "adm_empresa"

    def __str__(self) -> str:
        return self.razao_social


class Estado(BaseModel):
    nome = models.CharField(max_length=100)
    uf = models.CharField(max_length=2, unique=True)

    class Meta:
        db_table = "sis_estados"

    def __str__(self) -> str:
        return self.uf


class Cidade(BaseModel):
    nome = models.CharField(max_length=150)
    estado = models.ForeignKey(Estado, on_delete=models.CASCADE, related_name="cidades")

    class Meta:
        db_table = "sis_cidade"
        unique_together = ("nome", "estado")

    def __str__(self) -> str:
        return f"{self.nome} - {self.estado.uf}"


class SisOrigem(BaseModel):
    codigo = models.CharField(max_length=50, unique=True)
    nome = models.CharField(max_length=255)

    class Meta:
        db_table = "sis_origem"

    def __str__(self) -> str:
        return f"{self.codigo} - {self.nome}"


class SisConfig(BaseModel):
    TIPO_TEXTO = "texto"
    TIPO_INTEIRO = "inteiro"
    TIPO_DECIMAL = "decimal"
    TIPO_BOOLEANO = "booleano"

    TIPOS = (
        (TIPO_TEXTO, "Texto"),
        (TIPO_INTEIRO, "Inteiro"),
        (TIPO_DECIMAL, "Decimal"),
        (TIPO_BOOLEANO, "Booleano"),
    )

    chave = models.CharField(max_length=100)
    descricao = models.CharField(max_length=255)
    tipo = models.CharField(max_length=20, choices=TIPOS, default=TIPO_TEXTO)
    origem = models.ForeignKey(
        SisOrigem,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="parametros",
    )
    ativo = models.BooleanField(default=True)

    class Meta:
        db_table = "sis_config"
        unique_together = ("chave", "origem")

    def __str__(self) -> str:
        return self.chave


class Configuracao(BaseModel):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="configuracoes")
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="configuracoes")
    parametro = models.ForeignKey(SisConfig, on_delete=models.CASCADE, related_name="configuracoes")
    valor = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "adm_config"
        unique_together = ("empresa", "parametro")

    def __str__(self) -> str:
        return f"{self.tenant} · {self.parametro.chave}"
