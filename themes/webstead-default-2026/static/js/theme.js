(() => {
  document.documentElement.classList.add("js-enabled");

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const applyMotion = () => {
    document.documentElement.dataset.motion = reducedMotion.matches
      ? "reduced"
      : "full";
  };

  applyMotion();
  reducedMotion.addEventListener("change", applyMotion);

  const enhanceSliders = () => {
    const sliders = document.querySelectorAll(".photo-slider");
    sliders.forEach((slider) => {
      const track = slider.querySelector(".slides");
      if (!track) {
        return;
      }

      const slides = Array.from(track.querySelectorAll(".slide, li"));
      if (slides.length < 2) {
        return;
      }

      slider.classList.add("is-enhanced");
      slides.forEach((slide, index) => {
        slide.dataset.slideIndex = String(index);
      });

      const controls = document.createElement("div");
      controls.className = "slider-controls";

      const prevButton = document.createElement("button");
      prevButton.type = "button";
      prevButton.className = "slider-button slider-button--prev";
      prevButton.setAttribute("aria-label", "Previous photo");
      prevButton.textContent = "Prev";

      const nextButton = document.createElement("button");
      nextButton.type = "button";
      nextButton.className = "slider-button slider-button--next";
      nextButton.setAttribute("aria-label", "Next photo");
      nextButton.textContent = "Next";

      const counter = document.createElement("span");
      counter.className = "slider-counter";

      controls.append(prevButton, counter, nextButton);

      const nav = slider.querySelector(".slider-nav");
      if (nav) {
        nav.before(controls);
      } else {
        track.after(controls);
      }

      const navLinks = Array.from(
        slider.querySelectorAll(".slider-nav a")
      );
      let activeIndex = 0;

      const updateState = (index) => {
        activeIndex = index;
        counter.textContent = `${index + 1} / ${slides.length}`;
        navLinks.forEach((link, linkIndex) => {
          if (linkIndex === index) {
            link.setAttribute("aria-current", "true");
          } else {
            link.removeAttribute("aria-current");
          }
        });
        prevButton.disabled = index === 0;
        nextButton.disabled = index === slides.length - 1;
      };

      const scrollToIndex = (index) => {
        const safeIndex = Math.max(0, Math.min(slides.length - 1, index));
        const behavior =
          document.documentElement.dataset.motion === "reduced"
            ? "auto"
            : "smooth";
        slides[safeIndex].scrollIntoView({
          behavior,
          block: "nearest",
          inline: "center",
        });
      };

      prevButton.addEventListener("click", () => {
        scrollToIndex(activeIndex - 1);
      });

      nextButton.addEventListener("click", () => {
        scrollToIndex(activeIndex + 1);
      });

      navLinks.forEach((link, index) => {
        link.addEventListener("click", (event) => {
          event.preventDefault();
          scrollToIndex(index);
        });
      });

      track.addEventListener("keydown", (event) => {
        if (event.key === "ArrowRight") {
          event.preventDefault();
          scrollToIndex(activeIndex + 1);
        }
        if (event.key === "ArrowLeft") {
          event.preventDefault();
          scrollToIndex(activeIndex - 1);
        }
        if (event.key === "Home") {
          event.preventDefault();
          scrollToIndex(0);
        }
        if (event.key === "End") {
          event.preventDefault();
          scrollToIndex(slides.length - 1);
        }
      });

      const observer = new IntersectionObserver(
        (entries) => {
          const visible = entries
            .filter((entry) => entry.isIntersecting)
            .sort((a, b) => b.intersectionRatio - a.intersectionRatio);
          if (!visible.length) {
            return;
          }
          const index = Number(
            visible[0].target.dataset.slideIndex || "0"
          );
          if (index !== activeIndex) {
            updateState(index);
          }
        },
        {
          root: track,
          threshold: [0.6],
        }
      );

      slides.forEach((slide) => observer.observe(slide));
      updateState(0);
    });
  };

  const addCodeLineNumbers = () => {
    const codeBlocks = document.querySelectorAll(
      ".e-content pre code, .p-content pre code"
    );

    codeBlocks.forEach((code) => {
      const pre = code.parentElement;
      if (!pre || pre.classList.contains("code-with-lines")) {
        return;
      }

      const text = code.textContent ?? "";
      const lines = text.replace(/\n$/, "").split("\n");
      pre.classList.add("code-with-lines");
      code.textContent = "";

      lines.forEach((line) => {
        const span = document.createElement("span");
        span.className = "code-line";
        span.textContent = line.length ? line : " ";
        code.append(span);
      });
    });
  };

  const initCheckinMaps = () => {
    if (!window.L) {
      return;
    }

    const styles = getComputedStyle(document.documentElement);
    const accent = styles.getPropertyValue("--accent").trim() || "#d56b30";
    const accentDeep =
      styles.getPropertyValue("--accent-deep").trim() || accent;
    const panel = styles.getPropertyValue("--panel").trim() || "#ffffff";

    document.querySelectorAll("[data-checkin-map]").forEach((el) => {
      if (el.dataset.mapReady === "true") {
        return;
      }

      const lat = Number.parseFloat(el.dataset.lat || "");
      const lng = Number.parseFloat(el.dataset.lng || "");
      if (!Number.isFinite(lat) || !Number.isFinite(lng)) {
        return;
      }

      const map = L.map(el, { scrollWheelZoom: false }).setView([lat, lng], 14);
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        attribution:
          '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        maxZoom: 19,
      }).addTo(map);
      L.circleMarker([lat, lng], {
        radius: 14,
        weight: 1.5,
        color: accentDeep,
        opacity: 0.45,
        fillColor: accent,
        fillOpacity: 0.16,
        interactive: false,
      }).addTo(map);
      L.circleMarker([lat, lng], {
        radius: 7.5,
        weight: 3,
        color: panel,
        fillColor: accent,
        fillOpacity: 1,
      }).addTo(map);

      el.dataset.mapReady = "true";
      window.setTimeout(() => map.invalidateSize(), 120);
    });
  };

  const localizeEventTimes = () => {
    const formatter = new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    });

    document.querySelectorAll("time.js-local-datetime[datetime]").forEach((el) => {
      if (el.dataset.localized === "true") {
        return;
      }

      const sourceValue = el.getAttribute("datetime") || "";
      const parsed = new Date(sourceValue);
      if (Number.isNaN(parsed.getTime())) {
        return;
      }

      el.textContent = formatter.format(parsed);
      el.dataset.localized = "true";
      if (!el.title) {
        el.title = sourceValue;
      }
    });
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      enhanceSliders();
      addCodeLineNumbers();
      initCheckinMaps();
      localizeEventTimes();
    });
  } else {
    enhanceSliders();
    addCodeLineNumbers();
    initCheckinMaps();
    localizeEventTimes();
  }
})();
