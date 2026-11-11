from django.test import TestCase

from .grouping import item_quantity_groups
from .models import Batch, Customer, Order


class ShoppingListGroupingTests(TestCase):
    def setUp(self):
        self.batch = Batch.objects.create(source=Batch.Source.CHINA, label="Test batch")
        self.ama = Customer.objects.create(name="Ama", phone="0241111111")
        self.kofi = Customer.objects.create(name="Kofi", phone="0242222222")

    def make_order(self, customer, item_name, quantity, details="", cancelled=False):
        return Order.objects.create(
            customer=customer,
            batch=self.batch,
            source_type=Order.SourceType.CHINA_PREORDER,
            item_name=item_name,
            item_notes=details,
            quantity=quantity,
            is_cancelled=cancelled,
        )

    def test_normalizes_names_counts_unique_people_and_sorts_by_pieces(self):
        orders = [
            self.make_order(self.ama, "Black handbag", 2, "Large"),
            self.make_order(self.ama, " black  HANDBAG ", 1, "Size M"),
            self.make_order(self.kofi, "BLACK HANDBAG", 3, "Black"),
            self.make_order(self.ama, "Phone case", 2),
            self.make_order(self.kofi, "Phone case", 3),
            self.make_order(self.kofi, "Wrist watch", 3),
            self.make_order(self.ama, "Cancelled item", 10, cancelled=True),
        ]

        groups = item_quantity_groups(orders)
        self.assertEqual(
            [(item["name"].strip().casefold(), item["people"], item["quantity"]) for item in groups],
            [("black handbag", 2, 6), ("phone case", 2, 5), ("wrist watch", 1, 3)],
        )
        handbag = groups[0]
        ama = next(customer for customer in handbag["customers"] if customer["name"] == "Ama")
        self.assertEqual(ama["quantity"], 3)
        self.assertEqual(ama["details"], ["Large", "Size M"])
        self.assertNotIn("Cancelled item", [item["name"] for item in groups])

    def test_legacy_orders_group_by_notes(self):
        old_a = self.make_order(self.ama, "", 2, "Black handbag")
        old_b = self.make_order(self.kofi, "", 4, " black HANDBAG ")
        groups = item_quantity_groups([old_a, old_b])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["people"], 2)
        self.assertEqual(groups[0]["quantity"], 6)
