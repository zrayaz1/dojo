from flask import Blueprint, Response, render_template, abort


stream = Blueprint("stream", __name__)


@stream.route("/stream")
def view_stream():
    return render_template("stream.html")
