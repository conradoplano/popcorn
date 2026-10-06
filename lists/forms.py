from django import forms

from .models import Entry


class AddEntryForm(forms.Form):
    title = forms.CharField(max_length=200, widget=forms.TextInput(attrs={"placeholder": "Title", "autocomplete": "off"}))
    year = forms.IntegerField(
        required=False, min_value=1870, max_value=2100,
        widget=forms.NumberInput(attrs={"placeholder": "Year", "inputmode": "numeric"}),
    )

    def clean_title(self):
        return " ".join(self.cleaned_data["title"].split())


class EntryForm(forms.ModelForm):
    rating = forms.TypedChoiceField(
        choices=[("", "Not rated")] + [(n, "★" * n) for n in range(1, 6)],
        coerce=int, empty_value=None, required=False, widget=forms.RadioSelect,
    )

    class Meta:
        model = Entry
        fields = ["title", "year", "status", "rating", "notes"]
        widgets = {
            "status": forms.RadioSelect,
            "notes": forms.Textarea(attrs={"rows": 3, "placeholder": "Who recommended it, where to stream it, what you thought…"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].choices = [(s.value, s.label) for s in Entry.statuses_for(self.instance.kind)]
        self.fields["title"].widget.attrs["autocomplete"] = "off"

    def clean_title(self):
        return " ".join(self.cleaned_data["title"].split())

    def save(self, commit=True):
        entry = super().save(commit=False)
        entry.set_status(self.cleaned_data["status"])
        entry.set_rating(self.cleaned_data["rating"])
        if commit:
            entry.save()
        return entry
