from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Max, ProtectedError, Q, Sum, Exists, OuterRef
from decimal import Decimal
from django.shortcuts import get_object_or_404, redirect, render
from django.http import JsonResponse
from django.urls import reverse

from core.context_processors import get_empresas_contexto, get_empresa_matriz
from core.models import Estado, Cidade, Configuracao, SisConfig, Empresa
from accounts.models import PermissaoEmpresa
from finance.models import (
    Banco,
    CentroResultado,
    ContaFinanceira,
    ContaPagar,
    ContaPagarBaixa,
    ContaPagarRateio,
    ContaPagarVenc,
    ContaReceber,
    ContaReceberBaixa,
    ContaReceberRateio,
    ContaReceberVenc,
    Lancamento,
    Pessoa,
    PlanoConta,
    Transferencia,
    TipoDocumento,
)


def _get_empresas_tenant(request):
    """
    Retorna empresas do contexto: matriz selecionada + filiais (para filtro no sistema).
    Retorna None se o usuário não tiver tenant ou não houver empresa matriz na sessão.
    """
    return get_empresas_contexto(request)


def _cpf_cnpj_apenas_digitos(valor):
    """Retorna apenas os dígitos do CPF/CNPJ para armazenar no banco (máx. 14)."""
    if not valor:
        return ""
    digitos = "".join(c for c in str(valor) if c.isdigit())
    return digitos[:14]


def _get_mascara_centro_resultado(empresa: Empresa) -> str | None:
    """
    Busca a máscara de código de centro de resultado nas configurações
    (SIS_CONFIG/ADM_CONFIG) para a empresa matriz informada.

    Convencão: parâmetro SIS_CONFIG.chave = 'MASC_CENTRO_RESULTADO'.
    """
    if not empresa or not empresa.tenant_id:
        return None
    # Busca diretamente em ADM_CONFIG, garantindo que usamos o parâmetro
    # correto para esta empresa, origem "Centro de Resultado" e chave "Máscara".
    conf = (
        Configuracao.objects.filter(
            tenant=empresa.tenant,
            empresa=empresa,
            parametro__origem__nome__iexact="centro de resultado",
            parametro__chave__iexact="Máscara",
            parametro__ativo=True,
        )
        .select_related("parametro")
        .order_by("parametro__id")
        .first()
    )
    valor = (conf.valor or "").strip() if conf else ""
    return valor or None


def _get_mascara_plano_conta(empresa: Empresa) -> str | None:
    """
    Busca a máscara de código do plano de contas nas configurações
    (SIS_CONFIG/ADM_CONFIG) para a empresa matriz informada.

    Convenção: parametro.origem.nome = 'Plano de Conta', chave = 'Máscara'.
    """
    if not empresa or not empresa.tenant_id:
        return None
    conf = (
        Configuracao.objects.filter(
            tenant=empresa.tenant,
            empresa=empresa,
            parametro__origem__nome__iexact="plano de conta",
            parametro__chave__iexact="Máscara",
            parametro__ativo=True,
        )
        .select_related("parametro")
        .order_by("parametro__id")
        .first()
    )
    valor = (conf.valor or "").strip() if conf else ""
    return valor or None


def _codigo_respeita_mascara(codigo: str, mascara: str | None) -> bool:
    """
    Valida se o código respeita a máscara:
    - Mesma quantidade de caracteres.
    - Onde a máscara tem dígito (0-9), o código deve ter dígito.
    - Onde a máscara tem outro caractere (.,-/ etc.), deve coincidir exatamente.
    """
    if not mascara:
        return True
    codigo = (codigo or "").strip()
    mascara = mascara.strip()
    if not codigo:
        return False
    # Código deve ser um prefixo válido da máscara
    if len(codigo) > len(mascara):
        return False
    for c_ch, m_ch in zip(codigo, mascara):
        if m_ch.isdigit():
            if not c_ch.isdigit():
                return False
        else:
            if c_ch != m_ch:
                return False
    return True


def _sugerir_codigo_centro_resultado(empresa: Empresa, mascara: str | None, centro_pai: "CentroResultado | None"):
    """
    Sugere o próximo código para um centro de resultado, com base na máscara e no pai.

    Convenção de máscara: segmentos separados por '.', onde o tamanho de cada
    segmento é o número de dígitos (ex.: '9.9.99' => [1,1,2]).
    """
    from finance.models import CentroResultado  # import local para evitar ciclos

    if not mascara or not empresa:
        return ""

    mascara = mascara.strip()
    segmentos_mascara = mascara.split(".")
    comprimentos = [len(seg) for seg in segmentos_mascara]

    # sem pai => nível raiz
    if centro_pai is None:
        width = comprimentos[0]
        filhos = CentroResultado.objects.filter(empresa=empresa, centro_pai__isnull=True)
        max_num = 0
        for f in filhos:
            partes = (f.codigo or "").split(".")
            if not partes:
                continue
            try:
                num = int(partes[0])
            except (ValueError, TypeError):
                continue
            if num > max_num:
                max_num = num
        prox = str(max_num + 1).zfill(width)
        return prox

    # com pai: próximo segmento depois do código do pai
    codigo_pai = (centro_pai.codigo or "").strip()
    if not codigo_pai:
        return ""
    partes_pai = codigo_pai.split(".")
    nivel_pai = len(partes_pai)
    if nivel_pai >= len(comprimentos):
        # pai já está no último nível da máscara
        return codigo_pai

    width = comprimentos[nivel_pai]
    prefixo = codigo_pai + "."
    filhos = CentroResultado.objects.filter(empresa=empresa, codigo__startswith=prefixo)

    max_num = 0
    for f in filhos:
        partes = (f.codigo or "").split(".")
        if len(partes) <= nivel_pai:
            continue
        try:
            num = int(partes[nivel_pai])
        except (ValueError, TypeError):
            continue
        if num > max_num:
            max_num = num

    prox = str(max_num + 1).zfill(width)
    novas_partes = partes_pai + [prox]
    return ".".join(novas_partes)


def _sugerir_codigo_plano_conta(empresa: Empresa, mascara: str | None, conta_pai: "PlanoConta | None"):
    """
    Sugere o próximo código para uma conta do plano, com base na máscara e no pai.
    Mesma lógica do centro de resultado: segmentos separados por '.', tamanho por segmento.
    """
    if not mascara or not empresa:
        return ""

    mascara = mascara.strip()
    segmentos_mascara = mascara.split(".")
    comprimentos = [len(seg) for seg in segmentos_mascara]

    if conta_pai is None:
        width = comprimentos[0]
        filhos = PlanoConta.objects.filter(empresa=empresa, conta_pai__isnull=True)
        max_num = 0
        for f in filhos:
            partes = (f.codigo or "").split(".")
            if not partes:
                continue
            try:
                num = int(partes[0])
            except (ValueError, TypeError):
                continue
            if num > max_num:
                max_num = num
        prox = str(max_num + 1).zfill(width)
        return prox

    codigo_pai = (conta_pai.codigo or "").strip()
    if not codigo_pai:
        return ""
    partes_pai = codigo_pai.split(".")
    nivel_pai = len(partes_pai)
    if nivel_pai >= len(comprimentos):
        return codigo_pai

    width = comprimentos[nivel_pai]
    prefixo = codigo_pai + "."
    filhos = PlanoConta.objects.filter(empresa=empresa, codigo__startswith=prefixo)

    max_num = 0
    for f in filhos:
        partes = (f.codigo or "").split(".")
        if len(partes) <= nivel_pai:
            continue
        try:
            num = int(partes[nivel_pai])
        except (ValueError, TypeError):
            continue
        if num > max_num:
            max_num = num

    prox = str(max_num + 1).zfill(width)
    novas_partes = partes_pai + [prox]
    return ".".join(novas_partes)


def _usuario_tem_permissao_empresa(request, empresa, acao: str) -> bool:
    """
    Verifica se o usuário tem permissão na empresa informada, via Perfis/PermissaoEmpresa.
    Ações: 'visualizar', 'incluir', 'editar', 'excluir'.
    Admin do tenant sempre tem todas as permissões.
    """
    user = request.user
    if getattr(user, "is_tenant_admin", False):
        return True

    perfil_ids = list(user.perfis.values_list("perfil_id", flat=True))
    if not perfil_ids or not empresa:
        return False

    # Permissão pode ser configurada diretamente na empresa ou na sua matriz.
    empresa_ids = [empresa.id]
    matriz_id = getattr(empresa, "empresa_matriz_id", None)
    if matriz_id:
        empresa_ids.append(matriz_id)

    qs = PermissaoEmpresa.objects.filter(perfil_id__in=perfil_ids, empresa_id__in=empresa_ids)
    if acao == "visualizar":
        return qs.filter(pode_visualizar=True).exists()
    if acao == "incluir":
        return qs.filter(pode_incluir=True).exists()
    if acao == "editar":
        return qs.filter(pode_editar=True).exists()
    if acao == "excluir":
        return qs.filter(pode_excluir=True).exists()
    return False


def _usuario_tem_permissao_alguma_empresa(request, empresas, acao: str) -> bool:
    """
    Verifica se o usuário tem a permissão informada em pelo menos uma das empresas do queryset.
    Útil para cadastros que não são por empresa (ex.: bancos).
    """
    user = request.user
    if getattr(user, "is_tenant_admin", False):
        return True

    if not empresas:
        return False

    perfil_ids = list(user.perfis.values_list("perfil_id", flat=True))
    if not perfil_ids:
        return False

    qs = PermissaoEmpresa.objects.filter(perfil_id__in=perfil_ids, empresa__in=empresas)
    if acao == "visualizar":
        return qs.filter(pode_visualizar=True).exists()
    if acao == "incluir":
        return qs.filter(pode_incluir=True).exists()
    if acao == "editar":
        return qs.filter(pode_editar=True).exists()
    if acao == "excluir":
        return qs.filter(pode_excluir=True).exists()
    return False


def _placeholder_view(request, titulo):
    return render(request, "finance/placeholder.html", {"titulo": titulo})


# --- Pessoas (Cadastros) ---


@login_required
def cadastros_pessoas(request):
    empresas = _get_empresas_tenant(request)
    if empresas is None:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    # Mensagem vinda do botão Excluir (verificação de permissão)
    erro_excluir = (request.GET.get("erro_excluir") or "").strip()
    if erro_excluir == "permissao":
        messages.error(request, "Você não tem permissão para excluir registros desta empresa.")
    elif erro_excluir == "erro":
        messages.error(request, "Não foi possível verificar. Tente novamente.")

    qs = Pessoa.objects.filter(empresa__in=empresas).select_related("empresa").order_by("nome_razao")

    # Filtros
    nome = (request.GET.get("nome") or "").strip()
    cnpj = (request.GET.get("cnpj") or "").strip()
    tipo_cadastro = (request.GET.get("tipo_cadastro") or "").strip()
    # Padrão "A" (Cliente e Fornecedor) = não filtra, mostra todos
    if tipo_cadastro == "A":
        tipo_cadastro = ""
    if nome:
        qs = qs.filter(nome_razao__icontains=nome)
    if cnpj:
        qs = qs.filter(cpf_cnpj__icontains=cnpj)
    if tipo_cadastro and tipo_cadastro in dict(Pessoa.TIPO_CADASTRO_CHOICES):
        qs = qs.filter(tipo_cadastro=tipo_cadastro)
    # Para exibir no formulário, usar "A" quando vazio (padrão)
    filtro_tipo_display = tipo_cadastro or "A"

    context = {
        "pessoas": qs,
        "empresas": empresas,
        "filtros": {"nome": nome, "cnpj": cnpj, "tipo_cadastro": filtro_tipo_display},
        "tipo_cadastro_choices": Pessoa.TIPO_CADASTRO_CHOICES,
    }
    return render(request, "finance/pessoas_list.html", context)


@login_required
def cadastros_pessoas_novo(request):
    empresas = _get_empresas_tenant(request)
    if empresas is None:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    # Empresa usada para criação (matriz atual do contexto)
    empresa_contexto = empresas.first()
    if not _usuario_tem_permissao_empresa(request, empresa_contexto, "incluir"):
        messages.error(request, "Você não tem permissão para incluir registros nesta empresa.")
        return redirect("cadastros-pessoas")

    estados = Estado.objects.order_by("nome")
    # Todas as cidades, para o JS filtrar por UF no formulário
    todas_cidades = Cidade.objects.select_related("estado").order_by("estado__nome", "nome")

    if request.method == "POST":
        nome_razao = (request.POST.get("nome_razao") or "").strip()
        cpf_cnpj = _cpf_cnpj_apenas_digitos(request.POST.get("cpf_cnpj") or "")
        tipo = request.POST.get("tipo", Pessoa.TIPO_JURIDICA)
        tipo_cadastro = request.POST.get("tipo_cadastro", Pessoa.AMBOS)
        # Localização
        endereco = (request.POST.get("endereco") or "").strip()
        numero = (request.POST.get("numero") or "").strip()
        complemento = (request.POST.get("complemento") or "").strip()
        bairro = (request.POST.get("bairro") or "").strip()
        estado_id = request.POST.get("estado_id")
        cidade_id = request.POST.get("cidade_id")
        # Contato
        telefone = (request.POST.get("telefone") or "").strip()
        celular = (request.POST.get("celular") or "").strip()
        whatsapp_flag = request.POST.get("whatsapp")
        email = (request.POST.get("email") or "").strip()
        if not nome_razao:
            messages.error(request, "Informe o nome ou razão social.")
            return render(
                request,
                "finance/pessoa_form.html",
                {
                    "empresas": empresas,
                    "estados": estados,
                    "todas_cidades": todas_cidades,
                    "form": {
                        "nome_razao": nome_razao,
                        "cpf_cnpj": cpf_cnpj,
                        "tipo": tipo,
                        "tipo_cadastro": tipo_cadastro,
                        "endereco": endereco,
                        "numero": numero,
                        "complemento": complemento,
                        "bairro": bairro,
                        "estado_id": int(estado_id) if estado_id and estado_id.isdigit() else None,
                        "cidade_id": int(cidade_id) if cidade_id and cidade_id.isdigit() else None,
                        "telefone": telefone,
                        "celular": celular,
                        "whatsapp": bool(whatsapp_flag),
                        "email": email,
                    },
                    "tipo_choices": Pessoa.TIPOS,
                    "tipo_cadastro_choices": Pessoa.TIPO_CADASTRO_CHOICES,
                },
            )
        # Empresa sempre vem do contexto (matriz atual); ignoramos empresa_id do POST
        empresa = empresa_contexto or empresas.first()
        if not empresa:
            messages.error(request, "Selecione uma empresa.")
            return render(
                request,
                "finance/pessoa_form.html",
                {
                    "empresas": empresas,
                    "estados": estados,
                    "todas_cidades": todas_cidades,
                    "form": {
                        "nome_razao": nome_razao,
                        "cpf_cnpj": cpf_cnpj,
                        "tipo": tipo,
                        "tipo_cadastro": tipo_cadastro,
                        "endereco": endereco,
                        "numero": numero,
                        "complemento": complemento,
                        "bairro": bairro,
                        "estado_id": int(estado_id) if estado_id and estado_id.isdigit() else None,
                        "cidade_id": int(cidade_id) if cidade_id and cidade_id.isdigit() else None,
                        "telefone": telefone,
                        "celular": celular,
                        "whatsapp": bool(whatsapp_flag),
                        "email": email,
                    },
                    "tipo_choices": Pessoa.TIPOS,
                    "tipo_cadastro_choices": Pessoa.TIPO_CADASTRO_CHOICES,
                },
            )
        estado = Estado.objects.filter(pk=estado_id).first() if estado_id and estado_id.isdigit() else None
        cidade = (
            Cidade.objects.filter(pk=cidade_id, estado=estado).first()
            if cidade_id and cidade_id.isdigit() and estado
            else None
        )

        Pessoa.objects.create(
            empresa=empresa,
            tipo=tipo,
            nome_razao=nome_razao,
            cpf_cnpj=cpf_cnpj,
            tipo_cadastro=tipo_cadastro or Pessoa.AMBOS,
            endereco=endereco,
            numero=numero,
            complemento=complemento,
            bairro=bairro,
            estado=estado,
            cidade=cidade,
            telefone=telefone,
            celular=celular,
            whatsapp=bool(whatsapp_flag),
            email=email,
            created_by=request.user,
            updated_by=request.user,
        )
        messages.success(request, "Cliente/fornecedor cadastrado com sucesso.")
        return redirect("cadastros-pessoas")

    return render(
        request,
        "finance/pessoa_form.html",
        {
            "empresas": empresas,
            "estados": estados,
            "todas_cidades": todas_cidades,
            "form": {},
            "tipo_choices": Pessoa.TIPOS,
            "tipo_cadastro_choices": Pessoa.TIPO_CADASTRO_CHOICES,
        },
    )


@login_required
def cadastros_pessoas_editar(request, pessoa_id):
    empresas = _get_empresas_tenant(request)
    if empresas is None:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    pessoa = get_object_or_404(Pessoa, pk=pessoa_id, empresa__in=empresas)

    if not _usuario_tem_permissao_empresa(request, pessoa.empresa, "editar"):
        messages.error(request, "Você não tem permissão para editar registros desta empresa.")
        return redirect("cadastros-pessoas")

    estados = Estado.objects.order_by("nome")
    todas_cidades = Cidade.objects.select_related("estado").order_by("estado__nome", "nome")

    if request.method == "POST":
        nome_razao = (request.POST.get("nome_razao") or "").strip()
        cpf_cnpj = _cpf_cnpj_apenas_digitos(request.POST.get("cpf_cnpj") or "")
        tipo = request.POST.get("tipo", pessoa.tipo)
        tipo_cadastro = request.POST.get("tipo_cadastro", pessoa.tipo_cadastro)
        # Localização
        endereco = (request.POST.get("endereco") or "").strip()
        numero = (request.POST.get("numero") or "").strip()
        complemento = (request.POST.get("complemento") or "").strip()
        bairro = (request.POST.get("bairro") or "").strip()
        estado_id = request.POST.get("estado_id")
        cidade_id = request.POST.get("cidade_id")
        # Contato
        telefone = (request.POST.get("telefone") or "").strip()
        celular = (request.POST.get("celular") or "").strip()
        whatsapp_flag = request.POST.get("whatsapp")
        email = (request.POST.get("email") or "").strip()
        if not nome_razao:
            messages.error(request, "Informe o nome ou razão social.")
            return render(
                request,
                "finance/pessoa_form.html",
                {
                    "pessoa": pessoa,
                    "empresas": empresas,
                    "estados": estados,
                    "todas_cidades": todas_cidades,
                    "form": {
                        "nome_razao": nome_razao,
                        "cpf_cnpj": cpf_cnpj,
                        "tipo": tipo,
                        "tipo_cadastro": tipo_cadastro,
                        "endereco": endereco,
                        "numero": numero,
                        "complemento": complemento,
                        "bairro": bairro,
                        "estado_id": int(estado_id) if estado_id and estado_id.isdigit() else pessoa.estado_id,
                        "cidade_id": int(cidade_id) if cidade_id and cidade_id.isdigit() else pessoa.cidade_id,
                        "telefone": telefone,
                        "celular": celular,
                        "whatsapp": bool(whatsapp_flag) if whatsapp_flag is not None else pessoa.whatsapp,
                        "email": email,
                    },
                    "tipo_choices": Pessoa.TIPOS,
                    "tipo_cadastro_choices": Pessoa.TIPO_CADASTRO_CHOICES,
                },
            )
        empresa = pessoa.empresa
        estado = Estado.objects.filter(pk=estado_id).first() if estado_id and estado_id.isdigit() else pessoa.estado
        cidade = (
            Cidade.objects.filter(pk=cidade_id, estado=estado).first()
            if cidade_id and cidade_id.isdigit() and estado
            else pessoa.cidade
        )
        pessoa.nome_razao = nome_razao
        pessoa.cpf_cnpj = cpf_cnpj
        pessoa.tipo = tipo
        pessoa.tipo_cadastro = tipo_cadastro or Pessoa.AMBOS
        pessoa.empresa = empresa
        pessoa.endereco = endereco
        pessoa.numero = numero
        pessoa.complemento = complemento
        pessoa.bairro = bairro
        pessoa.estado = estado
        pessoa.cidade = cidade
        pessoa.telefone = telefone
        pessoa.celular = celular
        pessoa.whatsapp = bool(whatsapp_flag) if whatsapp_flag is not None else pessoa.whatsapp
        pessoa.email = email
        pessoa.updated_by = request.user
        pessoa.save()
        messages.success(request, "Cliente/fornecedor atualizado com sucesso.")
        return redirect("cadastros-pessoas")

    form = {
        "nome_razao": pessoa.nome_razao,
        "cpf_cnpj": pessoa.cpf_cnpj,
        "tipo": pessoa.tipo,
        "tipo_cadastro": pessoa.tipo_cadastro,
        "empresa_id": pessoa.empresa_id,
        "endereco": pessoa.endereco,
        "numero": pessoa.numero,
        "complemento": pessoa.complemento,
        "bairro": pessoa.bairro,
        "estado_id": pessoa.estado_id,
        "cidade_id": pessoa.cidade_id,
        "telefone": pessoa.telefone,
        "celular": pessoa.celular,
        "whatsapp": pessoa.whatsapp,
        "email": pessoa.email,
    }
    return render(
        request,
        "finance/pessoa_form.html",
        {
            "pessoa": pessoa,
            "empresas": empresas,
            "estados": estados,
            "todas_cidades": todas_cidades,
            "form": form,
            "tipo_choices": Pessoa.TIPOS,
            "tipo_cadastro_choices": Pessoa.TIPO_CADASTRO_CHOICES,
        },
    )


@login_required
def cadastros_pessoas_excluir(request, pessoa_id):
    empresas = _get_empresas_tenant(request)
    if empresas is None:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    pessoa = get_object_or_404(Pessoa, pk=pessoa_id, empresa__in=empresas)

    if not _usuario_tem_permissao_empresa(request, pessoa.empresa, "excluir"):
        messages.error(request, "Você não tem permissão para excluir registros desta empresa.")
        return redirect("cadastros-pessoas")
    if pessoa.contas_pagar.exists() or pessoa.contas_receber.exists():
        messages.error(
            request,
            "Não é possível excluir este cliente/fornecedor pois existem contas a pagar ou a receber vinculadas.",
        )
    else:
        pessoa.delete()
        messages.success(request, "Cliente/fornecedor excluído com sucesso.")
    return redirect("cadastros-pessoas")


@login_required
def cadastros_pessoas_verificar_excluir(request):
    """Retorna JSON: pode_excluir (permissão para excluir a pessoa)."""
    empresas = _get_empresas_tenant(request)
    if empresas is None:
        return JsonResponse({"pode_excluir": False})
    pessoa_id = (request.GET.get("pessoa_id") or "").strip()
    if not pessoa_id or not pessoa_id.isdigit():
        return JsonResponse({"pode_excluir": False})
    pessoa = Pessoa.objects.filter(pk=int(pessoa_id), empresa__in=empresas).select_related("empresa").first()
    if not pessoa:
        return JsonResponse({"pode_excluir": False})
    pode_excluir = _usuario_tem_permissao_empresa(request, pessoa.empresa, "excluir")
    return JsonResponse({"pode_excluir": pode_excluir})


# Cadastros (outros)


@login_required
def cadastros_centro_resultado(request):
    empresa_matriz = get_empresa_matriz(request)
    if not empresa_matriz:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    if not _usuario_tem_permissao_empresa(request, empresa_matriz, "visualizar"):
        messages.error(request, "Você não tem permissão para visualizar centros de resultado neste contexto.")
        return redirect("dashboard")

    # Mensagem vinda do botão Excluir (verificação de permissão)
    erro_excluir = (request.GET.get("erro_excluir") or "").strip()
    if erro_excluir == "permissao":
        messages.error(request, "Você não tem permissão para excluir centros de resultado desta empresa.")
    elif erro_excluir == "erro":
        messages.error(request, "Não foi possível verificar. Tente novamente.")

    qs = (
        CentroResultado.objects.filter(empresa=empresa_matriz)
        .select_related("empresa", "centro_pai")
        .order_by("empresa__razao_social", "codigo")
    )

    codigo = (request.GET.get("codigo") or "").strip()
    descricao = (request.GET.get("descricao") or "").strip()
    if codigo:
        # pesquisa por prefixo: equivalente a LIKE 'codigo%'
        qs = qs.filter(codigo__istartswith=codigo)
    if descricao:
        qs = qs.filter(descricao__icontains=descricao)

    context = {
        "centros": qs,
        "filtros": {"codigo": codigo, "descricao": descricao},
    }
    return render(request, "finance/centros_resultado_list.html", context)


