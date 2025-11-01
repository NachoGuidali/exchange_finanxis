from django.http import JsonResponse, Http404

from .models import Pais, Provincia, Localidad
from django.views.decorators.http import require_GET

@require_GET
def geo_paises(request):
    qs = Pais.objects.all().values("id","nombre","iso2").order_by("nombre")
    return JsonResponse({"paises": list(qs)})

@require_GET
def geo_provincias(request):
    pais_id = request.GET.get("pais_id")
    if not pais_id:
        return JsonResponse({"provincias": []})
    qs = Provincia.objects.filter(pais_id=pais_id).order_by("nombre")
    data = [{"id": p.id, "nombre": p.nombre} for p in qs]
    return JsonResponse({"provincias": data})

@require_GET
def geo_localidades(request):
    prov_id = request.GET.get("provincia_id")
    if not prov_id:
        return JsonResponse({"localidades": []})
    qs = Localidad.objects.filter(provincia_id=prov_id).order_by("nombre")
    data = [{"id": l.id, "nombre": l.nombre} for l in qs]
    return JsonResponse({"localidades": data})