from flask import Flask


def create_app() -> Flask:
    app = Flask(__name__)

    # Blueprints are registered in app/server.py to avoid circular imports
    return app
