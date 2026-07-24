"""
Safe idempotent migration that adds the four cached-URL fields.

Uses RunPython instead of AddField so it checks whether each column already
exists before issuing ALTER TABLE. This means the migration is safe to run
even if a previous crashed deploy already created some or all of the columns.

Columns added (all nullable/blank):
  myapp_elibrarymodel  .dropbox_thumbnail_url_cached  CharField(1000)
  myapp_elibrarymodel  .dropbox_thumbnail_url_expires DateTimeField
  myapp_hardbookimage  .dropbox_image_url_cached      CharField(1000)
  myapp_hardbookimage  .dropbox_image_url_expires     DateTimeField
"""

from django.db import migrations, connection


def _existing_columns(table):
    """Return a set of column names already present in *table*."""
    with connection.cursor() as cursor:
        cursor.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in cursor.fetchall()}


def add_columns_safe(apps, schema_editor):
    db = schema_editor.connection

    specs = [
        (
            "myapp_elibrarymodel",
            "dropbox_thumbnail_url_cached",
            "VARCHAR(1000) NULL",
        ),
        (
            "myapp_elibrarymodel",
            "dropbox_thumbnail_url_expires",
            "DATETIME NULL",
        ),
        (
            "myapp_hardbookimage",
            "dropbox_image_url_cached",
            "VARCHAR(1000) NULL",
        ),
        (
            "myapp_hardbookimage",
            "dropbox_image_url_expires",
            "DATETIME NULL",
        ),
    ]

    for table, column, col_type in specs:
        existing = _existing_columns(table)
        if column not in existing:
            with db.cursor() as cursor:
                cursor.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
                )


def noop_reverse(apps, schema_editor):
    # We intentionally do not drop columns on rollback to avoid data loss.
    pass


class Migration(migrations.Migration):

    dependencies = [
        # Depend on all three previous 0026/0027 no-ops so this runs last
        # regardless of which combination is recorded in django_migrations.
        ('myapp', '0026_cache_dropbox_image_urls'),
        ('myapp', '0026_merge_0002_elibrary_and_0025'),
        ('myapp', '0027_cache_dropbox_image_urls'),
    ]

    operations = [
        migrations.RunPython(add_columns_safe, reverse_code=noop_reverse),
    ]
