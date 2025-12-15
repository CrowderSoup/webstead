(function () {
  const initEditor = (textarea) => {
    if (!window.CodeMirror || textarea.dataset.codemirrorAttached === "1")
      return;

    const mode = textarea.dataset.codemirrorMode || "markdown";
    const height = textarea.dataset.codemirrorHeight || "500px";
    const darkModePref = (
      textarea.dataset.codemirrorDarkMode || "auto"
    ).toLowerCase();
    const darkTheme = textarea.dataset.codemirrorDarkTheme || "material";
    const lightTheme = textarea.dataset.codemirrorLightTheme || "default";

    const editor = CodeMirror.fromTextArea(textarea, {
      mode,
      theme: lightTheme,
      lineNumbers: true,
      lineWrapping: true,
      autoCloseBrackets: true,
      autoCloseTags: true,
      matchBrackets: true,
      tabSize: 2,
      indentUnit: 2,
      darkTheme: true,
    });

    const prefersDark = window.matchMedia("(prefers-color-scheme: dark)");
    const applyTheme = () => {
      const isDark =
        darkModePref === "always" ||
        (darkModePref === "auto" && prefersDark.matches) ||
        darkModePref === "true";
      const themeToUse = isDark ? darkTheme : lightTheme;
      editor.setOption("theme", themeToUse);
    };
    applyTheme();
    if (darkModePref === "auto" && prefersDark?.addEventListener) {
      prefersDark.addEventListener("change", applyTheme);
    }

    editor.setSize("100%", height);
    editor.on("change", () => editor.save());

    const form = textarea.closest("form");
    if (form) {
      form.addEventListener("submit", () => editor.save());
    }

    textarea.dataset.codemirrorAttached = "1";
  };

  const initWithin = (root = document) => {
    root.querySelectorAll("textarea.codemirror-widget").forEach(initEditor);
  };

  document.addEventListener("DOMContentLoaded", () => initWithin());
  document.addEventListener("formset:added", (event) => {
    if (!event.target) return;
    initWithin(event.target);
  });
})();
