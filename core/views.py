import json
from decimal import Decimal
from datetime import date, timedelta, datetime

from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Case, F, IntegerField, Max, Sum, Value, When
from django.db.models.functions import Coalesce
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt

from accounts.models import Perfil, PermissaoEmpresa, User, UserPerfil
from .context_processors import get_empresa_matriz, get_empresas_contexto
from .models import Configuracao, Empresa, SisConfig, Tenant

SESSION_EMPRESA_MATRIZ_ID = "empresa_matriz_id"


@login_required
def dashboard(request):
    """Dashboard com indicadores financeiros, filtros e dados para gráficos."""
    empresa_matriz = get_empresa_matriz(request)
    empresas = get_empresas_contexto(request)

    if not empresas and not request.user.is_superuser:
        return render(request, "dashboard.html", {
            "indicadores": None,
            "contas_financeiras": [],
            "filtros": {"data_de": "", "data_ate": "", "conta_financeira_id": ""},
            "selected_conta_id": None,
            "chart_saldo_evolucao": "[]",
            "chart_credito_debito": "[]",
            "chart_pagar_receber": "{}",
        })

    if not empresas:
        empresas = Empresa.objects.none()

    # Filtros: período (padrão mês atual) e opcionalmente conta
    data_de = (request.GET.get("data_de") or "").strip()
    data_ate = (request.GET.get("data_ate") or "").strip()
    conta_id = (request.GET.get("conta_financeira_id") or "").strip()

    hoje = date.today()
    if not data_de or not data_ate:
        primeiro = hoje.replace(day=1)
        if hoje.month == 12:
            ultimo = date(hoje.year + 1, 1, 1) - timedelta(days=1)
        else:
            ultimo = date(hoje.year, hoje.month + 1, 1) - timedelta(days=1)
        data_de = primeiro.isoformat()
        data_ate = ultimo.isoformat()

    try:
        dt_de = datetime.strptime(data_de, "%Y-%m-%d").date()
        dt_ate = datetime.strptime(data_ate, "%Y-%m-%d").date()
    except ValueError:
        dt_de = hoje.replace(day=1)
        dt_ate = hoje
        data_de = dt_de.isoformat()
        data_ate = dt_ate.isoformat()

    contas_financeiras = list(
        __import__("finance.models", fromlist=["ContaFinanceira"])
        .ContaFinanceira.objects.filter(empresa__in=empresas)
        .select_related("banco", "empresa")
        .order_by("empresa__razao_social", "banco__nome", "agencia", "conta")
    )
    if conta_id and conta_id.isdigit():
        contas_financeiras = [c for c in contas_financeiras if c.pk == int(conta_id)]

    # --- Indicadores ---
    FinanceModels = __import__("finance.models", fromlist=[
        "ContaFinanceira", "Saldo", "ContaPagarVenc", "ContaReceberVenc",
        "ContaPagarBaixa", "ContaReceberBaixa",
    ])
    ContaFinanceiraModel = FinanceModels.ContaFinanceira
    SaldoModel = FinanceModels.Saldo
    ContaPagarVencModel = FinanceModels.ContaPagarVenc
    ContaReceberVencModel = FinanceModels.ContaReceberVenc
    ContaPagarBaixaModel = FinanceModels.ContaPagarBaixa
    ContaReceberBaixaModel = FinanceModels.ContaReceberBaixa

    conta_ids = [c.id for c in contas_financeiras]

    # Saldo consolidado (ao final do período): para cada conta, último saldo com data <= dt_ate; senão saldo_inicial
    saldo_consolidado = Decimal("0")
    for conta in contas_financeiras:
        ultimo = (
            SaldoModel.objects.filter(conta_financeira=conta, data__lte=dt_ate)
            .order_by("-data")
            .values_list("saldo", flat=True)
            .first()
        )
        if ultimo is not None:
            saldo_consolidado += ultimo
        else:
            saldo_consolidado += conta.saldo_inicial

    # Total a pagar (vencimentos no período, valor pendente)
    venc_pagar = (
        ContaPagarVencModel.objects.filter(
            conta_pagar__empresa__in=empresas,
            data_vencimento__gte=dt_de,
            data_vencimento__lte=dt_ate,
            data_cancelamento__isnull=True,
        )
        .select_related("conta_pagar")
    )
    total_pagar = Decimal("0")
    for v in venc_pagar:
        baixado = (
            ContaPagarBaixaModel.objects.filter(conta_pagar_venc=v)
            .aggregate(s=Sum(F("valor_pago") + F("juros") + F("multa")))["s"]
            or Decimal("0")
        )
        total_pagar += (v.valor - baixado)

    # Total a receber (vencimentos no período, valor pendente)
    venc_receber = (
        ContaReceberVencModel.objects.filter(
            conta_receber__empresa__in=empresas,
            data_vencimento__gte=dt_de,
            data_vencimento__lte=dt_ate,
            data_cancelamento__isnull=True,
        )
    )
    total_receber = Decimal("0")
    for v in venc_receber:
        baixado = (
            ContaReceberBaixaModel.objects.filter(conta_receber_venc=v)
            .aggregate(s=Sum(F("valor_pago") + F("juros") + F("multa")))["s"]
            or Decimal("0")
        )
        total_receber += (v.valor - baixado)

    # Créditos e débitos no período (soma dos Saldo diários no intervalo)
    totais_periodo = (
        SaldoModel.objects.filter(
            conta_financeira_id__in=conta_ids,
            data__gte=dt_de,
            data__lte=dt_ate,
        )
        .aggregate(
            credito=Coalesce(Sum("credito"), Decimal("0")),
            debito=Coalesce(Sum("debito"), Decimal("0")),
        )
    )
    total_creditos = totais_periodo["credito"] or Decimal("0")
    total_debitos = totais_periodo["debito"] or Decimal("0")

    indicadores = {
        "saldo_consolidado": saldo_consolidado,
        "total_a_pagar": total_pagar,
        "total_a_receber": total_receber,
        "total_creditos": total_creditos,
        "total_debitos": total_debitos,
    }

    # --- Dados para gráficos ---
    # 1) Evolução do saldo consolidado por dia (soma do saldo de cada conta por data)
    saldos_por_data = (
        SaldoModel.objects.filter(
            conta_financeira_id__in=conta_ids,
            data__gte=dt_de,
            data__lte=dt_ate,
        )
        .values("data")
        .annotate(soma_saldo=Sum("saldo"))
        .order_by("data")
    )
    # Por data: uma linha por conta; Sum('saldo') = saldo consolidado do dia
    chart_saldo_evolucao = [
        {"data": str(s["data"]), "saldo": float(s["soma_saldo"] or 0)}
        for s in saldos_por_data
    ]
    # Se não houver saldo por dia, preencher com saldo_inicial no primeiro dia
    if not chart_saldo_evolucao and contas_financeiras:
        saldo_inicial_total = sum(c.saldo_inicial for c in contas_financeiras)
        chart_saldo_evolucao = [{"data": data_de, "saldo": float(saldo_inicial_total)}]

    # 2) Crédito e débito por dia (barras)
    credito_debito_por_data = (
        SaldoModel.objects.filter(
            conta_financeira_id__in=conta_ids,
            data__gte=dt_de,
            data__lte=dt_ate,
        )
        .values("data")
        .annotate(
            credito=Coalesce(Sum("credito"), Decimal("0")),
            debito=Coalesce(Sum("debito"), Decimal("0")),
        )
        .order_by("data")
    )
    chart_credito_debito = [
        {
            "data": str(c["data"]),
            "credito": float(c["credito"]),
            "debito": float(c["debito"]),
        }
        for c in credito_debito_por_data
    ]

    # 3) Resumo Pagar vs Receber (para pizza ou barra)
    chart_pagar_receber = {
        "a_pagar": float(total_pagar),
        "a_receber": float(total_receber),
    }

    # Contas para o select (todas do contexto, não filtradas)
    contas_para_filtro = list(
        ContaFinanceiraModel.objects.filter(empresa__in=empresas)
        .select_related("banco", "empresa")
        .order_by("empresa__razao_social", "banco__nome", "agencia", "conta")
    )

    context = {
        "indicadores": indicadores,
        "contas_financeiras": contas_para_filtro,
        "filtros": {
            "data_de": data_de,
            "data_ate": data_ate,
            "conta_financeira_id": conta_id,
        },
        "selected_conta_id": int(conta_id) if conta_id and conta_id.isdigit() else None,
        "chart_saldo_evolucao": json.dumps(chart_saldo_evolucao),
        "chart_credito_debito": json.dumps(chart_credito_debito),
        "chart_pagar_receber": json.dumps(chart_pagar_receber),
    }
    return render(request, "dashboard.html", context)


