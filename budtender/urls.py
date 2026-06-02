from django.urls import path

from . import views

urlpatterns = [
    path("health/", views.HealthView.as_view()),
    path("chat/session/start", views.SessionStartView.as_view()),
    path("products/search/", views.ProductSearchView.as_view()),
    path("pairing/for-sku", views.PairingView.as_view()),
    path("chat/resume-by-phone", views.ResumeByPhoneView.as_view()),
    path("chat/persist/", views.PersistView.as_view()),
    path("customer/profile-upsert", views.ProfileUpsertView.as_view()),
    path("track/", views.TrackView.as_view()),
    path("feedback/", views.FeedbackView.as_view()),
]
