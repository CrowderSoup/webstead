from django import template

register = template.Library()


@register.filter
def get_item(dictionary, key):
    """Return dictionary[key], or None if the key is absent."""
    return dictionary.get(key)
