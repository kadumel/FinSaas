from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin, BaseUserManager
from django.db import models

from core.models import BaseModel, Tenant, Empresa


class UserManager(BaseUserManager):
    def _create_user(self, email, nome, password=None, **extra_fields):
        if not email:
            raise ValueError("Usuário deve ter um e-mail")
        email = self.normalize_email(email)
        user = self.model(email=email, nome=nome, **extra_fields)
        if password:
            user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, nome, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, nome, password, **extra_fields)

    def create_superuser(self, email, nome, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self._create_user(email, nome, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin, BaseModel):
    # Para superusuário global, o tenant pode ser nulo
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="users",
        null=True,
        blank=True,
    )
    nome = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    ativo = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    # indica se este usuário é administrador principal de um tenant/empresa
    is_tenant_admin = models.BooleanField(default=False)

    USERNAME_FIELD = "email"
    # createsuperuser vai pedir apenas "nome" (tenant será opcional)
    REQUIRED_FIELDS = ["nome"]

    objects = UserManager()

    class Meta:
        db_table = "adm_user"

    def __str__(self) -> str:
        return self.email


class Perfil(BaseModel):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="perfis")
    nome = models.CharField(max_length=100)
    descricao = models.TextField(blank=True)

    class Meta:
        db_table = "adm_perfil"

    def __str__(self) -> str:
        return self.nome


class UserPerfil(BaseModel):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="perfis")
    perfil = models.ForeignKey(Perfil, on_delete=models.CASCADE, related_name="usuarios")

    class Meta:
        db_table = "adm_user_perfil"
        unique_together = ("user", "perfil")


class PermissaoEmpresa(BaseModel):
    perfil = models.ForeignKey(Perfil, on_delete=models.CASCADE, related_name="permissoes_empresas")
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="permissoes_perfis")
    pode_visualizar = models.BooleanField(default=False)
    pode_incluir = models.BooleanField(default=False)
    pode_editar = models.BooleanField(default=False)
    pode_excluir = models.BooleanField(default=False)
    pode_aprovar = models.BooleanField(default=False)

    class Meta:
        db_table = "adm_permissao_empresa"
        unique_together = ("perfil", "empresa")
