from django.conf import settings


def demo(request):
    return {"DEMO_MODE": getattr(settings, "DEMO_MODE", False)}
