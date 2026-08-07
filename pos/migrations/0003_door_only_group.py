"""Create the `door-only` group so it is visible in Django admin, ready but empty.

Empty is the point: adding this migration changes nothing for anybody until a
manager actually puts a name in the group. Membership pins that person to the door
role no matter what their browser posts (pos.dutchie_auth.role_for).

Reversing drops the group. That RESTORES the ability to sell to everyone in it, so
the reverse deliberately refuses if it would silently re-arm someone.
"""
from django.db import migrations

GROUP = "door-only"


def create(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.get_or_create(name=GROUP)


def drop(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    group = Group.objects.filter(name=GROUP).first()
    if group is None:
        return
    if group.user_set.exists():
        raise RuntimeError(
            f"refusing to drop {GROUP!r}: {group.user_set.count()} staff are pinned to "
            "the door by it, and dropping it would silently give them the till. "
            "Empty the group in admin first if this is really what you want."
        )
    group.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("pos", "0002_inventoryitem_received_date"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]
    operations = [migrations.RunPython(create, drop)]
