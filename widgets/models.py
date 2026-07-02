from django.db import models


class WidgetInstance(models.Model):
    widget_type = models.CharField(max_length=64)
    area = models.CharField(max_length=64)
    order = models.PositiveIntegerField(default=0)
    config = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["area", "order", "pk"]

    @property
    def configured_title(self):
        if not isinstance(self.config, dict):
            return ""
        title = self.config.get("title", "")
        return title.strip() if isinstance(title, str) else ""

    @property
    def widget_type_label(self):
        from core.plugins import registry

        widget_cls = registry.get_widget_type(self.widget_type)
        if widget_cls:
            return widget_cls.label
        return self.widget_type.replace("_", " ").title()

    @property
    def display_title(self):
        return self.configured_title or self.widget_type_label

    def __str__(self):
        return f"{self.widget_type} in {self.area} (order={self.order})"
