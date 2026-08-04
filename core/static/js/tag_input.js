(() => {
  const tagInputs = document.querySelectorAll("[data-tag-input]");
  if (!tagInputs.length) {
    return;
  }

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

  function initTagInput(wrapper) {
    const input = wrapper.querySelector("input");
    if (!input) return;
    const chips = wrapper.querySelector("[data-tag-chips]");
    const suggestions = wrapper.querySelector("[data-tag-suggestions]");
    const suggestUrl = wrapper.dataset.suggestUrl;
    const form = input.closest("form");

    let selectedTags = parseList(input.value || "");
    let tagValueInput = null;
    let suggestController = null;
    let suggestTimer = null;

    function setInputValue() {
      if (tagValueInput) {
        tagValueInput.value = selectedTags.join(",");
        return;
      }
      input.value = selectedTags.join(",");
    }

    function renderChips() {
      if (!chips) return;
      chips.innerHTML = "";
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
        chips.appendChild(button);
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

    if (form && input.name) {
      tagValueInput = document.createElement("input");
      tagValueInput.type = "hidden";
      tagValueInput.name = input.name;
      tagValueInput.value = selectedTags.join(",");
      form.appendChild(tagValueInput);
      input.removeAttribute("name");
      input.value = "";
    }

    if (form) {
      form.addEventListener("submit", () => {
        if (!input.value.trim()) return;
        addTags(input.value);
        input.value = "";
      });
    }

    input.addEventListener("input", () => requestSuggestions(input.value));
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === ",") {
        event.preventDefault();
        addTags(input.value);
        input.value = "";
        renderSuggestions([]);
      } else if (event.key === "Backspace" && !input.value && selectedTags.length) {
        event.preventDefault();
        removeTag(selectedTags[selectedTags.length - 1]);
      }
    });

    if (chips) {
      chips.addEventListener("click", (event) => {
        const button = event.target.closest("[data-remove-tag]");
        if (!button) return;
        removeTag(button.dataset.removeTag);
      });
    }

    if (suggestions) {
      suggestions.addEventListener("click", (event) => {
        const button = event.target.closest("[data-suggest-tag]");
        if (!button) return;
        addTags(button.dataset.suggestTag);
        button.remove();
        input.value = "";
        if (!suggestions.querySelector("[data-suggest-tag]")) {
          suggestions.hidden = true;
        }
      });
    }

    // "Suggest tags" button — fetches content-based suggestions and shows
    // them in the existing suggestion dropdown so clicking works normally.
    const suggestContentUrl = wrapper.dataset.suggestContentUrl;
    const suggestBtn = wrapper.closest("form")
      ? wrapper.closest("form").querySelector("[data-suggest-tags-btn]")
      : null;

    if (suggestBtn && suggestContentUrl) {
      suggestBtn.addEventListener("click", async () => {
        const parentForm = wrapper.closest("form");
        if (!parentForm) return;

        const titleField = parentForm.querySelector("input[name='title']");
        const contentField = parentForm.querySelector("textarea[name='content']");
        const title = titleField ? titleField.value : "";
        let content = "";
        if (contentField) {
          const container = contentField.closest(".EasyMDEContainer");
          if (container) {
            const cm = container.querySelector(".CodeMirror");
            content = cm && cm.CodeMirror ? cm.CodeMirror.getValue() : contentField.value;
          } else {
            content = contentField.value;
          }
        }

        suggestBtn.disabled = true;
        suggestBtn.textContent = "Suggesting\u2026";
        try {
          const csrfInput = parentForm.querySelector("input[name='csrfmiddlewaretoken']");
          const csrfToken = csrfInput ? csrfInput.value : "";
          const resp = await fetch(suggestContentUrl, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CSRFToken": csrfToken,
            },
            body: JSON.stringify({ title, content }),
          });
          const data = await resp.json();
          const tags = Array.isArray(data.tags) ? data.tags : [];
          // Filter out tags already selected
          const filtered = tags.filter((t) => !selectedTags.includes(t));
          renderSuggestions(filtered);
        } catch (e) {
          // Silently fail
        } finally {
          suggestBtn.disabled = false;
          suggestBtn.textContent = "Suggest tags";
        }
      });
    }

    renderChips();
  }

  tagInputs.forEach(initTagInput);
})();