@login_required
def cadastros_centro_resultado_novo(request):
    empresa_matriz = get_empresa_matriz(request)
    if not empresa_matriz:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    if not _usuario_tem_permissao_empresa(request, empresa_matriz, "incluir"):
        messages.error(
            request,
            "Você não tem permissão para incluir centros de resultado nesta empresa.",
        )
        return redirect("cadastros-centro-resultado")

    centros_disponiveis = (
        CentroResultado.objects.filter(
            empresa=empresa_matriz,
            tipo=CentroResultado.TIPO_SINTETICO,
        )
        .select_related("empresa")
        .order_by("codigo")
    )

    mascara = _get_mascara_centro_resultado(empresa_matriz)
    sugestao_root = _sugerir_codigo_centro_resultado(empresa_matriz, mascara, None)
    for c in centros_disponiveis:
        # atributo extra para o template usar como sugestão
        c.sugestao_codigo = _sugerir_codigo_centro_resultado(empresa_matriz, mascara, c)

    if request.method == "POST":
        codigo = (request.POST.get("codigo") or "").strip()
        descricao = (request.POST.get("descricao") or "").strip()
        centro_contabil = (request.POST.get("centro_contabil") or "").strip()
        tipo = (request.POST.get("tipo") or "").strip() or CentroResultado.TIPO_SINTETICO
        centro_pai_id = (request.POST.get("centro_pai_id") or "").strip()

        centro_pai = (
            centros_disponiveis.filter(pk=centro_pai_id).first()
            if centro_pai_id.isdigit()
            else None
        )

        # nível calculado automaticamente a partir do código (quantidade de segmentos)
        partes_nivel = [p for p in codigo.split(".") if p]
        nivel_calc = len(partes_nivel) if partes_nivel else 1

        if not codigo:
            messages.error(request, "Informe o código do centro de resultado.")
        elif mascara and not _codigo_respeita_mascara(codigo, mascara):
            messages.error(request, "Código fora do padrão da máscara de centro de resultado.")
        elif mascara and len(codigo) == len(mascara) and tipo != CentroResultado.TIPO_ANALITICO:
            messages.error(
                request,
                "Centros com código completo (todos os níveis da máscara) devem ser do tipo Analítico.",
            )
        elif not descricao:
            messages.error(request, "Informe a descrição do centro de resultado.")
        elif tipo == CentroResultado.TIPO_SINTETICO and centro_contabil:
            messages.error(request, "Centro contábil só pode ser preenchido para centros Analíticos.")
        else:
            # valida centro pai implícito pelo código
            partes = codigo.split(".")
            if len(partes) > 1:
                codigo_pai_implicito = ".".join(partes[:-1])
                if centro_pai:
                    if (centro_pai.codigo or "").strip() != codigo_pai_implicito:
                        messages.error(
                            request,
                            f"O código informado pertence ao pai {codigo_pai_implicito}, "
                            f"que é diferente do centro pai selecionado.",
                        )
                    else:
                        valido = True
                else:
                    pai_existente = CentroResultado.objects.filter(
                        empresa=empresa_matriz, codigo=codigo_pai_implicito
                    ).first()
                    if not pai_existente:
                        messages.error(
                            request,
                            f"O centro pai {codigo_pai_implicito} não existe. "
                            f"Crie-o primeiro ou selecione um centro pai válido.",
                        )
                    else:
                        centro_pai = pai_existente
                        valido = True
            else:
                valido = True

        if not any(True for _ in messages.get_messages(request)):
            # nenhuma mensagem de erro adicionada => segue criação
            CentroResultado.objects.create(
                empresa=empresa_matriz,
                codigo=codigo,
                descricao=descricao,
                centro_contabil=centro_contabil if tipo == CentroResultado.TIPO_ANALITICO else "",
                nivel=nivel_calc,
                tipo=tipo,
                centro_pai=centro_pai,
                created_by=request.user,
                updated_by=request.user,
            )
            messages.success(request, "Centro de resultado cadastrado com sucesso.")
            if request.POST.get("continuar"):
                return redirect(reverse("cadastros-centro-resultado-novo") + "?continuar=1")
            return redirect("cadastros-centro-resultado")

        form = {
            "codigo": codigo,
            "descricao": descricao,
            "centro_contabil": centro_contabil,
            "nivel": nivel_calc,
            "tipo": tipo,
            "centro_pai_id": int(centro_pai_id) if centro_pai_id.isdigit() else None,
        }
    else:
        form = {}

    return render(
        request,
        "finance/centro_resultado_form.html",
        {
            "empresa_matriz": empresa_matriz,
            "centros_disponiveis": centros_disponiveis,
            "form": form,
            "sugestao_root": sugestao_root,
            "mascara_codigo": mascara,
        },
    )


@login_required
def cadastros_centro_resultado_editar(request, centro_id):
    empresa_matriz = get_empresa_matriz(request)
    if not empresa_matriz:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    centro = get_object_or_404(CentroResultado, pk=centro_id, empresa=empresa_matriz)

    if not _usuario_tem_permissao_empresa(request, empresa_matriz, "editar"):
        messages.error(request, "Você não tem permissão para editar centros de resultado desta empresa.")
        return redirect("cadastros-centro-resultado")

    centros_disponiveis = (
        CentroResultado.objects.filter(
            empresa=empresa_matriz,
            tipo=CentroResultado.TIPO_SINTETICO,
        )
        .exclude(pk=centro.pk)
        .select_related("empresa")
        .order_by("codigo")
    )

    mascara = _get_mascara_centro_resultado(empresa_matriz)
    for c in centros_disponiveis:
        c.sugestao_codigo = _sugerir_codigo_centro_resultado(empresa_matriz, mascara, c)

    if request.method == "POST":
        codigo = (request.POST.get("codigo") or "").strip()
        descricao = (request.POST.get("descricao") or "").strip()
        centro_contabil = (request.POST.get("centro_contabil") or "").strip()
        tipo = (request.POST.get("tipo") or "").strip() or centro.tipo
        centro_pai_id = (request.POST.get("centro_pai_id") or "").strip()

        centro_pai = (
            centros_disponiveis.filter(pk=centro_pai_id).first()
            if centro_pai_id.isdigit()
            else None
        )

        # nível calculado automaticamente a partir do código
        partes_nivel = [p for p in codigo.split(".") if p]
        nivel_calc = len(partes_nivel) if partes_nivel else 1

        if not codigo:
            messages.error(request, "Informe o código do centro de resultado.")
        elif mascara and not _codigo_respeita_mascara(codigo, mascara):
            messages.error(request, "Código fora do padrão da máscara de centro de resultado.")
        elif mascara and len(codigo) == len(mascara) and tipo != CentroResultado.TIPO_ANALITICO:
            messages.error(
                request,
                "Centros com código completo (todos os níveis da máscara) devem ser do tipo Analítico.",
            )
        elif not descricao:
            messages.error(request, "Informe a descrição do centro de resultado.")
        elif tipo == CentroResultado.TIPO_SINTETICO and centro_contabil:
            messages.error(request, "Centro contábil só pode ser preenchido para centros Analíticos.")
        else:
            centro.empresa = empresa_matriz
            centro.codigo = codigo
            centro.descricao = descricao
            centro.centro_contabil = centro_contabil if tipo == CentroResultado.TIPO_ANALITICO else ""
            centro.nivel = nivel_calc
            centro.tipo = tipo
            centro.centro_pai = centro_pai
            centro.updated_by = request.user
            centro.save()
            messages.success(request, "Centro de resultado atualizado com sucesso.")
            return redirect("cadastros-centro-resultado")

        form = {
            "codigo": codigo,
            "descricao": descricao,
            "centro_contabil": centro_contabil,
            "nivel": nivel_calc,
            "tipo": tipo,
            "centro_pai_id": int(centro_pai_id) if centro_pai_id.isdigit() else centro.centro_pai_id,
        }
    else:
        form = {
            "codigo": centro.codigo,
            "descricao": centro.descricao,
            "centro_contabil": centro.centro_contabil,
            "nivel": centro.nivel,
            "tipo": centro.tipo,
            "centro_pai_id": centro.centro_pai_id,
        }

    return render(
        request,
        "finance/centro_resultado_form.html",
        {
            "empresa_matriz": empresa_matriz,
            "centros_disponiveis": centros_disponiveis,
            "form": form,
            "centro": centro,
            "mascara_codigo": mascara,
        },
    )


@login_required
def cadastros_centro_resultado_excluir(request, centro_id):
    empresas = _get_empresas_tenant(request)
    if empresas is None:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    centro = get_object_or_404(CentroResultado, pk=centro_id, empresa__in=empresas)

    if not _usuario_tem_permissao_empresa(request, centro.empresa, "excluir"):
        messages.error(request, "Você não tem permissão para excluir centros de resultado desta empresa.")
        return redirect("cadastros-centro-resultado")

    if centro.filhos.exists():
        messages.error(
            request,
            "Não é possível excluir este centro de resultado pois existem centros filhos vinculados.",
        )
    else:
        centro.delete()
        messages.success(request, "Centro de resultado excluído com sucesso.")
    return redirect("cadastros-centro-resultado")


@login_required
def cadastros_centro_resultado_verificar_excluir(request):
    """Retorna JSON: pode_excluir (permissão para excluir o centro de resultado)."""
    empresas = _get_empresas_tenant(request)
    if empresas is None:
        return JsonResponse({"pode_excluir": False})
    centro_id = (request.GET.get("centro_id") or "").strip()
    if not centro_id or not centro_id.isdigit():
        return JsonResponse({"pode_excluir": False})
    centro = (
        CentroResultado.objects.filter(pk=int(centro_id), empresa__in=empresas)
        .select_related("empresa")
        .first()
    )
    if not centro:
        return JsonResponse({"pode_excluir": False})
    pode_excluir = _usuario_tem_permissao_empresa(request, centro.empresa, "excluir")
    return JsonResponse({"pode_excluir": pode_excluir})


@login_required
def cadastros_plano_conta(request):
    empresa_matriz = get_empresa_matriz(request)
    if not empresa_matriz:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    if not _usuario_tem_permissao_empresa(request, empresa_matriz, "visualizar"):
        messages.error(request, "Você não tem permissão para visualizar plano de conta nesta empresa.")
        return redirect("dashboard")

    # Mensagem vinda do botão Excluir (verificação de permissão)
    erro_excluir = (request.GET.get("erro_excluir") or "").strip()
    if erro_excluir == "permissao":
        messages.error(request, "Você não tem permissão para excluir contas deste plano para esta empresa.")
    elif erro_excluir == "erro":
        messages.error(request, "Não foi possível verificar. Tente novamente.")

    qs = (
        PlanoConta.objects.filter(empresa=empresa_matriz)
        .select_related("conta_pai")
        .order_by("codigo")
    )

    codigo = (request.GET.get("codigo") or "").strip()
    descricao = (request.GET.get("descricao") or "").strip()
    if codigo:
        qs = qs.filter(codigo__istartswith=codigo)
    if descricao:
        qs = qs.filter(descricao__icontains=descricao)

    context = {
        "planos": qs,
        "filtros": {"codigo": codigo, "descricao": descricao},
    }
    return render(request, "finance/planos_conta_list.html", context)


@login_required
def cadastros_plano_conta_novo(request):
    empresa_matriz = get_empresa_matriz(request)
    if not empresa_matriz:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    if not _usuario_tem_permissao_empresa(request, empresa_matriz, "incluir"):
        messages.error(request, "Você não tem permissão para incluir contas neste plano para esta empresa.")
        return redirect("cadastros-plano-conta")

    contas_disponiveis = (
        PlanoConta.objects.filter(empresa=empresa_matriz, tipo=PlanoConta.TIPO_SINTETICO)
        .select_related("conta_pai")
        .order_by("codigo")
    )

    mascara = _get_mascara_plano_conta(empresa_matriz)
    sugestao_root = _sugerir_codigo_plano_conta(empresa_matriz, mascara, None)
    for c in contas_disponiveis:
        c.sugestao_codigo = _sugerir_codigo_plano_conta(empresa_matriz, mascara, c)

    if request.method == "POST":
        codigo = (request.POST.get("codigo") or "").strip()
        descricao = (request.POST.get("descricao") or "").strip()
        tipo = (request.POST.get("tipo") or "").strip() or PlanoConta.TIPO_ANALITICO
        natureza = (request.POST.get("natureza") or "").strip() or PlanoConta.NATUREZA_DESPESA
        conta_contabil = (request.POST.get("conta_contabil") or "").strip()
        conta_pai_id = (request.POST.get("conta_pai_id") or "").strip()

        conta_pai = (
            contas_disponiveis.filter(pk=conta_pai_id).first()
            if conta_pai_id.isdigit()
            else None
        )

        # nível calculado automaticamente a partir do código
        partes_nivel = [p for p in codigo.split(".") if p]
        nivel_calc = len(partes_nivel) if partes_nivel else 1

        if tipo not in (PlanoConta.TIPO_SINTETICO, PlanoConta.TIPO_ANALITICO):
            messages.error(request, "Tipo inválido. Use Sintético ou Analítico.")
        elif natureza not in (PlanoConta.NATUREZA_RECEITA, PlanoConta.NATUREZA_DESPESA):
            messages.error(request, "Natureza inválida. Use Receita ou Despesa.")
        elif mascara and not _codigo_respeita_mascara(codigo, mascara):
            messages.error(request, "Código fora do padrão da máscara do plano de contas.")
        elif mascara and len(codigo) == len(mascara) and tipo != PlanoConta.TIPO_ANALITICO:
            messages.error(
                request,
                "Contas com código completo (todos os níveis da máscara) devem ser do tipo Analítico.",
            )
        elif not codigo:
            messages.error(request, "Informe o código da conta.")
        elif PlanoConta.objects.filter(empresa=empresa_matriz, codigo=codigo).exists():
            messages.error(request, "Já existe uma conta com este código para esta empresa.")
        elif not descricao:
            messages.error(request, "Informe a descrição da conta.")
        elif tipo == PlanoConta.TIPO_SINTETICO and conta_contabil:
            messages.error(request, "Conta contábil só pode ser preenchida para contas Analíticas.")
        else:
            PlanoConta.objects.create(
                empresa=empresa_matriz,
                codigo=codigo,
                descricao=descricao,
                conta_pai=conta_pai,
                tipo=tipo,
                conta_contabil=conta_contabil if tipo == PlanoConta.TIPO_ANALITICO else "",
                natureza=natureza,
                nivel=nivel_calc,
                created_by=request.user,
                updated_by=request.user,
            )
            messages.success(request, "Conta do plano de contas criada com sucesso.")
            if request.POST.get("continuar"):
                return redirect(reverse("cadastros-plano-conta-novo") + "?continuar=1")
            return redirect("cadastros-plano-conta")

        form = {
            "codigo": codigo,
            "descricao": descricao,
            "tipo": tipo,
            "conta_contabil": conta_contabil,
            "natureza": natureza,
            "nivel": nivel_calc,
            "conta_pai_id": int(conta_pai_id) if conta_pai_id.isdigit() else None,
        }
    else:
        form = {}

    return render(
        request,
        "finance/plano_conta_form.html",
        {
            "contas_disponiveis": contas_disponiveis,
            "form": form,
            "mascara_codigo": mascara,
            "sugestao_root": sugestao_root,
        },
    )


@login_required
def cadastros_plano_conta_editar(request, plano_id):
    empresa_matriz = get_empresa_matriz(request)
    if not empresa_matriz:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    conta = get_object_or_404(PlanoConta, pk=plano_id, empresa=empresa_matriz)

    if not _usuario_tem_permissao_empresa(request, empresa_matriz, "editar"):
        messages.error(request, "Você não tem permissão para editar contas deste plano para esta empresa.")
        return redirect("cadastros-plano-conta")

    contas_disponiveis = (
        PlanoConta.objects.filter(empresa=empresa_matriz, tipo=PlanoConta.TIPO_SINTETICO)
        .exclude(pk=conta.pk)
        .select_related("conta_pai")
        .order_by("codigo")
    )

    mascara = _get_mascara_plano_conta(empresa_matriz)
    sugestao_root = _sugerir_codigo_plano_conta(empresa_matriz, mascara, None)
    for c in contas_disponiveis:
        c.sugestao_codigo = _sugerir_codigo_plano_conta(empresa_matriz, mascara, c)

    if request.method == "POST":
        codigo = (request.POST.get("codigo") or "").strip()
        descricao = (request.POST.get("descricao") or "").strip()
        tipo = (request.POST.get("tipo") or "").strip() or conta.tipo
        natureza = (request.POST.get("natureza") or "").strip() or conta.natureza
        conta_contabil = (request.POST.get("conta_contabil") or "").strip()
        conta_pai_id = (request.POST.get("conta_pai_id") or "").strip()

        conta_pai = (
            contas_disponiveis.filter(pk=conta_pai_id).first()
            if conta_pai_id.isdigit()
            else None
        )

        # nível calculado automaticamente a partir do código
        partes_nivel = [p for p in codigo.split(".") if p]
        nivel_calc = len(partes_nivel) if partes_nivel else 1

        if tipo not in (PlanoConta.TIPO_SINTETICO, PlanoConta.TIPO_ANALITICO):
            messages.error(request, "Tipo inválido. Use Sintético ou Analítico.")
        elif natureza not in (PlanoConta.NATUREZA_RECEITA, PlanoConta.NATUREZA_DESPESA):
            messages.error(request, "Natureza inválida. Use Receita ou Despesa.")
        elif mascara and not _codigo_respeita_mascara(codigo, mascara):
            messages.error(request, "Código fora do padrão da máscara do plano de contas.")
        elif mascara and len(codigo) == len(mascara) and tipo != PlanoConta.TIPO_ANALITICO:
            messages.error(
                request,
                "Contas com código completo (todos os níveis da máscara) devem ser do tipo Analítico.",
            )
        elif not codigo:
            messages.error(request, "Informe o código da conta.")
        elif (
            PlanoConta.objects.filter(empresa=empresa_matriz, codigo=codigo)
            .exclude(pk=conta.pk)
            .exists()
        ):
            messages.error(request, "Já existe outra conta com este código para esta empresa.")
        elif not descricao:
            messages.error(request, "Informe a descrição da conta.")
        elif tipo == PlanoConta.TIPO_SINTETICO and conta_contabil:
            messages.error(request, "Conta contábil só pode ser preenchida para contas Analíticas.")
        else:
            conta.codigo = codigo
            conta.descricao = descricao
            conta.tipo = tipo
            conta.conta_contabil = conta_contabil if tipo == PlanoConta.TIPO_ANALITICO else ""
            conta.natureza = natureza
            conta.nivel = nivel_calc
            conta.conta_pai = conta_pai
            conta.updated_by = request.user
            conta.save()
            messages.success(request, "Conta do plano de contas atualizada com sucesso.")
            return redirect("cadastros-plano-conta")

        form = {
            "codigo": codigo,
            "descricao": descricao,
            "tipo": tipo,
            "conta_contabil": conta_contabil,
            "natureza": natureza,
            "nivel": nivel_calc,
            "conta_pai_id": int(conta_pai_id) if conta_pai_id.isdigit() else conta.conta_pai_id,
        }
    else:
        form = {
            "codigo": conta.codigo,
            "descricao": conta.descricao,
            "tipo": conta.tipo,
            "conta_contabil": conta.conta_contabil,
            "natureza": conta.natureza,
            "nivel": conta.nivel,
            "conta_pai_id": conta.conta_pai_id,
        }

    return render(
        request,
        "finance/plano_conta_form.html",
        {
            "contas_disponiveis": contas_disponiveis,
            "form": form,
            "conta": conta,
            "mascara_codigo": mascara,
            "sugestao_root": sugestao_root,
        },
    )


@login_required
def cadastros_plano_conta_excluir(request, plano_id):
    empresa_matriz = get_empresa_matriz(request)
    if not empresa_matriz:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    conta = get_object_or_404(PlanoConta, pk=plano_id, empresa=empresa_matriz)

    if not _usuario_tem_permissao_empresa(request, empresa_matriz, "excluir"):
        messages.error(request, "Você não tem permissão para excluir contas deste plano para esta empresa.")
        return redirect("cadastros-plano-conta")

    if conta.filhos.exists():
        messages.error(
            request,
            "Não é possível excluir esta conta, pois existem contas filhas vinculadas.",
        )
    else:
        try:
            conta.delete()
            messages.success(request, "Conta do plano de contas excluída com sucesso.")
        except ProtectedError:
            messages.error(
                request,
                "Não é possível excluir esta conta pois existem lançamentos vinculados.",
            )
    return redirect("cadastros-plano-conta")


@login_required
def cadastros_plano_conta_verificar_excluir(request):
    """Retorna JSON: pode_excluir (permissão para excluir a conta do plano)."""
    empresa_matriz = get_empresa_matriz(request)
    if not empresa_matriz:
        return JsonResponse({"pode_excluir": False})
    plano_id = (request.GET.get("plano_id") or "").strip()
    if not plano_id or not plano_id.isdigit():
        return JsonResponse({"pode_excluir": False})
    conta = (
        PlanoConta.objects.filter(pk=int(plano_id), empresa=empresa_matriz)
        .select_related("empresa")
        .first()
    )
    if not conta:
        return JsonResponse({"pode_excluir": False})
    pode_excluir = _usuario_tem_permissao_empresa(request, empresa_matriz, "excluir")
    return JsonResponse({"pode_excluir": pode_excluir})


@login_required
def cadastros_bancos(request):
    empresas = _get_empresas_tenant(request)
    if empresas is None:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    # Permissão de visualização em pelo menos uma empresa do contexto
    if not _usuario_tem_permissao_alguma_empresa(request, empresas, "visualizar"):
        messages.error(request, "Você não tem permissão para visualizar bancos neste contexto.")
        return redirect("dashboard")

    # Mensagem vinda do botão Excluir (verificação de permissão)
    erro_excluir = (request.GET.get("erro_excluir") or "").strip()
    if erro_excluir == "permissao":
        messages.error(request, "Você não tem permissão para excluir bancos neste contexto.")
    elif erro_excluir == "erro":
        messages.error(request, "Não foi possível verificar. Tente novamente.")

    qs = Banco.objects.all().order_by("codigo", "nome")

    codigo = (request.GET.get("codigo") or "").strip()
    nome = (request.GET.get("nome") or "").strip()
    if codigo:
        qs = qs.filter(codigo__icontains=codigo)
    if nome:
        qs = qs.filter(nome__icontains=nome)

    context = {
        "bancos": qs,
        "filtros": {"codigo": codigo, "nome": nome},
    }
    return render(request, "finance/bancos_list.html", context)


@login_required
def cadastros_bancos_novo(request):
    empresas = _get_empresas_tenant(request)
    if empresas is None:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    if not _usuario_tem_permissao_alguma_empresa(request, empresas, "incluir"):
        messages.error(request, "Você não tem permissão para incluir bancos neste contexto.")
        return redirect("cadastros-bancos")

    if request.method == "POST":
        codigo = (request.POST.get("codigo") or "").strip()
        nome = (request.POST.get("nome") or "").strip()

        if not codigo:
            messages.error(request, "Informe o código do banco.")
        elif not nome:
            messages.error(request, "Informe o nome do banco.")
        elif Banco.objects.filter(codigo__iexact=codigo).exists():
            messages.error(request, "Já existe um banco com este código.")
        else:
            Banco.objects.create(
                codigo=codigo,
                nome=nome,
                created_by=request.user,
                updated_by=request.user,
            )
            messages.success(request, "Banco cadastrado com sucesso.")
            return redirect("cadastros-bancos")

        return render(
            request,
            "finance/banco_form.html",
            {"form": {"codigo": codigo, "nome": nome}},
        )

    return render(
        request,
        "finance/banco_form.html",
        {"form": {}},
    )


@login_required
def cadastros_bancos_editar(request, banco_id):
    empresas = _get_empresas_tenant(request)
    if empresas is None:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    if not _usuario_tem_permissao_alguma_empresa(request, empresas, "editar"):
        messages.error(request, "Você não tem permissão para editar bancos neste contexto.")
        return redirect("cadastros-bancos")

    banco = get_object_or_404(Banco, pk=banco_id)

    if request.method == "POST":
        codigo = (request.POST.get("codigo") or "").strip()
        nome = (request.POST.get("nome") or "").strip()

        if not codigo:
            messages.error(request, "Informe o código do banco.")
        elif not nome:
            messages.error(request, "Informe o nome do banco.")
        elif Banco.objects.filter(codigo__iexact=codigo).exclude(pk=banco.pk).exists():
            messages.error(request, "Já existe outro banco com este código.")
        else:
            banco.codigo = codigo
            banco.nome = nome
            banco.updated_by = request.user
            banco.save()
            messages.success(request, "Banco atualizado com sucesso.")
            return redirect("cadastros-bancos")

        form = {"codigo": codigo, "nome": nome}
    else:
        form = {"codigo": banco.codigo, "nome": banco.nome}

    return render(
        request,
        "finance/banco_form.html",
        {
            "form": form,
            "banco": banco,
        },
    )


