import requests

def get_client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")

def geolocate_ip(ip):
    if not ip:
        return {}
    try:
        r = requests.get(f"https://ipapi.co/{ip}/json/", timeout=0.5)
        data = r.json()
        return {
            "country": data.get("country_code"),
            "region": data.get("region"),
            "city": data.get("city"),
        }
    except Exception:
        return {}
