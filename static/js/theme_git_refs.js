document.addEventListener("DOMContentLoaded", () => {
  const containers = Array.from(document.querySelectorAll("[data-git-ref-endpoint]"));
  containers.forEach((container) => {
    const gitUrlInput = container.querySelector("[data-git-url-input]");
    const gitRefInput = container.querySelector("[data-git-ref-input]");
    const gitRefList = container.querySelector("[data-git-ref-list]");
    if (!(gitUrlInput && gitRefInput && gitRefList)) {
      return;
    }
    const endpoint = container.dataset.gitRefEndpoint;
    if (!endpoint) {
      return;
    }
    let debounceTimer;
    const fetchRefs = () => {
      const url = gitUrlInput.value.trim();
      if (!url) {
        gitRefList.textContent = "";
        return;
      }
      fetch(`${endpoint}?git_url=${encodeURIComponent(url)}`)
        .then((response) => (response.ok ? response.json() : Promise.reject()))
        .then((data) => {
          if (!data || !Array.isArray(data.refs)) {
            return;
          }
          gitRefList.textContent = "";
          data.refs.forEach((ref) => {
            const option = document.createElement("option");
            option.value = ref;
            gitRefList.appendChild(option);
          });
          const currentValue = gitRefInput.value.trim();
          const previousDefault = gitRefInput.dataset.defaultRef || "";
          const nextDefault = data.default_ref || "";
          gitRefInput.dataset.defaultRef = nextDefault;
          if ((!currentValue || currentValue === previousDefault) && nextDefault) {
            gitRefInput.value = nextDefault;
          }
        })
        .catch(() => {});
    };
    const scheduleFetch = () => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(fetchRefs, 500);
    };
    gitUrlInput.addEventListener("input", scheduleFetch);
    gitUrlInput.addEventListener("change", fetchRefs);
    if (gitUrlInput.value.trim()) {
      fetchRefs();
    }
  });
});
