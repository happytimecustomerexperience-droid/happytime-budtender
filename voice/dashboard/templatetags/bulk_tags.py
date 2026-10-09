"""Template helpers for the bulk-edit partials."""

from django import template

register = template.Library()


@register.filter
def cells(ds, obj):
    """``{% for text, badge in ds|cells:r %}`` -- the generic row's display cells."""
    return ds.cells(obj)
