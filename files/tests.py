import random
import xml.etree.ElementTree as ET

from django.test import TestCase

from .gpx import GpxAnonymizeOptions, anonymize_gpx


SAMPLE_GPX = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test">
  <trk>
    <name>Test</name>
    <trkseg>
      <trkpt lat="0.0" lon="0.0"><ele>5</ele><time>2024-01-01T00:00:00Z</time></trkpt>
      <trkpt lat="0.0" lon="0.001"><ele>6</ele><time>2024-01-01T00:01:00Z</time></trkpt>
      <trkpt lat="0.0" lon="0.002"><ele>7</ele><time>2024-01-01T00:02:00Z</time></trkpt>
      <trkpt lat="0.0" lon="0.003"><ele>8</ele><time>2024-01-01T00:03:00Z</time></trkpt>
    </trkseg>
  </trk>
</gpx>
"""


class GpxAnonymizeTests(TestCase):
    def test_trim_removes_start_end_points(self):
        options = GpxAnonymizeOptions(trim_enabled=True, trim_distance_m=100)
        output = anonymize_gpx(SAMPLE_GPX, options)
        xml = ET.fromstring(output)
        points = xml.findall(".//trkpt")
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0].get("lon"), "0.001")
        self.assertEqual(points[1].get("lon"), "0.002")

    def test_remove_timestamps_strips_time_elements(self):
        options = GpxAnonymizeOptions(
            trim_enabled=False, remove_timestamps=True
        )
        output = anonymize_gpx(SAMPLE_GPX, options)
        xml = ET.fromstring(output)
        self.assertEqual(len(xml.findall(".//time")), 0)

    def test_blur_offsets_coordinates(self):
        rng = random.Random(0)
        options = GpxAnonymizeOptions(trim_enabled=False, blur_enabled=True)
        output = anonymize_gpx(SAMPLE_GPX, options, rng=rng)
        xml = ET.fromstring(output)
        points = xml.findall(".//trkpt")
        self.assertEqual(len(points), 4)
        lons = [float(point.get("lon")) for point in points]
        self.assertTrue(any(lon != original for lon, original in zip(lons, [0.0, 0.001, 0.002, 0.003])))