@login_required
def cadastros_bancos_excluir(request, banco_id):
    empresas = _get_empresas_tenant(request)
    if empresas is None:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    if not _usuario_tem_permissao_alguma_empresa(request, empresas, "excluir"):
        messages.error(request, "Você não tem permissão para excluir bancos neste contexto.")
        return redirect("cadastros-bancos")

    banco = get_object_or_404(Banco, pk=banco_id)
    try:
        banco.delete()
        messages.success(request, "Banco excluído com sucesso.")
    except ProtectedError:
        messages.error(
            request,
            "Não é possível excluir este banco pois existem contas financeiras vinculadas.",
        )
    return redirect("cadastros-bancos")


@login_required
def cadastros_bancos_verificar_excluir(request):
    """Retorna JSON: pode_excluir (permissão para excluir o banco)."""
    empresas = _get_empresas_tenant(request)
    if empresas is None:
        return JsonResponse({"pode_excluir": False})
    banco_id = (request.GET.get("banco_id") or "").strip()
    if not banco_id or not banco_id.isdigit():
        return JsonResponse({"pode_excluir": False})
    banco = Banco.objects.filter(pk=int(banco_id)).first()
    if not banco:
        return JsonResponse({"pode_excluir": False})
    pode_excluir = _usuario_tem_permissao_alguma_empresa(request, empresas, "excluir")
    return JsonResponse({"pode_excluir": pode_excluir})


@login_required
def cadastros_tipos_documento(request):
    empresa_matriz = get_empresa_matriz(request)
    if not empresa_matriz:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    if not _usuario_tem_permissao_empresa(request, empresa_matriz, "visualizar"):
        messages.error(request, "Você não tem permissão para visualizar tipos de documento nesta empresa.")
        return redirect("dashboard")

    # Mensagem vinda do botão Excluir (verificação de permissão)
    erro_excluir = (request.GET.get("erro_excluir") or "").strip()
    if erro_excluir == "permissao":
        messages.error(request, "Você não tem permissão para excluir tipos de documento nesta empresa.")
    elif erro_excluir == "erro":
        messages.error(request, "Não foi possível verificar. Tente novamente.")

    qs = (
        TipoDocumento.objects.filter(empresa=empresa_matriz)
        .select_related("empresa")
        .order_by("nome")
    )

    nome = (request.GET.get("nome") or "").strip()
    if nome:
        qs = qs.filter(nome__icontains=nome)

    context = {
        "tipos": qs,
        "filtros": {"nome": nome},
    }
    return render(request, "finance/tipos_documento_list.html", context)


@login_required
def cadastros_tipos_documento_novo(request):
    empresa_matriz = get_empresa_matriz(request)
    if not empresa_matriz:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    if not _usuario_tem_permissao_empresa(request, empresa_matriz, "incluir"):
        messages.error(request, "Você não tem permissão para incluir tipos de documento nesta empresa.")
        return redirect("cadastros-tipos-documento")

    if request.method == "POST":
        nome = (request.POST.get("nome") or "").strip()

        if not nome:
            messages.error(request, "Informe o nome do tipo de documento.")
        elif TipoDocumento.objects.filter(empresa=empresa_matriz, nome__iexact=nome).exists():
            messages.error(request, "Já existe um tipo de documento com este nome para esta empresa.")
        else:
            TipoDocumento.objects.create(
                empresa=empresa_matriz,
                nome=nome,
                created_by=request.user,
                updated_by=request.user,
            )
            messages.success(request, "Tipo de documento cadastrado com sucesso.")
            return redirect("cadastros-tipos-documento")

        return render(
            request,
            "finance/tipo_documento_form.html",
            {
                "form": {"nome": nome},
            },
        )

    return render(
        request,
        "finance/tipo_documento_form.html",
        {
            "form": {},
        },
    )


@login_required
def cadastros_tipos_documento_editar(request, tipo_id):
    empresa_matriz = get_empresa_matriz(request)
    if not empresa_matriz:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    tipo = get_object_or_404(TipoDocumento, pk=tipo_id, empresa=empresa_matriz)

    if not _usuario_tem_permissao_empresa(request, empresa_matriz, "editar"):
        messages.error(request, "Você não tem permissão para editar tipos de documento nesta empresa.")
        return redirect("cadastros-tipos-documento")

    if request.method == "POST":
        nome = (request.POST.get("nome") or "").strip()

        if not nome:
            messages.error(request, "Informe o nome do tipo de documento.")
        elif TipoDocumento.objects.filter(empresa=tipo.empresa, nome__iexact=nome).exclude(pk=tipo.pk).exists():
            messages.error(request, "Já existe outro tipo de documento com este nome para esta empresa.")
        else:
            tipo.nome = nome
            tipo.updated_by = request.user
            tipo.save()
            messages.success(request, "Tipo de documento atualizado com sucesso.")
            return redirect("cadastros-tipos-documento")

        form = {"nome": nome}
    else:
        form = {"nome": tipo.nome}

    return render(
        request,
        "finance/tipo_documento_form.html",
        {
            "form": form,
            "tipo": tipo,
        },
    )


@login_required
def cadastros_tipos_documento_excluir(request, tipo_id):
    empresa_matriz = get_empresa_matriz(request)
    if not empresa_matriz:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    tipo = get_object_or_404(TipoDocumento, pk=tipo_id, empresa=empresa_matriz)

    if not _usuario_tem_permissao_empresa(request, empresa_matriz, "excluir"):
        messages.error(request, "Você não tem permissão para excluir tipos de documento nesta empresa.")
        return redirect("cadastros-tipos-documento")

    try:
        tipo.delete()
        messages.success(request, "Tipo de documento excluído com sucesso.")
    except ProtectedError:
        messages.error(
            request,
            "Não é possível excluir este tipo de documento pois existem contas a pagar ou a receber vinculadas.",
        )
    return redirect("cadastros-tipos-documento")


@login_required
def cadastros_tipos_documento_verificar_excluir(request):
    """Retorna JSON: pode_excluir (permissão para excluir o tipo de documento)."""
    empresa_matriz = get_empresa_matriz(request)
    if not empresa_matriz:
        return JsonResponse({"pode_excluir": False})
    tipo_id = (request.GET.get("tipo_id") or "").strip()
    if not tipo_id or not tipo_id.isdigit():
        return JsonResponse({"pode_excluir": False})
    tipo = (
        TipoDocumento.objects.filter(pk=int(tipo_id), empresa=empresa_matriz)
        .select_related("empresa")
        .first()
    )
    if not tipo:
        return JsonResponse({"pode_excluir": False})
    pode_excluir = _usuario_tem_permissao_empresa(request, empresa_matriz, "excluir")
    return JsonResponse({"pode_excluir": pode_excluir})


@login_required
def cadastros_contas_financeiras(request):
    empresas = _get_empresas_tenant(request)
    if empresas is None:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    # Somente usuários com permissão de visualizar em alguma empresa do contexto
    if not _usuario_tem_permissao_alguma_empresa(request, empresas, "visualizar"):
        messages.error(request, "Você não tem permissão para visualizar contas financeiras neste contexto.")
        return redirect("dashboard")

    # Mensagem vinda do botão Excluir (verificação de permissão)
    erro_excluir = (request.GET.get("erro_excluir") or "").strip()
    if erro_excluir == "permissao":
        messages.error(request, "Você não tem permissão para excluir contas desta empresa.")
    elif erro_excluir == "erro":
        messages.error(request, "Não foi possível verificar. Tente novamente.")

    qs = (
        ContaFinanceira.objects.filter(empresa__in=empresas)
        .select_related("empresa", "banco")
        .order_by("empresa__razao_social", "banco__codigo", "agencia", "conta")
    )

    banco_id = (request.GET.get("banco_id") or "").strip()
    agencia = (request.GET.get("agencia") or "").strip()
    conta = (request.GET.get("conta") or "").strip()

    if banco_id and banco_id.isdigit():
        qs = qs.filter(banco_id=int(banco_id))
    if agencia:
        qs = qs.filter(agencia__icontains=agencia)
    if conta:
        qs = qs.filter(conta__icontains=conta)

    context = {
        "contas": qs,
        "empresas": empresas,
        "bancos": Banco.objects.all().order_by("codigo", "nome"),
        "filtros": {
            "banco_id": int(banco_id) if banco_id.isdigit() else None,
            "agencia": agencia,
            "conta": conta,
        },
    }
    return render(request, "finance/contas_financeiras_list.html", context)


@login_required
def cadastros_contas_financeiras_novo(request):
    empresas_contexto = _get_empresas_tenant(request)
    if empresas_contexto is None:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    # Empresas nas quais o usuário pode incluir contas financeiras
    empresas = [e for e in empresas_contexto if _usuario_tem_permissao_empresa(request, e, "incluir")]
    if not empresas:
        messages.error(request, "Você não tem permissão para incluir contas financeiras em nenhuma empresa deste contexto.")
        return redirect("cadastros-contas-financeiras")

    bancos = Banco.objects.all().order_by("codigo", "nome")

    if request.method == "POST":
        empresa_id = (request.POST.get("empresa_id") or "").strip()
        banco_id = (request.POST.get("banco_id") or "").strip()
        agencia = (request.POST.get("agencia") or "").strip()
        conta = (request.POST.get("conta") or "").strip()
        saldo_inicial_raw = (request.POST.get("saldo_inicial") or "").strip().replace(" ", "")

        empresa = next((e for e in empresas if str(e.pk) == empresa_id), None) if empresa_id.isdigit() else None
        banco = bancos.filter(pk=banco_id).first() if banco_id.isdigit() else None

        if not empresa:
            messages.error(request, "Selecione uma empresa válida.")
        elif not banco:
            messages.error(request, "Selecione o banco.")
        elif not agencia:
            messages.error(request, "Informe a agência.")
        elif not conta:
            messages.error(request, "Informe a conta.")
        else:
            from decimal import Decimal, InvalidOperation

            try:
                if not saldo_inicial_raw:
                    valor_saldo = Decimal("0")
                elif "," in saldo_inicial_raw:
                    # Formato brasileiro: 1.234,56
                    normalizado = saldo_inicial_raw.replace(".", "").replace(",", ".")
                    valor_saldo = Decimal(normalizado)
                else:
                    # Sem vírgula: tratar ponto como separador decimal (1000.50)
                    valor_saldo = Decimal(saldo_inicial_raw)
            except InvalidOperation:
                messages.error(request, "Saldo inicial inválido.")
                return render(
                    request,
                    "finance/conta_financeira_form.html",
                    {
                        "empresas": empresas,
                        "bancos": bancos,
                        "form": {
                            "empresa_id": int(empresa_id) if empresa_id.isdigit() else None,
                            "banco_id": int(banco_id) if banco_id.isdigit() else None,
                            "agencia": agencia,
                            "conta": conta,
                            "saldo_inicial": saldo_inicial_raw,
                        },
                    },
                )

            ContaFinanceira.objects.create(
                empresa=empresa,
                banco=banco,
                agencia=agencia,
                conta=conta,
                saldo_inicial=valor_saldo,
                created_by=request.user,
                updated_by=request.user,
            )
            messages.success(request, "Conta financeira cadastrada com sucesso.")
            return redirect("cadastros-contas-financeiras")

        return render(
            request,
            "finance/conta_financeira_form.html",
            {
                "empresas": empresas,
                "bancos": bancos,
                "form": {
                    "empresa_id": int(empresa_id) if empresa_id.isdigit() else None,
                    "banco_id": int(banco_id) if banco_id.isdigit() else None,
                    "agencia": agencia,
                    "conta": conta,
                    "saldo_inicial": saldo_inicial_raw,
                },
            },
        )

    # Empresa padrão no formulário: primeira do contexto (matriz atual ou primeira filial)
    empresa_padrao = empresas[0] if empresas else None
    form_inicial = {"empresa_id": empresa_padrao.id} if empresa_padrao else {}

    return render(
        request,
        "finance/conta_financeira_form.html",
        {
            "empresas": empresas,
            "bancos": bancos,
            "form": form_inicial,
        },
    )


@login_required
def cadastros_contas_financeiras_editar(request, conta_id):
    empresas_contexto = _get_empresas_tenant(request)
    if empresas_contexto is None:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    conta = get_object_or_404(ContaFinanceira, pk=conta_id, empresa__in=empresas_contexto)

    if not _usuario_tem_permissao_empresa(request, conta.empresa, "editar"):
        messages.error(request, "Você não tem permissão para editar contas desta empresa.")
        return redirect("cadastros-contas-financeiras")

    # Empresas nas quais o usuário pode editar contas financeiras
    empresas = [e for e in empresas_contexto if _usuario_tem_permissao_empresa(request, e, "editar")]

    bancos = Banco.objects.all().order_by("codigo", "nome")

    if request.method == "POST":
        empresa_id = (request.POST.get("empresa_id") or "").strip()
        banco_id = (request.POST.get("banco_id") or "").strip()
        agencia = (request.POST.get("agencia") or "").strip()
        conta_numero = (request.POST.get("conta") or "").strip()
        saldo_inicial_raw = (request.POST.get("saldo_inicial") or "").strip().replace(" ", "")

        empresa_nova = next((e for e in empresas if str(e.pk) == empresa_id), None) if empresa_id.isdigit() else None
        banco = bancos.filter(pk=banco_id).first() if banco_id.isdigit() else None

        if not empresa_nova:
            messages.error(request, "Selecione uma empresa válida.")
        elif not banco:
            messages.error(request, "Selecione o banco.")
        elif not agencia:
            messages.error(request, "Informe a agência.")
        elif not conta_numero:
            messages.error(request, "Informe a conta.")
        else:
            from decimal import Decimal, InvalidOperation

            try:
                if not saldo_inicial_raw:
                    valor_saldo = conta.saldo_inicial
                elif "," in saldo_inicial_raw:
                    normalizado = saldo_inicial_raw.replace(".", "").replace(",", ".")
                    valor_saldo = Decimal(normalizado)
                else:
                    valor_saldo = Decimal(saldo_inicial_raw)
            except InvalidOperation:
                messages.error(request, "Saldo inicial inválido.")
            else:
                conta.empresa = empresa_nova
                conta.banco = banco
                conta.agencia = agencia
                conta.conta = conta_numero
                conta.saldo_inicial = valor_saldo
                conta.updated_by = request.user
                conta.save()
                messages.success(request, "Conta financeira atualizada com sucesso.")
                return redirect("cadastros-contas-financeiras")

        form = {
            "empresa_id": int(empresa_id) if empresa_id.isdigit() else conta.empresa_id,
            "banco_id": int(banco_id) if banco_id.isdigit() else conta.banco_id,
            "agencia": agencia,
            "conta": conta_numero,
            "saldo_inicial": saldo_inicial_raw,
        }
    else:
        form = {
            "empresa_id": conta.empresa_id,
            "banco_id": conta.banco_id,
            "agencia": conta.agencia,
            "conta": conta.conta,
            "saldo_inicial": f"{conta.saldo_inicial:.2f}",
        }

    return render(
        request,
        "finance/conta_financeira_form.html",
        {
            "empresas": empresas,
            "bancos": bancos,
            "form": form,
            "conta": conta,
        },
    )


@login_required
def cadastros_contas_financeiras_excluir(request, conta_id):
    empresas = _get_empresas_tenant(request)
    if empresas is None:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    conta = get_object_or_404(ContaFinanceira, pk=conta_id, empresa__in=empresas)

    if not _usuario_tem_permissao_empresa(request, conta.empresa, "excluir"):
        messages.error(request, "Você não tem permissão para excluir contas desta empresa.")
        return redirect("cadastros-contas-financeiras")

    try:
        conta.delete()
        messages.success(request, "Conta financeira excluída com sucesso.")
    except ProtectedError:
        messages.error(
            request,
            "Não é possível excluir esta conta financeira pois existem lançamentos vinculados.",
        )
    return redirect("cadastros-contas-financeiras")


@login_required
def cadastros_contas_financeiras_verificar_excluir(request):
    """Retorna JSON: pode_excluir (permissão para excluir a conta financeira)."""
    empresas = _get_empresas_tenant(request)
    if empresas is None:
        return JsonResponse({"pode_excluir": False})
    conta_id = (request.GET.get("conta_id") or "").strip()
    if not conta_id or not conta_id.isdigit():
        return JsonResponse({"pode_excluir": False})
    conta = (
        ContaFinanceira.objects.filter(pk=int(conta_id), empresa__in=empresas)
        .select_related("empresa", "banco")
        .first()
    )
    if not conta:
        return JsonResponse({"pode_excluir": False})
    pode_excluir = _usuario_tem_permissao_empresa(request, conta.empresa, "excluir")
    return JsonResponse({"pode_excluir": pode_excluir})


# Movimentos - Contas a Pagar
def _parse_decimal_post(raw_value):
    """Converte valor do POST (BR ou US) para Decimal."""
    if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
        return None
    from decimal import Decimal, InvalidOperation
    s = (raw_value or "").strip().replace(" ", "")
    # Trata formatos brasileiros (1.000,00 ou 500,00) e internacionais (1000.00)
    if "," in s and "." in s:
        # Assume ponto como separador de milhar e vírgula como decimal
        s = s.replace(".", "").replace(",", ".")
    elif "," in s and "." not in s:
        # Apenas vírgula: trata como separador decimal
        s = s.replace(",", ".")
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _parse_rateios_post(request, empresas_choices):
    """Extrai lista de rateios do POST para reexibir no formulário (empresa_id, centro_id, plano_id, valor)."""
    rateio_empresas = request.POST.getlist("rateio_empresa_id")
    rateio_centros = request.POST.getlist("rateio_centro_id")
    rateio_planos = request.POST.getlist("rateio_plano_id")
    rateio_valores = request.POST.getlist("rateio_valor")
    out = []
    for emp_id, centro_id, plano_id, val in zip(
        rateio_empresas, rateio_centros, rateio_planos, rateio_valores
    ):
        if _parse_decimal_post(val) and _parse_decimal_post(val) > 0:
            out.append({
                "empresa_id": (emp_id or "").strip(),
                "centro_id": (centro_id or "").strip(),
                "plano_id": (plano_id or "").strip(),
                "valor": (val or "").strip(),
            })
    return out


@login_required
def movimentos_contas_a_pagar(request):
    empresas = get_empresas_contexto(request)
    if not empresas:
        messages.warning(request, "Selecione uma empresa no menu para acessar Contas a Pagar.")
        return redirect("dashboard")

    if not _usuario_tem_permissao_alguma_empresa(request, empresas, "visualizar"):
        messages.error(request, "Você não tem permissão para visualizar contas a pagar.")
        return redirect("dashboard")

    qs = (
        ContaPagar.objects.filter(empresa__in=empresas)
        .select_related("empresa", "pessoa")
        .order_by("-id")
    )

    descricao = (request.GET.get("descricao") or "").strip()
    pessoa_id = (request.GET.get("pessoa_id") or "").strip()
    status = (request.GET.get("status") or "").strip()
    data_emissao_de = (request.GET.get("data_emissao_de") or "").strip()
    data_emissao_ate = (request.GET.get("data_emissao_ate") or "").strip()
    competencia = (request.GET.get("competencia") or "").strip()

    # Competência padrão: mês atual no formato MM/AAAA
    if not competencia:
        from datetime import date as _date
        hoje_comp = _date.today()
        competencia = hoje_comp.strftime("%m/%Y")

    if descricao:
        qs = qs.filter(descricao__icontains=descricao)
    if pessoa_id and pessoa_id.isdigit():
        qs = qs.filter(pessoa_id=int(pessoa_id))
    if status:
        qs = qs.filter(status=status)
    # Filtro por data de emissão
    if data_emissao_de or data_emissao_ate:
        from datetime import datetime as _dt
        try:
            if data_emissao_de:
                qs = qs.filter(data_emissao__gte=_dt.strptime(data_emissao_de, "%Y-%m-%d").date())
            if data_emissao_ate:
                qs = qs.filter(data_emissao__lte=_dt.strptime(data_emissao_ate, "%Y-%m-%d").date())
        except ValueError:
            pass
    # Filtro por competência (MM/AAAA ou parte)
    if competencia:
        qs = qs.filter(competencia__icontains=competencia)

    # Pré-carrega vencimentos (com status) e rateios para uso nos modais
    from django.db.models import Sum as _Sum
    vencimentos_map: dict[int, list[dict]] = {}
    for v in (
        ContaPagarVenc.objects.filter(conta_pagar__in=qs)
        .annotate(total_baixado=_Sum("baixas__valor_pago"))
        .order_by("conta_pagar_id", "sequencial_vencimento")
    ):
        total_baixado = v.total_baixado or Decimal("0")
        if total_baixado >= v.valor:
            status_v = "Pago"
        elif total_baixado > 0:
            status_v = "Parcial"
        else:
            status_v = "Em aberto"
        vencimentos_map.setdefault(v.conta_pagar_id, []).append(
            {
                "data": v.data_vencimento,
                "valor": v.valor,
                "status": status_v,
            }
        )

    rateios_map: dict[int, list[dict]] = {}
    for r in (
        ContaPagarRateio.objects.filter(conta_pagar__in=qs)
        .select_related("empresa", "centro_resultado", "plano_conta")
        .order_by("conta_pagar_id", "id")
    ):
        rateios_map.setdefault(r.conta_pagar_id, []).append(
            {
                "empresa": r.empresa.razao_social,
                "centro": f"{r.centro_resultado.codigo} - {r.centro_resultado.descricao}",
                "plano": f"{r.plano_conta.codigo} - {r.plano_conta.descricao}",
                "valor": r.valor,
            }
        )

    # Anexa dados de vencimentos e rateios em cada conta para facilitar no template
    for conta in qs:
        conta.vencimentos_resumo = vencimentos_map.get(conta.id, [])
        conta.rateios_resumo = rateios_map.get(conta.id, [])

    # Pessoas fornecedoras para filtro e formulários
    pessoas_fornecedoras = Pessoa.objects.filter(
        empresa__in=empresas
    ).filter(
        Q(tipo_cadastro=Pessoa.FORNECEDOR) | Q(tipo_cadastro=Pessoa.AMBOS)
    ).order_by("nome_razao")

    selected_pessoa_id = int(pessoa_id) if pessoa_id and pessoa_id.isdigit() else None
    context = {
        "contas": qs,
        "pessoas_fornecedoras": pessoas_fornecedoras,
        "filtros": {
            "descricao": descricao,
            "pessoa_id": pessoa_id,
            "status": status,
            "data_emissao_de": data_emissao_de,
            "data_emissao_ate": data_emissao_ate,
            "competencia": competencia,
        },
        "selected_pessoa_id": selected_pessoa_id,
        "status_choices": ContaPagar.STATUS_CHOICES,
    }
    return render(request, "finance/contas_a_pagar_list.html", context)


