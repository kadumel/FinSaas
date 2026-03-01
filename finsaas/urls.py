"""
URL configuration for finsaas project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path

from core.views import (
    CustomLoginView,
    dashboard,
    logout_view,
    post_login_redirect,
    set_empresa_matriz,
    tenant_admin_home,
    tenant_create,
    tenant_edit,
    tenant_empresas_create,
    tenant_empresas_delete,
    tenant_empresas_edit,
    tenant_empresas_list,
    tenant_perfil_permissoes,
    tenant_perfis_create,
    tenant_perfis_delete,
    tenant_perfis_edit,
    tenant_perfis_list,
    tenant_config_list,
    tenant_usuarios_create,
    tenant_usuarios_delete,
    tenant_usuarios_edit,
    tenant_usuarios_list,
    tenant_list,
)
from finance.views import (
    cadastros_bancos,
    cadastros_bancos_editar,
    cadastros_bancos_excluir,
    cadastros_bancos_novo,
    cadastros_bancos_verificar_excluir,
    cadastros_centro_resultado,
    cadastros_centro_resultado_editar,
    cadastros_centro_resultado_excluir,
    cadastros_centro_resultado_novo,
    cadastros_centro_resultado_verificar_excluir,
    cadastros_contas_financeiras,
    cadastros_contas_financeiras_editar,
    cadastros_contas_financeiras_excluir,
    cadastros_contas_financeiras_novo,
    cadastros_contas_financeiras_verificar_excluir,
    cadastros_pessoas,
    cadastros_pessoas_editar,
    cadastros_pessoas_excluir,
    cadastros_pessoas_novo,
    cadastros_pessoas_verificar_excluir,
    cadastros_plano_conta,
    cadastros_plano_conta_novo,
    cadastros_plano_conta_editar,
    cadastros_plano_conta_excluir,
    cadastros_plano_conta_verificar_excluir,
    cadastros_tipos_documento,
    cadastros_tipos_documento_editar,
    cadastros_tipos_documento_excluir,
    cadastros_tipos_documento_novo,
    cadastros_tipos_documento_verificar_excluir,
    financeiro_lancamentos,
    financeiro_lancamentos_excluir,
    financeiro_lancamentos_verificar_incluir,
    financeiro_saldo_bancario,
    financeiro_transferencias,
    financeiro_transferencias_editar,
    financeiro_transferencias_excluir,
    financeiro_transferencias_verificar_incluir,
    movimentos_contas_a_pagar,
    movimentos_contas_a_pagar_baixa,
    movimentos_contas_a_pagar_baixa_editar,
    movimentos_contas_a_pagar_baixa_excluir,
    movimentos_contas_a_pagar_baixa_estornar,
    movimentos_contas_a_pagar_baixa_verificar_vencimentos,
    movimentos_contas_a_pagar_baixa_verificar_estorno,
    movimentos_contas_a_pagar_vencimento_baixar,
    movimentos_contas_a_pagar_novo,
    movimentos_contas_a_pagar_editar,
    movimentos_contas_a_pagar_excluir,
    movimentos_contas_a_receber,
    movimentos_contas_a_receber_novo,
    movimentos_contas_a_receber_editar,
    movimentos_contas_a_receber_excluir,
    movimentos_contas_a_receber_baixa,
    movimentos_contas_a_receber_baixa_verificar_vencimentos,
    movimentos_contas_a_receber_baixa_verificar_estorno,
    movimentos_contas_a_receber_baixa_editar,
    movimentos_contas_a_receber_baixa_excluir,
    movimentos_contas_a_receber_baixa_estornar,
    movimentos_contas_a_receber_vencimento_baixar,
    relatorios_conciliacao_bancaria,
    relatorios_dre,
    relatorios_fluxo_caixa,
    relatorios_orcamento,
)

urlpatterns = [
    path("admin/", admin.site.urls),
    # Login customizado: sempre redireciona para post-login (superadmin -> sys/tenants)
    path("accounts/login/", CustomLoginView.as_view(), name="login"),
    path("accounts/logout/", logout_view, name="logout"),
    path("accounts/", include("django.contrib.auth.urls")),
    path("accounts/profile/", post_login_redirect, name="post-login"),
    path("set-empresa-matriz/", set_empresa_matriz, name="set-empresa-matriz"),
    # Administração por tenant (admin do tenant)
    path("tenant/admin/", tenant_admin_home, name="tenant-admin-home"),
    path("tenant/admin/empresas/", tenant_empresas_list, name="tenant-empresas-list"),
    path("tenant/admin/empresas/nova/", tenant_empresas_create, name="tenant-empresas-create"),
    path(
        "tenant/admin/empresas/<int:empresa_id>/editar/",
        tenant_empresas_edit,
        name="tenant-empresas-edit",
    ),
    path(
        "tenant/admin/empresas/<int:empresa_id>/excluir/",
        tenant_empresas_delete,
        name="tenant-empresas-delete",
    ),
    path("tenant/admin/usuarios/", tenant_usuarios_list, name="tenant-usuarios-list"),
    path("tenant/admin/usuarios/novo/", tenant_usuarios_create, name="tenant-usuarios-create"),
    path(
        "tenant/admin/usuarios/<int:usuario_id>/editar/",
        tenant_usuarios_edit,
        name="tenant-usuarios-edit",
    ),
    path(
        "tenant/admin/usuarios/<int:usuario_id>/excluir/",
        tenant_usuarios_delete,
        name="tenant-usuarios-delete",
    ),
    path("tenant/admin/perfis/", tenant_perfis_list, name="tenant-perfis-list"),
    path("tenant/admin/perfis/novo/", tenant_perfis_create, name="tenant-perfis-create"),
    path(
        "tenant/admin/perfis/<int:perfil_id>/editar/",
        tenant_perfis_edit,
        name="tenant-perfis-edit",
    ),
    path(
        "tenant/admin/perfis/<int:perfil_id>/excluir/",
        tenant_perfis_delete,
        name="tenant-perfis-delete",
    ),
    path(
        "tenant/admin/perfis/<int:perfil_id>/permissoes/",
        tenant_perfil_permissoes,
        name="tenant-perfil-permissoes",
    ),
    path("tenant/admin/configuracoes/", tenant_config_list, name="tenant-config-list"),
    # Área de sistema (apenas superusuário)
    path("sys/tenants/", tenant_list, name="sys-tenant-list"),
    path("sys/tenants/novo/", tenant_create, name="sys-tenant-create"),
    path("sys/tenants/<int:tenant_id>/editar/", tenant_edit, name="sys-tenant-edit"),
    # Cadastros (usuário padrão)
    path("cadastros/pessoas/", cadastros_pessoas, name="cadastros-pessoas"),
    path("cadastros/pessoas/novo/", cadastros_pessoas_novo, name="cadastros-pessoas-novo"),
    path(
        "cadastros/pessoas/verificar-excluir/",
        cadastros_pessoas_verificar_excluir,
        name="cadastros-pessoas-verificar-excluir",
    ),
    path("cadastros/pessoas/<int:pessoa_id>/editar/", cadastros_pessoas_editar, name="cadastros-pessoas-editar"),
    path("cadastros/pessoas/<int:pessoa_id>/excluir/", cadastros_pessoas_excluir, name="cadastros-pessoas-excluir"),
    path("cadastros/centro-resultado/", cadastros_centro_resultado, name="cadastros-centro-resultado"),
    path(
        "cadastros/centro-resultado/novo/",
        cadastros_centro_resultado_novo,
        name="cadastros-centro-resultado-novo",
    ),
    path(
        "cadastros/centro-resultado/verificar-excluir/",
        cadastros_centro_resultado_verificar_excluir,
        name="cadastros-centro-resultado-verificar-excluir",
    ),
    path(
        "cadastros/centro-resultado/<int:centro_id>/editar/",
        cadastros_centro_resultado_editar,
        name="cadastros-centro-resultado-editar",
    ),
    path(
        "cadastros/centro-resultado/<int:centro_id>/excluir/",
        cadastros_centro_resultado_excluir,
        name="cadastros-centro-resultado-excluir",
    ),
    path("cadastros/plano-conta/", cadastros_plano_conta, name="cadastros-plano-conta"),
    path(
        "cadastros/plano-conta/novo/",
        cadastros_plano_conta_novo,
        name="cadastros-plano-conta-novo",
    ),
    path(
        "cadastros/plano-conta/verificar-excluir/",
        cadastros_plano_conta_verificar_excluir,
        name="cadastros-plano-conta-verificar-excluir",
    ),
    path(
        "cadastros/plano-conta/<int:plano_id>/editar/",
        cadastros_plano_conta_editar,
        name="cadastros-plano-conta-editar",
    ),
    path(
        "cadastros/plano-conta/<int:plano_id>/excluir/",
        cadastros_plano_conta_excluir,
        name="cadastros-plano-conta-excluir",
    ),
    path("cadastros/tipos-documento/", cadastros_tipos_documento, name="cadastros-tipos-documento"),
    path(
        "cadastros/tipos-documento/novo/",
        cadastros_tipos_documento_novo,
        name="cadastros-tipos-documento-novo",
    ),
    path(
        "cadastros/tipos-documento/verificar-excluir/",
        cadastros_tipos_documento_verificar_excluir,
        name="cadastros-tipos-documento-verificar-excluir",
    ),
    path(
        "cadastros/tipos-documento/<int:tipo_id>/editar/",
        cadastros_tipos_documento_editar,
        name="cadastros-tipos-documento-editar",
    ),
    path(
        "cadastros/tipos-documento/<int:tipo_id>/excluir/",
        cadastros_tipos_documento_excluir,
        name="cadastros-tipos-documento-excluir",
    ),
    path("cadastros/bancos/", cadastros_bancos, name="cadastros-bancos"),
    path("cadastros/bancos/novo/", cadastros_bancos_novo, name="cadastros-bancos-novo"),
    path(
        "cadastros/bancos/verificar-excluir/",
        cadastros_bancos_verificar_excluir,
        name="cadastros-bancos-verificar-excluir",
    ),
    path("cadastros/bancos/<int:banco_id>/editar/", cadastros_bancos_editar, name="cadastros-bancos-editar"),
    path("cadastros/bancos/<int:banco_id>/excluir/", cadastros_bancos_excluir, name="cadastros-bancos-excluir"),
    path("cadastros/contas-financeiras/", cadastros_contas_financeiras, name="cadastros-contas-financeiras"),
    path(
        "cadastros/contas-financeiras/novo/",
        cadastros_contas_financeiras_novo,
        name="cadastros-contas-financeiras-novo",
    ),
    path(
        "cadastros/contas-financeiras/verificar-excluir/",
        cadastros_contas_financeiras_verificar_excluir,
        name="cadastros-contas-financeiras-verificar-excluir",
    ),
    path(
        "cadastros/contas-financeiras/<int:conta_id>/editar/",
        cadastros_contas_financeiras_editar,
        name="cadastros-contas-financeiras-editar",
    ),
    path(
        "cadastros/contas-financeiras/<int:conta_id>/excluir/",
        cadastros_contas_financeiras_excluir,
        name="cadastros-contas-financeiras-excluir",
    ),
    # Movimentos
    path("movimentos/contas-a-pagar/", movimentos_contas_a_pagar, name="movimentos-contas-a-pagar"),
    path("movimentos/contas-a-pagar-baixa/", movimentos_contas_a_pagar_baixa, name="movimentos-contas-a-pagar-baixa"),
    path(
        "movimentos/contas-a-pagar-baixa/verificar-vencimentos/",
        movimentos_contas_a_pagar_baixa_verificar_vencimentos,
        name="movimentos-contas-a-pagar-baixa-verificar-vencimentos",
    ),
    path(
        "movimentos/contas-a-pagar-baixa/verificar-estorno/",
        movimentos_contas_a_pagar_baixa_verificar_estorno,
        name="movimentos-contas-a-pagar-baixa-verificar-estorno",
    ),
    path(
        "movimentos/contas-a-pagar-baixa/<int:baixa_id>/editar/",
        movimentos_contas_a_pagar_baixa_editar,
        name="movimentos-contas-a-pagar-baixa-editar",
    ),
    path(
        "movimentos/contas-a-pagar-baixa/<int:baixa_id>/excluir/",
        movimentos_contas_a_pagar_baixa_excluir,
        name="movimentos-contas-a-pagar-baixa-excluir",
    ),
    path(
        "movimentos/contas-a-pagar-baixa/estornar/",
        movimentos_contas_a_pagar_baixa_estornar,
        name="movimentos-contas-a-pagar-baixa-estornar",
    ),
    path(
        "movimentos/contas-a-pagar/vencimento/<int:venc_id>/baixar/",
        movimentos_contas_a_pagar_vencimento_baixar,
        name="movimentos-contas-a-pagar-vencimento-baixar",
    ),
    path("movimentos/contas-a-pagar/novo/", movimentos_contas_a_pagar_novo, name="movimentos-contas-a-pagar-novo"),
    path(
        "movimentos/contas-a-pagar/<int:conta_id>/editar/",
        movimentos_contas_a_pagar_editar,
        name="movimentos-contas-a-pagar-editar",
    ),
    path(
        "movimentos/contas-a-pagar/<int:conta_id>/excluir/",
        movimentos_contas_a_pagar_excluir,
        name="movimentos-contas-a-pagar-excluir",
    ),
    path("movimentos/contas-a-receber/", movimentos_contas_a_receber, name="movimentos-contas-a-receber"),
    path(
        "movimentos/contas-a-receber/novo/",
        movimentos_contas_a_receber_novo,
        name="movimentos-contas-a-receber-novo",
    ),
    path(
        "movimentos/contas-a-receber/<int:conta_id>/editar/",
        movimentos_contas_a_receber_editar,
        name="movimentos-contas-a-receber-editar",
    ),
    path(
        "movimentos/contas-a-receber/<int:conta_id>/excluir/",
        movimentos_contas_a_receber_excluir,
        name="movimentos-contas-a-receber-excluir",
    ),
    path(
        "movimentos/contas-a-receber-baixa/",
        movimentos_contas_a_receber_baixa,
        name="movimentos-contas-a-receber-baixa",
    ),
    path(
        "movimentos/contas-a-receber-baixa/verificar-vencimentos/",
        movimentos_contas_a_receber_baixa_verificar_vencimentos,
        name="movimentos-contas-a-receber-baixa-verificar-vencimentos",
    ),
    path(
        "movimentos/contas-a-receber-baixa/verificar-estorno/",
        movimentos_contas_a_receber_baixa_verificar_estorno,
        name="movimentos-contas-a-receber-baixa-verificar-estorno",
    ),
    path(
        "movimentos/contas-a-receber-baixa/<int:baixa_id>/editar/",
        movimentos_contas_a_receber_baixa_editar,
        name="movimentos-contas-a-receber-baixa-editar",
    ),
    path(
        "movimentos/contas-a-receber-baixa/<int:baixa_id>/excluir/",
        movimentos_contas_a_receber_baixa_excluir,
        name="movimentos-contas-a-receber-baixa-excluir",
    ),
    path(
        "movimentos/contas-a-receber-baixa/estornar/",
        movimentos_contas_a_receber_baixa_estornar,
        name="movimentos-contas-a-receber-baixa-estornar",
    ),
    path(
        "movimentos/contas-a-receber/vencimento/<int:venc_id>/baixar/",
        movimentos_contas_a_receber_vencimento_baixar,
        name="movimentos-contas-a-receber-vencimento-baixar",
    ),
    # Financeiro
    path("financeiro/lancamentos/", financeiro_lancamentos, name="financeiro-lancamentos"),
    path(
        "financeiro/lancamentos/verificar-incluir/",
        financeiro_lancamentos_verificar_incluir,
        name="financeiro-lancamentos-verificar-incluir",
    ),
    path(
        "financeiro/lancamentos/<int:lanc_id>/excluir/",
        financeiro_lancamentos_excluir,
        name="financeiro-lancamentos-excluir",
    ),
    path("financeiro/transferencias/", financeiro_transferencias, name="financeiro-transferencias"),
    path(
        "financeiro/transferencias/verificar-incluir/",
        financeiro_transferencias_verificar_incluir,
        name="financeiro-transferencias-verificar-incluir",
    ),
    path(
        "financeiro/transferencias/<int:transferencia_id>/editar/",
        financeiro_transferencias_editar,
        name="financeiro-transferencias-editar",
    ),
    path(
        "financeiro/transferencias/<int:transferencia_id>/excluir/",
        financeiro_transferencias_excluir,
        name="financeiro-transferencias-excluir",
    ),
    path("financeiro/saldo-bancario/", financeiro_saldo_bancario, name="financeiro-saldo-bancario"),
    # Relatórios
    path("relatorios/conciliacao-bancaria/", relatorios_conciliacao_bancaria, name="relatorios-conciliacao-bancaria"),
    path("relatorios/fluxo-caixa/", relatorios_fluxo_caixa, name="relatorios-fluxo-caixa"),
    path("relatorios/orcamento/", relatorios_orcamento, name="relatorios-orcamento"),
    path("relatorios/dre/", relatorios_dre, name="relatorios-dre"),
    # Dashboard padrão
    path("", dashboard, name="dashboard"),
]
