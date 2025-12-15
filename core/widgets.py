from django import forms
from django.templatetags.static import static


class CodeMirrorTextarea(forms.Textarea):
    """Textarea widget that upgrades to a CodeMirror editor in the admin."""

    def __init__(self, *args, mode="markdown", dark_mode="auto", **kwargs):
        attrs = kwargs.setdefault("attrs", {})
        existing_classes = attrs.get("class", "")
        attrs["class"] = f"{existing_classes} codemirror-widget".strip()
        attrs.setdefault("data-codemirror-mode", mode)
        attrs.setdefault("data-codemirror-height", "500px")
        attrs.setdefault("data-codemirror-dark-mode", dark_mode)
        attrs.setdefault("data-codemirror-dark-theme", "material")
        attrs.setdefault("data-codemirror-light-theme", "default")
        super().__init__(*args, **kwargs)

    @property
    def media(self):
        cdn_base = "https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16"
        css = {
            "all": (
                f"{cdn_base}/codemirror.min.css",
                f"{cdn_base}/theme/material.min.css",
            )
        }
        js = (
            f"{cdn_base}/codemirror.min.js",
            f"{cdn_base}/mode/markdown/markdown.min.js",
            f"{cdn_base}/addon/edit/closebrackets.min.js",
            f"{cdn_base}/addon/edit/closetag.min.js",
            f"{cdn_base}/addon/edit/matchbrackets.min.js",
            f"{cdn_base}/addon/display/placeholder.min.js",
            static("core/js/codemirror-init.js"),
        )
        return forms.Media(css=css, js=js)
