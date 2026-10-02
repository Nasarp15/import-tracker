from django import forms

from .models import Address, Batch, Customer, Order, OrderPhoto, normalize_phone


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
        "batch",
        "item_notes",
        "quantity",
        "quoted_price",
        "cost",
    ]

    class Meta:
        model = Order
        fields = ["source_type", "batch", "item_notes", "quantity", "quoted_price", "cost"]
        labels = {
            "source_type": "Source",
            "item_notes": "Item notes (colour, variant, size)",
            "quantity": "Quantity",
            "quoted_price": "Quoted price (total, GHS)",
            "cost": "My cost (private, GHS)",
        }
        widgets = {
            "item_notes": forms.Textarea(attrs={"rows": 2}),
            "quantity": forms.NumberInput(attrs={"min": 1}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["batch"].queryset = Batch.objects.exclude(status=Batch.Status.CLOSED)
        self.fields["batch"].empty_label = "No batch yet"

    def clean_customer_phone(self):
        phone = normalize_phone(self.cleaned_data["customer_phone"])
        if len(phone) < 9:
            raise forms.ValidationError("Enter a valid phone number.")
        return phone

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

        if data.get("photo"):
            OrderPhoto.objects.create(order=order, image=data["photo"])
        return order