@login_required
def movimentos_contas_a_pagar_baixa(request):
    """Listagem de baixas (pagamentos) de contas a pagar."""
    empresas = get_empresas_contexto(request)
    if not empresas:
        messages.warning(request, "Selecione uma empresa no menu para acessar Contas a Pagar Baixa.")
        return redirect("dashboard")

    if not _usuario_tem_permissao_alguma_empresa(request, empresas, "visualizar"):
        messages.error(request, "Você não tem permissão para visualizar baixas de contas a pagar.")
        return redirect("dashboard")

    # Mensagens vindas do botão Baixar (verificação de permissão/vencimentos)
    erro_baixa = (request.GET.get("erro_baixa") or "").strip()
    if erro_baixa == "permissao":
        messages.error(request, "Você não tem permissão para incluir baixas de contas a pagar.")
    elif erro_baixa == "vencimentos":
        messages.warning(request, "Não há vencimentos em aberto para baixar. Todas as parcelas já foram pagas ou não existem contas a pagar com vencimentos pendentes.")
    elif erro_baixa == "erro":
        messages.error(request, "Não foi possível verificar. Tente novamente.")

    # Mensagens vindas do ícone Estorno (verificação de permissão / já estornado)
    erro_estorno = (request.GET.get("erro_estorno") or "").strip()
    if erro_estorno == "permissao":
        messages.error(request, "Você não tem permissão para lançar estornos nesta empresa.")
    elif erro_estorno == "ja_estornado":
        messages.error(request, "Esta baixa já possui um estorno lançado.")
    elif erro_estorno == "erro":
        messages.error(request, "Não foi possível verificar o estorno. Tente novamente.")

    qs = (
        ContaPagarBaixa.objects.filter(conta_pagar_venc__conta_pagar__empresa__in=empresas)
        .select_related(
            "conta_pagar_venc",
            "conta_pagar_venc__conta_pagar",
            "conta_pagar_venc__conta_pagar__empresa",
            "conta_pagar_venc__conta_pagar__pessoa",
            "conta_financeira",
            "conta_financeira__banco",
        )
        .order_by("-data_pagamento", "-id")
    )

    descricao = (request.GET.get("descricao") or "").strip()
    pessoa_id = (request.GET.get("pessoa_id") or "").strip()
    data_inicio = (request.GET.get("data_inicio") or "").strip()
    data_fim = (request.GET.get("data_fim") or "").strip()
    conta_financeira_id = (request.GET.get("conta_financeira_id") or "").strip()
    acao = (request.GET.get("acao") or "").strip()

    # Filtros do modal "Baixar" (datas do vencimento e fornecedor)
    venc_data_inicio = (request.GET.get("venc_data_inicio") or "").strip()
    venc_data_fim = (request.GET.get("venc_data_fim") or "").strip()
    venc_pessoa_id = (request.GET.get("venc_pessoa_id") or "").strip()

    # Datas padrão de pagamento: sempre o mês atual (1º ao último dia)
    from datetime import date as _date, timedelta as _timedelta
    hoje_pag = _date.today()
    primeiro_dia = hoje_pag.replace(day=1)
    # primeiro dia do mês seguinte - 1 dia
    if primeiro_dia.month == 12:
        proximo_mes = primeiro_dia.replace(year=primeiro_dia.year + 1, month=1)
    else:
        proximo_mes = primeiro_dia.replace(month=primeiro_dia.month + 1)
    ultimo_dia = proximo_mes - _timedelta(days=1)

    if not data_inicio:
        data_inicio = primeiro_dia.isoformat()
    if not data_fim:
        data_fim = ultimo_dia.isoformat()

    if descricao:
        qs = qs.filter(conta_pagar_venc__conta_pagar__descricao__icontains=descricao)
    if pessoa_id and pessoa_id.isdigit():
        qs = qs.filter(conta_pagar_venc__conta_pagar__pessoa_id=int(pessoa_id))
    # Aplica filtro de data de pagamento com os valores (padrão ou informados)
    try:
        from datetime import datetime as dt
        if data_inicio:
            qs = qs.filter(data_pagamento__gte=dt.strptime(data_inicio, "%Y-%m-%d").date())
        if data_fim:
            qs = qs.filter(data_pagamento__lte=dt.strptime(data_fim, "%Y-%m-%d").date())
    except ValueError:
        pass
    if conta_financeira_id and conta_financeira_id.isdigit():
        qs = qs.filter(conta_financeira_id=int(conta_financeira_id))

    pessoas_fornecedoras = Pessoa.objects.filter(
        empresa__in=empresas
    ).filter(
        Q(tipo_cadastro=Pessoa.FORNECEDOR) | Q(tipo_cadastro=Pessoa.AMBOS)
    ).order_by("nome_razao")

    contas_financeiras = list(
        ContaFinanceira.objects.filter(empresa__in=empresas)
        .select_related("banco")
        .order_by("banco__nome", "agencia", "conta")
    )
    centros_resultado = list(
        CentroResultado.objects.filter(empresa__in=empresas, tipo=CentroResultado.TIPO_ANALITICO)
        .order_by("codigo")
    )
    centros_resultado = list(
        CentroResultado.objects.filter(empresa__in=empresas, tipo=CentroResultado.TIPO_ANALITICO)
        .order_by("codigo")
    )
    planos_conta = list(
        PlanoConta.objects.filter(empresa__in=empresas, tipo=PlanoConta.TIPO_ANALITICO)
        .order_by("codigo")
    )

    # Busca de vencimentos para nova baixa (filtros do modal: data vencimento e fornecedor)
    vencimentos_para_baixa = []
    if acao == "buscar_vencimentos":
        from datetime import datetime as dt

        venc_qs = (
            ContaPagarVenc.objects.filter(
                conta_pagar__empresa__in=empresas,
                conta_pagar__status__in=[ContaPagar.STATUS_ABERTO, ContaPagar.STATUS_PARCIAL],
                baixas__isnull=True,
            )
            .select_related("conta_pagar", "conta_pagar__pessoa", "conta_pagar__empresa")
            .order_by("data_vencimento", "id")
        )
        if venc_pessoa_id and venc_pessoa_id.isdigit():
            venc_qs = venc_qs.filter(conta_pagar__pessoa_id=int(venc_pessoa_id))
        if venc_data_inicio:
            try:
                venc_qs = venc_qs.filter(data_vencimento__gte=dt.strptime(venc_data_inicio, "%Y-%m-%d").date())
            except ValueError:
                pass
        if venc_data_fim:
            try:
                venc_qs = venc_qs.filter(data_vencimento__lte=dt.strptime(venc_data_fim, "%Y-%m-%d").date())
            except ValueError:
                pass
        vencimentos_para_baixa = list(venc_qs)

    from datetime import date as _date, timedelta as _timedelta
    hoje = _date.today()

    # Data de referência para filtros do modal: ontem, ou sexta se hoje for segunda
    if hoje.weekday() == 0:  # segunda-feira (0 = Monday)
        data_vencimento_modal_padrao = (hoje - _timedelta(days=3)).isoformat()
    else:
        data_vencimento_modal_padrao = (hoje - _timedelta(days=1)).isoformat()

    if not venc_data_inicio and not venc_data_fim:
        venc_data_inicio = data_vencimento_modal_padrao
        venc_data_fim = data_vencimento_modal_padrao

    hoje_iso = hoje.isoformat()

    context = {
        "baixas": qs,
        "pessoas_fornecedoras": pessoas_fornecedoras,
        "contas_financeiras": contas_financeiras,
        "centros_resultado": centros_resultado,
        "planos_conta": planos_conta,
        "vencimentos_para_baixa": vencimentos_para_baixa,
        "acao": acao,
        "hoje_iso": hoje_iso,
        "data_vencimento_modal_padrao": data_vencimento_modal_padrao,
        "filtros": {
            "descricao": descricao,
            "pessoa_id": pessoa_id,
            "data_inicio": data_inicio,
            "data_fim": data_fim,
            "conta_financeira_id": conta_financeira_id,
        },
        "filtros_modal": {
            "venc_data_inicio": venc_data_inicio,
            "venc_data_fim": venc_data_fim,
            "venc_pessoa_id": venc_pessoa_id,
        },
        "selected_pessoa_id": int(pessoa_id) if pessoa_id and pessoa_id.isdigit() else None,
        "selected_conta_financeira_id": int(conta_financeira_id) if conta_financeira_id.isdigit() else None,
        "selected_venc_pessoa_id": int(venc_pessoa_id) if venc_pessoa_id and venc_pessoa_id.isdigit() else None,
    }
    return render(request, "finance/contas_a_pagar_baixa_list.html", context)


@login_required
def movimentos_contas_a_pagar_baixa_verificar_vencimentos(request):
    """Retorna JSON: pode_incluir (permissão para lançar baixa) e tem_vencimentos (existe vencimento em aberto)."""
    empresas = get_empresas_contexto(request)
    if not empresas:
        return JsonResponse({"pode_incluir": False, "tem_vencimentos": False})
    pode_incluir = _usuario_tem_permissao_alguma_empresa(request, empresas, "incluir")
    if not _usuario_tem_permissao_alguma_empresa(request, empresas, "visualizar"):
        return JsonResponse({"pode_incluir": False, "tem_vencimentos": False})
    tem_vencimentos = ContaPagarVenc.objects.filter(
        conta_pagar__empresa__in=empresas,
        conta_pagar__status__in=[ContaPagar.STATUS_ABERTO, ContaPagar.STATUS_PARCIAL],
        baixas__isnull=True,
    ).exists()
    return JsonResponse({"pode_incluir": pode_incluir, "tem_vencimentos": tem_vencimentos})


@login_required
def movimentos_contas_a_pagar_baixa_verificar_estorno(request):
    """Retorna JSON: pode_estornar e opcionalmente motivo (permissao, ja_estornado)."""
    empresas = get_empresas_contexto(request)
    if not empresas:
        return JsonResponse({"pode_estornar": False, "motivo": "erro"})
    baixa_id = (request.GET.get("baixa_id") or "").strip()
    if not baixa_id or not baixa_id.isdigit():
        return JsonResponse({"pode_estornar": False, "motivo": "erro"})
    baixa = (
        ContaPagarBaixa.objects.filter(
            pk=int(baixa_id),
            conta_pagar_venc__conta_pagar__empresa__in=empresas,
        )
        .select_related("conta_pagar_venc", "conta_pagar_venc__conta_pagar")
        .first()
    )
    if not baixa:
        return JsonResponse({"pode_estornar": False, "motivo": "erro"})
    conta = baixa.conta_pagar_venc.conta_pagar
    if not _usuario_tem_permissao_empresa(request, conta.empresa, "incluir"):
        return JsonResponse({"pode_estornar": False, "motivo": "permissao"})
    if Lancamento.objects.filter(baixa_conta_pagar=baixa).exists():
        return JsonResponse({"pode_estornar": False, "motivo": "ja_estornado"})
    return JsonResponse({"pode_estornar": True, "motivo": None})


@login_required
def movimentos_contas_a_receber_baixa(request):
    """Listagem de baixas (recebimentos) de contas a receber."""
    empresas = get_empresas_contexto(request)
    if not empresas:
        messages.warning(request, "Selecione uma empresa no menu para acessar Conta a Receber Baixa.")
        return redirect("dashboard")

    if not _usuario_tem_permissao_alguma_empresa(request, empresas, "visualizar"):
        messages.error(request, "Você não tem permissão para visualizar baixas de contas a receber.")
        return redirect("dashboard")

    # Mensagens vindas do botão Baixar (verificação de permissão/vencimentos)
    erro_baixa = (request.GET.get("erro_baixa") or "").strip()
    if erro_baixa == "permissao":
        messages.error(request, "Você não tem permissão para incluir baixas de contas a receber.")
    elif erro_baixa == "vencimentos":
        messages.warning(request, "Não há vencimentos em aberto para baixar. Todas as parcelas já foram recebidas ou não existem contas a receber com vencimentos pendentes.")
    elif erro_baixa == "erro":
        messages.error(request, "Não foi possível verificar. Tente novamente.")

    # Mensagens vindas do ícone Estorno (verificação de permissão / já estornado)
    erro_estorno = (request.GET.get("erro_estorno") or "").strip()
    if erro_estorno == "permissao":
        messages.error(request, "Você não tem permissão para lançar estornos nesta empresa.")
    elif erro_estorno == "ja_estornado":
        messages.error(request, "Esta baixa já possui um estorno lançado.")
    elif erro_estorno == "erro":
        messages.error(request, "Não foi possível verificar o estorno. Tente novamente.")

    qs = (
        ContaReceberBaixa.objects.filter(conta_receber_venc__conta_receber__empresa__in=empresas)
        .select_related(
            "conta_receber_venc",
            "conta_receber_venc__conta_receber",
            "conta_receber_venc__conta_receber__empresa",
            "conta_receber_venc__conta_receber__pessoa",
            "conta_financeira",
            "conta_financeira__banco",
        )
        .order_by("-data_pagamento", "-id")
    )

    descricao = (request.GET.get("descricao") or "").strip()
    pessoa_id = (request.GET.get("pessoa_id") or "").strip()
    data_inicio = (request.GET.get("data_inicio") or "").strip()
    data_fim = (request.GET.get("data_fim") or "").strip()
    conta_financeira_id = (request.GET.get("conta_financeira_id") or "").strip()
    acao = (request.GET.get("acao") or "").strip()

    # Filtros do modal "Baixar" (datas do vencimento e cliente)
    venc_data_inicio = (request.GET.get("venc_data_inicio") or "").strip()
    venc_data_fim = (request.GET.get("venc_data_fim") or "").strip()
    venc_pessoa_id = (request.GET.get("venc_pessoa_id") or "").strip()

    from datetime import date as _date, timedelta as _timedelta
    hoje_pag = _date.today()
    primeiro_dia = hoje_pag.replace(day=1)
    if primeiro_dia.month == 12:
        proximo_mes = primeiro_dia.replace(year=primeiro_dia.year + 1, month=1)
    else:
        proximo_mes = primeiro_dia.replace(month=primeiro_dia.month + 1)
    ultimo_dia = proximo_mes - _timedelta(days=1)

    if not data_inicio:
        data_inicio = primeiro_dia.isoformat()
    if not data_fim:
        data_fim = ultimo_dia.isoformat()

    if descricao:
        qs = qs.filter(conta_receber_venc__conta_receber__descricao__icontains=descricao)
    if pessoa_id and pessoa_id.isdigit():
        qs = qs.filter(conta_receber_venc__conta_receber__pessoa_id=int(pessoa_id))
    try:
        from datetime import datetime as dt
        if data_inicio:
            qs = qs.filter(data_pagamento__gte=dt.strptime(data_inicio, "%Y-%m-%d").date())
        if data_fim:
            qs = qs.filter(data_pagamento__lte=dt.strptime(data_fim, "%Y-%m-%d").date())
    except ValueError:
        pass
    if conta_financeira_id and conta_financeira_id.isdigit():
        qs = qs.filter(conta_financeira_id=int(conta_financeira_id))

    pessoas_clientes = Pessoa.objects.filter(
        empresa__in=empresas
    ).filter(
        Q(tipo_cadastro=Pessoa.CLIENTE) | Q(tipo_cadastro=Pessoa.AMBOS)
    ).order_by("nome_razao")

    contas_financeiras = list(
        ContaFinanceira.objects.filter(empresa__in=empresas)
        .select_related("banco")
        .order_by("banco__nome", "agencia", "conta")
    )
    centros_resultado = list(
        CentroResultado.objects.filter(empresa__in=empresas, tipo=CentroResultado.TIPO_ANALITICO)
        .order_by("codigo")
    )
    planos_conta = list(
        PlanoConta.objects.filter(empresa__in=empresas, tipo=PlanoConta.TIPO_ANALITICO)
        .order_by("codigo")
    )

    # Busca de vencimentos para nova baixa (filtros do modal: data vencimento e cliente)
    vencimentos_para_baixa = []
    if acao == "buscar_vencimentos":
        from datetime import datetime as dt

        venc_qs = (
            ContaReceberVenc.objects.filter(
                conta_receber__empresa__in=empresas,
                conta_receber__status__in=[ContaReceber.STATUS_ABERTO, ContaReceber.STATUS_PARCIAL],
                baixas__isnull=True,
            )
            .select_related("conta_receber", "conta_receber__pessoa", "conta_receber__empresa")
            .order_by("data_vencimento", "id")
        )
        if venc_pessoa_id and venc_pessoa_id.isdigit():
            venc_qs = venc_qs.filter(conta_receber__pessoa_id=int(venc_pessoa_id))
        if venc_data_inicio:
            try:
                venc_qs = venc_qs.filter(data_vencimento__gte=dt.strptime(venc_data_inicio, "%Y-%m-%d").date())
            except ValueError:
                pass
        if venc_data_fim:
            try:
                venc_qs = venc_qs.filter(data_vencimento__lte=dt.strptime(venc_data_fim, "%Y-%m-%d").date())
            except ValueError:
                pass
        vencimentos_para_baixa = list(venc_qs)

    hoje = _date.today()
    if hoje.weekday() == 0:
        data_vencimento_modal_padrao = (hoje - _timedelta(days=3)).isoformat()
    else:
        data_vencimento_modal_padrao = (hoje - _timedelta(days=1)).isoformat()

    if not venc_data_inicio and not venc_data_fim:
        venc_data_inicio = data_vencimento_modal_padrao
        venc_data_fim = data_vencimento_modal_padrao

    hoje_iso = hoje.isoformat()

    context = {
        "baixas": qs,
        "pessoas_clientes": pessoas_clientes,
        "contas_financeiras": contas_financeiras,
        "centros_resultado": centros_resultado,
        "planos_conta": planos_conta,
        "vencimentos_para_baixa": vencimentos_para_baixa,
        "acao": acao,
        "hoje_iso": hoje_iso,
        "data_vencimento_modal_padrao": data_vencimento_modal_padrao,
        "filtros": {
            "descricao": descricao,
            "pessoa_id": pessoa_id,
            "data_inicio": data_inicio,
            "data_fim": data_fim,
            "conta_financeira_id": conta_financeira_id,
        },
        "filtros_modal": {
            "venc_data_inicio": venc_data_inicio,
            "venc_data_fim": venc_data_fim,
            "venc_pessoa_id": venc_pessoa_id,
        },
        "selected_pessoa_id": int(pessoa_id) if pessoa_id and pessoa_id.isdigit() else None,
        "selected_conta_financeira_id": int(conta_financeira_id) if conta_financeira_id and conta_financeira_id.isdigit() else None,
        "selected_venc_pessoa_id": int(venc_pessoa_id) if venc_pessoa_id and venc_pessoa_id.isdigit() else None,
    }
    return render(request, "finance/contas_a_receber_baixa_list.html", context)


@login_required
def movimentos_contas_a_receber_baixa_verificar_vencimentos(request):
    """Retorna JSON: pode_incluir (permissão para lançar baixa) e tem_vencimentos (existe vencimento em aberto)."""
    empresas = get_empresas_contexto(request)
    if not empresas:
        return JsonResponse({"pode_incluir": False, "tem_vencimentos": False})
    pode_incluir = _usuario_tem_permissao_alguma_empresa(request, empresas, "incluir")
    if not _usuario_tem_permissao_alguma_empresa(request, empresas, "visualizar"):
        return JsonResponse({"pode_incluir": False, "tem_vencimentos": False})
    tem_vencimentos = ContaReceberVenc.objects.filter(
        conta_receber__empresa__in=empresas,
        conta_receber__status__in=[ContaReceber.STATUS_ABERTO, ContaReceber.STATUS_PARCIAL],
        baixas__isnull=True,
    ).exists()
    return JsonResponse({"pode_incluir": pode_incluir, "tem_vencimentos": tem_vencimentos})


@login_required
def movimentos_contas_a_receber_baixa_verificar_estorno(request):
    """Retorna JSON: pode_estornar e opcionalmente motivo (permissao, ja_estornado)."""
    empresas = get_empresas_contexto(request)
    if not empresas:
        return JsonResponse({"pode_estornar": False, "motivo": "erro"})
    baixa_id = (request.GET.get("baixa_id") or "").strip()
    if not baixa_id or not baixa_id.isdigit():
        return JsonResponse({"pode_estornar": False, "motivo": "erro"})
    baixa = (
        ContaReceberBaixa.objects.filter(
            pk=int(baixa_id),
            conta_receber_venc__conta_receber__empresa__in=empresas,
        )
        .select_related("conta_receber_venc", "conta_receber_venc__conta_receber")
        .first()
    )
    if not baixa:
        return JsonResponse({"pode_estornar": False, "motivo": "erro"})
    conta = baixa.conta_receber_venc.conta_receber
    if not _usuario_tem_permissao_empresa(request, conta.empresa, "incluir"):
        return JsonResponse({"pode_estornar": False, "motivo": "permissao"})
    if Lancamento.objects.filter(baixa_conta_receber=baixa).exists():
        return JsonResponse({"pode_estornar": False, "motivo": "ja_estornado"})
    return JsonResponse({"pode_estornar": True, "motivo": None})


@login_required
def movimentos_contas_a_receber_vencimento_baixar(request, venc_id):
    """Realiza a baixa (recebimento) de um vencimento específico de conta a receber."""
    empresas = get_empresas_contexto(request)
    if not empresas:
        messages.warning(request, "Selecione uma empresa no menu para acessar Conta a Receber Baixa.")
        return redirect("dashboard")

    venc = get_object_or_404(
        ContaReceberVenc.objects.select_related("conta_receber", "conta_receber__empresa"),
        pk=venc_id,
        conta_receber__empresa__in=empresas,
    )
    conta = venc.conta_receber

    if not _usuario_tem_permissao_empresa(request, conta.empresa, "incluir"):
        msg = "Você não tem permissão para baixar contas a receber nesta empresa."
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "error": msg}, status=403)
        messages.error(request, msg)
        return redirect("movimentos-contas-a-receber-baixa")

    if request.method != "POST":
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "error": "Método não permitido."}, status=405)
        return redirect("movimentos-contas-a-receber-baixa")

    conta_financeira_id = (request.POST.get("conta_financeira_id") or "").strip()
    data_pagamento_raw = (request.POST.get("data_pagamento") or "").strip()
    valor_pago_raw = (request.POST.get("valor_pago") or "").strip()
    juros_raw = (request.POST.get("juros") or "").strip()
    multa_raw = (request.POST.get("multa") or "").strip()

    conta_financeira = (
        ContaFinanceira.objects.filter(pk=conta_financeira_id, empresa__in=empresas)
        .select_related("banco")
        .first()
        if conta_financeira_id and conta_financeira_id.isdigit()
        else None
    )

    valor_pago = _parse_decimal_post(valor_pago_raw)
    juros = _parse_decimal_post(juros_raw) or Decimal("0")
    multa = _parse_decimal_post(multa_raw) or Decimal("0")

    from datetime import datetime as dt

    data_pagamento = None
    if data_pagamento_raw:
        try:
            data_pagamento = dt.strptime(data_pagamento_raw, "%Y-%m-%d").date()
        except ValueError:
            data_pagamento = None

    error_msg = None
    if not conta_financeira:
        error_msg = "Selecione uma conta financeira para realizar a baixa."
    elif not data_pagamento:
        error_msg = "Informe uma data de recebimento válida."
    elif not valor_pago or valor_pago <= 0:
        error_msg = "Informe um valor recebido válido."

    if error_msg:
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "error": error_msg}, status=400)
        messages.error(request, error_msg)
        return redirect("movimentos-contas-a-receber-baixa")

    ContaReceberBaixa.objects.create(
        conta_receber_venc=venc,
        conta_financeira=conta_financeira,
        data_pagamento=data_pagamento,
        valor_pago=valor_pago,
        juros=juros,
        multa=multa,
        created_by=request.user,
        updated_by=request.user,
    )

    from django.db.models import Sum as _Sum

    total_baixado = (
        ContaReceberBaixa.objects.filter(conta_receber_venc__conta_receber=conta).aggregate(
            total=_Sum("valor_pago")
        )["total"]
        or Decimal("0")
    )
    if total_baixado >= conta.valor_total:
        conta.status = ContaReceber.STATUS_PAGO
    elif total_baixado > 0 and conta.status != ContaReceber.STATUS_PAGO:
        conta.status = ContaReceber.STATUS_PARCIAL
    conta.updated_by = request.user
    conta.save(update_fields=["status", "updated_by", "updated_at"])

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse(
            {
                "ok": True,
                "conta_id": conta.id,
                "novo_status": conta.status,
            }
        )

    messages.success(request, "Baixa do vencimento realizada com sucesso.")
    return redirect("movimentos-contas-a-receber-baixa")


@login_required
def movimentos_contas_a_receber_baixa_editar(request, baixa_id):
    empresas = get_empresas_contexto(request)
    if not empresas:
        messages.warning(request, "Selecione uma empresa no menu para acessar Conta a Receber Baixa.")
        return redirect("dashboard")

    baixa = get_object_or_404(
        ContaReceberBaixa.objects.select_related(
            "conta_receber_venc",
            "conta_receber_venc__conta_receber",
            "conta_receber_venc__conta_receber__empresa",
            "conta_financeira",
            "conta_financeira__banco",
        ),
        pk=baixa_id,
        conta_receber_venc__conta_receber__empresa__in=empresas,
    )
    conta = baixa.conta_receber_venc.conta_receber

    if not _usuario_tem_permissao_empresa(request, conta.empresa, "editar"):
        messages.error(request, "Você não tem permissão para editar baixas desta empresa.")
        return redirect("movimentos-contas-a-receber-baixa")

    contas_financeiras = list(
        ContaFinanceira.objects.filter(empresa__in=empresas)
        .select_related("banco")
        .order_by("banco__nome", "agencia", "conta")
    )

    if request.method == "POST":
        conta_financeira_id = (request.POST.get("conta_financeira_id") or "").strip()
        data_pagamento_raw = (request.POST.get("data_pagamento") or "").strip()
        valor_pago_raw = (request.POST.get("valor_pago") or "").strip()
        juros_raw = (request.POST.get("juros") or "").strip()
        multa_raw = (request.POST.get("multa") or "").strip()

        conta_financeira = (
            ContaFinanceira.objects.filter(pk=conta_financeira_id, empresa__in=empresas)
            .select_related("banco")
            .first()
            if conta_financeira_id and conta_financeira_id.isdigit()
            else None
        )

        valor_pago = _parse_decimal_post(valor_pago_raw)
        juros = _parse_decimal_post(juros_raw) or Decimal("0")
        multa = _parse_decimal_post(multa_raw) or Decimal("0")

        from datetime import datetime as dt

        data_pagamento = None
        if data_pagamento_raw:
            try:
                data_pagamento = dt.strptime(data_pagamento_raw, "%Y-%m-%d").date()
            except ValueError:
                data_pagamento = None

        if not conta_financeira:
            messages.error(request, "Selecione uma conta financeira.")
        elif not data_pagamento:
            messages.error(request, "Informe uma data de recebimento válida.")
        elif not valor_pago or valor_pago <= 0:
            messages.error(request, "Informe um valor recebido válido.")
        else:
            baixa.conta_financeira = conta_financeira
            baixa.data_pagamento = data_pagamento
            baixa.valor_pago = valor_pago
            baixa.juros = juros
            baixa.multa = multa
            baixa.updated_by = request.user
            baixa.save()

            from django.db.models import Sum as _Sum

            total_baixado = (
                ContaReceberBaixa.objects.filter(conta_receber_venc__conta_receber=conta).aggregate(
                    total=_Sum("valor_pago")
                )["total"]
                or Decimal("0")
            )
            if total_baixado >= conta.valor_total:
                conta.status = ContaReceber.STATUS_PAGO
            elif total_baixado > 0:
                conta.status = ContaReceber.STATUS_PARCIAL
            else:
                conta.status = ContaReceber.STATUS_ABERTO
            conta.updated_by = request.user
            conta.save(update_fields=["status", "updated_by", "updated_at"])

            messages.success(request, "Baixa atualizada com sucesso.")
            return redirect("movimentos-contas-a-receber-baixa")

        form = {
            "conta_financeira_id": conta_financeira_id,
            "data_pagamento": data_pagamento_raw,
            "valor_pago": valor_pago_raw,
            "juros": juros_raw,
            "multa": multa_raw,
        }
    else:
        form = {
            "conta_financeira_id": baixa.conta_financeira_id,
            "data_pagamento": baixa.data_pagamento.isoformat() if baixa.data_pagamento else "",
            "valor_pago": f"{baixa.valor_pago:.2f}".replace(".", ","),
            "juros": f"{baixa.juros:.2f}".replace(".", ","),
            "multa": f"{baixa.multa:.2f}".replace(".", ","),
        }

    return render(
        request,
        "finance/conta_a_receber_baixa_form.html",
        {
            "baixa": baixa,
            "conta": conta,
            "contas_financeiras": contas_financeiras,
            "form": form,
        },
    )


