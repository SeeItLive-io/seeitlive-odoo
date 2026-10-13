{
    'name': 'SeeItLive — Live Video Support',
    'version': '17.0.1.1.0',
    'category': 'Services',
    'summary': 'Start and watch a live customer video session from any Odoo record.',
    'description': """
SeeItLive for Odoo
==================

Start a live video session with a customer from a CRM lead, a task or a
contact: they open a link on their phone, share their camera, and the agent
watches — and annotates — from inside Odoo. Snapshots taken during the call
are attached back to the record with a summary in the chatter.

SeeItLive is a SaaS service (https://seeitlive.io). This module talks to the
SeeItLive API using an API key you configure in Odoo. No data is sent to
SeeItLive beyond the sessions you explicitly start.
""",
    'author': 'SeeItLive',
    'maintainer': 'SeeItLive',
    'website': 'https://seeitlive.io',
    'support': 'support@seeitlive.io',
    'license': 'LGPL-3',
    'depends': ['base', 'mail'],
    'external_dependencies': {'python': ['requests']},
    'data': [
        'security/ir.model.access.csv',
        'views/res_config_settings_views.xml',
        'views/seeitlive_session_views.xml',
        'views/seeitlive_menus.xml',
        'data/ir_cron.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'seeitlive_connect/static/src/js/seeitlive_panel.js',
            'seeitlive_connect/static/src/xml/seeitlive_panel.xml',
            'seeitlive_connect/static/src/scss/seeitlive.scss',
        ],
    },
    'installable': True,
    'application': True,
}
