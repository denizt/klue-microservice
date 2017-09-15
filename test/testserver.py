#!/usr/bin/env python

import os
import sys
import logging
import json
from flask import Flask

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from klue_microservice import API, letsgo


log = logging.getLogger(__name__)


app = Flask(__name__)


def test_crash_reporter(msg, body):

    tmpdir = '/tmp/test-klue-microservice'
    try:
        os.stat(tmpdir)
    except:
        os.mkdir(tmpdir)

    data = {
        'title': msg,
        'body': json.loads(body),
    }

    with open(os.path.join(tmpdir, "error_report.json"), "a+") as f:
        f.write(json.dumps(data))


def start(port, debug):

    api = API(
        app,
        port=8765,
        debug=False,
        error_reporter=test_crash_reporter,
        jwt_secret=os.environ.get("KLUE_JWT_SECRET"),
        jwt_audience=os.environ.get("KLUE_JWT_AUDIENCE"),
        jwt_issuer=os.environ.get("KLUE_JWT_ISSUER"),
    )
    api.load_apis('.', include_crash_api=True)
    api.start(serve="crash")

letsgo(__name__, callback=start)