@login_required
def movimentos_contas_a_receber_baixa_excluir(request, baixa_id):
    empresas = get_empresas_contexto(request)
    if not empresas:
        messages.warning(request, "Selecione uma empresa no menu para acessar Conta a Receber Baixa.")
        return redirect("dashboard")

    baixa = get_object_or_404(
        ContaReceberBaixa.objects.select_related(
            "conta_receber_venc",
            "conta_receber_venc__conta_receber",
            "conta_receber_venc__conta_receber__empresa",
        ),
        pk=baixa_id,
        conta_receber_venc__conta_receber__empresa__in=empresas,
    )
    conta = baixa.conta_receber_venc.conta_receber

    if not _usuario_tem_permissao_empresa(request, conta.empresa, "excluir"):
        messages.error(request, "Você não tem permissão para excluir baixas desta empresa.")
        return redirect("movimentos-contas-a-receber-baixa")

    if request.method == "POST":
        baixa.delete()

        from django.db.models import Sum as _Sum

        total_baixado = (
            ContaReceberBaixa.objects.filter(conta_receber_venc__conta_receber=conta).aggregate(
                total=_Sum("valor_pago")
            )["total"]
            or Decimal("0")
        )
        if total_baixado >= conta.valor_total:
            conta.status = ContaReceber.STATUS_PAGO
        elif total_baixado > 0:
            conta.status = ContaReceber.STATUS_PARCIAL
        else:
            conta.status = ContaReceber.STATUS_ABERTO
        conta.updated_by = request.user
        conta.save(update_fields=["status", "updated_by", "updated_at"])

        messages.success(request, "Baixa excluída com sucesso.")
        return redirect("movimentos-contas-a-receber-baixa")

    return render(
        request,
        "finance/conta_a_receber_baixa_confirmar_exclusao.html",
        {
            "baixa": baixa,
            "conta": conta,
        },
    )


@login_required
def movimentos_contas_a_receber_baixa_estornar(request):
    empresas = get_empresas_contexto(request)
    if not empresas:
        messages.warning(request, "Selecione uma empresa no menu para acessar Conta a Receber Baixa.")
        return redirect("movimentos-contas-a-receber-baixa")

    if request.method != "POST":
        return redirect("movimentos-contas-a-receber-baixa")

    baixa_id = (request.POST.get("baixa_id") or "").strip()
    data_raw = (request.POST.get("data") or "").strip()
    centro_id = (request.POST.get("centro_resultado_id") or "").strip()
    plano_id = (request.POST.get("plano_conta_id") or "").strip()
    observacao = (request.POST.get("observacao") or "").strip()

    if not baixa_id or not baixa_id.isdigit():
        messages.error(request, "Baixa inválida para estorno.")
        return redirect("movimentos-contas-a-receber-baixa")

    baixa = get_object_or_404(
        ContaReceberBaixa.objects.select_related(
            "conta_receber_venc",
            "conta_receber_venc__conta_receber",
            "conta_receber_venc__conta_receber__empresa",
            "conta_financeira",
            "conta_financeira__banco",
        ),
        pk=int(baixa_id),
        conta_receber_venc__conta_receber__empresa__in=empresas,
    )
    conta = baixa.conta_receber_venc.conta_receber

    if not _usuario_tem_permissao_empresa(request, conta.empresa, "incluir"):
        messages.error(request, "Você não tem permissão para lançar estornos nesta empresa.")
        return redirect("movimentos-contas-a-receber-baixa")

    if Lancamento.objects.filter(baixa_conta_receber=baixa).exists():
        messages.error(request, "Esta baixa já possui um estorno lançado.")
        return redirect("movimentos-contas-a-receber-baixa")

    from datetime import datetime as _dt

    data = None
    if data_raw:
        try:
            data = _dt.strptime(data_raw, "%Y-%m-%d").date()
        except ValueError:
            data = None

    if not data:
        messages.error(request, "Informe uma data válida para o estorno.")
        return redirect("movimentos-contas-a-receber-baixa")

    centro = (
        CentroResultado.objects.filter(pk=centro_id, empresa=conta.empresa).first()
        if centro_id and centro_id.isdigit()
        else None
    )
    plano = (
        PlanoConta.objects.filter(pk=plano_id, empresa=conta.empresa).first()
        if plano_id and plano_id.isdigit()
        else None
    )

    valor_estorno = baixa.valor_pago + baixa.juros + baixa.multa
    if not valor_estorno or valor_estorno <= 0:
        messages.error(request, "Valor de estorno inválido.")
        return redirect("movimentos-contas-a-receber-baixa")

    from core.models import SisOrigem

    origem_obj = SisOrigem.objects.filter(codigo="06").first()
    origem_valor = f"{origem_obj.codigo} - {origem_obj.nome}" if origem_obj else "06 - Estorno"

    Lancamento.objects.create(
        empresa=conta.empresa,
        conta_financeira=baixa.conta_financeira,
        tipo=Lancamento.TIPO_DEBITO,
        valor=valor_estorno,
        data=data,
        origem=origem_valor,
        centro_resultado=centro,
        plano_conta=plano,
        baixa_conta_pagar=None,
        baixa_conta_receber=baixa,
        observacao=observacao,
        created_by=request.user,
        updated_by=request.user,
    )

    messages.success(request, "Estorno lançado com sucesso.")
    return redirect("movimentos-contas-a-receber-baixa")


@login_required
def movimentos_contas_a_pagar_vencimento_baixar(request, venc_id):
    """Realiza a baixa de um vencimento específico de conta a pagar."""
    empresas = get_empresas_contexto(request)
    if not empresas:
        messages.warning(request, "Selecione uma empresa no menu para acessar Contas a Pagar Baixa.")
        return redirect("dashboard")

    venc = get_object_or_404(
        ContaPagarVenc.objects.select_related("conta_pagar", "conta_pagar__empresa"),
        pk=venc_id,
        conta_pagar__empresa__in=empresas,
    )
    conta = venc.conta_pagar

    # Para realizar uma nova baixa usamos a permissão de \"incluir\" (não \"editar\")
    if not _usuario_tem_permissao_empresa(request, conta.empresa, "incluir"):
        msg = "Você não tem permissão para baixar contas a pagar nesta empresa."
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "error": msg}, status=403)
        messages.error(request, msg)
        return redirect("movimentos-contas-a-pagar-baixa")

    if request.method != "POST":
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "error": "Método não permitido."}, status=405)
        return redirect("movimentos-contas-a-pagar-baixa")

    conta_financeira_id = (request.POST.get("conta_financeira_id") or "").strip()
    data_pagamento_raw = (request.POST.get("data_pagamento") or "").strip()
    valor_pago_raw = (request.POST.get("valor_pago") or "").strip()
    juros_raw = (request.POST.get("juros") or "").strip()
    multa_raw = (request.POST.get("multa") or "").strip()

    conta_financeira = (
        ContaFinanceira.objects.filter(pk=conta_financeira_id, empresa__in=empresas)
        .select_related("banco")
        .first()
        if conta_financeira_id and conta_financeira_id.isdigit()
        else None
    )

    valor_pago = _parse_decimal_post(valor_pago_raw)
    juros = _parse_decimal_post(juros_raw) or Decimal("0")
    multa = _parse_decimal_post(multa_raw) or Decimal("0")

    from datetime import datetime as dt

    data_pagamento = None
    if data_pagamento_raw:
        try:
            data_pagamento = dt.strptime(data_pagamento_raw, "%Y-%m-%d").date()
        except ValueError:
            data_pagamento = None

    error_msg = None
    if not conta_financeira:
        error_msg = "Selecione uma conta financeira para realizar a baixa."
    elif not data_pagamento:
        error_msg = "Informe uma data de pagamento válida."
    elif not valor_pago or valor_pago <= 0:
        error_msg = "Informe um valor pago válido."

    if error_msg:
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "error": error_msg}, status=400)
        messages.error(request, error_msg)
        return redirect("movimentos-contas-a-pagar-baixa")

    else:
        # Cria a baixa
        ContaPagarBaixa.objects.create(
            conta_pagar_venc=venc,
            conta_financeira=conta_financeira,
            data_pagamento=data_pagamento,
            valor_pago=valor_pago,
            juros=juros,
            multa=multa,
            created_by=request.user,
            updated_by=request.user,
        )

        # Atualiza status da conta (pago / parcial)
        from django.db.models import Sum as _Sum

        total_baixado = (
            ContaPagarBaixa.objects.filter(conta_pagar_venc__conta_pagar=conta).aggregate(
                total=_Sum("valor_pago")
            )["total"]
            or Decimal("0")
        )
        if total_baixado >= conta.valor_total:
            conta.status = ContaPagar.STATUS_PAGO
        elif total_baixado > 0 and conta.status != ContaPagar.STATUS_PAGO:
            conta.status = ContaPagar.STATUS_PARCIAL
        conta.updated_by = request.user
        conta.save(update_fields=["status", "updated_by", "updated_at"])

        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse(
                {
                    "ok": True,
                    "conta_id": conta.id,
                    "novo_status": conta.status,
                }
            )

        messages.success(request, "Baixa do vencimento realizada com sucesso.")
        return redirect("movimentos-contas-a-pagar-baixa")


@login_required
def movimentos_contas_a_pagar_baixa_editar(request, baixa_id):
    empresas = get_empresas_contexto(request)
    if not empresas:
        messages.warning(request, "Selecione uma empresa no menu para acessar Contas a Pagar Baixa.")
        return redirect("dashboard")

    baixa = get_object_or_404(
        ContaPagarBaixa.objects.select_related(
            "conta_pagar_venc",
            "conta_pagar_venc__conta_pagar",
            "conta_pagar_venc__conta_pagar__empresa",
            "conta_financeira",
            "conta_financeira__banco",
        ),
        pk=baixa_id,
        conta_pagar_venc__conta_pagar__empresa__in=empresas,
    )
    conta = baixa.conta_pagar_venc.conta_pagar

    if not _usuario_tem_permissao_empresa(request, conta.empresa, "editar"):
        messages.error(request, "Você não tem permissão para editar baixas desta empresa.")
        return redirect("movimentos-contas-a-pagar-baixa")

    contas_financeiras = list(
        ContaFinanceira.objects.filter(empresa__in=empresas)
        .select_related("banco")
        .order_by("banco__nome", "agencia", "conta")
    )
    centros_resultado = list(
        CentroResultado.objects.filter(empresa__in=empresas, tipo=CentroResultado.TIPO_ANALITICO)
        .order_by("codigo")
    )
    planos_conta = list(
        PlanoConta.objects.filter(empresa__in=empresas, tipo=PlanoConta.TIPO_ANALITICO)
        .order_by("codigo")
    )

    # Controle do estado do modal em caso de erro de validação
    modal_lancamento_aberto = False
    form_lancamento = {}

    if request.method == "POST":
        conta_financeira_id = (request.POST.get("conta_financeira_id") or "").strip()
        data_pagamento_raw = (request.POST.get("data_pagamento") or "").strip()
        valor_pago_raw = (request.POST.get("valor_pago") or "").strip()
        juros_raw = (request.POST.get("juros") or "").strip()
        multa_raw = (request.POST.get("multa") or "").strip()

        conta_financeira = (
            ContaFinanceira.objects.filter(pk=conta_financeira_id, empresa__in=empresas)
            .select_related("banco")
            .first()
            if conta_financeira_id and conta_financeira_id.isdigit()
            else None
        )

        valor_pago = _parse_decimal_post(valor_pago_raw)
        juros = _parse_decimal_post(juros_raw) or Decimal("0")
        multa = _parse_decimal_post(multa_raw) or Decimal("0")

        from datetime import datetime as dt

        data_pagamento = None
        if data_pagamento_raw:
            try:
                data_pagamento = dt.strptime(data_pagamento_raw, "%Y-%m-%d").date()
            except ValueError:
                data_pagamento = None

        if not conta_financeira:
            messages.error(request, "Selecione uma conta financeira para realizar a baixa.")
        elif not data_pagamento:
            messages.error(request, "Informe uma data de pagamento válida.")
        elif not valor_pago or valor_pago <= 0:
            messages.error(request, "Informe um valor pago válido.")
        else:
            baixa.conta_financeira = conta_financeira
            baixa.data_pagamento = data_pagamento
            baixa.valor_pago = valor_pago
            baixa.juros = juros
            baixa.multa = multa
            baixa.updated_by = request.user
            baixa.save()

            from django.db.models import Sum as _Sum

            total_baixado = (
                ContaPagarBaixa.objects.filter(conta_pagar_venc__conta_pagar=conta).aggregate(
                    total=_Sum("valor_pago")
                )["total"]
                or Decimal("0")
            )
            if total_baixado >= conta.valor_total:
                conta.status = ContaPagar.STATUS_PAGO
            elif total_baixado > 0:
                conta.status = ContaPagar.STATUS_PARCIAL
            else:
                conta.status = ContaPagar.STATUS_ABERTO
            conta.updated_by = request.user
            conta.save(update_fields=["status", "updated_by", "updated_at"])

            messages.success(request, "Baixa atualizada com sucesso.")
            return redirect("movimentos-contas-a-pagar-baixa")

        form = {
            "conta_financeira_id": conta_financeira_id,
            "data_pagamento": data_pagamento_raw,
            "valor_pago": valor_pago_raw,
            "juros": juros_raw,
            "multa": multa_raw,
        }
    else:
        form = {
            "conta_financeira_id": baixa.conta_financeira_id,
            "data_pagamento": baixa.data_pagamento.isoformat() if baixa.data_pagamento else "",
            "valor_pago": f"{baixa.valor_pago:.2f}".replace(".", ","),
            "juros": f"{baixa.juros:.2f}".replace(".", ","),
            "multa": f"{baixa.multa:.2f}".replace(".", ","),
        }

    return render(
        request,
        "finance/conta_a_pagar_baixa_form.html",
        {
            "baixa": baixa,
            "conta": conta,
            "contas_financeiras": contas_financeiras,
            "form": form,
        },
    )


@login_required
def movimentos_contas_a_pagar_baixa_excluir(request, baixa_id):
    empresas = get_empresas_contexto(request)
    if not empresas:
        messages.warning(request, "Selecione uma empresa no menu para acessar Contas a Pagar Baixa.")
        return redirect("dashboard")

    baixa = get_object_or_404(
        ContaPagarBaixa.objects.select_related(
            "conta_pagar_venc",
            "conta_pagar_venc__conta_pagar",
            "conta_pagar_venc__conta_pagar__empresa",
        ),
        pk=baixa_id,
        conta_pagar_venc__conta_pagar__empresa__in=empresas,
    )
    conta = baixa.conta_pagar_venc.conta_pagar

    if not _usuario_tem_permissao_empresa(request, conta.empresa, "excluir"):
        messages.error(request, "Você não tem permissão para excluir baixas desta empresa.")
        return redirect("movimentos-contas-a-pagar-baixa")

    if request.method == "POST":
        baixa.delete()

        from django.db.models import Sum as _Sum

        total_baixado = (
            ContaPagarBaixa.objects.filter(conta_pagar_venc__conta_pagar=conta).aggregate(
                total=_Sum("valor_pago")
            )["total"]
            or Decimal("0")
        )
        if total_baixado >= conta.valor_total:
            conta.status = ContaPagar.STATUS_PAGO
        elif total_baixado > 0:
            conta.status = ContaPagar.STATUS_PARCIAL
        else:
            conta.status = ContaPagar.STATUS_ABERTO
        conta.updated_by = request.user
        conta.save(update_fields=["status", "updated_by", "updated_at"])

        messages.success(request, "Baixa excluída com sucesso.")
        return redirect("movimentos-contas-a-pagar-baixa")

    return render(
        request,
        "finance/conta_a_pagar_baixa_confirmar_exclusao.html",
        {
            "baixa": baixa,
            "conta": conta,
        },
    )


@login_required
def movimentos_contas_a_pagar_baixa_estornar(request):
    empresas = get_empresas_contexto(request)
    if not empresas:
        messages.warning(request, "Selecione uma empresa no menu para acessar Contas a Pagar Baixa.")
        return redirect("dashboard")

    if request.method != "POST":
        return redirect("movimentos-contas-a-pagar-baixa")

    baixa_id = (request.POST.get("baixa_id") or "").strip()
    data_raw = (request.POST.get("data") or "").strip()
    centro_id = (request.POST.get("centro_resultado_id") or "").strip()
    plano_id = (request.POST.get("plano_conta_id") or "").strip()
    observacao = (request.POST.get("observacao") or "").strip()

    if not baixa_id or not baixa_id.isdigit():
        messages.error(request, "Baixa inválida para estorno.")
        return redirect("movimentos-contas-a-pagar-baixa")

    baixa = get_object_or_404(
        ContaPagarBaixa.objects.select_related(
            "conta_pagar_venc",
            "conta_pagar_venc__conta_pagar",
            "conta_pagar_venc__conta_pagar__empresa",
            "conta_financeira",
            "conta_financeira__banco",
        ),
        pk=int(baixa_id),
        conta_pagar_venc__conta_pagar__empresa__in=empresas,
    )
    conta = baixa.conta_pagar_venc.conta_pagar

    # Permissão para incluir lançamentos de estorno na empresa da conta
    if not _usuario_tem_permissao_empresa(request, conta.empresa, "incluir"):
        messages.error(request, "Você não tem permissão para lançar estornos nesta empresa.")
        return redirect("movimentos-contas-a-pagar-baixa")

    # Evita múltiplos estornos para a mesma baixa
    if Lancamento.objects.filter(baixa_conta_pagar=baixa).exists():
        messages.error(request, "Esta baixa já possui um estorno lançado.")
        return redirect("movimentos-contas-a-pagar-baixa")

    from datetime import datetime as _dt

    data = None
    if data_raw:
        try:
            data = _dt.strptime(data_raw, "%Y-%m-%d").date()
        except ValueError:
            data = None

    if not data:
        messages.error(request, "Informe uma data válida para o estorno.")
        return redirect("movimentos-contas-a-pagar-baixa")

    centro = (
        CentroResultado.objects.filter(pk=centro_id, empresa=conta.empresa).first()
        if centro_id and centro_id.isdigit()
        else None
    )
    plano = (
        PlanoConta.objects.filter(pk=plano_id, empresa=conta.empresa).first()
        if plano_id and plano_id.isdigit()
        else None
    )

    # Calcula o valor do estorno (valor pago + juros + multa)
    valor_estorno = baixa.valor_pago + baixa.juros + baixa.multa
    if not valor_estorno or valor_estorno <= 0:
        messages.error(request, "Valor de estorno inválido.")
        return redirect("movimentos-contas-a-pagar-baixa")

    # Origem "06 - Estorno"
    from core.models import SisOrigem

    origem_valor = None
    try:
        origem_obj = SisOrigem.objects.filter(codigo="06").first()
    except Exception:
        origem_obj = None

    if origem_obj:
        origem_valor = f"{origem_obj.codigo} - {origem_obj.nome}"
    else:
        origem_valor = "06 - Estorno"

    Lancamento.objects.create(
        empresa=conta.empresa,
        conta_financeira=baixa.conta_financeira,
        tipo=Lancamento.TIPO_CREDITO,
        valor=valor_estorno,
        data=data,
        origem=origem_valor,
        centro_resultado=centro,
        plano_conta=plano,
        baixa_conta_pagar=baixa,
        baixa_conta_receber=None,
        observacao=observacao,
        created_by=request.user,
        updated_by=request.user,
    )

    messages.success(request, "Estorno lançado com sucesso.")
    return redirect("movimentos-contas-a-pagar-baixa")

