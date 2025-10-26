import markdown

from django.db import models
from django.utils.text import slugify, Truncator
from django.utils.safestring import mark_safe
from django.utils.html import strip_tags

from markdownx.models import MarkdownxField


class Tag(models.Model):
    tag = models.SlugField(max_length=64, unique=True)
    
    def __str__(self):
        return self.tag

class Post(models.Model):
    title = models.CharField(max_length=512)
    slug = models.SlugField(max_length=255, unique=True)
    content = MarkdownxField()
    published_on = models.DateTimeField("date published", null=True, blank=True)
    tags = models.ManyToManyField(Tag)

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.title or '') or 'page'
            slug = base
            i = 2
            # Ensure uniqueness without race conditions
            while Page.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{i}"
                i += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def html(self):
        md = markdown.Markdown(extensions=["fenced_code"])
        return mark_safe(md.convert(self.content))


    def summary(self):
        md = markdown.Markdown(extensions=["fenced_code"])

        html = md.convert(self.content)
        text = strip_tags(html)

        return Truncator(text).chars(500, truncate="...")
    
    def is_published(self):
        return self.published_on is not None