def _is_superuser(user):
    return user.is_authenticated and user.is_superuser


superuser_required = user_passes_test(_is_superuser)


def _tenant_admin_required(user):
    return user.is_authenticated and getattr(user, "is_tenant_admin", False) and user.tenant_id


tenant_admin_required = user_passes_test(_tenant_admin_required)


@login_required
def post_login_redirect(request):
    """
    Redireciona o usuário após login:
    - superusuário -> gestão global de tenants (SaaS)
    - admin de tenant -> painel de administração do seu tenant
    - demais -> dashboard financeiro
    """
    user = request.user
    if user.is_superuser:
        return redirect("sys-tenant-list")
    if getattr(user, "is_tenant_admin", False) and user.tenant_id:
        # Define a primeira empresa matriz na sessão para o admin também
        _set_primeira_matriz_na_sessao(request)
        return redirect("tenant-admin-home")
    _set_primeira_matriz_na_sessao(request)
    return redirect("dashboard")


def _set_primeira_matriz_na_sessao(request):
    """Define na sessão a primeira empresa matriz permitida ao usuário (via perfis ou todas se admin)."""
    from .context_processors import get_matrizes_para_select, SESSION_EMPRESA_MATRIZ_ID
    matrizes = get_matrizes_para_select(request)
    if matrizes:
        request.session[SESSION_EMPRESA_MATRIZ_ID] = matrizes[0].pk
        request.session.modified = True


