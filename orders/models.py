from decimal import Decimal

from django.db import models, transaction
from django.db.models import Q, Sum
from django.utils import timezone

# The shipping grid is defined in this currency. Order costs are converted to it
# (using the exchange rate saved on the order) before the grid lookup.
GRID_CURRENCY = "GHS"


class Currency(models.TextChoices):
    GHS = "GHS", "Ghana cedi"
    NGN = "NGN", "Nigerian naira"
    CNY = "CNY", "Chinese yuan"
    USD = "USD", "US dollar"


def normalize_phone(raw):
    """Store phone numbers in one format so repeat customers always match.

    Assumes Ghana (+233) when the number starts with a local 0. Customers with
    other country codes should be entered as +234..., +86..., and so on.
    """
    cleaned = "".join(ch for ch in raw if ch.isdigit() or ch == "+")
    if cleaned.startswith("+"):
        return cleaned
    if cleaned.startswith("00"):
        return "+" + cleaned[2:]
    if cleaned.startswith("0"):
        return "+233" + cleaned[1:]
    return "+" + cleaned


class Customer(models.Model):
    name = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=20, unique=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "phone"]

    def save(self, *args, **kwargs):
        self.phone = normalize_phone(self.phone)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name or 'Unnamed'} ({self.phone})"


class Address(models.Model):
    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE, related_name="addresses"
    )
    label = models.CharField(max_length=40, blank=True)
    details = models.TextField(help_text="Area, landmark, directions")
    is_default = models.BooleanField(default=False)

    class Meta:
        verbose_name_plural = "addresses"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_default:
            Address.objects.filter(customer=self.customer).exclude(pk=self.pk).update(
                is_default=False
            )

    def __str__(self):
        return f"{self.label or 'Address'}: {self.details[:40]}"


class Batch(models.Model):
    class Source(models.TextChoices):
        CHINA = "china", "China"
        NIGERIA = "nigeria", "Nigeria"

    class Status(models.TextChoices):
        ORDERING = "ordering", "Ordering"
        SHIPPED = "shipped", "Shipped"
        ARRIVED = "arrived", "Arrived in Ghana"
        CLOSED = "closed", "Closed"

    source = models.CharField(max_length=10, choices=Source.choices)
    label = models.CharField(max_length=80)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.ORDERING
    )
    shipped_on = models.DateField(null=True, blank=True)
    arrived_on = models.DateField(null=True, blank=True)
    freight_total = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    freight_currency = models.CharField(
        max_length=3, choices=Currency.choices, default=Currency.GHS
    )

    class Meta:
        ordering = ["-id"]
        verbose_name_plural = "batches"

    def mark_arrived(self, arrived_on=None):
        """Mark the batch arrived and move all its in-transit orders with it.

        Returns the number of orders updated.
        """
        with transaction.atomic():
            self.status = self.Status.ARRIVED
            self.arrived_on = arrived_on or timezone.localdate()
            self.save(update_fields=["status", "arrived_on"])
            return (
                self.orders.active()
                .filter(stage__in=[Order.Stage.PURCHASED, Order.Stage.IN_TRANSIT])
                .update(stage=Order.Stage.ARRIVED)
            )

    def __str__(self):
        return self.label


