from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterable, Tuple

import requests
from PIL import Image, ImageDraw


@dataclass
class FlyoverStyle:
    width: int = 1280
    height: int = 720
    fps: int = 24
    duration_seconds: int = 9
    padding_ratio: float = 0.12
    zoom_ratio: float = 0.65
    line_color: tuple[int, int, int] = (230, 116, 64)
    glow_color: tuple[int, int, int] = (255, 202, 154)
    base_color: tuple[int, int, int] = (255, 244, 232)
    accent_color: tuple[int, int, int] = (49, 32, 26)
    grid_color: tuple[int, int, int] = (230, 215, 200)


def fetch_remote_gpx(url: str) -> bytes | None:
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
    except requests.RequestException:
        return None
    return response.content


def generate_flyover_video(gpx_bytes: bytes) -> Tuple[bytes, str]:
    style = FlyoverStyle()
    points = _parse_gpx_points(gpx_bytes)
    if len(points) < 2:
        raise ValueError("GPX track must include at least 2 points.")

    points = _downsample(points, 1800)
    projected = [_project_point(lat, lon) for lat, lon in points]
    normalized, bounds = _normalize_points(projected, style.padding_ratio)
    distances = _cumulative_distances(normalized)
    total_distance = distances[-1] if distances else 0.0

    frame_count = style.duration_seconds * style.fps
    frames = []

    for frame_index in range(frame_count):
        progress = frame_index / (frame_count - 1)
        target_distance = total_distance * progress
        idx = _find_index(distances, target_distance)
        frame = _render_frame(
            normalized,
            idx,
            progress,
            bounds,
            style,
        )
        frames.append(frame)

    video_bytes = _encode_video(frames, style)
    return video_bytes, "activity-flyover.mp4"


def _parse_gpx_points(gpx_bytes: bytes) -> list[tuple[float, float]]:
    root = ET.fromstring(gpx_bytes)
    points: list[tuple[float, float]] = []
    for trkpt in root.findall(".//{*}trkpt"):
        lat = trkpt.attrib.get("lat")
        lon = trkpt.attrib.get("lon")
        if lat is None or lon is None:
            continue
        points.append((float(lat), float(lon)))
    return points


def _downsample(points: list[tuple[float, float]], max_points: int) -> list[tuple[float, float]]:
    if len(points) <= max_points:
        return points
    step = max(1, len(points) // max_points)
    return points[::step]


def _project_point(lat: float, lon: float) -> tuple[float, float]:
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)
    x = lon_rad
    y = math.log(math.tan(math.pi / 4 + lat_rad / 2))
    return (x, y)


def _normalize_points(
    points: list[tuple[float, float]], padding_ratio: float
) -> tuple[list[tuple[float, float]], tuple[float, float, float, float]]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    span_x = max_x - min_x
    span_y = max_y - min_y
    pad_x = span_x * padding_ratio or 0.01
    pad_y = span_y * padding_ratio or 0.01

    min_x -= pad_x
    max_x += pad_x
    min_y -= pad_y
    max_y += pad_y

    span_x = max_x - min_x
    span_y = max_y - min_y

    normalized = [((x - min_x) / span_x, (y - min_y) / span_y) for x, y in points]
    return normalized, (0.0, 1.0, 0.0, 1.0)


def _cumulative_distances(points: list[tuple[float, float]]) -> list[float]:
    distances = [0.0]
    for i in range(1, len(points)):
        prev = points[i - 1]
        cur = points[i]
        distances.append(distances[-1] + _distance(prev, cur))
    return distances


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    dx = a[0] - b[0]
    dy = a[1] - b[1]
    return math.hypot(dx, dy)


def _find_index(distances: list[float], target: float) -> int:
    if not distances:
        return 0
    for i, distance in enumerate(distances):
        if distance >= target:
            return i
    return len(distances) - 1


def _render_frame(
    points: list[tuple[float, float]],
    index: int,
    progress: float,
    bounds: tuple[float, float, float, float],
    style: FlyoverStyle,
) -> Image.Image:
    image = Image.new("RGB", (style.width, style.height), style.base_color)
    draw = ImageDraw.Draw(image, "RGBA")

    _draw_background(draw, style)

    center = points[index]
    viewport = _viewport(center, style, bounds)

    screen_points = [_to_screen(point, viewport, style) for point in points]
    progress_points = screen_points[: max(2, index + 1)]

    _draw_route(draw, screen_points, progress_points, style)
    _draw_marker(draw, progress_points[-1], style, progress)
    _draw_heading(draw, style)

    return image


