from django.urls import path

from . import views

urlpatterns = [
    path("health/", views.HealthView.as_view()),
    path("chat/session/start", views.SessionStartView.as_view()),
    path("chat/message", views.ChatReplyView.as_view()),
    path("chat/history", views.ChatHistoryView.as_view()),
    path("products/search/", views.ProductSearchView.as_view()),
    path("products/in-stock/", views.InStockProductsView.as_view()),
    path("new-drops/", views.NewDropsView.as_view()),
    path("deals/", views.DealsView.as_view()),
    path("products/by-sku/", views.ProductBySkuView.as_view()),
    path("products/price-bands", views.PriceBandsView.as_view()),
    path("products/subtypes", views.SubtypesView.as_view()),
    path("products/sizes", views.SizesView.as_view()),
    path("products/doh-options", views.DohOptionsView.as_view()),
    # search v2 (docs/contracts/search-v2.md)
    path("products/categories", views.CategoriesView.as_view()),
    path("products/facets", views.FacetsView.as_view()),
    path("products/specify-more", views.SpecifyMoreView.as_view()),
    path("products/similar", views.SimilarView.as_view()),
    path("pairing/for-sku", views.PairingView.as_view()),
    path("chat/resume-by-phone", views.ResumeByPhoneView.as_view()),
    path("chat/persist/", views.PersistView.as_view()),
    path("phone-cart/upsert", views.PhoneCartUpsertView.as_view()),
    path("phone-cart/release", views.PhoneCartReleaseView.as_view()),
    path("phone-cart/claim", views.PhoneCartClaimView.as_view()),
    path("customer/profile-upsert", views.ProfileUpsertView.as_view()),
    path("customer/caller-context", views.CallerContextView.as_view()),     # voice: who is calling
    path("customer/session-context", views.SessionContextView.as_view()),   # website: typed phone
    path("customer/memory/learn", views.MemoryLearnView.as_view()),        # voice call end / internal
    path("customer/memory/clear", views.MemoryClearView.as_view()),        # staff: wipe a customer's memory
    path("customer/call-ids", views.CustomerCallIdsView.as_view()),        # staff: a customer's Vapi call ids
    path("customer/name-match", views.CustomerNameMatchView.as_view()),    # staff: how many customers have this exact name
    path("customer/list", views.CustomerListView.as_view()),       # P7 staff roster (dashboard)
    path("customer/detail", views.CustomerDetailView.as_view()),   # P7 full profile (dashboard)
    path("track/", views.TrackView.as_view()),
    path("analytics/summary", views.AnalyticsSummaryView.as_view()),
    path("analytics/funnel", views.AnalyticsFunnelView.as_view()),     # owner dashboard: per-session funnel
    path("analytics/session", views.AnalyticsSessionView.as_view()),   # owner dashboard: one session's timeline
    path("analytics/suggestions", views.AnalyticsSuggestionsView.as_view()),           # suggestion-analytics-v1
    path("analytics/suggestions/list", views.AnalyticsSuggestionsListView.as_view()),
    path("customer/suggestions", views.CustomerSuggestionsView.as_view()),
    path("suggestions/shown", views.SuggestionsShownView.as_view()),   # voice: the picks a call spoke
    path("admin/ranking-weights", views.AdminRankingWeightsView.as_view()),
    path("feedback/", views.FeedbackView.as_view()),
    path("persona/refresh", views.PersonaRefreshView.as_view()),
    path("store-facts/refresh", views.StoreFactsRefreshView.as_view()),
]
