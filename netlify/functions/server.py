import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import serverless_wsgi
from app import app, db, criar_dados_iniciais

with app.app_context():
    db.create_all()
    criar_dados_iniciais()


def handler(event, context):
    return serverless_wsgi.handle_request(app, event, context)
