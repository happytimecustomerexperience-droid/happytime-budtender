from django.contrib import admin

from .models import InventoryItem


@admin.register(InventoryItem)
class InventoryItemAdmin(admin.ModelAdmin):
    list_display = ("name", "store", "product_id", "price", "available", "updated_at")
    list_filter = ("store", "category")
    search_fields = ("name", "product_id", "serial_no", "brand")
