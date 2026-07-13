from collections import defaultdict
from datetime import timedelta
import csv

from django.contrib import admin
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import path, reverse
from django.utils import timezone

from .models import InventoryItem


@admin.register(InventoryItem)
class InventoryItemAdmin(admin.ModelAdmin):
    list_display = ("name", "store", "product_id", "serial_no", "received_date", "price", "available", "updated_at")
    list_filter = ("store", "category")
    search_fields = ("name", "product_id", "serial_no", "brand")
    readonly_fields = ("updated_at",)

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path("package-report/", self.admin_site.admin_view(self.package_report_view), name="pos_inventory_package_report"),
        ]
        return custom + urls

    def package_report_view(self, request):
        rows = list(InventoryItem.objects.all().order_by("store", "name", "-received_date", "-updated_at"))
        grouped = defaultdict(list)
        for row in rows:
            grouped[(row.store, row.product_id, row.name, row.brand, row.category)].append(row)

        report = []
        cutoff = timezone.now().date() - timedelta(days=14)
        for (store, product_id, name, brand, category), items in grouped.items():
            received_dates = [it.received_date for it in items if it.received_date]
            latest = max(received_dates) if received_dates else None
            stale = [it for it in items if it.received_date and latest and it.received_date < latest and it.received_date <= cutoff]
            if not stale:
                continue
            report.append({
                "store": store,
                "product_id": product_id,
                "name": name,
                "brand": brand,
                "category": category,
                "latest_received": latest,
                "stale": stale,
                "active": [it for it in items if it not in stale],
            })

        if request.GET.get("format") == "csv":
            resp = HttpResponse(content_type="text/csv")
            resp["Content-Disposition"] = 'attachment; filename="package-report.csv"'
            w = csv.writer(resp)
            w.writerow(["store", "product_id", "name", "brand", "category", "stale_serial", "stale_received", "active_serials"])
            for row in report:
                active_serials = ";".join(str(it.serial_no or it.batch_id or it.product_id or "") for it in row["active"])
                for stale in row["stale"]:
                    w.writerow([
                        row["store"], row["product_id"], row["name"], row["brand"], row["category"],
                        stale.serial_no or stale.batch_id or stale.product_id or "",
                        stale.received_date or "",
                        active_serials,
                    ])
            return resp

        return render(request, "admin/pos/inventoryitem/package_report.html", {
            **self.admin_site.each_context(request),
            "title": "Package report",
            "report": report,
            "csv_url": reverse("admin:pos_inventory_package_report") + "?format=csv",
        })