@login_required
def set_empresa_matriz(request):
    """
    Altera a empresa matriz do contexto (sessão) e redireciona para a página inicial.
    Recebe empresa_id via GET. A empresa deve ser uma matriz permitida ao usuário
    (conforme perfis/PermissaoEmpresa ou todas se for admin da conta).
    """
    if request.user.is_superuser:
        return redirect("dashboard")
    from .context_processors import get_matrizes_para_select, SESSION_EMPRESA_MATRIZ_ID
    matrizes = get_matrizes_para_select(request)
    empresa_id = request.GET.get("empresa_id")
    if not empresa_id or not matrizes:
        return redirect("dashboard")
    try:
        eid = int(empresa_id)
    except ValueError:
        return redirect("dashboard")
    if any(m.pk == eid for m in matrizes):
        request.session[SESSION_EMPRESA_MATRIZ_ID] = eid
        request.session.modified = True
    return redirect("dashboard")


@login_required
def tenant_admin_home(request):
    """
    Home de administração para o administrador de um tenant específico.
    """
    user = request.user
    if not getattr(user, "is_tenant_admin", False) or not user.tenant_id:
        return redirect("dashboard")

    tenant = user.tenant
    empresas = (
        tenant.empresas.filter(ativo=True)
        .annotate(
            tipo_order=Case(
                When(tipo=Empresa.TIPO_MATRIZ, then=Value(0)),
                When(tipo=Empresa.TIPO_FILIAL, then=Value(1)),
                default=Value(2),
                output_field=IntegerField(),
            )
        )
        .order_by("tipo_order", "empresa_matriz__razao_social", "razao_social")
    )
    usuarios = tenant.users.all().order_by("nome")

    context = {
        "tenant": tenant,
        "empresas": empresas,
        "usuarios": usuarios,
    }
    return render(request, "tenant/admin_home.html", context)


@tenant_admin_required
def tenant_empresas_list(request):
    tenant = request.user.tenant
    empresas = (
        tenant.empresas.all()
        .annotate(
            tipo_order=Case(
                When(tipo=Empresa.TIPO_MATRIZ, then=Value(0)),
                When(tipo=Empresa.TIPO_FILIAL, then=Value(1)),
                default=Value(2),
                output_field=IntegerField(),
            )
        )
        .order_by("tipo_order", "empresa_matriz__razao_social", "razao_social")
    )
    return render(request, "tenant/empresas_list.html", {"tenant": tenant, "empresas": empresas})


@tenant_admin_required
def tenant_empresas_create(request):
    tenant = request.user.tenant
    if request.method == "POST":
        razao_social = request.POST.get("razao_social") or ""
        cnpj = request.POST.get("cnpj") or ""
        tipo = request.POST.get("tipo") or "M"
        empresa_matriz_id = request.POST.get("empresa_matriz_id") or None

        errors = []
        # valida limite de matrizes contratadas
        if tipo == Empresa.TIPO_MATRIZ:
            matrizes_ativas = tenant.empresas.filter(tipo=Empresa.TIPO_MATRIZ, ativo=True).count()
            if matrizes_ativas >= tenant.qtd_empresas_contratadas:
                errors.append(
                    "Quantidade máxima de empresas matriz contratadas para este tenant já foi atingida."
                )

        # se for filial, matriz é obrigatória
        if tipo == Empresa.TIPO_FILIAL and not empresa_matriz_id:
            errors.append("Selecione a empresa matriz para a filial.")

        empresa_matriz = None
        if empresa_matriz_id:
            empresa_matriz = tenant.empresas.filter(pk=empresa_matriz_id, tipo=Empresa.TIPO_MATRIZ).first()

        if errors:
            matrizes = tenant.empresas.filter(tipo=Empresa.TIPO_MATRIZ, ativo=True).order_by("razao_social")
            context = {
                "tenant": tenant,
                "empresa": None,
                "matrizes": matrizes,
                "errors": errors,
                "form": {
                    "razao_social": razao_social,
                    "cnpj": cnpj,
                    "tipo": tipo,
                    "empresa_matriz_id": empresa_matriz_id,
                },
            }
            return render(request, "tenant/empresa_form.html", context)

        Empresa.objects.create(
            tenant=tenant,
            empresa_matriz=empresa_matriz,
            razao_social=razao_social,
            cnpj=cnpj,
            tipo=tipo,
            ativo=True,
            created_by=request.user,
            updated_by=request.user,
        )
        return redirect("tenant-empresas-list")

    matrizes = request.user.tenant.empresas.filter(tipo=Empresa.TIPO_MATRIZ, ativo=True).order_by("razao_social")
    context = {"tenant": tenant, "empresa": None, "matrizes": matrizes, "form": {}}
    return render(request, "tenant/empresa_form.html", context)


