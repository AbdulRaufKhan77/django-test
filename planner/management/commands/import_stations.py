import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from planner.models import FuelStation

DATA_DIR = Path(settings.BASE_DIR) / "data"
US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
}


class Command(BaseCommand):
    help = "Load the fuel price CSV into FuelStation, joined to data/city_coords.csv."

    def add_arguments(self, parser):
        parser.add_argument(
            "--prices", default=str(DATA_DIR / "fuel-prices-for-be-assessment.csv")
        )
        parser.add_argument("--coords", default=str(DATA_DIR / "city_coords.csv"))

    def handle(self, *args, prices, coords, **options):
        try:
            with open(coords, newline="") as fh:
                city_coords = {
                    (r["state"], r["city"]): (float(r["lat"]), float(r["lon"]), r["source"])
                    for r in csv.DictReader(fh)
                }
            with open(prices, newline="") as fh:
                rows = list(csv.DictReader(fh))
        except FileNotFoundError as exc:
            raise CommandError(str(exc)) from exc

        stations: dict[int, FuelStation] = {}
        skipped_foreign = skipped_bad_price = no_coords = 0
        for row in rows:
            state, city = row["State"].strip(), row["City"].strip()
            if state not in US_STATES:  # the file includes Canadian provinces
                skipped_foreign += 1
                continue
            try:
                price = Decimal(row["Retail Price"].strip())
            except InvalidOperation:
                skipped_bad_price += 1
                continue
            if price <= 0:
                skipped_bad_price += 1
                continue
            located = city_coords.get((state, city))
            if located is None:
                no_coords += 1
                continue

            opis_id = int(row["OPIS Truckstop ID"])
            existing = stations.get(opis_id)
            if existing is not None:
                existing.price_rows += 1
                if price < existing.price:
                    existing.price = price
                    existing.name = row["Truckstop Name"].strip()
                continue
            lat, lon, source = located
            rack = row["Rack ID"].strip()
            stations[opis_id] = FuelStation(
                opis_id=opis_id,
                name=row["Truckstop Name"].strip(),
                address=" ".join(row["Address"].split()),
                city=city,
                state=state,
                rack_id=int(rack) if rack.isdigit() else None,
                price=price,
                latitude=lat,
                longitude=lon,
                geocode_source=source,
            )

        with transaction.atomic():
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(stations.values(), batch_size=1000)

        merged = sum(s.price_rows - 1 for s in stations.values())
        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {len(stations)} US stations from {len(rows)} rows "
                f"({merged} duplicate-ID rows merged, {skipped_foreign} non-US, "
                f"{skipped_bad_price} bad price, {no_coords} without coordinates)."
            )
        )