@login_required
def movimentos_contas_a_pagar_novo(request):
    empresas = get_empresas_contexto(request)
    if not empresas:
        messages.warning(request, "Selecione uma empresa no menu.")
        return redirect("dashboard")

    if not _usuario_tem_permissao_alguma_empresa(request, empresas, "incluir"):
        messages.error(request, "Você não tem permissão para incluir contas a pagar.")
        return redirect("movimentos-contas-a-pagar")

    pessoas_fornecedoras = Pessoa.objects.filter(
        empresa__in=empresas
    ).filter(
        Q(tipo_cadastro=Pessoa.FORNECEDOR) | Q(tipo_cadastro=Pessoa.AMBOS)
    ).order_by("nome_razao")

    empresas_choices = list(empresas)
    # Filtrar empresas onde o usuário tem permissão de incluir
    empresas_choices = [e for e in empresas_choices if _usuario_tem_permissao_empresa(request, e, "incluir")]
    if not empresas_choices:
        messages.error(request, "Você não tem permissão para incluir em nenhuma empresa do contexto.")
        return redirect("movimentos-contas-a-pagar")

    if request.method == "POST":
        from datetime import date, datetime

        empresa_id = (request.POST.get("empresa_id") or "").strip()
        pessoa_id = (request.POST.get("pessoa_id") or "").strip()
        descricao = (request.POST.get("descricao") or "").strip()
        documento = (request.POST.get("documento") or "").strip()
        tipo_documento_id = (request.POST.get("tipo_documento_id") or "").strip()
        competencia = (request.POST.get("competencia") or "").strip()
        data_emissao_raw = (request.POST.get("data_emissao") or "").strip()
        valor_total_raw = request.POST.get("valor_total")
        status = (request.POST.get("status") or "").strip() or ContaPagar.STATUS_ABERTO
        datas_venc = request.POST.getlist("data_vencimento")
        valores_raw = request.POST.getlist("valor")

        empresa = next((e for e in empresas_choices if str(e.id) == empresa_id), None)
        pessoa = Pessoa.objects.filter(pk=pessoa_id, empresa__in=empresas).first() if pessoa_id.isdigit() else None
        tipo_documento = TipoDocumento.objects.filter(pk=tipo_documento_id).first() if tipo_documento_id.isdigit() else None

        vencimentos_ok = []
        for i, (d, v_raw) in enumerate(zip(datas_venc, valores_raw)):
            d = (d or "").strip()
            v = _parse_decimal_post(v_raw)
            if not v or v <= 0:
                continue
            try:
                data_venc = datetime.strptime(d, "%Y-%m-%d").date() if d else date.today()
            except ValueError:
                data_venc = date.today()
            vencimentos_ok.append({"data": data_venc, "valor": v})

        valor_total = _parse_decimal_post(valor_total_raw)
        # Se não conseguir converter (por formato estranho), usa o valor atual da conta
        if valor_total is None:
            valor_total = conta.valor_total
        # Se não conseguir converter (por formato estranho), mantém o valor atual da conta
        if valor_total is None:
            valor_total = conta.valor_total
        total_vencimentos = sum((v["valor"] for v in vencimentos_ok), Decimal("0"))
        data_emissao = None
        if data_emissao_raw:
            try:
                data_emissao = datetime.strptime(data_emissao_raw, "%Y-%m-%d").date()
            except ValueError:
                data_emissao = None

        if not competencia and data_emissao:
            competencia = data_emissao.strftime("%m/%Y")

        if not descricao:
            messages.error(request, "Informe a descrição.")
        elif not documento:
            messages.error(request, "Informe o número do documento.")
        elif valor_total is None or valor_total <= 0:
            messages.error(request, "Informe um valor total válido.")
        elif not vencimentos_ok:
            messages.error(request, "Adicione ao menos um vencimento com valor maior que zero.")
        elif total_vencimentos != valor_total:
            messages.error(request, "A soma dos vencimentos deve ser igual ao valor total da conta.")
        elif not data_emissao:
            messages.error(request, "Informe a data de emissão.")
        elif not empresa:
            messages.error(request, "Selecione a empresa.")
        elif not pessoa:
            messages.error(request, "Selecione o fornecedor.")
        elif not tipo_documento:
            messages.error(request, "Selecione o tipo de documento.")
        elif status not in (ContaPagar.STATUS_ABERTO, ContaPagar.STATUS_PAGO, ContaPagar.STATUS_CANCELADO, ContaPagar.STATUS_PARCIAL):
            messages.error(request, "Status inválido.")
        else:
            matriz = get_empresa_matriz(request)
            rateio_empresas = request.POST.getlist("rateio_empresa_id")
            rateio_centros = request.POST.getlist("rateio_centro_id")
            rateio_planos = request.POST.getlist("rateio_plano_id")
            rateio_valores = request.POST.getlist("rateio_valor")
            rateio_ok = True
            total_rateio = Decimal("0")
            for emp_id, centro_id, plano_id, val_raw in zip(
                rateio_empresas, rateio_centros, rateio_planos, rateio_valores
            ):
                val = _parse_decimal_post(val_raw)
                if not val or val <= 0:
                    continue
                total_rateio += val
                emp = next((e for e in empresas_choices if str(e.id) == (emp_id or "").strip()), None)
                centro = CentroResultado.objects.filter(pk=centro_id, empresa=matriz).first() if centro_id and str(centro_id).strip().isdigit() else None
                plano = PlanoConta.objects.filter(pk=plano_id, empresa=matriz).first() if plano_id and str(plano_id).strip().isdigit() else None
                if not emp or not centro or not plano:
                    rateio_ok = False
                    break
            if total_rateio <= 0:
                rateio_ok = False
                messages.error(request, "Adicione ao menos um rateio com valor maior que zero.")
            elif total_rateio != valor_total:
                rateio_ok = False
                messages.error(request, "A soma dos rateios deve ser igual ao valor total da conta.")

            if rateio_ok:
                cp = ContaPagar.objects.create(
                    empresa=empresa,
                    pessoa=pessoa,
                    descricao=descricao,
                    documento=documento,
                    valor_total=valor_total,
                    status=status,
                    tipo_documento=tipo_documento,
                    competencia=competencia,
                    data_emissao=data_emissao,
                    created_by=request.user,
                    updated_by=request.user,
                )
                for seq, venc in enumerate(vencimentos_ok, start=1):
                    ContaPagarVenc.objects.create(
                        conta_pagar=cp,
                        sequencial_vencimento=seq,
                        data_vencimento=venc["data"],
                        valor=venc["valor"],
                    )
                for emp_id, centro_id, plano_id, val_raw in zip(
                    rateio_empresas, rateio_centros, rateio_planos, rateio_valores
                ):
                    val = _parse_decimal_post(val_raw)
                    if not val or val <= 0:
                        continue
                    emp = next((e for e in empresas_choices if str(e.id) == (emp_id or "").strip()), None)
                    centro = CentroResultado.objects.filter(pk=centro_id, empresa=matriz).first() if centro_id and str(centro_id).strip().isdigit() else None
                    plano = PlanoConta.objects.filter(pk=plano_id, empresa=matriz).first() if plano_id and str(plano_id).strip().isdigit() else None
                    if emp and centro and plano:
                        ContaPagarRateio.objects.create(
                            conta_pagar=cp,
                            empresa=emp,
                            centro_resultado=centro,
                            plano_conta=plano,
                            valor=val,
                        )
                messages.success(request, "Conta a pagar cadastrada com sucesso.")
                return redirect("movimentos-contas-a-pagar")
            else:
                messages.error(request, "Em cada linha de rateio com valor, preencha empresa, centro de resultado e plano de contas.")

        rateios_form = _parse_rateios_post(request, empresas_choices)
        # Preenche percentual para exibição após erro de validação
        for r in rateios_form:
            if valor_total and valor_total > 0:
                v = _parse_decimal_post(r.get("valor"))
                if v and v > 0:
                    r["percentual"] = (v / valor_total * 100).quantize(Decimal("0.01"))

        form = {
            "empresa_id": int(empresa_id) if empresa_id.isdigit() else empresa_id,
            "pessoa_id": int(pessoa_id) if pessoa_id.isdigit() else pessoa_id,
            "descricao": descricao,
            "documento": documento,
            "tipo_documento_id": int(tipo_documento_id) if tipo_documento_id.isdigit() else tipo_documento_id,
            "competencia": competencia,
            "data_emissao": data_emissao_raw,
            "valor_total": valor_total_raw or "",
            "status": status,
            "vencimentos": [
                {"data": v["data"].strftime("%Y-%m-%d") if hasattr(v["data"], "strftime") else v["data"], "valor": str(v["valor"])}
                for v in vencimentos_ok
            ],
            "rateios": rateios_form,
        }
    else:
        matriz = get_empresa_matriz(request)
        form = {"empresa_id": matriz.id} if matriz and matriz in empresas_choices else {}

    matriz = get_empresa_matriz(request)
    centros_resultado = (
        list(CentroResultado.objects.filter(empresa=matriz, tipo=CentroResultado.TIPO_ANALITICO).order_by("codigo"))
        if matriz else []
    )
    planos_conta = (
        list(PlanoConta.objects.filter(empresa=matriz, tipo=PlanoConta.TIPO_ANALITICO).order_by("codigo"))
        if matriz else []
    )
    tipos_documento = list(TipoDocumento.objects.filter(empresa__in=empresas_choices).order_by("nome"))
    centros_para_js = [{"id": c.id, "label": f"{c.codigo} - {c.descricao}"} for c in centros_resultado]
    planos_para_js = [{"id": p.id, "label": f"{p.codigo} - {p.descricao}"} for p in planos_conta]
    for r in form.get("rateios") or []:
        r.setdefault("centro_label", next((f"{c.codigo} - {c.descricao}" for c in centros_resultado if str(c.id) == str(r.get("centro_id"))), ""))
        r.setdefault("plano_label", next((f"{p.codigo} - {p.descricao}" for p in planos_conta if str(p.id) == str(r.get("plano_id"))), ""))

    return render(
        request,
        "finance/conta_a_pagar_form.html",
        {
            "form": form,
            "empresas": empresas_choices,
            "pessoas_fornecedoras": pessoas_fornecedoras,
            "status_choices": ContaPagar.STATUS_CHOICES,
            "tipos_documento": tipos_documento,
            "vencimentos_existentes": [],
            "centros_resultado": centros_resultado,
            "planos_conta": planos_conta,
            "centros_para_js": centros_para_js,
            "planos_para_js": planos_para_js,
            "rateios_existentes": [],
        },
    )


@login_required
def movimentos_contas_a_pagar_editar(request, conta_id):
    empresas = get_empresas_contexto(request)
    if not empresas:
        messages.warning(request, "Selecione uma empresa no menu.")
        return redirect("dashboard")

    conta = get_object_or_404(ContaPagar, pk=conta_id, empresa__in=empresas)

    if not _usuario_tem_permissao_empresa(request, conta.empresa, "editar"):
        messages.error(request, "Você não tem permissão para editar esta conta a pagar.")
        return redirect("movimentos-contas-a-pagar")

    pessoas_fornecedoras = Pessoa.objects.filter(
        empresa__in=empresas
    ).filter(
        Q(tipo_cadastro=Pessoa.FORNECEDOR) | Q(tipo_cadastro=Pessoa.AMBOS)
    ).order_by("nome_razao")

    empresas_choices = [e for e in empresas if _usuario_tem_permissao_empresa(request, e, "editar")]

    # Separa vencimentos com/sem baixa para controlar o que pode ser editado/removido
    vencimentos_com_baixa_qs = (
        ContaPagarVenc.objects.filter(conta_pagar=conta, baixas__isnull=False)
        .distinct()
        .order_by("sequencial_vencimento")
    )
    vencimentos_sem_baixa_qs = (
        ContaPagarVenc.objects.filter(conta_pagar=conta, baixas__isnull=True)
        .order_by("sequencial_vencimento")
    )

    if request.method == "POST":
        from datetime import date, datetime
        empresa_id = (request.POST.get("empresa_id") or "").strip()
        pessoa_id = (request.POST.get("pessoa_id") or "").strip()
        descricao = (request.POST.get("descricao") or "").strip()
        documento = (request.POST.get("documento") or "").strip()
        tipo_documento_id = (request.POST.get("tipo_documento_id") or "").strip()
        competencia = (request.POST.get("competencia") or "").strip()
        data_emissao_raw = (request.POST.get("data_emissao") or "").strip()
        valor_total_raw = request.POST.get("valor_total")
        status = (request.POST.get("status") or "").strip() or conta.status
        datas_venc = request.POST.getlist("data_vencimento")
        valores_raw = request.POST.getlist("valor")
        venc_ids = request.POST.getlist("vencimento_id")

        valor_total = _parse_decimal_post(valor_total_raw)
        # Se não conseguiu converter a partir do POST, usa o valor atual da conta
        if valor_total is None:
            valor_total = conta.valor_total
        empresa = next((e for e in empresas_choices if str(e.id) == empresa_id), None)
        pessoa = Pessoa.objects.filter(pk=pessoa_id, empresa__in=empresas).first() if pessoa_id.isdigit() else None
        tipo_documento = TipoDocumento.objects.filter(pk=tipo_documento_id).first() if tipo_documento_id.isdigit() else None
        data_emissao = None
        if data_emissao_raw:
            try:
                data_emissao = datetime.strptime(data_emissao_raw, "%Y-%m-%d").date()
            except ValueError:
                data_emissao = None

        if not competencia and data_emissao:
            competencia = data_emissao.strftime("%m/%Y")

        # Monta lista de vencimentos editáveis (sem baixa) e novos vencimentos
        vencimentos_editar = []
        novos_vencimentos = []
        venc_sem_baixa_ids = set(vencimentos_sem_baixa_qs.values_list("id", flat=True))

        for vid, d, v_raw in zip(venc_ids, datas_venc, valores_raw):
            d = (d or "").strip()
            v = _parse_decimal_post(v_raw)
            if not v or v <= 0:
                continue
            try:
                data_venc = datetime.strptime(d, "%Y-%m-%d").date() if d else date.today()
            except ValueError:
                data_venc = date.today()
            if vid and str(vid).strip().isdigit() and int(vid) in venc_sem_baixa_ids:
                vencimentos_editar.append({"id": int(vid), "data": data_venc, "valor": v})
            else:
                novos_vencimentos.append({"data": data_venc, "valor": v})

        if not descricao:
            messages.error(request, "Informe a descrição.")
        elif not documento:
            messages.error(request, "Informe o número do documento.")
        elif valor_total is None or valor_total <= 0:
            messages.error(request, "Informe um valor total válido.")
        elif not data_emissao:
            messages.error(request, "Informe a data de emissão.")
        else:
            # Validar soma dos vencimentos:
            # - vencimentos com baixa: valor fixo (não editável)
            # - vencimentos sem baixa: valor vindo do formulário (editáveis/removíveis)
            from django.db.models import Sum

            total_vencimentos_com_baixa = (
                vencimentos_com_baixa_qs.aggregate(total=Sum("valor"))["total"] or Decimal("0")
            )
            total_vencimentos_sem_baixa = sum((v["valor"] for v in vencimentos_editar), Decimal("0"))
            total_vencimentos_novos = sum((v["valor"] for v in novos_vencimentos), Decimal("0"))
            total_vencimentos = (
                total_vencimentos_com_baixa + total_vencimentos_sem_baixa + total_vencimentos_novos
            )

            if total_vencimentos <= 0:
                messages.error(request, "Adicione ao menos um vencimento com valor maior que zero.")
            elif total_vencimentos != valor_total:
                messages.error(request, "A soma dos vencimentos deve ser igual ao valor total da conta.")
            elif not empresa:
                messages.error(request, "Selecione a empresa.")
            elif not pessoa:
                messages.error(request, "Selecione o fornecedor.")
            elif not tipo_documento:
                messages.error(request, "Selecione o tipo de documento.")
            elif status not in (ContaPagar.STATUS_ABERTO, ContaPagar.STATUS_PAGO, ContaPagar.STATUS_CANCELADO, ContaPagar.STATUS_PARCIAL):
                messages.error(request, "Status inválido.")
            else:
                matriz = get_empresa_matriz(request)
                rateio_empresas = request.POST.getlist("rateio_empresa_id")
                rateio_centros = request.POST.getlist("rateio_centro_id")
                rateio_planos = request.POST.getlist("rateio_plano_id")
                rateio_valores = request.POST.getlist("rateio_valor")
                rateio_ok = True
                total_rateio = Decimal("0")
                for emp_id, centro_id, plano_id, val_raw in zip(
                    rateio_empresas, rateio_centros, rateio_planos, rateio_valores
                ):
                    val = _parse_decimal_post(val_raw)
                    if not val or val <= 0:
                        continue
                    total_rateio += val
                    emp = next((e for e in empresas_choices if str(e.id) == (emp_id or "").strip()), None)
                    centro = (
                        CentroResultado.objects.filter(pk=centro_id, empresa=matriz).first()
                        if centro_id and str(centro_id).strip().isdigit()
                        else None
                    )
                    plano = (
                        PlanoConta.objects.filter(pk=plano_id, empresa=matriz).first()
                        if plano_id and str(plano_id).strip().isdigit()
                        else None
                    )
                    if not emp or not centro or not plano:
                        rateio_ok = False
                        break
                if total_rateio <= 0:
                    rateio_ok = False
                    messages.error(request, "Adicione ao menos um rateio com valor maior que zero.")
                elif total_rateio != valor_total:
                    rateio_ok = False
                    messages.error(request, "A soma dos rateios deve ser igual ao valor total da conta.")

                if rateio_ok:
                    conta.empresa = empresa
                    conta.pessoa = pessoa
                    conta.descricao = descricao
                    conta.documento = documento
                    conta.valor_total = valor_total
                    conta.status = status
                    conta.tipo_documento = tipo_documento
                    conta.competencia = competencia
                    conta.data_emissao = data_emissao
                    conta.updated_by = request.user
                    conta.save()

                    # Atualiza vencimentos sem baixa existentes (data/valor) e remove os omitidos
                    ids_mantidos = set()
                    for v in vencimentos_editar:
                        vid = v["id"]
                        try:
                            obj = ContaPagarVenc.objects.get(pk=vid, conta_pagar=conta, baixas__isnull=True)
                        except ContaPagarVenc.DoesNotExist:
                            continue
                        obj.data_vencimento = v["data"]
                        obj.valor = v["valor"]
                        obj.save()
                        ids_mantidos.add(vid)

                    # Remove vencimentos sem baixa que não foram reenviados no formulário
                    for venc in vencimentos_sem_baixa_qs.exclude(id__in=ids_mantidos):
                        venc.delete()

                    # Cria novos vencimentos ao final da sequência
                    if novos_vencimentos:
                        prox_seq = (
                            conta.vencimentos.aggregate(Max("sequencial_vencimento"))["sequencial_vencimento__max"] or 0
                        ) + 1
                        for venc in novos_vencimentos:
                            ContaPagarVenc.objects.create(
                                conta_pagar=conta,
                                sequencial_vencimento=prox_seq,
                                data_vencimento=venc["data"],
                                valor=venc["valor"],
                            )
                            prox_seq += 1

                    conta.rateios.all().delete()
                    for emp_id, centro_id, plano_id, val_raw in zip(
                        rateio_empresas, rateio_centros, rateio_planos, rateio_valores
                    ):
                        val = _parse_decimal_post(val_raw)
                        if not val or val <= 0:
                            continue
                        emp = next((e for e in empresas_choices if str(e.id) == (emp_id or "").strip()), None)
                        centro = (
                            CentroResultado.objects.filter(pk=centro_id, empresa=matriz).first()
                            if centro_id and str(centro_id).strip().isdigit()
                            else None
                        )
                        plano = (
                            PlanoConta.objects.filter(pk=plano_id, empresa=matriz).first()
                            if plano_id and str(plano_id).strip().isdigit()
                            else None
                        )
                        if emp and centro and plano:
                            ContaPagarRateio.objects.create(
                                conta_pagar=conta,
                                empresa=emp,
                                centro_resultado=centro,
                                plano_conta=plano,
                                valor=val,
                            )

                    messages.success(request, "Conta a pagar atualizada com sucesso.")
                    return redirect("movimentos-contas-a-pagar")

        # Reconstrói vencimentos do formulário (editáveis + novos) para reexibir em caso de erro
        form_vencimentos = []
        for v in vencimentos_editar:
            form_vencimentos.append(
                {
                    "id": v["id"],
                    "data": v["data"].strftime("%Y-%m-%d"),
                    "valor": f"{v['valor']:.2f}".replace(".", ","),
                }
            )
        for v in novos_vencimentos:
            form_vencimentos.append(
                {
                    "id": "",
                    "data": v["data"].strftime("%Y-%m-%d"),
                    "valor": f"{v['valor']:.2f}".replace(".", ","),
                }
            )

        form = {
            "empresa_id": empresa_id,
            "pessoa_id": int(pessoa_id) if pessoa_id.isdigit() else pessoa_id,
            "descricao": descricao,
            "documento": documento,
            "tipo_documento_id": int(tipo_documento_id) if tipo_documento_id.isdigit() else tipo_documento_id,
            "competencia": competencia,
            "data_emissao": data_emissao_raw,
            "valor_total": valor_total_raw or "",
            "status": status,
            "vencimentos": form_vencimentos,
            "rateios": _parse_rateios_post(request, empresas_choices),
        }
    else:
        # GET: carrega vencimentos sem baixa como editáveis no modal
        form_vencimentos_get = [
            {
                "id": v.id,
                "data": v.data_vencimento.isoformat(),
                "valor": f"{v.valor:.2f}".replace(".", ","),
            }
            for v in vencimentos_sem_baixa_qs
        ]

        form = {
            "empresa_id": conta.empresa_id,
            "pessoa_id": conta.pessoa_id,
            "descricao": conta.descricao,
            "documento": conta.documento,
            "tipo_documento_id": conta.tipo_documento_id,
            "competencia": conta.competencia or "",
            "data_emissao": conta.data_emissao.isoformat() if conta.data_emissao else "",
            # Formata no padrão brasileiro para o JS (parseValor) funcionar corretamente
            "valor_total": f"{conta.valor_total:.2f}".replace(".", ",") if conta.valor_total is not None else "",
            "status": conta.status,
            "vencimentos": form_vencimentos_get,
        }

    matriz = get_empresa_matriz(request)
    centros_resultado = (
        list(CentroResultado.objects.filter(empresa=matriz, tipo=CentroResultado.TIPO_ANALITICO).order_by("codigo"))
        if matriz else []
    )
    planos_conta = (
        list(PlanoConta.objects.filter(empresa=matriz, tipo=PlanoConta.TIPO_ANALITICO).order_by("codigo"))
        if matriz else []
    )
    tipos_documento = list(TipoDocumento.objects.filter(empresa__in=empresas_choices).order_by("nome"))
    centros_para_js = [{"id": c.id, "label": f"{c.codigo} - {c.descricao}"} for c in centros_resultado]
    planos_para_js = [{"id": p.id, "label": f"{p.codigo} - {p.descricao}"} for p in planos_conta]

    # Vencimentos existentes com baixa permanecem somente para exibição (não editáveis)
    vencimentos_existentes = list(vencimentos_com_baixa_qs) if conta else []
    rateios_existentes = (
        list(conta.rateios.select_related("empresa", "centro_resultado", "plano_conta").order_by("id"))
        if conta else []
    )
    # Preenche form.rateios com os rateios existentes (modo edição) quando não vieram do POST,
    # para que apareçam já editáveis no modal.
    if not form.get("rateios"):
        form["rateios"] = [
            {
                "empresa_id": r.empresa_id,
                "centro_id": r.centro_resultado_id,
                "plano_id": r.plano_conta_id,
                # Formata valor no padrão brasileiro para o JS (parseValor) não multiplicar por 100
                "valor": f"{r.valor:.2f}".replace(".", ",") if r.valor is not None else "",
                "percentual": (
                    f"{(r.valor / conta.valor_total * 100):.2f}".replace(".", ",")
                    if conta.valor_total
                    else ""
                ),
            }
            for r in rateios_existentes
        ]
    for r in form.get("rateios") or []:
        r.setdefault(
            "centro_label",
            next(
                (f"{c.codigo} - {c.descricao}" for c in centros_resultado if str(c.id) == str(r.get("centro_id"))),
                "",
            ),
        )
        r.setdefault(
            "plano_label",
            next(
                (f"{p.codigo} - {p.descricao}" for p in planos_conta if str(p.id) == str(r.get("plano_id"))),
                "",
            ),
        )

    return render(
        request,
        "finance/conta_a_pagar_form.html",
        {
            "form": form,
            "conta": conta,
            "empresas": empresas_choices,
            "pessoas_fornecedoras": pessoas_fornecedoras,
            "status_choices": ContaPagar.STATUS_CHOICES,
            "tipos_documento": tipos_documento,
            "vencimentos_existentes": vencimentos_existentes,
            "centros_resultado": centros_resultado,
            "planos_conta": planos_conta,
            "centros_para_js": centros_para_js,
            "planos_para_js": planos_para_js,
            "rateios_existentes": [],
        },
    )


@login_required
def movimentos_contas_a_pagar_excluir(request, conta_id):
    empresas = get_empresas_contexto(request)
    if not empresas:
        messages.warning(request, "Selecione uma empresa no menu.")
        return redirect("dashboard")

    conta = get_object_or_404(
        ContaPagar.objects.select_related("pessoa"), pk=conta_id, empresa__in=empresas
    )

    if not _usuario_tem_permissao_empresa(request, conta.empresa, "excluir"):
        messages.error(request, "Você não tem permissão para excluir esta conta a pagar.")
        return redirect("movimentos-contas-a-pagar")

    if request.method == "POST":
        try:
            conta.delete()
            messages.success(request, "Conta a pagar excluída com sucesso.")
        except ProtectedError:
            messages.error(request, "Não é possível excluir: existem vencimentos ou baixas vinculadas.")
        return redirect("movimentos-contas-a-pagar")

    return render(
        request,
        "finance/contas_a_pagar_confirmar_exclusao.html",
        {"conta": conta},
    )


@login_required
def movimentos_contas_a_receber(request):
    empresas = get_empresas_contexto(request)
    if not empresas:
        messages.warning(request, "Selecione uma empresa no menu para acessar Contas a Receber.")
        return redirect("dashboard")

    if not _usuario_tem_permissao_alguma_empresa(request, empresas, "visualizar"):
        messages.error(request, "Você não tem permissão para visualizar contas a receber.")
        return redirect("dashboard")

    qs = (
        ContaReceber.objects.filter(empresa__in=empresas)
        .select_related("empresa", "pessoa")
        .order_by("-data_emissao", "-id")
    )

    descricao = (request.GET.get("descricao") or "").strip()
    pessoa_id = (request.GET.get("pessoa_id") or "").strip()
    status = (request.GET.get("status") or "").strip()
    data_emissao_de = (request.GET.get("data_emissao_de") or "").strip()
    data_emissao_ate = (request.GET.get("data_emissao_ate") or "").strip()
    competencia = (request.GET.get("competencia") or "").strip()

    # Competência padrão: mês atual no formato MM/AAAA
    if not competencia:
        from datetime import date as _date
        hoje_comp = _date.today()
        competencia = hoje_comp.strftime("%m/%Y")

    if descricao:
        qs = qs.filter(descricao__icontains=descricao)
    if pessoa_id and pessoa_id.isdigit():
        qs = qs.filter(pessoa_id=int(pessoa_id))
    if status:
        qs = qs.filter(status=status)
    if data_emissao_de or data_emissao_ate:
        from datetime import datetime as _dt
        try:
            if data_emissao_de:
                qs = qs.filter(data_emissao__gte=_dt.strptime(data_emissao_de, "%Y-%m-%d").date())
            if data_emissao_ate:
                qs = qs.filter(data_emissao__lte=_dt.strptime(data_emissao_ate, "%Y-%m-%d").date())
        except ValueError:
            pass
    if competencia:
        qs = qs.filter(competencia__icontains=competencia)

    # Resumo de vencimentos e rateios por conta a receber
    from django.db.models import Sum as _Sum

    vencimentos_map: dict[int, list[dict]] = {}
    for v in (
        ContaReceberVenc.objects.filter(conta_receber__in=qs)
        .annotate(total_baixado=_Sum("baixas__valor_pago"))
        .order_by("conta_receber_id", "sequencial_vencimento")
    ):
        total_baixado = v.total_baixado or Decimal("0")
        if total_baixado >= v.valor:
            status_v = "Recebido"
        elif total_baixado > 0:
            status_v = "Parcial"
        else:
            status_v = "Em aberto"
        vencimentos_map.setdefault(v.conta_receber_id, []).append(
            {"data": v.data_vencimento, "valor": v.valor, "status": status_v}
        )

    rateios_map: dict[int, list[dict]] = {}
    for r in (
        ContaReceberRateio.objects.filter(conta_receber__in=qs)
        .select_related("empresa", "centro_resultado", "plano_conta")
        .order_by("conta_receber_id", "empresa__razao_social")
    ):
        rateios_map.setdefault(r.conta_receber_id, []).append(
            {
                "empresa": r.empresa.razao_social,
                "centro": f"{r.centro_resultado.codigo} - {r.centro_resultado.descricao}",
                "plano": f"{r.plano_conta.codigo} - {r.plano_conta.descricao}",
                "valor": r.valor,
            }
        )

    # Anexa resumos aos objetos para uso no template
    for c in qs:
        c.vencimentos_resumo = vencimentos_map.get(c.id, [])
        c.rateios_resumo = rateios_map.get(c.id, [])

    # Clientes (pessoas que podem receber)
    pessoas_clientes = Pessoa.objects.filter(
        empresa__in=empresas
    ).filter(
        Q(tipo_cadastro=Pessoa.CLIENTE) | Q(tipo_cadastro=Pessoa.AMBOS)
    ).order_by("nome_razao")

    context = {
        "contas": qs,
        "pessoas_clientes": pessoas_clientes,
        "status_choices": ContaReceber.STATUS_CHOICES,
        "filtros": {
            "descricao": descricao,
            "pessoa_id": pessoa_id,
            "status": status,
            "data_emissao_de": data_emissao_de,
            "data_emissao_ate": data_emissao_ate,
            "competencia": competencia,
        },
        "selected_pessoa_id": int(pessoa_id) if pessoa_id.isdigit() else None,
    }
    return render(request, "finance/contas_a_receber_list.html", context)


