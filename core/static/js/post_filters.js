(() => {
  const filterBar = document.querySelector("[data-filter-bar]");
  if (!filterBar) {
    return;
  }

  const form = filterBar.querySelector("[data-filter-form]");
  const tagInput = filterBar.querySelector("#tag-input");
  let tagValueInput = null;
  const tagChips = filterBar.querySelector("[data-tag-chips]");
  const clearButton = filterBar.querySelector("[data-clear]");
  const toggleButton = filterBar.querySelector("[data-filter-toggle]");
  const filterHeader = filterBar.querySelector("[data-filter-header]");
  const filterBody = filterBar.querySelector("[data-filter-body]");
  const suggestions = filterBar.querySelector("[data-tag-suggestions]");
  const results = document.querySelector("[data-filter-results]");
  const pagination = document.querySelector("[data-filter-pagination]");
  const feedLink = document.querySelector("[data-filter-feed]");
  const suggestUrl = filterBar.dataset.suggestUrl;
  const defaultKinds = parseList(filterBar.dataset.defaultKinds || "");

  let selectedTags = parseList(tagInput?.value || "");
  let fetchController = null;
  let suggestController = null;
  let suggestTimer = null;

  function parseList(value) {
    if (!value) return [];
    const parts = value
      .split(",")
      .map((part) => part.trim().toLowerCase())
      .filter(Boolean);
    const seen = new Set();
    const deduped = [];
    parts.forEach((part) => {
      if (seen.has(part)) return;
      seen.add(part);
      deduped.push(part);
    });
    return deduped;
  }

  function setInputValue() {
    if (tagValueInput) {
      tagValueInput.value = selectedTags.join(",");
      return;
    }
    if (tagInput) {
      tagInput.value = selectedTags.join(",");
    }
  }

  function renderChips() {
    if (!tagChips) return;
    tagChips.innerHTML = "";
    selectedTags.forEach((tag) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "tag-chip";
      button.dataset.removeTag = tag;
      button.setAttribute("aria-label", `Remove tag ${tag}`);
      button.textContent = `#${tag} `;
      const close = document.createElement("span");
      close.setAttribute("aria-hidden", "true");
      close.textContent = "x";
      button.appendChild(close);
      tagChips.appendChild(button);
    });
  }

  function selectedKinds() {
    if (!form) return [];
    return Array.from(form.querySelectorAll('input[name="kind"]:checked')).map(
      (input) => input.value
    );
  }

  function updateClearButton(query) {
    if (!clearButton) return;
    clearButton.hidden = !query;
  }

  function updateFeedLink(query) {
    if (!feedLink) return;
    const base = feedLink.dataset.baseHref || feedLink.getAttribute("href") || "";
    feedLink.setAttribute("href", query ? `${base}?${query}` : base);
  }

  function setCollapsed(collapsed) {
    if (!filterBody || !toggleButton) return;
    filterBar.classList.toggle("is-collapsed", collapsed);
    toggleButton.setAttribute("aria-expanded", collapsed ? "false" : "true");
    toggleButton.textContent = collapsed ? "Show filters" : "Hide filters";
  }

  function arraysMatch(left, right) {
    if (left.length !== right.length) return false;
    return left.every((value, index) => value === right[index]);
  }

  function applyFilters() {
    let kinds = selectedKinds();
    if (!kinds.length && !selectedTags.length && defaultKinds.length) {
      kinds = defaultKinds.slice();
      form
        ?.querySelectorAll('input[name="kind"]')
        .forEach((input) => {
          input.checked = defaultKinds.includes(input.value);
        });
    }
    const isDefaultKinds =
      !selectedTags.length && defaultKinds.length && arraysMatch(kinds, defaultKinds);
    const params = new URLSearchParams();
    if (kinds.length && !isDefaultKinds) {
      params.set("kind", kinds.join(","));
    }
    if (selectedTags.length) {
      params.set("tag", selectedTags.join(","));
    }
    const feedParams = new URLSearchParams();
    if (kinds.length) {
      feedParams.set("kind", kinds.join(","));
    }
    if (selectedTags.length) {
      feedParams.set("tag", selectedTags.join(","));
    }
    const query = params.toString();
    const url = `${window.location.pathname}${query ? `?${query}` : ""}`;

    updateClearButton(query);
    updateFeedLink(feedParams.toString());
    history.replaceState({}, "", url);
    fetchResults(url);
  }

  function fetchResults(url) {
    if (!results) {
      window.location.assign(url);
      return;
    }
    if (fetchController) {
      fetchController.abort();
    }
    fetchController = new AbortController();
    fetch(url, { signal: fetchController.signal })
      .then((response) => {
        if (!response.ok) {
          throw new Error("Failed to fetch results");
        }
        return response.text();
      })
      .then((html) => {
        const doc = new DOMParser().parseFromString(html, "text/html");
        const nextResults = doc.querySelector("[data-filter-results]");
        const nextPagination = doc.querySelector("[data-filter-pagination]");
        if (nextResults) {
          results.innerHTML = nextResults.innerHTML;
        }
        if (pagination && nextPagination) {
          pagination.innerHTML = nextPagination.innerHTML;
        }
        if (window.initializeActivityMaps) {
          window.initializeActivityMaps();
        }
      })
      .catch((error) => {
        if (error.name === "AbortError") return;
        window.location.assign(url);
      });
  }

  function addTags(rawValue) {
    if (!rawValue) return false;
    const incoming = parseList(rawValue);
    let changed = false;
    incoming.forEach((tag) => {
      if (selectedTags.includes(tag)) return;
      selectedTags.push(tag);
      changed = true;
    });
    if (!changed) return false;
    setInputValue();
    renderChips();
    return true;
  }

  function removeTag(tag) {
    const nextTags = selectedTags.filter((value) => value !== tag);
    if (nextTags.length === selectedTags.length) return false;
    selectedTags = nextTags;
    setInputValue();
    renderChips();
    return true;
  }

  function clearFilters() {
    selectedTags = [];
    setInputValue();
    renderChips();
    form
      ?.querySelectorAll('input[name="kind"]')
      .forEach((input) => {
        input.checked = defaultKinds.includes(input.value);
      });
    applyFilters();
  }

  function renderSuggestions(tags) {
    if (!suggestions) return;
    suggestions.innerHTML = "";
    if (!tags.length) {
      suggestions.hidden = true;
      return;
    }
    const list = document.createElement("div");
    list.className = "tag-suggestions__list";
    tags.forEach((tag) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "tag-suggestion";
      button.textContent = `#${tag}`;
      button.dataset.suggestTag = tag;
      list.appendChild(button);
    });
    suggestions.appendChild(list);
    suggestions.hidden = false;
  }

  function requestSuggestions(value) {
    if (!suggestUrl || !suggestions) return;
    const query = value.trim();
    if (!query) {
      suggestions.hidden = true;
      return;
    }
    if (suggestTimer) window.clearTimeout(suggestTimer);
    suggestTimer = window.setTimeout(() => {
      if (suggestController) {
        suggestController.abort();
      }
      suggestController = new AbortController();
      const url = `${suggestUrl}?q=${encodeURIComponent(query)}`;
      fetch(url, { signal: suggestController.signal })
        .then((response) => response.json())
        .then((data) => {
          renderSuggestions(Array.isArray(data.tags) ? data.tags : []);
        })
        .catch((error) => {
          if (error.name === "AbortError") return;
          suggestions.hidden = true;
        });
    }, 200);
  }

  if (form) {
    if (tagInput) {
      tagValueInput = document.createElement("input");
      tagValueInput.type = "hidden";
      tagValueInput.name = "tag";
      tagValueInput.value = selectedTags.join(",");
      form.appendChild(tagValueInput);
      tagInput.removeAttribute("name");
    }
    form.addEventListener("change", (event) => {
      if (event.target?.name === "kind") {
        applyFilters();
      }
    });
  }

  if (tagInput) {
    tagInput.addEventListener("input", () => requestSuggestions(tagInput.value));
    tagInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === ",") {
        event.preventDefault();
        const changed = addTags(tagInput.value);
        tagInput.value = "";
        renderSuggestions([]);
        if (changed) applyFilters();
      } else if (
        event.key === "Backspace" &&
        !tagInput.value &&
        selectedTags.length
      ) {
        event.preventDefault();
        const lastTag = selectedTags[selectedTags.length - 1];
        if (removeTag(lastTag)) applyFilters();
      }
    });
  }

  if (tagChips) {
    tagChips.addEventListener("click", (event) => {
      const button = event.target.closest("[data-remove-tag]");
      if (!button) return;
      if (removeTag(button.dataset.removeTag)) {
        applyFilters();
      }
    });
  }

  if (clearButton) {
    clearButton.addEventListener("click", () => {
      clearFilters();
    });
  }

  if (toggleButton) {
    toggleButton.addEventListener("click", () => {
      const collapsed = filterBar.classList.contains("is-collapsed");
      setCollapsed(!collapsed);
    });
  }

  if (filterHeader) {
    filterHeader.addEventListener("click", (event) => {
      if (!filterBar.classList.contains("is-collapsed")) return;
      if (event.target.closest("button")) return;
      setCollapsed(false);
    });
  }

  if (suggestions) {
    suggestions.addEventListener("click", (event) => {
      const button = event.target.closest("[data-suggest-tag]");
      if (!button) return;
      const changed = addTags(button.dataset.suggestTag);
      if (tagInput) tagInput.value = "";
      renderSuggestions([]);
      if (changed) applyFilters();
    });
  }

  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-tag]");
    if (!link || !form) return;
    event.preventDefault();
    const tag = link.dataset.tag || link.textContent.replace("#", "").trim();
    const changed = addTags(tag);
    if (changed) applyFilters();
  });

  renderChips();
  updateClearButton(new URLSearchParams(window.location.search).toString());
  setCollapsed(filterBar.classList.contains("is-collapsed"));
})();
