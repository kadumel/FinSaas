from django.contrib import admin

from core.models import Estado, Cidade, SisConfig, SisOrigem

@admin.register(Estado)
class EstadoAdmin(admin.ModelAdmin):
    list_display = ("nome", "uf")
    search_fields = ("nome", "uf")
    list_filter = ("uf",)
    ordering = ("nome",)
    list_per_page = 10
    list_max_show_all = 100
    list_editable = ("uf",)
    list_display_links = ("nome",)

@admin.register(Cidade)
class CidadeAdmin(admin.ModelAdmin):
    list_display = ("nome", "estado")
    search_fields = ("nome", "estado")
    list_filter = ("estado",)
    ordering = ("nome",)
    list_per_page = 10
    list_max_show_all = 100
    list_editable = ("estado",)
    list_display_links = ("nome",)
    list_select_related = ("estado",)


@admin.register(SisConfig)
class SisConfigAdmin(admin.ModelAdmin):
    list_display = ("chave", "descricao", "tipo", "origem", "ativo")
    search_fields = ("chave", "descricao")
    list_filter = ("tipo", "origem", "ativo")
    ordering = ("chave",)
    list_per_page = 10
    list_max_show_all = 100
    list_editable = ("ativo",)
    list_display_links = ("chave",)


@admin.register(SisOrigem)
class SisOrigemAdmin(admin.ModelAdmin):
    list_display = ("codigo", "nome")
    search_fields = ("codigo", "nome")
    ordering = ("codigo",)
    list_per_page = 10
    list_max_show_all = 100