from decimal import Decimal

from django import forms
from django.db.models import F

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
            "item_notes",
            "quantity",
            "quoted_price",
            "cost",
            "cost_currency",
        ]
        labels = {
            "source_type": "Source",
            "stock_item": "Stock item (Nigeria stock sales only)",
            "item_notes": "Item notes (colour, variant, size)",
            "quantity": "Quantity",
            "quoted_price": "Quoted price (total, GHS)",
            "cost": "My cost (private)",
            "cost_currency": "Cost currency",
        }
        widgets = {
            "item_notes": forms.Textarea(attrs={"rows": 2}),
            "quantity": forms.NumberInput(attrs={"min": 1}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["batch"].queryset = Batch.objects.exclude(status=Batch.Status.CLOSED)
        self.fields["batch"].empty_label = "No batch yet"
        self.fields["stock_item"].queryset = StockItem.objects.filter(quantity__gt=0)
        self.fields["stock_item"].empty_label = "Not from stock"

    def clean_customer_phone(self):
        phone = normalize_phone(self.cleaned_data["customer_phone"])
        if len(phone) < 9:
            raise forms.ValidationError("Enter a valid phone number.")
        return phone

    def clean(self):
        cleaned = super().clean()
        stock = cleaned.get("stock_item")
        if stock:
            quantity = cleaned.get("quantity") or 1
            if cleaned.get("source_type") != Order.SourceType.NIGERIA_STOCK:
                self.add_error("stock_item", "Stock items are for Nigeria stock sales.")
            elif quantity > stock.quantity:
                self.add_error("quantity", f"Only {stock.quantity} left in stock.")
            else:
                if not cleaned.get("item_notes"):
                    cleaned["item_notes"] = stock.name
                if cleaned.get("quoted_price") is None:
                    cleaned["quoted_price"] = stock.price * quantity
        size = (cleaned.get("size") or "").strip()
        if size:
            notes = cleaned.get("item_notes") or ""
            cleaned["item_notes"] = f"{notes} (size {size})".strip()
        return cleaned

    def save_order(self):
        """Create (or find) the customer, then save the order and its photo."""
        data = self.cleaned_data

        customer, _ = Customer.objects.get_or_create(
            phone=data["customer_phone"],
            defaults={"name": data["customer_name"]},
        )
        if data["customer_name"] and not customer.name:
            customer.name = data["customer_name"]
            customer.save(update_fields=["name"])

        order = self.save(commit=False)
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

        order.stage = Order.Stage.QUOTED if order.quoted_price else Order.Stage.REQUESTED
        order.save()

        if order.stock_item_id:
            StockItem.objects.filter(pk=order.stock_item_id).update(
                quantity=F("quantity") - order.quantity
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