@tenant_admin_required
def tenant_empresas_edit(request, empresa_id: int):
    tenant = request.user.tenant
    empresa = tenant.empresas.filter(pk=empresa_id).first()
    if not empresa:
        return redirect("tenant-empresas-list")

    if request.method == "POST":
        razao_social = request.POST.get("razao_social") or ""
        cnpj = request.POST.get("cnpj") or ""
        novo_tipo = request.POST.get("tipo") or empresa.tipo
        empresa_matriz_id = request.POST.get("empresa_matriz_id") or None
        ativo_flag = request.POST.get("ativo")

        errors = []
        # se estiver mudando de filial para matriz, validar limite
        if empresa.tipo != Empresa.TIPO_MATRIZ and novo_tipo == Empresa.TIPO_MATRIZ:
            matrizes_ativas = tenant.empresas.filter(tipo=Empresa.TIPO_MATRIZ, ativo=True).count()
            if matrizes_ativas >= tenant.qtd_empresas_contratadas:
                errors.append(
                    "Quantidade máxima de empresas matriz contratadas para este tenant já foi atingida."
                )

        # se for filial, matriz é obrigatória
        if novo_tipo == Empresa.TIPO_FILIAL and not empresa_matriz_id:
            errors.append("Selecione a empresa matriz para a filial.")

        if errors:
            matrizes = tenant.empresas.filter(tipo=Empresa.TIPO_MATRIZ, ativo=True).exclude(pk=empresa.pk).order_by(
                "razao_social"
            )
            # atualiza apenas dados de exibição, sem salvar
            empresa.razao_social = razao_social or empresa.razao_social
            empresa.cnpj = cnpj or empresa.cnpj
            empresa.tipo = novo_tipo
            context = {
                "tenant": tenant,
                "empresa": empresa,
                "matrizes": matrizes,
                "errors": errors,
            }
            return render(request, "tenant/empresa_form.html", context)

        empresa.razao_social = razao_social or empresa.razao_social
        empresa.cnpj = cnpj or empresa.cnpj
        empresa.tipo = novo_tipo
        empresa.ativo = bool(ativo_flag)

        if empresa_matriz_id and empresa.tipo == Empresa.TIPO_FILIAL:
            empresa_matriz = tenant.empresas.filter(pk=empresa_matriz_id, tipo=Empresa.TIPO_MATRIZ).first()
            empresa.empresa_matriz = empresa_matriz
        elif empresa.tipo == Empresa.TIPO_MATRIZ:
            empresa.empresa_matriz = None

        empresa.updated_by = request.user
        empresa.save()
        return redirect("tenant-empresas-list")

    matrizes = tenant.empresas.filter(tipo=Empresa.TIPO_MATRIZ, ativo=True).exclude(pk=empresa.pk).order_by(
        "razao_social"
    )
    form = {
        "razao_social": empresa.razao_social,
        "cnpj": empresa.cnpj,
        "tipo": empresa.tipo,
        "empresa_matriz_id": empresa.empresa_matriz_id,
    }
    context = {"tenant": tenant, "empresa": empresa, "matrizes": matrizes, "form": form}
    return render(request, "tenant/empresa_form.html", context)


@tenant_admin_required
def tenant_empresas_delete(request, empresa_id: int):
    tenant = request.user.tenant
    empresa = tenant.empresas.filter(pk=empresa_id).first()
    if empresa:
        # só permite exclusão se não houver filiais ou outras referências críticas
        has_filiais = tenant.empresas.filter(empresa_matriz=empresa, ativo=True).exists()

        if has_filiais:
            messages.error(
                request,
                "Não é possível excluir a empresa pois existem filiais vinculadas a ela. "
                "Desative ou remova as filiais primeiro.",
            )
        else:
            empresa.delete()
            messages.success(request, "Empresa excluída com sucesso.")
    return redirect("tenant-empresas-list")


@tenant_admin_required
def tenant_usuarios_list(request):
    tenant = request.user.tenant
    usuarios = tenant.users.all().order_by("-is_tenant_admin", "nome")
    return render(request, "tenant/usuarios_list.html", {"tenant": tenant, "usuarios": usuarios})


