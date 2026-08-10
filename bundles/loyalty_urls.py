"""The /loyalty route.

Its own module rather than an entry in `bundles/urls.py`, because that one is
mounted at /custom-order and everything in it inherits that prefix. Keeping
core/urls.py in the include() style is worth three lines.
"""
from django.urls import path

from . import views

urlpatterns = [path("", views.loyalty, name="loyalty")]
