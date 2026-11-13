#!/bin/sh
# Build a throwaway Odoo with this module installed, so that a directory such as Glama can
# start the server and check that it answers MCP introspection.
#
#   sh glama/build.sh          # Debian 13 (trixie), as root, from the repository root
#
# Installs PostgreSQL and the Debian packages Odoo needs, fetches the matching Odoo Community
# branch (only base, web and rpc), creates a database "glama" with this module and demo data,
# and an API key for its administrator. Everything listens on 127.0.0.1 inside the container.
# Not needed to use the module: see README.md for a normal installation.
set -eu

APP=$(cd "$(dirname "$0")/.." && pwd)
SERIES=$(sed -n "s/^ *'version': *'\([0-9]*\.[0-9]*\)\..*/\1/p" "$APP/calderkit_mcp/__manifest__.py")
ODOO=/opt/odoo
DATA=/var/lib/odoo-glama
CONF=/etc/odoo-glama.conf
DB=glama

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates git postgresql python3 \
    python3-asn1crypto python3-babel python3-cbor2 python3-chardet python3-cryptography \
    python3-dateutil python3-docutils python3-freezegun python3-geoip2 python3-gevent \
    python3-greenlet python3-h11 python3-idna python3-jinja2 python3-ldap python3-libsass \
    python3-lxml python3-lxml-html-clean python3-magic python3-markupsafe python3-num2words \
    python3-ofxparse python3-openpyxl python3-openssl python3-passlib python3-pil python3-polib \
    python3-psutil python3-psycopg2 python3-pypdf python3-qrcode python3-reportlab \
    python3-requests python3-rjsmin python3-serial python3-stdnum python3-urllib3 python3-usb \
    python3-vobject python3-werkzeug python3-xlrd python3-xlsxwriter python3-zeep
rm -rf /var/lib/apt/lists/*

# Odoo Community, same series as the module. The whole tree is needed: creating a database
# reads the manifests of all modules, and installing web pulls in its auto-install companions.
git clone --quiet --depth 1 --branch "$SERIES" https://github.com/odoo/odoo.git "$ODOO"
rm -rf "$ODOO/.git"

id odoo >/dev/null 2>&1 || useradd --system --user-group --create-home --home-dir "$DATA" \
    --shell /usr/sbin/nologin odoo
# Odoo runs as "odoo" and cannot write bytecode next to the sources: compile it once here.
/usr/bin/python3 -m compileall -q -j 0 "$ODOO/odoo" "$ODOO/addons" "$APP/calderkit_mcp" >/dev/null || true

PGV=$(ls /etc/postgresql 2>/dev/null | sort -n | tail -n 1)
if [ -z "$PGV" ]; then
    PGV=$(ls /usr/lib/postgresql | sort -n | tail -n 1)
    pg_createcluster "$PGV" main
fi
pg_ctlcluster "$PGV" main start
runuser -u postgres -- psql -qc "CREATE ROLE odoo LOGIN CREATEDB" 2>/dev/null || true

ADMIN_PASSWD=$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n')
cat > "$CONF" <<EOF
[options]
addons_path = $ODOO/odoo/addons,$ODOO/addons,$APP
data_dir = $DATA
db_name = $DB
dbfilter = ^$DB\$
list_db = False
http_interface = 127.0.0.1
http_port = 8069
log_level = warn
admin_passwd = $ADMIN_PASSWD
EOF
chmod 640 "$CONF"
chgrp odoo "$CONF"

case "$SERIES" in
    18.*) DEMO= ;;            # Odoo 18 loads demo data unless told not to
    *) DEMO=--with-demo ;;
esac
runuser -u odoo -- /usr/bin/python3 "$ODOO/odoo-bin" -c "$CONF" -i calderkit_mcp $DEMO --stop-after-init

# A persistent API key for the demo administrator, valid only in this throwaway database.
runuser -u odoo -- /usr/bin/python3 "$ODOO/odoo-bin" shell -c "$CONF" --no-http <<'PY'
admin = env.ref('base.user_admin')
key = env['res.users.apikeys'].with_user(admin).sudo()._generate('mcp', 'Directory check', None)
env.cr.commit()
with open('/var/lib/odoo-glama/mcp_key', 'w') as handle:
    handle.write(key)
PY
test -s "$DATA/mcp_key"

pg_ctlcluster "$PGV" main stop
echo "Odoo $SERIES with calderkit_mcp is ready. Start it with: python3 glama/serve.py"