@tenant_admin_required
def tenant_usuarios_create(request):
    tenant = request.user.tenant
    errors: list[str] = []

    if request.method == "POST":
        nome = request.POST.get("nome") or ""
        email = request.POST.get("email") or ""
        password1 = request.POST.get("password1") or ""
        password2 = request.POST.get("password2") or ""
        ativo_flag = request.POST.get("ativo")

        if not nome:
            errors.append("Informe o nome do usuário.")
        if not email:
            errors.append("Informe o e-mail do usuário.")
        if email and User.objects.filter(email__iexact=email).exists():
            errors.append("Este e-mail já está em uso por outro usuário.")
        if not password1 or not password2:
            errors.append("Informe a senha do usuário e a confirmação.")
        elif password1 != password2:
            errors.append("As senhas do usuário não conferem.")
        # Limite de usuários operacionais (conta apenas usuários ATIVOS e não administradores)
        will_be_active = bool(ativo_flag) if ativo_flag is not None else True
        if will_be_active:
            usuarios_operacionais_ativos = tenant.users.filter(
                is_tenant_admin=False,
                ativo=True,
            ).count()
            if usuarios_operacionais_ativos >= tenant.qtd_usuarios_contratados:
                errors.append(
                    "A quantidade máxima de usuários permitida para esta conta foi atingida. "
                    "Administradores da conta não entram nesse limite."
                )

        if not errors:
            user = User.objects.create_user(
                email=email,
                nome=nome,
                password=password1,
                tenant=tenant,
                ativo=bool(ativo_flag) if ativo_flag is not None else True,
                is_staff=False,
                is_tenant_admin=False,
            )
            user.created_by = request.user
            user.updated_by = request.user
            user.save()
            perfis_ids = [
                int(x) for x in request.POST.getlist("perfis")
                if x.isdigit()
            ]
            perfis_do_tenant = tenant.perfis.filter(pk__in=perfis_ids)
            for perfil in perfis_do_tenant:
                UserPerfil.objects.create(
                    user=user,
                    perfil=perfil,
                    created_by=request.user,
                    updated_by=request.user,
                )
            return redirect("tenant-usuarios-list")

        context = {
            "tenant": tenant,
            "errors": errors,
            "form": {
                "nome": nome,
                "email": email,
                "ativo": request.POST.get("ativo") == "1",
            },
            "perfis": tenant.perfis.all().order_by("nome"),
            "perfis_selecionados": [
                int(x) for x in request.POST.getlist("perfis") if x.isdigit()
            ],
        }
        return render(request, "tenant/usuario_form.html", context)

    context = {
        "tenant": tenant,
        "perfis": tenant.perfis.all().order_by("nome"),
        "perfis_selecionados": [],
        "form": {"ativo": True},
    }
    return render(request, "tenant/usuario_form.html", context)


@tenant_admin_required
def tenant_usuarios_edit(request, usuario_id: int):
    tenant = request.user.tenant
    usuario = tenant.users.filter(pk=usuario_id).first()
    if not usuario:
        return redirect("tenant-usuarios-list")

    errors: list[str] = []

    if request.method == "POST":
        nome = request.POST.get("nome") or ""
        email = request.POST.get("email") or ""
        password1 = request.POST.get("password1") or ""
        password2 = request.POST.get("password2") or ""
        ativo_flag = request.POST.get("ativo")

        if not nome:
            errors.append("Informe o nome do usuário.")
        if not email:
            errors.append("Informe o e-mail do usuário.")
        if email and User.objects.filter(email__iexact=email).exclude(pk=usuario_id).exists():
            errors.append("Este e-mail já está em uso por outro usuário.")
        if (password1 or password2) and password1 != password2:
            errors.append("As senhas do usuário não conferem.")

        # Checkbox desmarcado não envia no POST; "ativo" presente com valor "1" = ativo
        new_ativo = request.POST.get("ativo") == "1"
        # Se for reativar um usuário operacional, validar limite de ativos
        if (
            not usuario.is_tenant_admin  # só usuários operacionais contam
            and not usuario.ativo        # atualmente está inativo
            and new_ativo                # será salvo como ativo
        ):
            usuarios_operacionais_ativos = tenant.users.filter(
                is_tenant_admin=False,
                ativo=True,
            ).exclude(pk=usuario.pk).count()
            if usuarios_operacionais_ativos >= tenant.qtd_usuarios_contratados:
                errors.append(
                    "A quantidade máxima de usuários permitida para esta conta foi atingida. "
                    "Administradores da conta não entram nesse limite."
                )

        if not errors:
            usuario.nome = nome
            usuario.email = email
            usuario.ativo = new_ativo
            if password1:
                usuario.set_password(password1)
            usuario.updated_by = request.user
            usuario.save()
            perfis_ids = [
                int(x) for x in request.POST.getlist("perfis")
                if x.isdigit()
            ]
            perfis_do_tenant = tenant.perfis.filter(pk__in=perfis_ids)
            usuario.perfis.all().delete()
            for perfil in perfis_do_tenant:
                UserPerfil.objects.create(
                    user=usuario,
                    perfil=perfil,
                    created_by=request.user,
                    updated_by=request.user,
                )
            return redirect("tenant-usuarios-list")

        context = {
            "tenant": tenant,
            "usuario": usuario,
            "errors": errors,
            "form": {
                "nome": nome,
                "email": email,
                "ativo": new_ativo,
            },
            "perfis": tenant.perfis.all().order_by("nome"),
            "perfis_selecionados": [
                int(x) for x in request.POST.getlist("perfis") if x.isdigit()
            ],
        }
        return render(request, "tenant/usuario_form.html", context)

    form = {
        "nome": usuario.nome,
        "email": usuario.email,
        "ativo": usuario.ativo,
    }
    perfis_selecionados = list(
        usuario.perfis.values_list("perfil_id", flat=True)
    )
    context = {
        "tenant": tenant,
        "usuario": usuario,
        "form": form,
        "perfis": tenant.perfis.all().order_by("nome"),
        "perfis_selecionados": perfis_selecionados,
    }
    return render(request, "tenant/usuario_form.html", context)


