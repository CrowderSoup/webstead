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

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", enhanceSliders);
  } else {
    enhanceSliders();
  }
})();
