from django import forms

from .models import Entry, Tag


class AddEntryForm(forms.Form):
    title = forms.CharField(max_length=200, widget=forms.TextInput(attrs={"placeholder": "Title", "autocomplete": "off"}))
    year = forms.IntegerField(
        required=False, min_value=1870, max_value=2100,
        widget=forms.NumberInput(attrs={"placeholder": "Year", "inputmode": "numeric"}),
    )
    # Set when the title was picked from the TMDB search, and for a show, the season picked (empty: whole show).
    tmdb_id = forms.IntegerField(required=False, min_value=1, widget=forms.HiddenInput)
    season = forms.IntegerField(required=False, min_value=1, max_value=500, widget=forms.HiddenInput)

    def clean_title(self):
        return " ".join(self.cleaned_data["title"].split())


def split_names(text):
    """ "Netflix, Disney Plus" → ["Netflix", "Disney Plus"], without repeats."""
    names = []
    for name in text.split(","):
        name = " ".join(name.split())[:60]
        if name and name.lower() not in {n.lower() for n in names}:
            names.append(name)
    return names


class EntryForm(forms.ModelForm):
    rating = forms.TypedChoiceField(
        choices=[("", "Not rated")] + [(n, "★" * n) for n in range(1, 6)],
        coerce=int, empty_value=None, required=False, widget=forms.RadioSelect,
    )
    genre_names = forms.CharField(
        label="Genres", required=False, max_length=300,
        widget=forms.TextInput(attrs={"placeholder": "e.g. Comedy, Drama", "autocomplete": "off", "aria-label": "Genres"}),
    )
    provider_names = forms.CharField(
        label="Where to watch", required=False, max_length=1000,
        widget=forms.TextInput(attrs={"placeholder": "e.g. Netflix, Disney Plus", "autocomplete": "off", "aria-label": "Where to watch"}),
        help_text="Pick from the list or type your own, e.g. a DVD you own.",
    )

    class Meta:
        model = Entry
        fields = ["title", "year", "season", "status", "rating", "tags", "providers_sync", "notes"]
        widgets = {
            "status": forms.RadioSelect,
            "tags": forms.CheckboxSelectMultiple,
            "notes": forms.Textarea(attrs={"rows": 3, "placeholder": "Who recommended it, what you thought…"}),
        }

    def __init__(self, *args, logos=None, known=None, seasons=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Names TMDB lists for your country (lower case); others typed here become your other services.
        # None: unknown right now (TMDB not reachable), so nothing is added to them.
        self.known = known
        self.synced = self.instance.providers_sync
        if not self.instance.tmdb_id:
            del self.fields["providers_sync"]
        if self.instance.kind != Entry.Kind.SHOW:
            del self.fields["season"]
        else:
            # TMDB's seasons to choose from when it knows them, else a number.
            field = self.fields["season"]
            field.label = "Season"
            field.help_text = "Empty: the whole show, as it is now."
            if seasons:
                choices = [(s["number"], s["name"] + (f" ({s['year']})" if s["year"] else "")) for s in seasons]
                if self.instance.season and self.instance.season not in {s["number"] for s in seasons}:
                    choices.append((self.instance.season, f"Season {self.instance.season}"))
                field.widget = forms.Select(choices=[("", "The whole show")] + choices)
            else:
                field.widget = forms.NumberInput(attrs={"placeholder": "Whole show", "inputmode": "numeric"})
        # Logos of known streaming services, for ones entered by hand: {"netflix": "/logo.jpg"}.
        self.logos = logos or {}
        self.fields["status"].choices = [(s.value, s.label) for s in Entry.statuses_for(self.instance.kind)]
        self.fields["title"].widget.attrs["autocomplete"] = "off"
        self.fields["tags"].queryset = Tag.objects.filter(user=self.instance.user)
        self.fields["tags"].label = "My groups"
        self.initial["genre_names"] = ", ".join(self.instance.genres)
        # TMDB's services are shown (and kept) separately while synced; this field is the ones added by hand.
        self.initial["provider_names"] = ", ".join(p["name"] for p in self.instance.own_providers)

    def clean_title(self):
        return " ".join(self.cleaned_data["title"].split())

    def clean(self):
        data = super().clean()
        entry = self.instance
        if "season" in self.fields and "title" in data:
            others = entry.user.entries.filter(kind=entry.kind, season=data.get("season")).exclude(pk=entry.pk)
            same = others.filter(tmdb_id=entry.tmdb_id) if entry.tmdb_id else others.filter(
                tmdb_id__isnull=True, title__iexact=data["title"], year=data.get("year"))
            if same.exists():
                self.add_error("season", "That's already on your list.")
        return data

    def save(self, commit=True):
        entry = super().save(commit=False)
        entry.set_status(self.cleaned_data["status"])
        entry.set_rating(self.cleaned_data["rating"])
        entry.genres = split_names(self.cleaned_data["genre_names"])
        names = split_names(self.cleaned_data["provider_names"])
        logos = {**self.logos, **{p["name"].lower(): p["logo"] for p in entry.watch if p["logo"]}}
        entry.own_providers = [{"name": n, "logo": logos.get(n.lower(), "")} for n in names]
        if self.synced and not entry.providers_sync:
            if self.data.get("providers_complete"):
                # The page already moved TMDB's into the field (and some may have been removed there).
                entry.providers, entry.providers_sync = [], False
            else:
                entry.stop_sync()
        if self.known is not None:
            user = entry.user
            have = {s.lower() for s in user.all_services}
            new = [n for n in names if n.lower() not in self.known and n.lower() not in have]
            if new:
                user.own_services = user.own_services + new
                user.save(update_fields=["own_services"])
        if commit:
            entry.save()
            self.save_m2m()
        return entry


class ImportForm(forms.Form):
    text = forms.CharField(
        label="Titles", max_length=20000,
        widget=forms.Textarea(attrs={
            "rows": 8,
            "placeholder": "One per line, e.g.\nDune (2021)\nSeverance\nThe Bear S2\n\nOr paste a message with recommendations.",
        }),
    )
    kind = forms.ChoiceField(
        label="The list has", widget=forms.RadioSelect,
        choices=[(Entry.Kind.MOVIE.value, "Only movies"), (Entry.Kind.SHOW.value, "Only TV shows"), ("both", "Both")],
        error_messages={"required": "Choose whether these are movies, TV shows or both."},
    )
    add_as = forms.ChoiceField(
        label="Add them as", initial=Entry.Status.WANT, widget=forms.RadioSelect,
        choices=[(Entry.Status.WANT.value, "Want to watch"), (Entry.Status.WATCHED.value, "Watched")],
    )
    tag = forms.ModelChoiceField(label="Put them in", queryset=Tag.objects.none(), required=False, empty_label="No group")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tag"].queryset = user.tags.all()
