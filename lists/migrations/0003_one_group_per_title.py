"""A title is in one of its owner's groups at most: the many-to-many "tags" becomes the foreign key "tag".
Titles that were in several groups keep the one they were put in first."""
import django.db.models.deletion
from django.db import migrations, models


def keep_first_group(apps, schema_editor):
    Entry = apps.get_model("lists", "Entry")
    Through = Entry._meta.get_field("tags").remote_field.through
    done = set()
    for entry_id, tag_id in Through.objects.order_by("id").values_list("entry_id", "tag_id"):
        if entry_id not in done:
            done.add(entry_id)
            Entry.objects.filter(pk=entry_id).update(tag_id=tag_id)


def back_to_several(apps, schema_editor):
    Entry = apps.get_model("lists", "Entry")
    Through = Entry._meta.get_field("tags").remote_field.through
    Through.objects.bulk_create([
        Through(entry_id=entry_id, tag_id=tag_id)
        for entry_id, tag_id in Entry.objects.filter(tag__isnull=False).values_list("id", "tag_id")
    ])


class Migration(migrations.Migration):

    dependencies = [
        ("lists", "0002_recommendations"),
    ]

    operations = [
        migrations.AddField(
            model_name="entry",
            name="tag",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="+", to="lists.tag", verbose_name="group"),
        ),
        migrations.RunPython(keep_first_group, back_to_several),
        migrations.RemoveField(model_name="entry", name="tags"),
        migrations.AlterField(
            model_name="entry",
            name="tag",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="entries", to="lists.tag", verbose_name="group"),
        ),
    ]