@tenant_admin_required
def tenant_usuarios_delete(request, usuario_id: int):
    tenant = request.user.tenant
    usuario = tenant.users.filter(pk=usuario_id).first()
    if usuario and usuario.pk != request.user.pk:
        try:
            usuario.delete()
            messages.success(request, "Usuário excluído com sucesso.")
        except Exception as e:
            # Se houver alguma restrição (ex.: dados no financeiro), apenas desativa
            usuario.ativo = False
            usuario.updated_by = request.user
            usuario.save()
            messages.warning(
                request,
                "Usuário possui dados relacionados e foi apenas desativado.",
            )
    return redirect("tenant-usuarios-list")


# --- Perfis (admin da conta) ---


@tenant_admin_required
def tenant_perfis_list(request):
    tenant = request.user.tenant
    perfis = tenant.perfis.all().order_by("nome")
    return render(request, "tenant/perfis_list.html", {"tenant": tenant, "perfis": perfis})


@tenant_admin_required
def tenant_perfis_create(request):
    tenant = request.user.tenant
    if request.method == "POST":
        nome = request.POST.get("nome") or ""
        descricao = request.POST.get("descricao") or ""
        if not nome:
            messages.error(request, "Informe o nome do perfil.")
            return render(
                request,
                "tenant/perfil_form.html",
                {"tenant": tenant, "form": {"nome": nome, "descricao": descricao}},
            )
        Perfil.objects.create(
            tenant=tenant,
            nome=nome,
            descricao=descricao,
            created_by=request.user,
            updated_by=request.user,
        )
        messages.success(request, "Perfil criado com sucesso.")
        return redirect("tenant-perfis-list")
    return render(request, "tenant/perfil_form.html", {"tenant": tenant, "form": {}})


@tenant_admin_required
def tenant_perfis_edit(request, perfil_id: int):
    tenant = request.user.tenant
    perfil = tenant.perfis.filter(pk=perfil_id).first()
    if not perfil:
        return redirect("tenant-perfis-list")
    if request.method == "POST":
        nome = request.POST.get("nome") or ""
        descricao = request.POST.get("descricao") or ""
        if not nome:
            messages.error(request, "Informe o nome do perfil.")
            return render(
                request,
                "tenant/perfil_form.html",
                {"tenant": tenant, "perfil": perfil, "form": {"nome": nome, "descricao": descricao}},
            )
        perfil.nome = nome
        perfil.descricao = descricao
        perfil.updated_by = request.user
        perfil.save()
        messages.success(request, "Perfil atualizado com sucesso.")
        return redirect("tenant-perfis-list")
    form = {"nome": perfil.nome, "descricao": perfil.descricao}
    return render(request, "tenant/perfil_form.html", {"tenant": tenant, "perfil": perfil, "form": form})


@tenant_admin_required
def tenant_perfis_delete(request, perfil_id: int):
    tenant = request.user.tenant
    perfil = tenant.perfis.filter(pk=perfil_id).first()
    if perfil:
        if perfil.usuarios.exists() or perfil.permissoes_empresas.exists():
            messages.error(
                request,
                "Não é possível excluir o perfil pois existem usuários ou permissões vinculadas. "
                "Remova os vínculos primeiro.",
            )
        else:
            perfil.delete()
            messages.success(request, "Perfil excluído com sucesso.")
    return redirect("tenant-perfis-list")


@tenant_admin_required
def tenant_config_list(request):
    """
    Gestão de configurações do tenant (ADM_CONFIG):
    - Lista todos os parâmetros ativos de SIS_CONFIG.
    - Permite definir/alterar o valor de cada parâmetro para o tenant atual.
    """
    tenant = request.user.tenant
    # apenas matrizes do tenant
    empresas_matriz = (
        tenant.empresas.filter(ativo=True, tipo=Empresa.TIPO_MATRIZ)
        .order_by("razao_social")
    )
    if not empresas_matriz:
        messages.warning(request, "Não há empresas matriz cadastradas para este tenant.")
        return redirect("tenant-admin-home")

    # empresa selecionada via GET/POST ou primeira matriz
    empresa_id = request.POST.get("empresa_id") or request.GET.get("empresa_id")
    try:
        empresa_selecionada = (
            empresas_matriz.get(pk=int(empresa_id)) if empresa_id else empresas_matriz.first()
        )
    except (ValueError, Empresa.DoesNotExist):
        empresa_selecionada = empresas_matriz.first()

    parametros = SisConfig.objects.filter(ativo=True).order_by("chave")
    existentes = {
        c.parametro_id: c
        for c in Configuracao.objects.filter(
            tenant=tenant,
            empresa=empresa_selecionada,
            parametro__in=parametros,
        ).select_related("parametro")
    }

    if request.method == "POST":
        for p in parametros:
            field_name = f"config_{p.id}"
            valor = (request.POST.get(field_name) or "").strip()
            conf = existentes.get(p.id)
            if conf:
                conf.valor = valor
                conf.updated_by = request.user
                conf.save()
            else:
                Configuracao.objects.create(
                    tenant=tenant,
                    empresa=empresa_selecionada,
                    parametro=p,
                    valor=valor,
                    created_by=request.user,
                    updated_by=request.user,
                )
        messages.success(request, "Configurações salvas com sucesso.")
        return redirect("tenant-config-list")

    configs = []
    for p in parametros:
        conf = existentes.get(p.id)
        configs.append(
            {
                "parametro": p,
                "valor": conf.valor if conf else "",
            }
        )

    return render(
        request,
        "tenant/config_list.html",
        {
            "tenant": tenant,
            "empresas_matriz": empresas_matriz,
            "empresa_selecionada": empresa_selecionada,
            "configs": configs,
        },
    )


