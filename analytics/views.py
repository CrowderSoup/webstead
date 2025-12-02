import json
from django.http import HttpResponse
from django.utils.timezone import now
from django.views.decorators.csrf import csrf_exempt
from .models import Visit

@csrf_exempt
def beacon_leave(request):
    if request.method != "POST":
        return HttpResponse(status=405)

    try:
        body = request.body.decode("utf-8")
        data = json.loads(body)
        visit_id = data.get("visit_id")
        ts = data.get("ts")

        if not visit_id:
            return HttpResponse(status=400)

        visit = Visit.objects.filter(id=visit_id).first()
        if not visit:
            return HttpResponse(status=404)

        visit.ended_at = now()
        if visit.started_at:
            visit.duration_seconds = int((visit.ended_at - visit.started_at).total_seconds())
        visit.save()

    except Exception:
        return HttpResponse(status=400)

    return HttpResponse(status=204)