@login_required
def movimentos_contas_a_receber_novo(request):
    empresas = get_empresas_contexto(request)
    if not empresas:
        messages.warning(request, "Selecione uma empresa no menu.")
        return redirect("dashboard")

    if not _usuario_tem_permissao_alguma_empresa(request, empresas, "incluir"):
        messages.error(request, "Você não tem permissão para incluir contas a receber.")
        return redirect("movimentos-contas-a-receber")

    pessoas_clientes = Pessoa.objects.filter(
        empresa__in=empresas
    ).filter(
        Q(tipo_cadastro=Pessoa.CLIENTE) | Q(tipo_cadastro=Pessoa.AMBOS)
    ).order_by("nome_razao")

    empresas_choices = list(empresas)
    empresas_choices = [e for e in empresas_choices if _usuario_tem_permissao_empresa(request, e, "incluir")]
    if not empresas_choices:
        messages.error(request, "Você não tem permissão para incluir em nenhuma empresa do contexto.")
        return redirect("movimentos-contas-a-receber")

    if request.method == "POST":
        from datetime import date, datetime as _dt

        empresa_id = (request.POST.get("empresa_id") or "").strip()
        pessoa_id = (request.POST.get("pessoa_id") or "").strip()
        descricao = (request.POST.get("descricao") or "").strip()
        tipo_documento_id = (request.POST.get("tipo_documento_id") or "").strip()
        competencia = (request.POST.get("competencia") or "").strip()
        data_emissao_raw = (request.POST.get("data_emissao") or "").strip()
        valor_total_raw = (request.POST.get("valor_total") or "").strip()
        datas_venc = request.POST.getlist("data_vencimento")
        valores_raw = request.POST.getlist("valor")

        empresa = next((e for e in empresas_choices if str(e.id) == empresa_id), None)
        pessoa = Pessoa.objects.filter(pk=pessoa_id, empresa__in=empresas).first() if pessoa_id.isdigit() else None
        tipo_documento = TipoDocumento.objects.filter(pk=tipo_documento_id).first() if tipo_documento_id.isdigit() else None

        vencimentos_ok = []
        for d, v_raw in zip(datas_venc, valores_raw):
            d = (d or "").strip()
            v = _parse_decimal_post(v_raw)
            if not v or v <= 0:
                continue
            try:
                data_venc = _dt.strptime(d, "%Y-%m-%d").date() if d else date.today()
            except ValueError:
                data_venc = date.today()
            vencimentos_ok.append({"data": data_venc, "valor": v})

        valor_total = _parse_decimal_post(valor_total_raw)
        total_vencimentos = sum((v["valor"] for v in vencimentos_ok), Decimal("0"))
        data_emissao = None
        if data_emissao_raw:
            try:
                data_emissao = _dt.strptime(data_emissao_raw, "%Y-%m-%d").date()
            except ValueError:
                data_emissao = None

        if not competencia and data_emissao:
            competencia = data_emissao.strftime("%m/%Y")

        if not descricao:
            messages.error(request, "Informe a descrição.")
        elif valor_total is None or valor_total <= 0:
            messages.error(request, "Informe um valor total válido.")
        elif not data_emissao:
            messages.error(request, "Informe a data de emissão.")
        elif not vencimentos_ok:
            messages.error(request, "Adicione ao menos um vencimento com valor maior que zero.")
        elif total_vencimentos != valor_total:
            messages.error(request, "A soma dos vencimentos deve ser igual ao valor total da conta.")
        elif not empresa:
            messages.error(request, "Selecione a empresa.")
        elif not pessoa:
            messages.error(request, "Selecione o cliente.")
        elif not tipo_documento:
            messages.error(request, "Selecione o tipo de documento.")
        else:
            matriz = get_empresa_matriz(request)
            rateio_empresas = request.POST.getlist("rateio_empresa_id")
            rateio_centros = request.POST.getlist("rateio_centro_id")
            rateio_planos = request.POST.getlist("rateio_plano_id")
            rateio_valores = request.POST.getlist("rateio_valor")
            rateio_ok = True
            total_rateio = Decimal("0")
            for emp_id, centro_id, plano_id, val_raw in zip(
                rateio_empresas, rateio_centros, rateio_planos, rateio_valores
            ):
                val = _parse_decimal_post(val_raw)
                if not val or val <= 0:
                    continue
                total_rateio += val
                emp = next((e for e in empresas_choices if str(e.id) == (emp_id or "").strip()), None)
                centro = (
                    CentroResultado.objects.filter(pk=centro_id, empresa=matriz).first()
                    if centro_id and str(centro_id).strip().isdigit()
                    else None
                )
                plano = (
                    PlanoConta.objects.filter(pk=plano_id, empresa=matriz).first()
                    if plano_id and str(plano_id).strip().isdigit()
                    else None
                )
                if not emp or not centro or not plano:
                    rateio_ok = False
                    break
            if total_rateio <= 0:
                rateio_ok = False
                messages.error(request, "Adicione ao menos um rateio com valor maior que zero.")
            elif total_rateio != valor_total:
                rateio_ok = False
                messages.error(request, "A soma dos rateios deve ser igual ao valor total da conta.")

            if rateio_ok:
                conta_receber = ContaReceber.objects.create(
                    empresa=empresa,
                    pessoa=pessoa,
                    descricao=descricao,
                    valor_total=valor_total,
                    status=ContaReceber.STATUS_ABERTO,
                    tipo_documento=tipo_documento,
                    competencia=competencia,
                    data_emissao=data_emissao,
                    created_by=request.user,
                    updated_by=request.user,
                )
                for seq, venc in enumerate(vencimentos_ok, start=1):
                    ContaReceberVenc.objects.create(
                        conta_receber=conta_receber,
                        sequencial_vencimento=seq,
                        data_vencimento=venc["data"],
                        valor=venc["valor"],
                    )
                for emp_id, centro_id, plano_id, val_raw in zip(
                    rateio_empresas, rateio_centros, rateio_planos, rateio_valores
                ):
                    val = _parse_decimal_post(val_raw)
                    if not val or val <= 0:
                        continue
                    emp = next((e for e in empresas_choices if str(e.id) == (emp_id or "").strip()), None)
                    centro = (
                        CentroResultado.objects.filter(pk=centro_id, empresa=matriz).first()
                        if centro_id and str(centro_id).strip().isdigit()
                        else None
                    )
                    plano = (
                        PlanoConta.objects.filter(pk=plano_id, empresa=matriz).first()
                        if plano_id and str(plano_id).strip().isdigit()
                        else None
                    )
                    if emp and centro and plano:
                        ContaReceberRateio.objects.create(
                            conta_receber=conta_receber,
                            empresa=emp,
                            centro_resultado=centro,
                            plano_conta=plano,
                            valor=val,
                        )
                messages.success(request, "Conta a receber cadastrada com sucesso.")
                return redirect("movimentos-contas-a-receber")
            else:
                messages.error(request, "Em cada linha de rateio com valor, preencha empresa, centro de resultado e plano de contas.")

        form_vencimentos = [
            {"id": "", "data": v["data"].strftime("%Y-%m-%d"), "valor": f"{v['valor']:.2f}".replace(".", ",")}
            for v in vencimentos_ok
        ]
        rateios_form = _parse_rateios_post(request, empresas_choices)
        if valor_total and valor_total > 0:
            for r in rateios_form:
                v = _parse_decimal_post(r.get("valor"))
                if v and v > 0:
                    r["percentual"] = f"{(v / valor_total * 100):.2f}".replace(".", ",")
        form = {
            "empresa_id": int(empresa_id) if empresa_id.isdigit() else empresa_id,
            "pessoa_id": int(pessoa_id) if pessoa_id.isdigit() else pessoa_id,
            "descricao": descricao,
            "tipo_documento_id": int(tipo_documento_id) if tipo_documento_id.isdigit() else tipo_documento_id,
            "competencia": competencia,
            "data_emissao": data_emissao_raw,
            "valor_total": valor_total_raw or "",
            "status": ContaReceber.STATUS_ABERTO,
            "vencimentos": form_vencimentos,
            "rateios": rateios_form,
        }
    else:
        matriz = get_empresa_matriz(request)
        form = {"empresa_id": matriz.id, "vencimentos": [], "rateios": []} if matriz and matriz in empresas_choices else {"vencimentos": [], "rateios": []}

    tipos_documento = list(TipoDocumento.objects.filter(empresa__in=empresas_choices).order_by("nome"))
    matriz = get_empresa_matriz(request)
    centros_resultado = (
        list(CentroResultado.objects.filter(empresa=matriz, tipo=CentroResultado.TIPO_ANALITICO).order_by("codigo"))
        if matriz else []
    )
    planos_conta = (
        list(PlanoConta.objects.filter(empresa=matriz, tipo=PlanoConta.TIPO_ANALITICO).order_by("codigo"))
        if matriz else []
    )
    centros_para_js = [{"id": c.id, "label": f"{c.codigo} - {c.descricao}"} for c in centros_resultado]
    planos_para_js = [{"id": p.id, "label": f"{p.codigo} - {p.descricao}"} for p in planos_conta]
    for r in form.get("rateios") or []:
        r.setdefault("centro_label", next((f"{c.codigo} - {c.descricao}" for c in centros_resultado if str(c.id) == str(r.get("centro_id"))), ""))
        r.setdefault("plano_label", next((f"{p.codigo} - {p.descricao}" for p in planos_conta if str(p.id) == str(r.get("plano_id"))), ""))

    return render(
        request,
        "finance/conta_a_receber_form.html",
        {
            "form": form,
            "empresas": empresas_choices,
            "pessoas_clientes": pessoas_clientes,
            "tipos_documento": tipos_documento,
            "centros_resultado": centros_resultado,
            "planos_conta": planos_conta,
            "centros_para_js": centros_para_js,
            "planos_para_js": planos_para_js,
            "conta": None,
            "status_choices": ContaReceber.STATUS_CHOICES,
        },
    )


@login_required
def movimentos_contas_a_receber_editar(request, conta_id):
    empresas = get_empresas_contexto(request)
    if not empresas:
        messages.warning(request, "Selecione uma empresa no menu.")
        return redirect("dashboard")

    conta = get_object_or_404(ContaReceber, pk=conta_id, empresa__in=empresas)

    if not _usuario_tem_permissao_empresa(request, conta.empresa, "editar"):
        messages.error(request, "Você não tem permissão para editar esta conta a receber.")
        return redirect("movimentos-contas-a-receber")

    pessoas_clientes = Pessoa.objects.filter(
        empresa__in=empresas
    ).filter(
        Q(tipo_cadastro=Pessoa.CLIENTE) | Q(tipo_cadastro=Pessoa.AMBOS)
    ).order_by("nome_razao")

    empresas_choices = [e for e in empresas if _usuario_tem_permissao_empresa(request, e, "editar")]

    vencimentos_com_baixa_qs = (
        ContaReceberVenc.objects.filter(conta_receber=conta, baixas__isnull=False)
        .distinct()
        .order_by("sequencial_vencimento")
    )
    vencimentos_sem_baixa_qs = (
        ContaReceberVenc.objects.filter(conta_receber=conta, baixas__isnull=True)
        .order_by("sequencial_vencimento")
    )

    if request.method == "POST":
        from datetime import date
        from datetime import datetime as _dt

        empresa_id = (request.POST.get("empresa_id") or "").strip()
        pessoa_id = (request.POST.get("pessoa_id") or "").strip()
        descricao = (request.POST.get("descricao") or "").strip()
        tipo_documento_id = (request.POST.get("tipo_documento_id") or "").strip()
        competencia = (request.POST.get("competencia") or "").strip()
        data_emissao_raw = (request.POST.get("data_emissao") or "").strip()
        valor_total_raw = request.POST.get("valor_total")
        status = (request.POST.get("status") or "").strip() or conta.status
        datas_venc = request.POST.getlist("data_vencimento")
        valores_raw = request.POST.getlist("valor")
        venc_ids = request.POST.getlist("vencimento_id")

        valor_total = _parse_decimal_post(valor_total_raw)
        if valor_total is None:
            valor_total = conta.valor_total
        empresa = next((e for e in empresas_choices if str(e.id) == empresa_id), None)
        pessoa = Pessoa.objects.filter(pk=pessoa_id, empresa__in=empresas).first() if pessoa_id.isdigit() else None
        tipo_documento = TipoDocumento.objects.filter(pk=tipo_documento_id).first() if tipo_documento_id.isdigit() else None
        data_emissao = None
        if data_emissao_raw:
            try:
                data_emissao = _dt.strptime(data_emissao_raw, "%Y-%m-%d").date()
            except ValueError:
                data_emissao = None

        if not competencia and data_emissao:
            competencia = data_emissao.strftime("%m/%Y")

        vencimentos_editar = []
        novos_vencimentos = []
        venc_sem_baixa_ids = set(vencimentos_sem_baixa_qs.values_list("id", flat=True))

        for vid, d, v_raw in zip(venc_ids, datas_venc, valores_raw):
            d = (d or "").strip()
            v = _parse_decimal_post(v_raw)
            if not v or v <= 0:
                continue
            try:
                data_venc = _dt.strptime(d, "%Y-%m-%d").date() if d else date.today()
            except ValueError:
                data_venc = date.today()
            if vid and str(vid).strip().isdigit() and int(vid) in venc_sem_baixa_ids:
                vencimentos_editar.append({"id": int(vid), "data": data_venc, "valor": v})
            else:
                novos_vencimentos.append({"data": data_venc, "valor": v})

        # Monta form para reexibição em caso de erro de validação
        form_vencimentos_post = []
        for v in vencimentos_editar:
            form_vencimentos_post.append(
                {"id": v["id"], "data": v["data"].strftime("%Y-%m-%d"), "valor": f"{v['valor']:.2f}".replace(".", ",")}
            )
        for v in novos_vencimentos:
            form_vencimentos_post.append(
                {"id": "", "data": v["data"].strftime("%Y-%m-%d"), "valor": f"{v['valor']:.2f}".replace(".", ",")}
            )
        form = {
            "empresa_id": empresa_id,
            "pessoa_id": int(pessoa_id) if pessoa_id.isdigit() else pessoa_id,
            "descricao": descricao,
            "tipo_documento_id": int(tipo_documento_id) if tipo_documento_id.isdigit() else tipo_documento_id,
            "competencia": competencia,
            "data_emissao": data_emissao_raw,
            "valor_total": valor_total_raw or "",
            "status": status,
            "vencimentos": form_vencimentos_post,
            "rateios": _parse_rateios_post(request, empresas_choices),
        }

        if not descricao:
            messages.error(request, "Informe a descrição.")
        elif valor_total is None or valor_total <= 0:
            messages.error(request, "Informe um valor total válido.")
        elif not data_emissao:
            messages.error(request, "Informe a data de emissão.")
        else:
            from django.db.models import Sum

            total_vencimentos_com_baixa = (
                vencimentos_com_baixa_qs.aggregate(total=Sum("valor"))["total"] or Decimal("0")
            )
            total_vencimentos_sem_baixa = sum((x["valor"] for x in vencimentos_editar), Decimal("0"))
            total_vencimentos_novos = sum((x["valor"] for x in novos_vencimentos), Decimal("0"))
            total_vencimentos = (
                total_vencimentos_com_baixa + total_vencimentos_sem_baixa + total_vencimentos_novos
            )

            if total_vencimentos <= 0:
                messages.error(request, "Adicione ao menos um vencimento com valor maior que zero.")
            elif total_vencimentos != valor_total:
                messages.error(request, "A soma dos vencimentos deve ser igual ao valor total da conta.")
            elif not empresa:
                messages.error(request, "Selecione a empresa.")
            elif not pessoa:
                messages.error(request, "Selecione o cliente.")
            elif not tipo_documento:
                messages.error(request, "Selecione o tipo de documento.")
            elif status not in (ContaReceber.STATUS_ABERTO, ContaReceber.STATUS_PAGO, ContaReceber.STATUS_CANCELADO, ContaReceber.STATUS_PARCIAL):
                messages.error(request, "Status inválido.")
            else:
                matriz = get_empresa_matriz(request)
                rateio_empresas = request.POST.getlist("rateio_empresa_id")
                rateio_centros = request.POST.getlist("rateio_centro_id")
                rateio_planos = request.POST.getlist("rateio_plano_id")
                rateio_valores = request.POST.getlist("rateio_valor")
                rateio_ok = True
                total_rateio = Decimal("0")
                for emp_id, centro_id, plano_id, val_raw in zip(
                    rateio_empresas, rateio_centros, rateio_planos, rateio_valores
                ):
                    val = _parse_decimal_post(val_raw)
                    if not val or val <= 0:
                        continue
                    total_rateio += val
                    emp = next((e for e in empresas_choices if str(e.id) == (emp_id or "").strip()), None)
                    centro = (
                        CentroResultado.objects.filter(pk=centro_id, empresa=matriz).first()
                        if centro_id and str(centro_id).strip().isdigit()
                        else None
                    )
                    plano = (
                        PlanoConta.objects.filter(pk=plano_id, empresa=matriz).first()
                        if plano_id and str(plano_id).strip().isdigit()
                        else None
                    )
                    if not emp or not centro or not plano:
                        rateio_ok = False
                        break
                if total_rateio <= 0:
                    rateio_ok = False
                    messages.error(request, "Adicione ao menos um rateio com valor maior que zero.")
                elif total_rateio != valor_total:
                    rateio_ok = False
                    messages.error(request, "A soma dos rateios deve ser igual ao valor total da conta.")

                if rateio_ok:
                    conta.empresa = empresa
                    conta.pessoa = pessoa
                    conta.descricao = descricao
                    conta.valor_total = valor_total
                    conta.status = status
                    conta.tipo_documento = tipo_documento
                    conta.competencia = competencia
                    conta.data_emissao = data_emissao
                    conta.updated_by = request.user
                    conta.save()

                    ids_mantidos = set()
                    for v in vencimentos_editar:
                        vid = v["id"]
                        try:
                            obj = ContaReceberVenc.objects.get(pk=vid, conta_receber=conta, baixas__isnull=True)
                        except ContaReceberVenc.DoesNotExist:
                            continue
                        obj.data_vencimento = v["data"]
                        obj.valor = v["valor"]
                        obj.save()
                        ids_mantidos.add(vid)

                    for venc in vencimentos_sem_baixa_qs.exclude(id__in=ids_mantidos):
                        venc.delete()

                    if novos_vencimentos:
                        prox_seq = (
                            conta.vencimentos.aggregate(Max("sequencial_vencimento"))["sequencial_vencimento__max"] or 0
                        ) + 1
                        for venc in novos_vencimentos:
                            ContaReceberVenc.objects.create(
                                conta_receber=conta,
                                sequencial_vencimento=prox_seq,
                                data_vencimento=venc["data"],
                                valor=venc["valor"],
                            )
                            prox_seq += 1

                    conta.rateios.all().delete()
                    for emp_id, centro_id, plano_id, val_raw in zip(
                        rateio_empresas, rateio_centros, rateio_planos, rateio_valores
                    ):
                        val = _parse_decimal_post(val_raw)
                        if not val or val <= 0:
                            continue
                        emp = next((e for e in empresas_choices if str(e.id) == (emp_id or "").strip()), None)
                        centro = (
                            CentroResultado.objects.filter(pk=centro_id, empresa=matriz).first()
                            if centro_id and str(centro_id).strip().isdigit()
                            else None
                        )
                        plano = (
                            PlanoConta.objects.filter(pk=plano_id, empresa=matriz).first()
                            if plano_id and str(plano_id).strip().isdigit()
                            else None
                        )
                        if emp and centro and plano:
                            ContaReceberRateio.objects.create(
                                conta_receber=conta,
                                empresa=emp,
                                centro_resultado=centro,
                                plano_conta=plano,
                                valor=val,
                            )

                    messages.success(request, "Conta a receber atualizada com sucesso.")
                    return redirect("movimentos-contas-a-receber")
    else:
        form_vencimentos_get = [
            {
                "id": v.id,
                "data": v.data_vencimento.isoformat(),
                "valor": f"{v.valor:.2f}".replace(".", ","),
            }
            for v in vencimentos_sem_baixa_qs
        ]

        form = {
            "empresa_id": conta.empresa_id,
            "pessoa_id": conta.pessoa_id,
            "descricao": conta.descricao,
            "tipo_documento_id": conta.tipo_documento_id,
            "competencia": conta.competencia or "",
            "data_emissao": conta.data_emissao.isoformat() if conta.data_emissao else "",
            "valor_total": f"{conta.valor_total:.2f}".replace(".", ",") if conta.valor_total is not None else "",
            "status": conta.status,
            "vencimentos": form_vencimentos_get,
        }

    matriz = get_empresa_matriz(request)
    centros_resultado = (
        list(CentroResultado.objects.filter(empresa=matriz, tipo=CentroResultado.TIPO_ANALITICO).order_by("codigo"))
        if matriz else []
    )
    planos_conta = (
        list(PlanoConta.objects.filter(empresa=matriz, tipo=PlanoConta.TIPO_ANALITICO).order_by("codigo"))
        if matriz else []
    )
    tipos_documento = list(TipoDocumento.objects.filter(empresa__in=empresas_choices).order_by("nome"))
    centros_para_js = [{"id": c.id, "label": f"{c.codigo} - {c.descricao}"} for c in centros_resultado]
    planos_para_js = [{"id": p.id, "label": f"{p.codigo} - {p.descricao}"} for p in planos_conta]

    rateios_existentes = (
        list(conta.rateios.select_related("empresa", "centro_resultado", "plano_conta").order_by("id"))
        if conta else []
    )
    if not form.get("rateios"):
        form["rateios"] = [
            {
                "empresa_id": r.empresa_id,
                "centro_id": r.centro_resultado_id,
                "plano_id": r.plano_conta_id,
                "valor": f"{r.valor:.2f}".replace(".", ",") if r.valor is not None else "",
                "percentual": (
                    f"{(r.valor / conta.valor_total * 100):.2f}".replace(".", ",")
                    if conta.valor_total
                    else ""
                ),
            }
            for r in rateios_existentes
        ]
    for r in form.get("rateios") or []:
        r.setdefault(
            "centro_label",
            next(
                (f"{c.codigo} - {c.descricao}" for c in centros_resultado if str(c.id) == str(r.get("centro_id"))),
                "",
            ),
        )
        r.setdefault(
            "plano_label",
            next(
                (f"{p.codigo} - {p.descricao}" for p in planos_conta if str(p.id) == str(r.get("plano_id"))),
                "",
            ),
        )

    return render(
        request,
        "finance/conta_a_receber_form.html",
        {
            "form": form,
            "conta": conta,
            "empresas": empresas_choices,
            "pessoas_clientes": pessoas_clientes,
            "tipos_documento": tipos_documento,
            "centros_resultado": centros_resultado,
            "planos_conta": planos_conta,
            "centros_para_js": centros_para_js,
            "planos_para_js": planos_para_js,
            "status_choices": ContaReceber.STATUS_CHOICES,
        },
    )


@login_required
def movimentos_contas_a_receber_excluir(request, conta_id):
    empresas = get_empresas_contexto(request)
    if not empresas:
        messages.warning(request, "Selecione uma empresa no menu.")
        return redirect("dashboard")

    conta = get_object_or_404(
        ContaReceber.objects.select_related("pessoa"), pk=conta_id, empresa__in=empresas
    )

    if not _usuario_tem_permissao_empresa(request, conta.empresa, "excluir"):
        messages.error(request, "Você não tem permissão para excluir esta conta a receber.")
        return redirect("movimentos-contas-a-receber")

    if request.method == "POST":
        try:
            conta.delete()
            messages.success(request, "Conta a receber excluída com sucesso.")
        except ProtectedError:
            messages.error(request, "Não é possível excluir: existem vencimentos ou baixas vinculadas.")
        return redirect("movimentos-contas-a-receber")

    return render(
        request,
        "finance/contas_a_receber_confirmar_exclusao.html",
        {"conta": conta},
    )


