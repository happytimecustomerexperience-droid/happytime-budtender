"""Local inventory cache — so browse doesn't hit Dutchie on every keystroke (scalable).

Refreshed periodically from the Dutchie REST read API (PosReadClient). The live
batch/serial used for the actual cart-add is still re-fetched at add time when a
product_search path is wired; the cache powers fast, paginated browse.
"""

from django.db import models


class InventoryItem(models.Model):
    store = models.CharField(max_length=120, db_index=True)
    product_id = models.BigIntegerField(null=True, db_index=True)
    batch_id = models.BigIntegerField(null=True)
    serial_no = models.CharField(max_length=64, blank=True)
    name = models.CharField(max_length=300)
    category = models.CharField(max_length=120, blank=True)
    brand = models.CharField(max_length=120, blank=True)
    price = models.FloatField(default=0)
    available = models.FloatField(default=0)
    cannabis = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["store", "name"]),
            models.Index(fields=["store", "product_id"]),
        ]
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} @ {self.store}"

    def as_cart_item(self) -> dict:
        return {
            "ProductId": self.product_id, "BatchId": self.batch_id,
            "SerialNo": self.serial_no, "AvailOz": self.available,
            "RecUnitPrice": self.price, "UnitPrice": self.price,
            "ProductDesc": self.name, "CannbisProduct": "Yes" if self.cannabis else "No",
        }