@tenant_admin_required
def tenant_perfil_permissoes(request, perfil_id: int):
    tenant = request.user.tenant
    perfil = tenant.perfis.filter(pk=perfil_id).first()
    if not perfil:
        return redirect("tenant-perfis-list")
    empresas = (
        tenant.empresas.all()
        .annotate(
            tipo_order=Case(
                When(tipo=Empresa.TIPO_MATRIZ, then=Value(0)),
                When(tipo=Empresa.TIPO_FILIAL, then=Value(1)),
                default=Value(2),
                output_field=IntegerField(),
            )
        )
        .order_by("tipo_order", "empresa_matriz__razao_social", "razao_social")
    )
    if request.method == "POST":
        for empresa in empresas:
            perm, created = PermissaoEmpresa.objects.get_or_create(perfil=perfil, empresa=empresa)
            if created:
                perm.created_by = request.user
            perm.pode_visualizar = request.POST.get(f"viz_{empresa.id}") == "1"
            perm.pode_incluir = request.POST.get(f"inc_{empresa.id}") == "1"
            perm.pode_editar = request.POST.get(f"edit_{empresa.id}") == "1"
            perm.pode_excluir = request.POST.get(f"exc_{empresa.id}") == "1"
            perm.pode_aprovar = request.POST.get(f"apr_{empresa.id}") == "1"
            perm.updated_by = request.user
            perm.save()
        messages.success(request, "Permissões salvas com sucesso.")
        return redirect("tenant-perfis-list")
    permissoes_por_empresa = {
        p.empresa_id: p
        for p in perfil.permissoes_empresas.select_related("empresa").all()
    }
    empresas_com_perm = [
        {"empresa": e, "perm": permissoes_por_empresa.get(e.id)}
        for e in empresas
    ]
    return render(
        request,
        "tenant/perfil_permissoes.html",
        {
            "tenant": tenant,
            "perfil": perfil,
            "empresas_com_perm": empresas_com_perm,
        },
    )


@superuser_required
def tenant_list(request):
    tenants = Tenant.objects.all().order_by("razao_social")
    return render(request, "sys/tenant_list.html", {"tenants": tenants})


@superuser_required
def tenant_create(request):
    if request.method == "POST":
        razao_social = request.POST.get("razao_social") or ""
        nome_fantasia = request.POST.get("nome_fantasia") or ""
        cnpj = request.POST.get("cnpj") or ""
        qtd_empresas = request.POST.get("qtd_empresas_contratadas") or "1"
        qtd_usuarios = request.POST.get("qtd_usuarios_contratados") or "1"
        admin_nome = request.POST.get("admin_nome") or ""
        admin_email = (request.POST.get("admin_email") or "").strip()
        admin_password1 = request.POST.get("admin_password1") or ""
        admin_password2 = request.POST.get("admin_password2") or ""

        errors = []
        if admin_password1 != admin_password2:
            errors.append("As senhas do administrador do tenant não conferem.")
        if not admin_password1 or not admin_password2:
            errors.append("Informe a senha e a confirmação do administrador.")
        if not admin_email:
            errors.append("Informe o e-mail do administrador do tenant.")
        if admin_email and User.objects.filter(email__iexact=admin_email).exists():
            errors.append("Este e-mail já está em uso por outro usuário.")
        if not admin_nome:
            errors.append("Informe o nome do administrador do tenant.")

        if errors:
            context = {
                "errors": errors,
                "form": {
                    "razao_social": razao_social,
                    "nome_fantasia": nome_fantasia,
                    "cnpj": cnpj,
                    "qtd_empresas_contratadas": qtd_empresas,
                    "qtd_usuarios_contratados": qtd_usuarios,
                    "admin_nome": admin_nome,
                    "admin_email": admin_email,
                },
            }
            return render(request, "sys/tenant_form.html", context)

        tenant = None
        try:
            tenant = Tenant.objects.create(
                razao_social=razao_social,
                nome_fantasia=nome_fantasia,
                cnpj=cnpj,
                qtd_empresas_contratadas=int(qtd_empresas or 1),
                qtd_usuarios_contratados=int(qtd_usuarios or 1),
                created_by=request.user,
                updated_by=request.user,
            )

            # cria usuário administrador do tenant (ligação apenas via ADM_USER.tenant)
            admin_user = User.objects.create_user(
                email=admin_email,
                nome=admin_nome,
                password=admin_password1,
                tenant=tenant,
                ativo=True,
                is_staff=True,
                is_tenant_admin=True,
            )
            admin_user.created_by = request.user
            admin_user.updated_by = request.user
            admin_user.save()
        except Exception as e:
            from django.db import IntegrityError
            if tenant is not None:
                try:
                    tenant.delete()
                except Exception:
                    pass
            if isinstance(e, IntegrityError):
                messages.error(
                    request,
                    "Não foi possível criar o tenant. E-mail ou CNPJ já pode estar em uso.",
                )
            else:
                messages.error(
                    request,
                    f"Erro ao criar tenant: {e}",
                )
            context = {
                "errors": [],
                "form": {
                    "razao_social": razao_social,
                    "nome_fantasia": nome_fantasia,
                    "cnpj": cnpj,
                    "qtd_empresas_contratadas": qtd_empresas,
                    "qtd_usuarios_contratados": qtd_usuarios,
                    "admin_nome": admin_nome,
                    "admin_email": admin_email,
                },
            }
            return render(request, "sys/tenant_form.html", context)

        return redirect("sys-tenant-list")

    return render(request, "sys/tenant_form.html")


