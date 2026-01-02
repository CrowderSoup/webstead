(() => {
  async function parseGpxPoints(url) {
    const response = await fetch(url);
    if (!response.ok) return [];
    const text = await response.text();
    const xml = new DOMParser().parseFromString(text, "application/xml");
    if (xml.querySelector("parsererror")) return [];

    const points = Array.from(xml.querySelectorAll("trkpt, rtept"))
      .map((point) => {
        const lat = parseFloat(point.getAttribute("lat"));
        const lon = parseFloat(point.getAttribute("lon"));
        if (Number.isNaN(lat) || Number.isNaN(lon)) return null;
        const eleText = point.querySelector("ele")?.textContent;
        const ele = eleText ? parseFloat(eleText) : null;
        const timeText = point.querySelector("time")?.textContent;
        const time = timeText ? Date.parse(timeText) : null;
        return {
          lat,
          lon,
          ele: Number.isFinite(ele) ? ele : null,
          time: Number.isFinite(time) ? time : null,
        };
      })
      .filter(Boolean);

    return points;
  }

  function haversineDistance(a, b) {
    const toRad = (value) => (value * Math.PI) / 180;
    const radius = 6371000;
    const dLat = toRad(b.lat - a.lat);
    const dLon = toRad(b.lon - a.lon);
    const lat1 = toRad(a.lat);
    const lat2 = toRad(b.lat);
    const sinLat = Math.sin(dLat / 2);
    const sinLon = Math.sin(dLon / 2);
    const h =
      sinLat * sinLat + Math.cos(lat1) * Math.cos(lat2) * sinLon * sinLon;
    return 2 * radius * Math.asin(Math.sqrt(h));
  }

  function formatDistance(meters) {
    if (!Number.isFinite(meters)) return "--";
    if (meters < 1000) return `${Math.round(meters)} m`;
    return `${(meters / 1000).toFixed(2)} km`;
  }

  function formatDuration(seconds) {
    if (!Number.isFinite(seconds)) return "--";
    const total = Math.max(0, Math.round(seconds));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    if (hours > 0) return `${hours}h ${minutes}m`;
    if (minutes > 0) return `${minutes}m ${secs}s`;
    return `${secs}s`;
  }

  function formatSpeed(mps) {
    if (!Number.isFinite(mps)) return "--";
    const kmh = mps * 3.6;
    return `${kmh.toFixed(1)} km/h`;
  }

  function formatElevation(meters) {
    if (!Number.isFinite(meters)) return "--";
    return `${Math.round(meters)} m`;
  }

  function computeStats(points) {
    const movingThreshold = 0.8;
    let totalDistance = 0;
    let movingDistance = 0;
    let movingTime = 0;
    let maxSpeed = null;
    let hasTime = false;
    let elevationGain = 0;
    let elevationLoss = 0;

    let startTime = null;
    let endTime = null;
    const timePoints = points.filter((point) => Number.isFinite(point.time));
    if (timePoints.length >= 2) {
      startTime = timePoints[0].time;
      endTime = timePoints[timePoints.length - 1].time;
    }

    for (let i = 1; i < points.length; i += 1) {
      const prev = points[i - 1];
      const curr = points[i];
      const distance = haversineDistance(prev, curr);
      totalDistance += distance;

      if (Number.isFinite(prev.ele) && Number.isFinite(curr.ele)) {
        const diff = curr.ele - prev.ele;
        if (diff > 0) elevationGain += diff;
        if (diff < 0) elevationLoss += Math.abs(diff);
      }

      if (Number.isFinite(prev.time) && Number.isFinite(curr.time)) {
        const delta = (curr.time - prev.time) / 1000;
        if (delta > 0) {
          hasTime = true;
          const speed = distance / delta;
          maxSpeed = maxSpeed === null ? speed : Math.max(maxSpeed, speed);
          if (speed >= movingThreshold) {
            movingTime += delta;
            movingDistance += distance;
          }
        }
      }
    }

    if (!hasTime) {
      movingTime = null;
      movingDistance = 0;
      maxSpeed = null;
    }

    const totalTime =
      Number.isFinite(startTime) && Number.isFinite(endTime)
        ? (endTime - startTime) / 1000
        : null;
    const stoppedTime =
      Number.isFinite(totalTime) && Number.isFinite(movingTime)
        ? totalTime - movingTime
        : null;
    const avgMovingSpeed = movingTime > 0 ? movingDistance / movingTime : null;

    return {
      totalDistance,
      movingTime,
      stoppedTime,
      totalTime,
      avgMovingSpeed,
      maxSpeed,
      elevationGain,
      elevationLoss,
    };
  }

  function renderStats(container, stats) {
    if (!container || !stats) return;
    const mapping = {
      distance: formatDistance(stats.totalDistance),
      "moving-time": formatDuration(stats.movingTime),
      "stopped-time": formatDuration(stats.stoppedTime),
      "total-time": formatDuration(stats.totalTime),
      "avg-moving-speed": formatSpeed(stats.avgMovingSpeed),
      "max-speed": formatSpeed(stats.maxSpeed),
      "elevation-gain": formatElevation(stats.elevationGain),
      "elevation-loss": formatElevation(stats.elevationLoss),
    };
    Object.entries(mapping).forEach(([key, value]) => {
      container.querySelectorAll(`[data-stat="${key}"]`).forEach((el) => {
        el.textContent = value;
      });
    });
  }

  function findClosestIndex(values, target) {
    let low = 0;
    let high = values.length - 1;
    while (low <= high) {
      const mid = Math.floor((low + high) / 2);
      const value = values[mid];
      if (value === target) return mid;
      if (value < target) {
        low = mid + 1;
      } else {
        high = mid - 1;
      }
    }
    if (low <= 0) return 0;
    if (low >= values.length) return values.length - 1;
    return Math.abs(values[low] - target) < Math.abs(values[low - 1] - target)
      ? low
      : low - 1;
  }

  function downsample(points, maxPoints) {
    if (points.length <= maxPoints) return points;
    const step = Math.ceil(points.length / maxPoints);
    return points.filter(
      (point, index) => index % step === 0 || index === points.length - 1,
    );
  }

  function renderElevationChart(container, points, map, marker) {
    if (!container) return;
    const chart = container.querySelector(".activity-elevation__chart");
    const empty = container.querySelector(".activity-elevation__empty");
    if (!chart) return;

    const elevationPoints = points.filter(
      (point) => Number.isFinite(point.time) && Number.isFinite(point.ele),
    );
    if (elevationPoints.length < 2) {
      chart.style.display = "none";
      if (empty) empty.hidden = false;
      return;
    }

    const timePoints = points.filter((point) => Number.isFinite(point.time));
    if (timePoints.length < 2) {
      chart.style.display = "none";
      if (empty) empty.hidden = false;
      return;
    }

    const reducedPoints = downsample(elevationPoints, 500);
    const minEle = Math.min(...reducedPoints.map((point) => point.ele));
    const maxEle = Math.max(...reducedPoints.map((point) => point.ele));
    const range = Math.max(1, maxEle - minEle);

    chart.style.display = "";
    if (empty) empty.hidden = true;

    const startTime = reducedPoints[0].time;
    const endTime = reducedPoints[reducedPoints.length - 1].time;
    if (!Number.isFinite(startTime) || !Number.isFinite(endTime)) return;
    const duration = Math.max(1, endTime - startTime);

    const width = 1000;
    const height = 200;
    const padding = 12;
    const scaleX = (time) =>
      padding + ((time - startTime) / duration) * (width - padding * 2);
    const scaleY = (ele) =>
      padding + (1 - (ele - minEle) / range) * (height - padding * 2);

    const coords = reducedPoints.map((point) => ({
      time: point.time,
      x: scaleX(point.time),
      y: scaleY(point.ele),
    }));

    const path = coords
      .map(
        (point, index) =>
          `${index === 0 ? "M" : "L"}${point.x.toFixed(2)},${point.y.toFixed(2)}`,
      )
      .join(" ");

    const areaPath = `${path} L${coords[coords.length - 1].x.toFixed(2)},${(
      height - padding
    ).toFixed(2)} L${coords[0].x.toFixed(2)},${(height - padding).toFixed(
      2,
    )} Z`;

    const ns = "http://www.w3.org/2000/svg";
    chart.setAttribute("viewBox", `0 0 ${width} ${height}`);
    chart.setAttribute("preserveAspectRatio", "none");
    chart.innerHTML = "";

    const fill = document.createElementNS(ns, "path");
    fill.setAttribute("d", areaPath);
    fill.setAttribute("class", "activity-elevation__fill");
    chart.appendChild(fill);

    const line = document.createElementNS(ns, "path");
    line.setAttribute("d", path);
    line.setAttribute("class", "activity-elevation__line");
    chart.appendChild(line);

    const cursor = document.createElementNS(ns, "circle");
    cursor.setAttribute("r", "6");
    cursor.setAttribute("class", "activity-elevation__cursor");
    chart.appendChild(cursor);

    const overlay = document.createElementNS(ns, "rect");
    overlay.setAttribute("x", "0");
    overlay.setAttribute("y", "0");
    overlay.setAttribute("width", `${width}`);
    overlay.setAttribute("height", `${height}`);
    overlay.setAttribute("class", "activity-elevation__overlay");
    chart.appendChild(overlay);

    const chartTimes = coords.map((point) => point.time);
    const mapTimes = timePoints.map((point) => point.time);

    const showCursor = (index) => {
      const point = coords[index];
      cursor.setAttribute("cx", point.x.toFixed(2));
      cursor.setAttribute("cy", point.y.toFixed(2));
      cursor.style.display = "block";
    };

    const hideCursor = () => {
      cursor.style.display = "none";
    };

    overlay.addEventListener("pointermove", (event) => {
      const bounds = chart.getBoundingClientRect();
      if (!bounds.width) return;
      const offsetX = Math.min(
        Math.max(0, event.clientX - bounds.left),
        bounds.width,
      );
      const targetTime = startTime + (offsetX / bounds.width) * duration;

      const chartIndex = findClosestIndex(chartTimes, targetTime);
      showCursor(chartIndex);

      if (map && marker) {
        const mapIndex = findClosestIndex(mapTimes, targetTime);
        const point = timePoints[mapIndex];
        marker.setLatLng([point.lat, point.lon]);
        marker.setStyle({ opacity: 1, fillOpacity: 0.9 });
      }
    });

    overlay.addEventListener("pointerleave", () => {
      hideCursor();
      if (marker) marker.setStyle({ opacity: 0, fillOpacity: 0 });
    });
  }

  function initMaps() {
    if (!window.L) return;

    const iconBase = "https://unpkg.com/leaflet@1.9.4/dist/images/";
    L.Icon.Default.mergeOptions({
      iconRetinaUrl: `${iconBase}marker-icon-2x.png`,
      iconUrl: `${iconBase}marker-icon.png`,
      shadowUrl: `${iconBase}marker-shadow.png`,
    });

    document.querySelectorAll(".activity-map").forEach(async (el) => {
      const gpxUrl = el.dataset.gpxUrl;
      if (!gpxUrl) return;

      const wrapper = el.closest(".entry-activity");
      const statsEl = wrapper?.querySelector("[data-activity-stats]");
      const elevationEl = wrapper?.querySelector("[data-activity-elevation]");

      const map = L.map(el, { scrollWheelZoom: false });
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        attribution:
          '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      }).addTo(map);

      const points = await parseGpxPoints(gpxUrl);
      if (!points.length) {
        map.setView([0, 0], 2);
        return;
      }
      const line = L.polyline(
        points.map((point) => [point.lat, point.lon]),
        {
          color: "#1d4ed8",
          weight: 4,
          opacity: 0.9,
        },
      ).addTo(map);
      map.fitBounds(line.getBounds(), { padding: [20, 20] });

      const marker = L.circleMarker([points[0].lat, points[0].lon], {
        radius: 6,
        color: "#1d4ed8",
        weight: 2,
        fillColor: "#1d4ed8",
        fillOpacity: 0,
        opacity: 0,
      }).addTo(map);

      const stats = computeStats(points);
      renderStats(statsEl, stats);
      renderStats(elevationEl, stats);
      renderElevationChart(elevationEl, points, map, marker);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initMaps);
  } else {
    initMaps();
  }
})();
