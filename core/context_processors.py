"""
Context processor e helpers para empresa matriz (sessão).
Todo o sistema usa a empresa matriz selecionada e suas filiais como contexto.
As empresas no filtro são apenas aquelas às quais o usuário tem acesso via perfis (PermissaoEmpresa).
"""
from django.db.models import Q

from .models import Empresa


SESSION_EMPRESA_MATRIZ_ID = "empresa_matriz_id"


def get_matrizes_para_select(request):
    """
    Retorna lista de empresas matriz que o usuário pode escolher no filtro:
    - Admin da conta (is_tenant_admin): todas as matrizes ativas do tenant.
    - Demais: apenas matrizes das empresas que aparecem em PermissaoEmpresa
      em algum perfil vinculado ao usuário (matriz direta ou matriz da filial).
    """
    if not getattr(request.user, "is_authenticated", False) or getattr(request.user, "is_superuser", False):
        return []
    tenant = getattr(request.user, "tenant", None)
    if not tenant:
        return []

    # Admin da conta vê todas as matrizes do tenant
    if getattr(request.user, "is_tenant_admin", False):
        return list(tenant.empresas.filter(tipo=Empresa.TIPO_MATRIZ, ativo=True).order_by("razao_social"))

    # Usuário com perfil: apenas empresas que têm alguma permissão (visualizar/incluir/editar/excluir/aprovar)
    perfil_ids = request.user.perfis.values_list("perfil_id", flat=True)
    if not perfil_ids:
        return []

    from accounts.models import PermissaoEmpresa
    empresa_ids = (
        PermissaoEmpresa.objects.filter(perfil_id__in=perfil_ids)
        .filter(
            Q(pode_visualizar=True)
            | Q(pode_incluir=True)
            | Q(pode_editar=True)
            | Q(pode_excluir=True)
            | Q(pode_aprovar=True)
        )
        .values_list("empresa_id", flat=True)
        .distinct()
    )
    if not empresa_ids:
        return []

    # Para cada empresa permitida, obter a matriz (a própria se for matriz, senão empresa_matriz)
    empresas = Empresa.objects.filter(pk__in=empresa_ids, ativo=True).select_related("empresa_matriz")
    matriz_ids = set()
    for emp in empresas:
        if emp.tipo == Empresa.TIPO_MATRIZ:
            matriz_ids.add(emp.pk)
        elif emp.empresa_matriz_id and emp.empresa_matriz.ativo:
            matriz_ids.add(emp.empresa_matriz_id)

    if not matriz_ids:
        return []
    return list(Empresa.objects.filter(pk__in=matriz_ids, ativo=True).order_by("razao_social"))


def get_empresa_matriz(request):
    """
    Retorna a empresa matriz do contexto atual (da sessão).
    Só considera matrizes que o usuário pode ver (get_matrizes_para_select).
    Se não houver na sessão ou for inválida, define a primeira permitida e retorna.
    Retorna None se o usuário não tiver tenant ou não tiver nenhuma empresa permitida.
    """
    if not getattr(request.user, "is_authenticated", False) or getattr(request.user, "is_superuser", False):
        return None
    tenant = getattr(request.user, "tenant", None)
    if not tenant:
        return None

    matrizes = get_matrizes_para_select(request)
    if not matrizes:
        return None

    matriz_id = request.session.get(SESSION_EMPRESA_MATRIZ_ID)
    if matriz_id:
        for m in matrizes:
            if m.pk == matriz_id:
                return m
    # Define a primeira matriz permitida na sessão
    primeira = matrizes[0]
    request.session[SESSION_EMPRESA_MATRIZ_ID] = primeira.pk
    request.session.modified = True
    return primeira


def get_empresas_contexto(request):
    """
    Retorna queryset de empresas do contexto: a matriz selecionada + suas filiais ativas.
    Usado nas views para filtrar dados (pessoas, etc.) pela empresa matriz e filiais.
    Retorna None se não houver empresa matriz definida ou não permitida ao usuário.
    """
    matriz = get_empresa_matriz(request)
    if not matriz:
        return None
    # Matriz + filiais ativas
    return Empresa.objects.filter(ativo=True).filter(
        Q(pk=matriz.pk) | Q(empresa_matriz_id=matriz.pk)
    ).order_by("empresa_matriz_id", "razao_social")


def empresa_matriz_context(request):
    """Context processor: adiciona empresa_matriz e matrizes ao context de todos os templates."""
    context = {"empresa_matriz": None, "matrizes": []}
    if getattr(request.user, "is_authenticated", False) and not getattr(request.user, "is_superuser", False):
        context["matrizes"] = get_matrizes_para_select(request)
        context["empresa_matriz"] = get_empresa_matriz(request)
    return context
