from django.db import models


class FuelStation(models.Model):
    """One truck stop from the OPIS price file, placed at its town's coordinates."""

    opis_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=128)
    address = models.CharField(max_length=128)
    city = models.CharField(max_length=64)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.PositiveIntegerField(null=True, blank=True)
    # Several rows share an OPIS ID with different prices; we keep the lowest and
    # record how many rows were merged.
    price = models.DecimalField(max_digits=9, decimal_places=6)
    price_rows = models.PositiveSmallIntegerField(default=1)
    latitude = models.FloatField()
    longitude = models.FloatField()
    geocode_source = models.CharField(max_length=16)

    class Meta:
        ordering = ["opis_id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) ${self.price:.3f}"
