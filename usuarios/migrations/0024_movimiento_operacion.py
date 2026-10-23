import uuid
from datetime import timedelta

from django.db import migrations, models


def agrupar_existentes(apps, schema_editor):
    """
    Las compras, ventas y swaps previos no tienen 'operacion'. Sus dos patas se crean
    una después de la otra en la misma transacción: se emparejan por cercanía.
    """
    Movimiento = apps.get_model('usuarios', 'Movimiento')
    qs = (Movimiento.objects
          .filter(operacion__isnull=True, tipo__in=('compra', 'venta'))
          .exclude(monto=0)
          .order_by('usuario_id', 'id'))
    anterior = None
    for m in qs.iterator():
        if (anterior is not None
                and anterior.usuario_id == m.usuario_id
                and anterior.moneda != m.moneda
                and (anterior.monto > 0) != (m.monto > 0)
                and abs(m.fecha - anterior.fecha) <= timedelta(seconds=5)):
            op = uuid.uuid4()
            Movimiento.objects.filter(pk__in=(anterior.pk, m.pk)).update(operacion=op)
            anterior = None
            continue
        anterior = m


class Migration(migrations.Migration):

    dependencies = [
        ('usuarios', '0023_compliance_cotizaciones_onchain'),
    ]

    operations = [
        migrations.AddField(
            model_name='movimiento',
            name='operacion',
            field=models.UUIDField(blank=True, db_index=True, null=True),
        ),
        migrations.RunPython(agrupar_existentes, migrations.RunPython.noop),
    ]