# Financeiro
@login_required
def financeiro_lancamentos(request):
    empresas = _get_empresas_tenant(request)
    if empresas is None:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    # Permissão de visualização em pelo menos uma empresa do contexto
    if not _usuario_tem_permissao_alguma_empresa(request, empresas, "visualizar"):
        messages.error(request, "Você não tem permissão para visualizar lançamentos neste contexto.")
        return redirect("dashboard")

    # Mensagem vinda do botão "Novo lançamento" (verificação de permissão)
    erro_novo = (request.GET.get("erro_novo") or "").strip()
    if erro_novo == "permissao":
        messages.error(request, "Você não tem permissão para incluir lançamentos neste contexto.")
    elif erro_novo == "erro":
        messages.error(request, "Não foi possível verificar. Tente novamente.")

    contas_financeiras = list(
        ContaFinanceira.objects.filter(empresa__in=empresas)
        .select_related("banco")
        .order_by("banco__nome", "agencia", "conta")
    )
    centros_resultado = list(
        CentroResultado.objects.filter(empresa__in=empresas, tipo=CentroResultado.TIPO_ANALITICO)
        .order_by("codigo")
    )
    planos_conta = list(
        PlanoConta.objects.filter(empresa__in=empresas, tipo=PlanoConta.TIPO_ANALITICO)
        .order_by("codigo")
    )

    # Estado padrão do modal e do formulário (para GET e para POST sem erro)
    modal_lancamento_aberto = False
    form_lancamento: dict[str, str] = {}

    if request.method == "POST":
        # Criação/edição via modal
        lancamento_id = (request.POST.get("lancamento_id") or "").strip()
        empresa_id = (request.POST.get("empresa_id") or "").strip()
        conta_financeira_id = (request.POST.get("conta_financeira_id") or "").strip()
        data_raw = (request.POST.get("data") or "").strip()
        tipo = (request.POST.get("tipo") or "").strip()
        valor_raw = (request.POST.get("valor") or "").strip()
        observacao = (request.POST.get("observacao") or "").strip()
        # origem será determinada automaticamente pela tabela SisOrigem (código 03)
        centro_id = (request.POST.get("centro_resultado_id") or "").strip()
        plano_id = (request.POST.get("plano_conta_id") or "").strip()

        empresa = (
            empresas.filter(pk=empresa_id).first()
            if empresa_id and empresa_id.isdigit()
            else None
        )

        conta_financeira = (
            ContaFinanceira.objects.filter(pk=conta_financeira_id, empresa__in=empresas)
            .select_related("empresa")
            .first()
            if conta_financeira_id and conta_financeira_id.isdigit()
            else None
        )

        centro = (
            CentroResultado.objects.filter(pk=centro_id, empresa__in=empresas)
            .first()
            if centro_id and centro_id.isdigit()
            else None
        )
        plano = (
            PlanoConta.objects.filter(pk=plano_id, empresa__in=empresas)
            .first()
            if plano_id and plano_id.isdigit()
            else None
        )

        from datetime import datetime as _dt

        data = None
        if data_raw:
            try:
                data = _dt.strptime(data_raw, "%Y-%m-%d").date()
            except ValueError:
                data = None

        valor = _parse_decimal_post(valor_raw)

        # Define a origem automaticamente como "03 - Lançamento"
        # usando a tabela SisOrigem; se não existir, usa um fallback textual.
        from core.models import SisOrigem

        origem_valor = None
        try:
            origem_obj = SisOrigem.objects.filter(codigo="03").first()
        except Exception:
            origem_obj = None

        if origem_obj:
            origem_valor = f"{origem_obj.codigo} - {origem_obj.nome}"
        else:
            origem_valor = "03 - Lançamento"

        # Determina se é inclusão ou edição
        lanc = None
        if lancamento_id and lancamento_id.isdigit():
            lanc = (
                Lancamento.objects.filter(pk=int(lancamento_id), empresa__in=empresas)
                .select_related("empresa")
                .first()
            )

        if lanc is None:
            # inclusão
            if not _usuario_tem_permissao_alguma_empresa(request, empresas, "incluir"):
                messages.error(request, "Você não tem permissão para incluir lançamentos neste contexto.")
                return redirect("financeiro-lancamentos")
        else:
            # edição
            if not _usuario_tem_permissao_empresa(request, lanc.empresa, "editar"):
                messages.error(request, "Você não tem permissão para editar este lançamento.")
                return redirect("financeiro-lancamentos")

        # Preserva os dados preenchidos para reabrir o modal em caso de erro
        form_lancamento = {
            "lancamento_id": lancamento_id,
            "empresa_id": empresa_id,
            "conta_financeira_id": conta_financeira_id,
            "data": data_raw,
            "tipo": tipo,
            "valor": valor_raw,
             "observacao": observacao,
            "centro_resultado_id": centro_id,
            "plano_conta_id": plano_id,
        }

        tem_erro = False

        is_estorno = lanc is not None and (
            getattr(lanc, "baixa_conta_pagar_id", None) is not None
            or getattr(lanc, "baixa_conta_receber_id", None) is not None
        )

        if not empresa:
            messages.error(request, "Selecione a empresa.")
            tem_erro = True
        elif not conta_financeira:
            messages.error(request, "Selecione a conta financeira.")
            tem_erro = True
        elif not data:
            messages.error(request, "Informe uma data válida para o lançamento.")
            tem_erro = True
        elif tipo not in dict(Lancamento.TIPOS).keys():
            messages.error(request, "Selecione um tipo de lançamento válido.")
            tem_erro = True
        elif not valor or valor <= 0:
            messages.error(request, "Informe um valor válido para o lançamento.")
            tem_erro = True
        elif conta_financeira and empresa and conta_financeira.empresa_id != empresa.id:
            messages.error(request, "A conta financeira selecionada não pertence à empresa escolhida.")
            tem_erro = True
        elif not is_estorno and not centro:
            messages.error(request, "Selecione o centro de resultado.")
            tem_erro = True
        elif not is_estorno and not plano:
            messages.error(request, "Selecione o plano de contas.")
            tem_erro = True
        else:
            if lanc is None:
                Lancamento.objects.create(
                    empresa=empresa,
                    conta_financeira=conta_financeira,
                    tipo=tipo,
                    valor=valor,
                    data=data,
                    origem=origem_valor,
                    centro_resultado=centro,
                    plano_conta=plano,
                    observacao=observacao,
                    created_by=request.user,
                    updated_by=request.user,
                )
                messages.success(request, "Lançamento criado com sucesso.")
            else:
                if not is_estorno:
                    # Lançamento normal: permite alterar todos os campos financeiros
                    lanc.empresa = empresa
                    lanc.conta_financeira = conta_financeira
                    lanc.tipo = tipo
                    lanc.valor = valor
                    lanc.origem = origem_valor
                lanc.data = data
                lanc.centro_resultado = centro
                lanc.plano_conta = plano
                lanc.observacao = observacao
                lanc.updated_by = request.user
                lanc.save()
                messages.success(request, "Lançamento atualizado com sucesso.")

            return redirect("financeiro-lancamentos")

        # Se houve erro, reabrir o modal com os dados preenchidos
        if tem_erro:
            modal_lancamento_aberto = True

    # GET: listagem com filtros
    qs = (
        Lancamento.objects.filter(empresa__in=empresas)
        .select_related("empresa", "conta_financeira", "conta_financeira__banco")
        .order_by("-data", "-id")
    )

    data_de = (request.GET.get("data_de") or "").strip()
    data_ate = (request.GET.get("data_ate") or "").strip()
    conta_financeira_id = (request.GET.get("conta_financeira_id") or "").strip()
    tipo = (request.GET.get("tipo") or "").strip()
    origem_codigo = (request.GET.get("origem") or "").strip()

    # Define período padrão: mês atual, se nenhum filtro de data foi informado
    if not data_de and not data_ate:
        from datetime import date as _date, timedelta as _td

        hoje = _date.today()
        primeiro_dia = hoje.replace(day=1)
        if hoje.month == 12:
            proximo_mes = _date(hoje.year + 1, 1, 1)
        else:
            proximo_mes = _date(hoje.year, hoje.month + 1, 1)
        ultimo_dia = proximo_mes - _td(days=1)

        data_de = primeiro_dia.isoformat()
        data_ate = ultimo_dia.isoformat()

    # Filtro por período
    if data_de or data_ate:
        from datetime import datetime as _dt
        try:
            if data_de:
                qs = qs.filter(data__gte=_dt.strptime(data_de, "%Y-%m-%d").date())
            if data_ate:
                qs = qs.filter(data__lte=_dt.strptime(data_ate, "%Y-%m-%d").date())
        except ValueError:
            pass

    if conta_financeira_id and conta_financeira_id.isdigit():
        qs = qs.filter(conta_financeira_id=int(conta_financeira_id))
    if tipo in dict(Lancamento.TIPOS).keys():
        qs = qs.filter(tipo=tipo)
    if origem_codigo in ("03", "06"):
        # Filtra pelo código de origem (ex.: "03 - Lançamento", "06 - Estorno")
        qs = qs.filter(origem__startswith=origem_codigo)

    context = {
        "lancamentos": qs,
        "empresas": empresas,
        "contas_financeiras": contas_financeiras,
        "centros_resultado": centros_resultado,
        "planos_conta": planos_conta,
        "modal_lancamento_aberto": modal_lancamento_aberto,
        "form_lancamento": form_lancamento,
        "filtros": {
            "data_de": data_de,
            "data_ate": data_ate,
            "conta_financeira_id": conta_financeira_id,
            "tipo": tipo,
            "origem": origem_codigo,
        },
    }
    return render(request, "finance/lancamentos_list.html", context)


@login_required
def financeiro_lancamentos_verificar_incluir(request):
    """Retorna JSON: pode_incluir (permissão para incluir lançamento)."""
    empresas = _get_empresas_tenant(request)
    if empresas is None:
        return JsonResponse({"pode_incluir": False})
    if not _usuario_tem_permissao_alguma_empresa(request, empresas, "visualizar"):
        return JsonResponse({"pode_incluir": False})
    pode_incluir = _usuario_tem_permissao_alguma_empresa(request, empresas, "incluir")
    return JsonResponse({"pode_incluir": pode_incluir})


@login_required
def financeiro_lancamentos_excluir(request, lanc_id: int):
    empresas = _get_empresas_tenant(request)
    if empresas is None:
        messages.warning(request, "Acesso restrito a usuários vinculados a uma conta.")
        return redirect("dashboard")

    lanc = get_object_or_404(
        Lancamento.objects.select_related("empresa", "conta_financeira", "conta_financeira__banco"),
        pk=lanc_id,
        empresa__in=empresas,
    )

    if not _usuario_tem_permissao_empresa(request, lanc.empresa, "excluir"):
        messages.error(request, "Você não tem permissão para excluir lançamentos desta empresa.")
        return redirect("financeiro-lancamentos")

    if request.method == "POST":
        lanc.delete()
        messages.success(request, "Lançamento excluído com sucesso.")
        return redirect("financeiro-lancamentos")

    context = {
        "lancamento": lanc,
    }
    return render(request, "finance/lancamento_confirmar_exclusao.html", context)


@login_required
def financeiro_transferencias(request):
    """
    Transferências entre contas financeiras.
    Sempre no contexto da mesma empresa matriz: origem e destino podem ser
    qualquer conta cuja empresa seja a matriz ou uma de suas filiais.
    """
    empresa_matriz = get_empresa_matriz(request)
    if not empresa_matriz:
        messages.warning(request, "Selecione uma empresa no menu para acessar Transferências.")
        return redirect("dashboard")

    empresas = get_empresas_contexto(request)
    if not empresas:
        messages.warning(request, "Selecione uma empresa no menu para acessar Transferências.")
        return redirect("dashboard")

    if not _usuario_tem_permissao_empresa(request, empresa_matriz, "visualizar"):
        messages.error(request, "Você não tem permissão para visualizar transferências.")
        return redirect("dashboard")

    # Mensagem vinda do botão "Nova transferência" (verificação de permissão)
    erro_novo = (request.GET.get("erro_novo") or "").strip()
    if erro_novo == "permissao":
        messages.error(request, "Você não tem permissão para lançar transferências.")
    elif erro_novo == "erro":
        messages.error(request, "Não foi possível verificar. Tente novamente.")

    # Contas elegíveis: todas da matriz + filiais (mesma empresa matriz)
    contas_financeiras = list(
        ContaFinanceira.objects.filter(empresa__in=empresas)
        .select_related("banco", "empresa")
        .order_by("empresa__razao_social", "banco__nome", "agencia", "conta")
    )

    if request.method == "POST":
        conta_origem_id = (request.POST.get("conta_origem_id") or "").strip()
        conta_destino_id = (request.POST.get("conta_destino_id") or "").strip()
        valor_raw = (request.POST.get("valor") or "").strip()
        data_raw = (request.POST.get("data") or "").strip()

        if not _usuario_tem_permissao_empresa(request, empresa_matriz, "incluir"):
            messages.error(request, "Você não tem permissão para lançar transferências.")
            return redirect("financeiro-transferencias")

        valor = _parse_decimal_post(valor_raw)
        if not valor or valor <= 0:
            messages.error(request, "Informe um valor válido para a transferência.")
            return redirect("financeiro-transferencias")

        if not data_raw:
            messages.error(request, "Informe a data da transferência.")
            return redirect("financeiro-transferencias")

        from datetime import datetime as _dt
        try:
            data = _dt.strptime(data_raw, "%Y-%m-%d").date()
        except ValueError:
            messages.error(request, "Data da transferência inválida.")
            return redirect("financeiro-transferencias")

        if not conta_origem_id or not conta_origem_id.isdigit():
            messages.error(request, "Selecione a conta de origem.")
            return redirect("financeiro-transferencias")
        if not conta_destino_id or not conta_destino_id.isdigit():
            messages.error(request, "Selecione a conta de destino.")
            return redirect("financeiro-transferencias")

        if conta_origem_id == conta_destino_id:
            messages.error(request, "A conta de origem e a de destino devem ser diferentes.")
            return redirect("financeiro-transferencias")

        conta_origem = ContaFinanceira.objects.filter(
            pk=int(conta_origem_id), empresa__in=empresas
        ).select_related("empresa", "banco").first()
        conta_destino = ContaFinanceira.objects.filter(
            pk=int(conta_destino_id), empresa__in=empresas
        ).select_related("empresa", "banco").first()

        if not conta_origem:
            messages.error(request, "Conta de origem inválida.")
            return redirect("financeiro-transferencias")
        if not conta_destino:
            messages.error(request, "Conta de destino inválida.")
            return redirect("financeiro-transferencias")

        from core.models import SisOrigem
        origem_obj = SisOrigem.objects.filter(codigo="05").first()
        origem_valor = f"{origem_obj.codigo} - {origem_obj.nome}" if origem_obj else "05 - Transferência"

        trans = Transferencia.objects.create(
            empresa=empresa_matriz,
            conta_origem=conta_origem,
            conta_destino=conta_destino,
            valor=valor,
            data=data,
            created_by=request.user,
            updated_by=request.user,
        )

        Lancamento.objects.create(
            empresa=conta_origem.empresa,
            conta_financeira=conta_origem,
            tipo=Lancamento.TIPO_DEBITO,
            valor=valor,
            data=data,
            origem=origem_valor,
            centro_resultado=None,
            plano_conta=None,
            transferencia=trans,
            observacao=f"Transferência para {conta_destino.banco.nome} – {conta_destino.agencia}/{conta_destino.conta}",
            created_by=request.user,
            updated_by=request.user,
        )
        Lancamento.objects.create(
            empresa=conta_destino.empresa,
            conta_financeira=conta_destino,
            tipo=Lancamento.TIPO_CREDITO,
            valor=valor,
            data=data,
            origem=origem_valor,
            centro_resultado=None,
            plano_conta=None,
            transferencia=trans,
            observacao=f"Transferência de {conta_origem.banco.nome} – {conta_origem.agencia}/{conta_origem.conta}",
            created_by=request.user,
            updated_by=request.user,
        )

        messages.success(request, "Transferência realizada com sucesso.")
        return redirect("financeiro-transferencias")

    # GET: listar transferências do contexto da empresa matriz (com filtros)
    from datetime import date as _date
    from datetime import datetime as _dt
    hoje_iso = _date.today().isoformat()

    data_de = (request.GET.get("data_de") or "").strip()
    data_ate = (request.GET.get("data_ate") or "").strip()
    filtro_conta_origem_id = (request.GET.get("conta_origem_id") or "").strip()
    filtro_conta_destino_id = (request.GET.get("conta_destino_id") or "").strip()

    qs = (
        Transferencia.objects.filter(empresa=empresa_matriz)
        .select_related(
            "conta_origem",
            "conta_origem__banco",
            "conta_origem__empresa",
            "conta_destino",
            "conta_destino__banco",
            "conta_destino__empresa",
        )
        .order_by("-data", "-id")
    )

    if data_de:
        try:
            qs = qs.filter(data__gte=_dt.strptime(data_de, "%Y-%m-%d").date())
        except ValueError:
            pass
    if data_ate:
        try:
            qs = qs.filter(data__lte=_dt.strptime(data_ate, "%Y-%m-%d").date())
        except ValueError:
            pass
    if filtro_conta_origem_id and filtro_conta_origem_id.isdigit():
        qs = qs.filter(conta_origem_id=int(filtro_conta_origem_id))
    if filtro_conta_destino_id and filtro_conta_destino_id.isdigit():
        qs = qs.filter(conta_destino_id=int(filtro_conta_destino_id))

    context = {
        "transferencias": qs,
        "contas_financeiras": contas_financeiras,
        "hoje_iso": hoje_iso,
        "filtros": {
            "data_de": data_de,
            "data_ate": data_ate,
            "conta_origem_id": filtro_conta_origem_id,
            "conta_destino_id": filtro_conta_destino_id,
        },
        "selected_conta_origem_id": int(filtro_conta_origem_id) if filtro_conta_origem_id and filtro_conta_origem_id.isdigit() else None,
        "selected_conta_destino_id": int(filtro_conta_destino_id) if filtro_conta_destino_id and filtro_conta_destino_id.isdigit() else None,
    }
    return render(request, "finance/transferencias_list.html", context)


@login_required
def financeiro_transferencias_verificar_incluir(request):
    """Retorna JSON: pode_incluir (permissão para incluir transferência)."""
    empresa_matriz = get_empresa_matriz(request)
    if not empresa_matriz:
        return JsonResponse({"pode_incluir": False})
    empresas = get_empresas_contexto(request)
    if not empresas:
        return JsonResponse({"pode_incluir": False})
    if not _usuario_tem_permissao_empresa(request, empresa_matriz, "visualizar"):
        return JsonResponse({"pode_incluir": False})
    pode_incluir = _usuario_tem_permissao_empresa(request, empresa_matriz, "incluir")
    return JsonResponse({"pode_incluir": pode_incluir})


@login_required
def financeiro_transferencias_editar(request, transferencia_id):
    empresa_matriz = get_empresa_matriz(request)
    if not empresa_matriz:
        messages.warning(request, "Selecione uma empresa no menu.")
        return redirect("dashboard")
    empresas = get_empresas_contexto(request)
    if not empresas:
        return redirect("dashboard")
    if not _usuario_tem_permissao_empresa(request, empresa_matriz, "editar"):
        messages.error(request, "Você não tem permissão para editar transferências.")
        return redirect("financeiro-transferencias")

    trans = get_object_or_404(
        Transferencia.objects.select_related(
            "conta_origem",
            "conta_origem__banco",
            "conta_origem__empresa",
            "conta_destino",
            "conta_destino__banco",
            "conta_destino__empresa",
        ),
        pk=transferencia_id,
        empresa=empresa_matriz,
    )

    contas_financeiras = list(
        ContaFinanceira.objects.filter(empresa__in=empresas)
        .select_related("banco", "empresa")
        .order_by("empresa__razao_social", "banco__nome", "agencia", "conta")
    )

    if request.method == "POST":
        conta_origem_id = (request.POST.get("conta_origem_id") or "").strip()
        conta_destino_id = (request.POST.get("conta_destino_id") or "").strip()
        valor_raw = (request.POST.get("valor") or "").strip()
        data_raw = (request.POST.get("data") or "").strip()

        valor = _parse_decimal_post(valor_raw)
        if not valor or valor <= 0:
            messages.error(request, "Informe um valor válido.")
            return redirect("financeiro-transferencias-editar", transferencia_id=trans.id)
        from datetime import datetime as _dt
        try:
            data = _dt.strptime(data_raw, "%Y-%m-%d").date()
        except ValueError:
            messages.error(request, "Data inválida.")
            return redirect("financeiro-transferencias-editar", transferencia_id=trans.id)
        if not conta_origem_id or not conta_origem_id.isdigit() or not conta_destino_id or not conta_destino_id.isdigit():
            messages.error(request, "Selecione conta de origem e de destino.")
            return redirect("financeiro-transferencias-editar", transferencia_id=trans.id)
        if conta_origem_id == conta_destino_id:
            messages.error(request, "Conta de origem e de destino devem ser diferentes.")
            return redirect("financeiro-transferencias-editar", transferencia_id=trans.id)

        conta_origem = ContaFinanceira.objects.filter(pk=int(conta_origem_id), empresa__in=empresas).select_related("empresa", "banco").first()
        conta_destino = ContaFinanceira.objects.filter(pk=int(conta_destino_id), empresa__in=empresas).select_related("empresa", "banco").first()
        if not conta_origem or not conta_destino:
            messages.error(request, "Conta de origem ou destino inválida.")
            return redirect("financeiro-transferencias-editar", transferencia_id=trans.id)

        from core.models import SisOrigem
        origem_obj = SisOrigem.objects.filter(codigo="05").first()
        origem_valor = f"{origem_obj.codigo} - {origem_obj.nome}" if origem_obj else "05 - Transferência"

        trans.conta_origem = conta_origem
        trans.conta_destino = conta_destino
        trans.valor = valor
        trans.data = data
        trans.updated_by = request.user
        trans.save()

        lancamentos = list(trans.lancamentos.all().order_by("tipo"))
        for lanc in lancamentos:
            if lanc.tipo == Lancamento.TIPO_DEBITO:
                lanc.empresa = conta_origem.empresa
                lanc.conta_financeira = conta_origem
                lanc.valor = valor
                lanc.data = data
                lanc.observacao = f"Transferência para {conta_destino.banco.nome} – {conta_destino.agencia}/{conta_destino.conta}"
            else:
                lanc.empresa = conta_destino.empresa
                lanc.conta_financeira = conta_destino
                lanc.valor = valor
                lanc.data = data
                lanc.observacao = f"Transferência de {conta_origem.banco.nome} – {conta_origem.agencia}/{conta_origem.conta}"
            lanc.updated_by = request.user
            lanc.save()

        messages.success(request, "Transferência atualizada com sucesso.")
        return redirect("financeiro-transferencias")

    form = {
        "conta_origem_id": trans.conta_origem_id,
        "conta_destino_id": trans.conta_destino_id,
        "valor": f"{trans.valor:.2f}".replace(".", ","),
        "data": trans.data.isoformat() if trans.data else "",
    }
    return render(
        request,
        "finance/transferencia_editar.html",
        {"transferencia": trans, "contas_financeiras": contas_financeiras, "form": form},
    )


@login_required
def financeiro_transferencias_excluir(request, transferencia_id):
    empresa_matriz = get_empresa_matriz(request)
    if not empresa_matriz:
        messages.warning(request, "Selecione uma empresa no menu.")
        return redirect("dashboard")
    if not _usuario_tem_permissao_empresa(request, empresa_matriz, "excluir"):
        messages.error(request, "Você não tem permissão para excluir transferências.")
        return redirect("financeiro-transferencias")

    trans = get_object_or_404(
        Transferencia.objects.select_related(
            "conta_origem",
            "conta_origem__banco",
            "conta_origem__empresa",
            "conta_destino",
            "conta_destino__banco",
            "conta_destino__empresa",
        ),
        pk=transferencia_id,
        empresa=empresa_matriz,
    )

    if request.method == "POST":
        trans.delete()
        messages.success(request, "Transferência excluída com sucesso.")
        return redirect("financeiro-transferencias")

    return render(request, "finance/transferencia_confirmar_exclusao.html", {"transferencia": trans})


@login_required
def financeiro_saldo_bancario(request):
    return _placeholder_view(request, "Saldo Bancário")


# Relatórios
@login_required
def relatorios_conciliacao_bancaria(request):
    return _placeholder_view(request, "Conciliação Bancária")


@login_required
def relatorios_fluxo_caixa(request):
    return _placeholder_view(request, "Fluxo de caixa")


@login_required
def relatorios_orcamento(request):
    return _placeholder_view(request, "Orçamento")


@login_required
def relatorios_dre(request):
    return _placeholder_view(request, "DRE")
