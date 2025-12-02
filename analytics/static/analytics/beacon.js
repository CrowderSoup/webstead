(function () {
  // Grab the visit ID from a meta tag
  const visitId = document.querySelector('meta[name="visit-id"]')?.content;
  if (!visitId) return;

  function sendBeacon() {
    navigator.sendBeacon(
      "/analytics/leave/",
      JSON.stringify({ visit_id: visitId, ts: Date.now() }),
    );
  }

  // Fire when the page is hidden or unloading
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") sendBeacon();
  });

  window.addEventListener("pagehide", () => {
    sendBeacon();
  });
})();
