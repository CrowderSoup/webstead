(() => {
  const flyoverTiles = document.querySelectorAll("[data-flyover-status-url]");
  if (!flyoverTiles.length) {
    return;
  }

  const setupAutoplay = (tile) => {
    if (tile.dataset.flyoverAutoplay === "true") {
      return;
    }

    const video = tile.querySelector("[data-flyover-video]");
    if (!video || !("IntersectionObserver" in window)) {
      return;
    }

    tile.dataset.flyoverAutoplay = "true";
    video.muted = true;
    video.playsInline = true;
    video.setAttribute("muted", "");
    video.setAttribute("playsinline", "");

    const slider = tile.closest(".media-slider");
    const root = slider ? slider.querySelector(".slides") : null;

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (video.hidden) {
            return;
          }
          if (entry.isIntersecting) {
            const playPromise = video.play();
            if (playPromise && typeof playPromise.catch === "function") {
              playPromise.catch(() => {});
            }
          } else if (!video.paused) {
            video.pause();
          }
        });
      },
      {
        root,
        threshold: [0.6],
      }
    );

    observer.observe(video);
  };

  const setStatusText = (statusEl, message) => {
    if (statusEl) {
      statusEl.textContent = message;
    }
  };

  const updateFlyover = (tile, payload) => {
    const placeholder = tile.querySelector("[data-flyover-placeholder]");
    const video = tile.querySelector("[data-flyover-video]");
    const statusEl = tile.querySelector("[data-flyover-status]");

    if (payload.status === "ready" && payload.url) {
      if (video) {
        let source = video.querySelector("source");
        if (!source) {
          source = document.createElement("source");
          source.type = "video/mp4";
          video.append(source);
        }
        if (source.src !== payload.url) {
          source.src = payload.url;
          video.load();
        }
        video.hidden = false;
        setupAutoplay(tile);
      }
      if (placeholder) {
        placeholder.hidden = true;
      }
      setStatusText(statusEl, "Flyover ready.");
      tile.dataset.flyoverState = "ready";
      return true;
    }

    if (payload.status === "failed") {
      if (placeholder) {
        placeholder.hidden = false;
      }
      if (video) {
        video.hidden = true;
      }
      setStatusText(statusEl, payload.error || "Flyover failed to generate.");
      tile.dataset.flyoverState = "failed";
      return true;
    }

    if (placeholder) {
      placeholder.hidden = false;
    }
    if (video) {
      video.hidden = true;
    }
    setStatusText(statusEl, "Generating flyover video.");
    tile.dataset.flyoverState = "pending";
    return false;
  };

  const startPolling = (tile) => {
    const url = tile.dataset.flyoverStatusUrl;
    if (!url) {
      return;
    }

    let delay = 2000;

    const poll = async () => {
      try {
        const response = await fetch(url, {
          headers: {
            Accept: "application/json",
          },
          cache: "no-store",
        });

        if (!response.ok) {
          setStatusText(
            tile.querySelector("[data-flyover-status]"),
            "Flyover unavailable."
          );
          return;
        }

        const payload = await response.json();
        const done = updateFlyover(tile, payload);
        if (!done) {
          delay = Math.min(delay * 1.5, 15000);
          window.setTimeout(poll, delay);
        }
      } catch (error) {
        delay = Math.min(delay * 1.5, 15000);
        window.setTimeout(poll, delay);
      }
    };

    poll();
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      flyoverTiles.forEach(startPolling);
    });
  } else {
    flyoverTiles.forEach(startPolling);
  }
})();
