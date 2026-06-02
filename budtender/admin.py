"""
Django admin — interim merchandising controls (full dashboard in subsystem 5).
Editing a product's `bucket` here flips `bucket_source` to "manual" so the
nightly classifier never overwrites a human decision.
"""
from django.contrib import admin

from .models import (AdminAudit, AnalyticsEvent, CustomerProfile, Feedback,
                     ManualPairing, Product, Setting, SuggestedProduct)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "location_slug", "category", "subcategory",
                    "price", "margin", "margin_pct", "bucket", "bucket_source",
                    "quantity_on_hand")
    list_filter = ("location_slug", "category", "bucket", "bucket_source", "availability")
    search_fields = ("name", "brand", "strain", "sku")
    list_editable = ("bucket",)
    readonly_fields = ("margin_pct", "margin_z", "price_z", "velocity", "classified_at")

    def save_model(self, request, obj, form, change):
        # A manual bucket edit pins the product against re-classification.
        if change and "bucket" in form.changed_data:
            obj.bucket_source = "manual"
        super().save_model(request, obj, form, change)


@admin.register(SuggestedProduct)
class SuggestedProductAdmin(admin.ModelAdmin):
    list_display = ("sku", "location_slug", "kind", "source", "reason_code",
                    "paired_with_sku", "accepted", "shown_at")
    list_filter = ("location_slug", "kind", "source", "accepted")
    search_fields = ("sku", "paired_with_sku")


@admin.register(CustomerProfile)
class CustomerProfileAdmin(admin.ModelAdmin):
    list_display = ("phone", "total_orders", "price_tier", "novelty_score", "last_purchase_at", "computed_at")
    search_fields = ("phone",)


@admin.register(AnalyticsEvent)
class AnalyticsEventAdmin(admin.ModelAdmin):
    list_display = ("ts", "event_type", "channel", "location_slug", "session_token")
    list_filter = ("event_type", "channel", "location_slug")
    date_hierarchy = "ts"
    readonly_fields = ("session_token", "phone_hash", "location_slug", "channel", "event_type", "props", "ts")

    def has_add_permission(self, request):
        return False


@admin.register(AdminAudit)
class AdminAuditAdmin(admin.ModelAdmin):
    list_display = ("ts", "actor", "action", "target")
    readonly_fields = ("actor", "action", "target", "before", "after", "ts")

    def has_add_permission(self, request):
        return False


@admin.register(ManualPairing)
class ManualPairingAdmin(admin.ModelAdmin):
    list_display = ("location_slug", "anchor_sku", "pair_sku", "active", "note")
    list_filter = ("location_slug", "active")
    search_fields = ("anchor_sku", "pair_sku")


@admin.register(Setting)
class SettingAdmin(admin.ModelAdmin):
    list_display = ("key", "updated_at")


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    list_display = ("ts", "rating", "category", "channel", "location_slug", "resolved", "short_msg")
    list_filter = ("rating", "category", "channel", "location_slug", "resolved")
    list_editable = ("resolved",)
    search_fields = ("message", "contact_email")
    readonly_fields = ("rating", "category", "message", "session_token", "phone_hash",
                       "location_slug", "channel", "contact_email", "ts")
    date_hierarchy = "ts"

    def short_msg(self, obj):
        return (obj.message[:60] + "…") if len(obj.message) > 60 else obj.message

    def has_add_permission(self, request):
        return False
