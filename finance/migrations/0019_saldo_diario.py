# Saldo diário por conta financeira (credito, debito, saldo)

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0018_add_lancamento_transferencia_fk"),
    ]

    operations = [
        migrations.DeleteModel(
            name="Saldo",
        ),
        migrations.CreateModel(
            name="Saldo",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("data", models.DateField()),
                ("credito", models.DecimalField(decimal_places=2, default=0, max_digits=15)),
                ("debito", models.DecimalField(decimal_places=2, default=0, max_digits=15)),
                ("saldo", models.DecimalField(decimal_places=2, default=0, max_digits=15)),
                (
                    "conta_financeira",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="saldos_diarios",
                        to="finance.contafinanceira",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_updated",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "fin_saldo",
                "ordering": ["conta_financeira", "data"],
            },
        ),
        migrations.AddConstraint(
            model_name="saldo",
            constraint=models.UniqueConstraint(
                fields=("conta_financeira", "data"),
                name="fin_saldo_conta_data_uniq",
            ),
        ),
    ]