class SizeBand(models.Model):
    name = models.CharField(max_length=30, unique=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class ShippingRule(models.Model):
    """One cell of the shipping grid: a size band plus a cost range gives a fee.

    The cost range is cost_min <= cost < cost_max, in GRID_CURRENCY.
    Leave cost_max empty for "and above".
    """

    size_band = models.ForeignKey(
        SizeBand, on_delete=models.CASCADE, related_name="rules"
    )
    cost_min = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cost_max = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    fee = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        ordering = ["size_band__sort_order", "cost_min"]

    @classmethod
    def lookup(cls, size_band, cost):
        """Return the suggested fee for a size band and cost, or None."""
        if size_band is None or cost is None:
            return None
        rule = (
            cls.objects.filter(size_band=size_band, cost_min__lte=cost)
            .filter(Q(cost_max__isnull=True) | Q(cost_max__gt=cost))
            .order_by("-cost_min")
            .first()
        )
        return rule.fee if rule else None

    def __str__(self):
        upper = self.cost_max if self.cost_max is not None else "and above"
        return f"{self.size_band}: {self.cost_min} to {upper} -> {self.fee}"


class OrderQuerySet(models.QuerySet):
    def active(self):
        return self.filter(is_cancelled=False)

    def awaiting_shipping_payment(self):
        return self.active().filter(stage=Order.Stage.ARRIVED)


class Order(models.Model):
    class SourceType(models.TextChoices):
        CHINA_PREORDER = "china_preorder", "China preorder"
        NIGERIA_PREORDER = "nigeria_preorder", "Nigeria preorder"
        NIGERIA_STOCK = "nigeria_stock", "Nigeria stock"

    class Stage(models.TextChoices):
        REQUESTED = "requested", "Requested"
        QUOTED = "quoted", "Quoted"
        PAID = "paid", "Paid"
        PURCHASED = "purchased", "Purchased"
        IN_TRANSIT = "in_transit", "In transit"
        ARRIVED = "arrived", "Arrived"
        SHIPPING_PAID = "shipping_paid", "Shipping paid"
        DELIVERED = "delivered", "Delivered"

    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="orders"
    )
    address = models.ForeignKey(
        Address, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders"
    )
    batch = models.ForeignKey(
        Batch, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders"
    )
    source_type = models.CharField(max_length=20, choices=SourceType.choices)
    item_notes = models.TextField(blank=True, help_text="Colour, variant, etc.")
    quantity = models.PositiveIntegerField(default=1)

    # Totals for the whole order line, not per unit.
    quoted_price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    currency = models.CharField(
        max_length=3, choices=Currency.choices, default=Currency.GHS
    )

    # Private to the owner. Never include these in customer-facing messages.
    cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    cost_currency = models.CharField(
        max_length=3, choices=Currency.choices, default=Currency.GHS
    )
    exchange_rate = models.DecimalField(
        max_digits=12,
        decimal_places=6,
        null=True,
        blank=True,
        help_text="Multiply cost by this to get cedis. Saved so later rate changes don't alter history.",
    )

    size_band = models.ForeignKey(
        SizeBand, on_delete=models.SET_NULL, null=True, blank=True
    )
    shipping_fee = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )

    stage = models.CharField(
        max_length=15, choices=Stage.choices, default=Stage.REQUESTED
    )
    is_cancelled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    delivered_on = models.DateField(null=True, blank=True)

    objects = OrderQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["stage"])]

    @property
    def cost_in_grid_currency(self):
        if self.cost is None:
            return None
        if self.cost_currency == GRID_CURRENCY:
            return self.cost
        if self.exchange_rate:
            return (self.cost * self.exchange_rate).quantize(Decimal("0.01"))
        return None

    def suggested_shipping_fee(self):
        return ShippingRule.lookup(self.size_band, self.cost_in_grid_currency)

    def paid_total(self, kind):
        total = self.payments.filter(kind=kind, currency=self.currency).aggregate(
            t=Sum("amount")
        )["t"]
        return total or Decimal("0")

    @property
    def shipping_balance(self):
        if self.shipping_fee is None:
            return None
        return self.shipping_fee - self.paid_total(Payment.Kind.SHIPPING)

    def refresh_stage_from_payments(self):
        """Advance the stage when the matching payment is complete."""
        if self.is_cancelled:
            return
        new_stage = None
        if (
            self.stage in (self.Stage.REQUESTED, self.Stage.QUOTED)
            and self.quoted_price
            and self.paid_total(Payment.Kind.ITEM) >= self.quoted_price
        ):
            new_stage = self.Stage.PAID
        elif (
            self.stage == self.Stage.ARRIVED
            and self.shipping_fee is not None
            and self.paid_total(Payment.Kind.SHIPPING) >= self.shipping_fee
        ):
            new_stage = self.Stage.SHIPPING_PAID
        if new_stage:
            self.stage = new_stage
            self.save(update_fields=["stage"])

    def __str__(self):
        return f"Order {self.pk} for {self.customer}"


class OrderPhoto(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="photos")
    image = models.ImageField(upload_to="orders/%Y/%m/")
    created_at = models.DateTimeField(auto_now_add=True)


class Payment(models.Model):
    class Kind(models.TextChoices):
        ITEM = "item", "Item"
        SHIPPING = "shipping", "Shipping"

    class Method(models.TextChoices):
        MOMO = "momo", "Mobile money"
        CASH = "cash", "Cash"
        BANK = "bank", "Bank transfer"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="payments")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(
        max_length=3, choices=Currency.choices, default=Currency.GHS
    )
    method = models.CharField(max_length=10, choices=Method.choices, default=Method.MOMO)
    paid_on = models.DateField(default=timezone.localdate)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.order.refresh_stage_from_payments()