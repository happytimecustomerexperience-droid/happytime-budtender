from django.urls import path

from core import views

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
]