@superuser_required
def tenant_edit(request, tenant_id: int):
    tenant = Tenant.objects.get(pk=tenant_id)
    admin_user = (
        User.objects.filter(tenant=tenant, is_tenant_admin=True).order_by("id").first()
    )

    if request.method == "POST":
        # campos do tenant
        razao_social = request.POST.get("razao_social") or ""
        nome_fantasia = request.POST.get("nome_fantasia") or ""
        cnpj = request.POST.get("cnpj") or ""
        qtd_empresas = request.POST.get("qtd_empresas_contratadas") or tenant.qtd_empresas_contratadas
        qtd_usuarios = request.POST.get("qtd_usuarios_contratados") or tenant.qtd_usuarios_contratados
        ativo = request.POST.get("ativo")

        # campos do administrador
        admin_nome = request.POST.get("admin_nome") or ""
        admin_email = request.POST.get("admin_email") or ""
        admin_admin_flag = request.POST.get("admin_is_tenant_admin")
        new_password1 = request.POST.get("admin_password1") or ""
        new_password2 = request.POST.get("admin_password2") or ""

        errors: list[str] = []
        if (new_password1 or new_password2) and new_password1 != new_password2:
            errors.append("As senhas do administrador do tenant não conferem.")

        if errors:
            form_data = {
                "razao_social": razao_social or tenant.razao_social,
                "nome_fantasia": nome_fantasia,
                "cnpj": cnpj or tenant.cnpj,
                "qtd_empresas_contratadas": qtd_empresas,
                "qtd_usuarios_contratados": qtd_usuarios,
                "ativo": bool(ativo),
                "admin_nome": admin_nome or (admin_user.nome if admin_user else ""),
                "admin_email": admin_email or (admin_user.email if admin_user else ""),
                "admin_is_tenant_admin": bool(admin_admin_flag)
                if admin_admin_flag is not None
                else (admin_user.is_tenant_admin if admin_user else False),
            }
            context = {
                "tenant": tenant,
                "form": form_data,
                "has_admin": bool(admin_user),
                "errors": errors,
            }
            return render(request, "sys/tenant_edit.html", context)

        # salva tenant
        tenant.razao_social = razao_social or tenant.razao_social
        tenant.nome_fantasia = nome_fantasia
        tenant.cnpj = cnpj or tenant.cnpj
        tenant.qtd_empresas_contratadas = int(qtd_empresas)
        tenant.qtd_usuarios_contratados = int(qtd_usuarios)
        tenant.ativo = bool(ativo)
        tenant.updated_by = request.user
        tenant.save()

        # atualiza dados do administrador do tenant, se existir, senão cria se informado
        if admin_user:
            admin_user.nome = admin_nome or admin_user.nome
            admin_user.email = admin_email or admin_user.email
            admin_user.is_tenant_admin = bool(admin_admin_flag)
            if new_password1:
                admin_user.set_password(new_password1)

            admin_user.updated_by = request.user
            admin_user.save()
        elif admin_email and admin_nome and new_password1:
            # cria admin para tenants antigos que ainda não tinham administrador
            new_admin = User.objects.create_user(
                email=admin_email,
                nome=admin_nome,
                password=new_password1,
                tenant=tenant,
                ativo=True,
                is_staff=True,
                is_tenant_admin=bool(admin_admin_flag) or True,
            )
            new_admin.created_by = request.user
            new_admin.updated_by = request.user
            new_admin.save()

        return redirect("sys-tenant-list")

    form_data = {
        "razao_social": tenant.razao_social,
        "nome_fantasia": tenant.nome_fantasia,
        "cnpj": tenant.cnpj,
        "qtd_empresas_contratadas": tenant.qtd_empresas_contratadas,
        "qtd_usuarios_contratados": tenant.qtd_usuarios_contratados,
        "ativo": tenant.ativo,
        "admin_nome": admin_user.nome if admin_user else "",
        "admin_email": admin_user.email if admin_user else "",
        "admin_is_tenant_admin": admin_user.is_tenant_admin if admin_user else False,
    }
    context = {
        "tenant": tenant,
        "form": form_data,
        "has_admin": bool(admin_user),
    }
    return render(request, "sys/tenant_edit.html", context)


@csrf_exempt
def logout_view(request):
    logout(request)
    return redirect("login")
