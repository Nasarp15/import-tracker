from django.contrib import admin

from .models import (
    Address,
    Batch,
    Customer,
    Order,
    OrderPhoto,
    Payment,
    ShippingRule,
    SizeBand,
)


class AddressInline(admin.TabularInline):
    model = Address
    extra = 0


class OrderPhotoInline(admin.TabularInline):
    model = OrderPhoto
    extra = 1


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0


class ShippingRuleInline(admin.TabularInline):
    model = ShippingRule
    extra = 0


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "created_at")
    search_fields = ("name", "phone")
    inlines = [AddressInline]


@admin.register(Batch)
class BatchAdmin(admin.ModelAdmin):
    list_display = ("label", "source", "status", "shipped_on", "arrived_on")
    list_filter = ("source", "status")
    actions = ["mark_arrived"]

    @admin.action(description="Mark selected batches as arrived")
    def mark_arrived(self, request, queryset):
        updated = sum(batch.mark_arrived() for batch in queryset)
        self.message_user(request, f"{updated} orders moved to Arrived.")


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "customer",
        "source_type",
        "stage",
        "quoted_price",
        "currency",
        "shipping_fee",
        "batch",
        "created_at",
    )
    list_filter = ("stage", "source_type", "batch", "is_cancelled")
    search_fields = ("customer__name", "customer__phone", "item_notes")
    autocomplete_fields = ["customer"]
    inlines = [OrderPhotoInline, PaymentInline]
    actions = ["apply_suggested_fees"]

    @admin.action(description="Fill empty shipping fees from the grid")
    def apply_suggested_fees(self, request, queryset):
        filled = 0
        for order in queryset.filter(shipping_fee__isnull=True):
            fee = order.suggested_shipping_fee()
            if fee is not None:
                order.shipping_fee = fee
                order.save(update_fields=["shipping_fee"])
                filled += 1
        self.message_user(request, f"Suggested fees applied to {filled} orders.")


@admin.register(SizeBand)
class SizeBandAdmin(admin.ModelAdmin):
    list_display = ("name", "sort_order")
    inlines = [ShippingRuleInline]