def _draw_background(draw: ImageDraw.ImageDraw, style: FlyoverStyle) -> None:
    for i in range(style.height):
        shade = int(12 * (i / style.height))
        color = (
            max(0, style.base_color[0] - shade),
            max(0, style.base_color[1] - shade),
            max(0, style.base_color[2] - shade),
        )
        draw.line([(0, i), (style.width, i)], fill=color)

    grid_spacing = 80
    for x in range(0, style.width, grid_spacing):
        draw.line([(x, 0), (x, style.height)], fill=style.grid_color + (70,))
    for y in range(0, style.height, grid_spacing):
        draw.line([(0, y), (style.width, y)], fill=style.grid_color + (70,))


def _draw_route(
    draw: ImageDraw.ImageDraw,
    full_route: list[tuple[float, float]],
    progress_route: list[tuple[float, float]],
    style: FlyoverStyle,
) -> None:
    if len(full_route) >= 2:
        draw.line(full_route, fill=style.grid_color + (160,), width=6, joint="curve")

    if len(progress_route) >= 2:
        draw.line(progress_route, fill=style.glow_color + (120,), width=16, joint="curve")
        draw.line(progress_route, fill=style.line_color + (255,), width=6, joint="curve")


def _draw_marker(
    draw: ImageDraw.ImageDraw, point: tuple[float, float], style: FlyoverStyle, progress: float
) -> None:
    x, y = point
    pulse = 10 + int(6 * math.sin(progress * math.pi * 2))
    draw.ellipse(
        [(x - pulse, y - pulse), (x + pulse, y + pulse)],
        fill=style.glow_color + (120,),
    )
    draw.ellipse(
        [(x - 6, y - 6), (x + 6, y + 6)],
        fill=style.line_color + (255,),
        outline=(255, 255, 255, 220),
        width=2,
    )


def _draw_heading(draw: ImageDraw.ImageDraw, style: FlyoverStyle) -> None:
    label = "Route flyover"
    draw.text((32, 28), label, fill=style.accent_color + (200,))
    draw.text((32, 50), "Generated by CrowderSoup", fill=(120, 96, 84, 180))


def _viewport(
    center: tuple[float, float],
    style: FlyoverStyle,
    bounds: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    min_x, max_x, min_y, max_y = bounds
    total_width = max_x - min_x
    total_height = max_y - min_y

    zoom = style.zoom_ratio
    viewport_width = total_width * zoom
    viewport_height = total_height * zoom

    aspect = style.width / style.height
    viewport_height = max(viewport_height, viewport_width / aspect)
    viewport_width = viewport_height * aspect

    cx, cy = center
    half_w = viewport_width / 2
    half_h = viewport_height / 2

    left = max(min_x, min(cx - half_w, max_x - viewport_width))
    right = left + viewport_width
    bottom = max(min_y, min(cy - half_h, max_y - viewport_height))
    top = bottom + viewport_height

    return left, right, bottom, top


def _to_screen(
    point: tuple[float, float],
    viewport: tuple[float, float, float, float],
    style: FlyoverStyle,
) -> tuple[float, float]:
    left, right, bottom, top = viewport
    x = (point[0] - left) / (right - left)
    y = 1 - (point[1] - bottom) / (top - bottom)
    return (x * style.width, y * style.height)


def _encode_video(frames: Iterable[Image.Image], style: FlyoverStyle) -> bytes:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required to generate flyover videos.")

    with tempfile.TemporaryDirectory() as tmpdir:
        frame_pattern = os.path.join(tmpdir, "frame_%05d.png")
        for i, frame in enumerate(frames):
            frame.save(frame_pattern % i, format="PNG", optimize=True)

        output_path = os.path.join(tmpdir, "flyover.mp4")
        command = [
            "ffmpeg",
            "-y",
            "-framerate",
            str(style.fps),
            "-i",
            os.path.join(tmpdir, "frame_%05d.png"),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            output_path,
        ]
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        with open(output_path, "rb") as handle:
            return handle.read()
