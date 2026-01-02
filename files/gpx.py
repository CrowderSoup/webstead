import math
import random
import xml.etree.ElementTree as ET
from dataclasses import dataclass


class GpxAnonymizeError(ValueError):
    pass


@dataclass(frozen=True)
class GpxAnonymizeOptions:
    trim_enabled: bool = True
    trim_distance_m: float = 500.0
    blur_enabled: bool = False
    blur_min_m: float = 5.0
    blur_max_m: float = 20.0
    remove_timestamps: bool = False


def anonymize_gpx(gpx_bytes, options, rng=None):
    if not gpx_bytes:
        raise GpxAnonymizeError("Empty GPX payload.")
    try:
        root = ET.fromstring(gpx_bytes)
    except ET.ParseError as exc:
        raise GpxAnonymizeError("Invalid GPX XML.") from exc

    rng = rng or random.Random()
    points = _collect_points(root)
    if not points:
        return _serialize_gpx(root)

    if options.trim_enabled:
        points = _trim_points(points, options.trim_distance_m)

    if options.remove_timestamps:
        for point in points:
            _strip_timestamp(point["element"])

    if options.blur_enabled:
        for point in points:
            _blur_point(point["element"], rng, options.blur_min_m, options.blur_max_m)

    return _serialize_gpx(root)


def _collect_points(root):
    points = []
    for parent in root.iter():
        for child in list(parent):
            if child.tag.endswith("trkpt") or child.tag.endswith("rtept"):
                lat, lon = _point_coords(child)
                if lat is None or lon is None:
                    continue
                points.append(
                    {
                        "parent": parent,
                        "element": child,
                        "lat": lat,
                        "lon": lon,
                    }
                )
    return points


def _trim_points(points, trim_distance_m):
    if trim_distance_m <= 0 or len(points) < 2:
        return points

    distances = _cumulative_distances(points)
    total_distance = distances[-1]
    if total_distance <= 0:
        return points

    if total_distance <= 2 * trim_distance_m:
        trim_distance_m = total_distance / 4

    start_index = 0
    for i, distance in enumerate(distances):
        if distance >= trim_distance_m:
            start_index = i
            break

    end_index = len(points) - 1
    for i in range(len(points) - 1, -1, -1):
        if total_distance - distances[i] >= trim_distance_m:
            end_index = i
            break

    if start_index >= end_index:
        return points

    keep = {points[i]["element"] for i in range(start_index, end_index + 1)}
    for point in points:
        if point["element"] not in keep:
            point["parent"].remove(point["element"])

    return [point for point in points if point["element"] in keep]


def _cumulative_distances(points):
    distances = [0.0]
    for i in range(1, len(points)):
        prev = points[i - 1]
        curr = points[i]
        distances.append(distances[-1] + _haversine(prev, curr))
    return distances


def _haversine(a, b):
    radius = 6371000
    lat1 = math.radians(a["lat"])
    lat2 = math.radians(b["lat"])
    dlat = lat2 - lat1
    dlon = math.radians(b["lon"] - a["lon"])
    sin_lat = math.sin(dlat / 2)
    sin_lon = math.sin(dlon / 2)
    h = sin_lat * sin_lat + math.cos(lat1) * math.cos(lat2) * sin_lon * sin_lon
    return 2 * radius * math.asin(math.sqrt(h))


def _strip_timestamp(point):
    for child in list(point):
        if child.tag.endswith("time"):
            point.remove(child)


def _blur_point(point, rng, min_m, max_m):
    lat, lon = _point_coords(point)
    if lat is None or lon is None:
        return
    min_m = max(0.0, min_m)
    max_m = max(min_m, max_m)
    distance = rng.uniform(min_m, max_m)
    bearing = rng.uniform(0, 2 * math.pi)
    new_lat, new_lon = _offset_lat_lon(lat, lon, distance, bearing)
    point.set("lat", f"{new_lat:.7f}")
    point.set("lon", f"{new_lon:.7f}")


def _offset_lat_lon(lat, lon, distance_m, bearing_rad):
    radius = 6371000
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)
    delta = distance_m / radius
    new_lat = math.asin(
        math.sin(lat_rad) * math.cos(delta)
        + math.cos(lat_rad) * math.sin(delta) * math.cos(bearing_rad)
    )
    new_lon = lon_rad + math.atan2(
        math.sin(bearing_rad) * math.sin(delta) * math.cos(lat_rad),
        math.cos(delta) - math.sin(lat_rad) * math.sin(new_lat),
    )
    return math.degrees(new_lat), math.degrees(new_lon)


def _point_coords(point):
    try:
        lat = float(point.get("lat"))
        lon = float(point.get("lon"))
    except (TypeError, ValueError):
        return None, None
    if math.isnan(lat) or math.isnan(lon):
        return None, None
    return lat, lon


def _serialize_gpx(root):
    tree = ET.ElementTree(root)
    return ET.tostring(tree.getroot(), encoding="utf-8", xml_declaration=True)
