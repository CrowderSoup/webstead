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
})();
