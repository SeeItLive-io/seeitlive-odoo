"""Drop the retired `seeitlive.base_url` system parameter.

The API host is a constant now (see models/seeitlive_api.py), and its
Settings field is gone. Removing only the field would have stranded the row
in Settings > Technical > System Parameters, where it still reads like live
configuration while changing nothing — an upgraded instance that had been
pointed at a staging API would show that address forever and talk to
production regardless.
"""


def migrate(cr, version):
    cr.execute("DELETE FROM ir_config_parameter WHERE key = 'seeitlive.base_url'")
