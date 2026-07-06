from django.contrib import admin

from .models import Customer, DutchieWriteAudit, ShopEvent, ShopVisit


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("id", "first_name", "last_name", "phone", "dutchie_acct_id", "over_21", "updated_at")
    search_fields = ("phone", "last_name", "first_name", "dutchie_acct_id")
    list_filter = ("over_21", "state")


@admin.register(DutchieWriteAudit)
class DutchieWriteAuditAdmin(admin.ModelAdmin):
    list_display = ("created_at", "store", "action", "ok", "acct_id", "shipment_id", "username", "summary")
    search_fields = ("store", "action", "username", "summary", "acct_id", "shipment_id")
    list_filter = ("ok", "store", "action")
    readonly_fields = ("store", "action", "acct_id", "shipment_id", "summary", "ok", "username", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class ShopEventInline(admin.TabularInline):
    model = ShopEvent
    extra = 0
    can_delete = False
    fields = ("at", "kind", "product_name", "detail", "meta")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ShopVisit)
class ShopVisitAdmin(admin.ModelAdmin):
    list_display = ("started_at", "store", "budtender", "acct_name", "outcome",
                    "event_count", "items_viewed", "items_added", "cart_total")
    list_filter = ("outcome", "store", "budtender", "how_started")
    search_fields = ("acct_name", "phone", "acct_id", "budtender")
    date_hierarchy = "started_at"
    inlines = (ShopEventInline,)
    readonly_fields = ("started_at", "ended_at")

    def has_add_permission(self, request):
        return False


@admin.register(ShopEvent)
class ShopEventAdmin(admin.ModelAdmin):
    list_display = ("at", "kind", "budtender", "acct_id", "product_name", "detail")
    list_filter = ("kind", "budtender")
    search_fields = ("product_name", "detail", "acct_id", "budtender")
    date_hierarchy = "at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
