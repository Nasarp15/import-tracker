from decimal import Decimal

from django import forms
from django.db import transaction
from django.db.models import F, Q

from .models import (
    Address,
    Batch,
    Customer,
    Order,
    OrderPhoto,
    Payment,
    StockItem,
    normalize_phone,
)


class OrderForm(forms.ModelForm):
    item_name = forms.CharField(
        label="Item name",
        max_length=120,
        required=False,
        widget=forms.TextInput(
            attrs={"list": "item-name-suggestions", "autocomplete": "off", "placeholder": "e.g. Black handbag"}
        ),
    )
    photo = forms.ImageField(
        label="Screenshot or photo",
        required=False,
        widget=forms.FileInput(attrs={"accept": "image/*"}),
    )
    customer_phone = forms.CharField(
        label="Customer phone",
        max_length=20,
        widget=forms.TextInput(
            attrs={"inputmode": "tel", "placeholder": "024 555 0123"}
        ),
    )
    customer_name = forms.CharField(label="Customer name", max_length=120, required=False)
    size = forms.CharField(label="Size (if the item has sizes)", max_length=40, required=False)
    delivery_details = forms.CharField(
        label="Delivery address",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    field_order = [
        "photo",
        "customer_phone",
        "customer_name",
        "delivery_details",
        "source_type",
        "stock_item",
        "batch",
        "item_name",
        "item_notes",
        "size",
        "quantity",
        "quoted_price",
        "cost",
        "cost_currency",
    ]

    class Meta:
        model = Order
        fields = [
            "source_type",
            "stock_item",
            "batch",
            "item_name",
            "item_notes",
            "quantity",
            "quoted_price",
            "cost",
            "cost_currency",
        ]
        labels = {
            "source_type": "Source",
            "stock_item": "Stock item (Nigeria stock sales only)",
            "item_name": "Item name",
            "item_notes": "Details (colour, size, variant, extras)",
            "quantity": "Quantity",
            "quoted_price": "Quoted price (total, GHS)",
            "cost": "My cost (private)",
            "cost_currency": "Cost currency",
        }
        widgets = {
            "item_notes": forms.Textarea(attrs={"rows": 2, "placeholder": "e.g. Large, size M, black"}),
            "quantity": forms.NumberInput(attrs={"min": 1}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["batch"].queryset = Batch.objects.exclude(status=Batch.Status.CLOSED)
        self.fields["batch"].empty_label = "No batch yet"
        stock_queryset = StockItem.objects.filter(quantity__gt=0)
        if self.instance and self.instance.pk and self.instance.stock_item_id:
            stock_queryset = StockItem.objects.filter(
                Q(quantity__gt=0) | Q(pk=self.instance.stock_item_id)
            )
        self.fields["stock_item"].queryset = stock_queryset
        self.fields["stock_item"].empty_label = "Not from stock"
        used_names = list(
            Order.objects.exclude(item_name="")
            .values_list("item_name", flat=True)
            .distinct()
            .order_by("item_name")[:100]
        )
        legacy_names = list(
            Order.objects.filter(item_name="")
            .exclude(item_notes="")
            .values_list("item_notes", flat=True)
            .distinct()
            .order_by("item_notes")[:100]
        )
        self.item_name_suggestions = list(dict.fromkeys(used_names + legacy_names))[:150]

        if self.instance and self.instance.pk:
            # Older orders stored the whole item description in item_notes.
            # Show that legacy value as the item name when editing, without
            # requiring a data migration or losing the original description.
            if not self.instance.item_name and self.instance.item_notes:
                self.fields["item_name"].initial = self.instance.item_notes.strip()
                self.fields["item_notes"].initial = ""
            self.fields["customer_phone"].initial = self.instance.customer.phone
            self.fields["customer_name"].initial = self.instance.customer.name
            if self.instance.address_id:
                self.fields["delivery_details"].initial = self.instance.address.details

    def clean_customer_phone(self):
        phone = normalize_phone(self.cleaned_data["customer_phone"])
        if len(phone) < 9:
            raise forms.ValidationError("Enter a valid phone number.")
        return phone

    def clean(self):
        cleaned = super().clean()
        stock = cleaned.get("stock_item")
        item_name = (cleaned.get("item_name") or "").strip()
        if stock:
            quantity = cleaned.get("quantity") or 1
            if cleaned.get("source_type") != Order.SourceType.NIGERIA_STOCK:
                self.add_error("stock_item", "Stock items are for Nigeria stock sales.")
            elif quantity > stock.quantity:
                self.add_error("quantity", f"Only {stock.quantity} left in stock.")
            else:
                if not item_name:
                    item_name = stock.name.strip()
                if cleaned.get("quoted_price") is None:
                    cleaned["quoted_price"] = stock.price * quantity
        if not item_name:
            self.add_error("item_name", "Enter the main item name.")
        else:
            cleaned["item_name"] = item_name
        size = (cleaned.get("size") or "").strip()
        if size:
            notes = cleaned.get("item_notes") or ""
            cleaned["item_notes"] = f"{notes} (size {size})".strip()
        return cleaned

    def save_order(self):
        """Create or update an order and keep stock quantities in sync."""
        data = self.cleaned_data

        with transaction.atomic():
            is_new_order = not bool(self.instance.pk)
            old_stock_id = self.instance.stock_item_id if not is_new_order else None
            old_quantity = (
                self.instance.quantity if not is_new_order and old_stock_id else 0
            )
            order = self.save(commit=False)
            new_stock_id = data["stock_item"].pk if data.get("stock_item") else None
            new_quantity = data["quantity"] if new_stock_id else 0

            # Lock the affected stock rows so simultaneous edits cannot oversell.
            stock_ids = sorted({pk for pk in (old_stock_id, new_stock_id) if pk})
            locked_stock = {
                item.pk: item
                for item in StockItem.objects.select_for_update().filter(pk__in=stock_ids)
            }

            if new_stock_id:
                available = locked_stock[new_stock_id].quantity
                if old_stock_id == new_stock_id:
                    available += old_quantity
                if new_quantity > available:
                    raise forms.ValidationError(
                        f"Only {available} left in stock. Please review the quantity."
                    )

            customer, _ = Customer.objects.get_or_create(
                phone=data["customer_phone"],
                defaults={"name": data["customer_name"]},
            )
            if data["customer_name"] and customer.name != data["customer_name"]:
                customer.name = data["customer_name"]
                customer.save(update_fields=["name"])

            order.customer = customer
            details = data["delivery_details"].strip()
            if details:
                address = customer.addresses.filter(details=details).first()
                if address is None:
                    address = Address.objects.create(
                        customer=customer,
                        details=details,
                        is_default=not customer.addresses.exists(),
                    )
                order.address = address
            else:
                order.address = None

            if is_new_order:
                order.stage = (
                    Order.Stage.QUOTED if order.quoted_price else Order.Stage.REQUESTED
                )
            order.save()

            # Restore the previous stock allocation, then deduct the edited one.
            if old_stock_id and old_stock_id != new_stock_id:
                StockItem.objects.filter(pk=old_stock_id).update(
                    quantity=F("quantity") + old_quantity
                )
            if new_stock_id:
                if old_stock_id == new_stock_id:
                    difference = old_quantity - new_quantity
                    if difference:
                        StockItem.objects.filter(pk=new_stock_id).update(
                            quantity=F("quantity") + difference
                        )
                else:
                    StockItem.objects.filter(pk=new_stock_id).update(
                        quantity=F("quantity") - new_quantity
                    )

            if data.get("photo"):
                OrderPhoto.objects.create(order=order, image=data["photo"])

        return order



class PaymentForm(forms.ModelForm):
    class Meta:
        model = Payment
        fields = ["kind", "amount", "method"]
        labels = {"kind": "Payment for", "amount": "Amount", "method": "Paid by"}

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount is None or amount <= 0:
            raise forms.ValidationError("Enter an amount greater than zero.")
        return amount


class ShippingForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = ["shipping_fee"]
        labels = {"shipping_fee": "Shipping fee (GHS)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["shipping_fee"].required = True

    def clean_shipping_fee(self):
        fee = self.cleaned_data["shipping_fee"]
        if fee is None or fee < 0:
            raise forms.ValidationError("Enter the shipping fee as a number.")
        return fee


class QuoteForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = ["quoted_price", "cost", "cost_currency"]
        labels = {
            "quoted_price": "Quoted price (total)",
            "cost": "My cost (private)",
            "cost_currency": "Cost currency",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["quoted_price"].required = True


class BatchForm(forms.ModelForm):
    class Meta:
        model = Batch
        fields = ["source", "label"]
        labels = {
            "source": "Where is it coming from?",
            "label": "Batch name (for example: China batch, 2 Sept)",
        }


class BatchRateForm(forms.ModelForm):
    class Meta:
        model = Batch
        fields = ["exchange_rate"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["exchange_rate"]
        field.required = True
        field.help_text = ""
        if self.instance.source == Batch.Source.CHINA:
            field.label = "Exchange rate (yuan per 1 cedi)"
        else:
            field.label = "Exchange rate (naira per 1 cedi)"

    def clean_exchange_rate(self):
        rate = self.cleaned_data["exchange_rate"]
        if rate is None or rate <= 0:
            raise forms.ValidationError("Enter a rate greater than zero.")
        return rate


class BatchBillForm(forms.ModelForm):
    class Meta:
        model = Batch
        fields = ["freight_total", "freight_margin_percent"]
        labels = {
            "freight_total": "Agent's bill for this batch (GHS)",
            "freight_margin_percent": "Extra on top, % (optional)",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["freight_total"].required = True
        self.fields["freight_total"].help_text = ""
        self.fields["freight_margin_percent"].required = False
        self.fields["freight_margin_percent"].help_text = ""

    def clean_freight_total(self):
        total = self.cleaned_data["freight_total"]
        if total is None or total <= 0:
            raise forms.ValidationError("Enter an amount greater than zero.")
        return total

    def clean_freight_margin_percent(self):
        margin = self.cleaned_data.get("freight_margin_percent")
        if margin is None:
            return Decimal("0")
        if margin < 0 or margin > 100:
            raise forms.ValidationError("Enter a percentage between 0 and 100.")
        return margin


class StockItemForm(forms.ModelForm):
    class Meta:
        model = StockItem
        fields = ["name", "description", "sizes", "price", "quantity", "image", "is_visible"]
        labels = {
            "name": "Item name",
            "description": "Short description",
            "sizes": "Sizes (optional, separated by commas)",
            "price": "Price (GHS)",
            "quantity": "How many are available",
            "image": "Photo",
            "is_visible": "Show on the public shop page",
        }
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "sizes": forms.TextInput(attrs={"placeholder": "S, M, L  or  38, 39, 40"}),
            "image": forms.FileInput(attrs={"accept": "image/*"}),
            "is_visible": forms.CheckboxInput(attrs={"style": "width:auto"}),
        }

    def clean_price(self):
        price = self.cleaned_data["price"]
        if price is None or price <= 0:
            raise forms.ValidationError("Enter a price greater than zero.")
        return price


class TrackForm(forms.Form):
    phone = forms.CharField(
        label="Your phone number",
        max_length=20,
        widget=forms.TextInput(
            attrs={"inputmode": "tel", "placeholder": "024 123 4567", "autocomplete": "tel"}
        ),
    )

    def clean_phone(self):
        phone = normalize_phone(self.cleaned_data["phone"])
        if len(phone) < 9 or len(phone) > 16:
            raise forms.ValidationError("Enter the phone number you used to order.")
        return phone