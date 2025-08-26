from flask import Blueprint, Response, render_template, abort, current_app, request


import os


import requests

stream = Blueprint("stream", __name__)


@stream.route("/stream")
def view_stream():
    return render_template("stream.html")
