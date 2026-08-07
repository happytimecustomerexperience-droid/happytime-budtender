"""Public storefront routes. No auth — see bundles/views.py for the leak rules.

Mounted at /custom-order in core/urls.py. The app owns no models of its own; the
cart and the resulting order are both `budtender.PhoneCartDraft` rows.
"""
from django.urls import path

from . import views

urlpatterns = [
    path("", views.landing, name="bundle_landing"),
    path("menu", views.menu, name="bundle_menu"),
    path("results", views.results, name="bundle_results"),
    path("lab/<str:product_id>", views.product_lab, name="bundle_product_lab"),
    path("cart", views.cart_view, name="bundle_cart"),
    path("cart/add", views.cart_add, name="bundle_cart_add"),
    path("cart/update", views.cart_update, name="bundle_cart_update"),
    path("cart/remove", views.cart_remove, name="bundle_cart_remove"),
    path("lookup-customer", views.lookup_customer, name="bundle_lookup_customer"),
    path("checkout", views.checkout, name="bundle_checkout"),
]
