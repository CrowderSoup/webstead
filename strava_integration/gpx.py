"""
Build a minimal GPX 1.1 document from Strava activity stream data so it can
be run through the existing `files.gpx.anonymize_gpx()` privacy pipeline and
attached to a post exactly like a manually-uploaded track.
"""

from datetime import timedelta, timezone as dt_timezone
from xml.etree.ElementTree import Element, SubElement, tostring

GPX_NS = "http://www.topografix.com/GPX/1/1"


def streams_to_gpx(latlng_stream, altitude_stream, time_stream, start_date, name="") -> bytes:
    """
    latlng_stream:   list of [lat, lon] pairs (required)
    altitude_stream: list of floats in meters, same length as latlng_stream, or None
    time_stream:     list of ints (seconds offset from start), same length, or None
    start_date:      timezone-aware datetime — the activity's start time
    """
    if not latlng_stream:
        raise ValueError("latlng_stream is required to build a GPX track")

    gpx = Element("gpx", {
        "version": "1.1",
        "creator": "webstead-strava-integration",
        "xmlns": GPX_NS,
    })
    trk = SubElement(gpx, "trk")
    if name:
        SubElement(trk, "name").text = name
    trkseg = SubElement(trk, "trkseg")

    for index, point in enumerate(latlng_stream):
        if not point or len(point) != 2:
            continue
        lat, lon = point
        trkpt = SubElement(trkseg, "trkpt", {"lat": f"{lat:.7f}", "lon": f"{lon:.7f}"})

        if altitude_stream and index < len(altitude_stream) and altitude_stream[index] is not None:
            SubElement(trkpt, "ele").text = str(altitude_stream[index])

        if time_stream and index < len(time_stream) and time_stream[index] is not None:
            point_time = start_date + timedelta(seconds=time_stream[index])
            SubElement(trkpt, "time").text = point_time.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return tostring(gpx, encoding="utf-8", xml_declaration=True)
