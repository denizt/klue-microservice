import os
import sys
import logging
import click
import pkg_resources
from uuid import uuid4
import yaml
from flask import Response, redirect
from flask_compress import Compress
from flask_cors import CORS
from klue.swagger.apipool import ApiPool
from klue_microservice.log import set_level
from klue_microservice.api import do_ping
from klue_microservice.crash import set_error_reporter, crash_handler
from klue_microservice.exceptions import format_error
from klue_microservice.auth import set_jwt_defaults


log = logging.getLogger(__name__)


#
# API: class to define then run a micro service api
#

class API(object):


    def __init__(self, app, host='localhost', port=80, debug=False, log_level=logging.DEBUG, formats=None, timeout=20, error_reporter=None, jwt_secret=None, jwt_audience=None, jwt_issuer=None, default_user_id=None, error_callback=format_error):
        """Take the flask app, and optionally the http port to listen on, and
        whether flask's debug mode is one or not, which callback to call when
        catching exceptions, and the api's log level"""
        assert app
        self.app = app
        self.port = port
        self.host = host
        self.debug = debug
        self.formats = formats
        self.timeout = timeout
        self.error_callback = error_callback
        set_jwt_defaults(
            secret=jwt_secret,
            audience=jwt_audience,
            issuer=jwt_issuer,
            user_id=default_user_id,
        )
        set_level(log_level)
        if error_reporter:
            set_error_reporter(error_reporter)
        log.info("Initialized API (%s:%s) (Flask debug:%s)" % (host, port, debug))


    def load_clients(self, path=None, apis=[]):
        """Generate client libraries for the given apis, without starting an
        api server"""

        if not path:
            raise Exception("Missing path to api swagger files")

        if type(apis) is not list:
            raise Exception("'apis' should be a list of api names")

        if len(apis) == 0:
            raise Exception("'apis' is an empty list - Expected at least one api name")

        for api_name in apis:
            api_path = os.path.join(path, '%s.yaml' % api_name)
            if not os.path.isfile(api_path):
                raise Exception("Cannot find swagger specification at %s" % api_path)
            log.info("Loading api %s from %s" % (api_name, api_path))
            ApiPool.add(
                api_name,
                yaml_path=api_path,
                timeout=self.timeout,
                error_callback=self.error_callback,
                formats=self.formats,
                do_persist=False,
                local=False,
            )

        return self


    def load_apis(self, path, ignore=[]):
        """Load all swagger files found at the given path, except those whose
        names are in the 'ignore' list"""

        if not path:
            raise Exception("Missing path to api swagger files")

        if type(ignore) is not list:
            raise Exception("'ignore' should be a list of api names")

        # Find all swagger apis under 'path'
        apis = {}

        log.debug("Searching path %s" % path)
        for root, dirs, files in os.walk(path):
            for f in files:
                if f.endswith('.yaml'):
                    api_name = f.replace('.yaml', '')

                    if api_name in ignore:
                        log.info("Ignoring api %s" % api_name)
                        continue

                    apis[api_name] = os.path.join(path, f)
                    log.debug("Found api %s in %s" % (api_name, f))

        # And add klue-microservice's default ping api
        ping_path = pkg_resources.resource_filename(__name__, 'klue_microservice/ping.yaml')
        if not os.path.isfile(ping_path):
            ping_path = os.path.join(os.path.dirname(sys.modules[__name__].__file__), 'ping.yaml')
        apis['ping'] = ping_path

        # Save found apis
        self.path_apis = path
        self.apis = apis

        return self


    def publish_apis(self, path='doc'):
        """Publish all loaded apis on under the uri /<path>/<api-name>, by
        redirecting to http://petstore.swagger.io/
        """

        assert path

        if not self.apis:
            raise Exception("You must call .load_apis() before .publish_apis()")

        # Get the live host from klue-config.yaml
        config_path = os.path.join(os.path.dirname(sys.argv[0]), 'klue-config.yaml')
        if not os.path.isfile(config_path):
            config_path = '/klue/klue-config.yaml'

        config = None
        with open(config_path, 'r') as stream:
            config = yaml.load(stream)

        if 'live_host' not in config:
            raise Exception("Cannot publish apis: klue-config.yaml lacks the 'live_host' key")

        proto = 'http'
        if 'aws_cert_arn' in config:
            proto = 'https'

        live_host = "%s://%s" % (proto, config['live_host'])

        # Allow cross-origin calls
        cors = CORS(self.app, resources={r"/%s/*" % path: {"origins": "*"}})

        # Add routes to serve api specs and redirect to petstore ui for each one
        for api_name, api_path in self.apis.items():

            api_filename = os.path.basename(api_path)
            log.info("Publishing api %s at /%s/%s" % (api_name, path, api_name))

            def redirect_to_petstore(live_host, api_filename):
                def f():
                    url = 'http://petstore.swagger.io/?url=%s/doc/%s' % (live_host, api_filename)
                    log.info("Redirecting to %s" % url)
                    return redirect(url, code=302)
                return f

            def serve_api_spec(api_path):
                def f():
                    with open(api_path, 'r') as f:
                        spec = f.read()
                        log.info("Serving %s" % api_path)
                        return Response(spec, mimetype='text/plain')
                return f

            self.app.add_url_rule('/%s/%s' % (path, api_name), str(uuid4()), redirect_to_petstore(live_host, api_filename))
            self.app.add_url_rule('/%s/%s' % (path, api_filename), str(uuid4()), serve_api_spec(api_path))

        return self


    def start(self, serve=[]):
        """Load all apis, either as local apis served by the flask app, or as
        remote apis to be called from whithin the app's endpoints, then start
        the app server"""

        # Check arguments
        if type(serve) is str:
            serve = [serve]
        elif type(serve) is list:
            pass
        else:
            raise Exception("'serve' should be an api name or a list of api names")

        if len(serve) == 0:
            raise Exception("You must specify at least one api to serve")

        for api_name in serve:
            if api_name not in self.apis:
                raise Exception("Can't find %s.yaml (swagger file) in the api directory %s" % (api_name, self.path_apis))

        app = self.app
        app.secret_key = os.urandom(24)

        # Always serve the ping api
        serve.append('ping')

        # Let's compress returned data when possible
        compress = Compress()
        compress.init_app(app)

        # All apis that are not served locally are not persistent
        not_persistent = []
        for api_name in self.apis.keys():
            if api_name in serve:
                pass
            else:
                not_persistent.append(api_name)

        # Now load those apis into the ApiPool
        for api_name, api_path in self.apis.items():

            host = None
            port = None

            if api_name in serve:
                # We are serving this api locally: override the host:port specified in the swagger spec
                host = self.host
                port = self.port

            do_persist = True if api_name not in not_persistent else False
            local = True if api_name in serve else False

            log.info("Loading api %s from %s (persist: %s)" % (api_name, api_path, do_persist))
            ApiPool.add(
                api_name,
                yaml_path=api_path,
                timeout=self.timeout,
                error_callback=self.error_callback,
                formats=self.formats,
                do_persist=do_persist,
                host=host,
                port=port,
                local=local,
            )

        ApiPool.merge()

        # Now spawn flask routes for all endpoints
        for api_name in self.apis.keys():
            if api_name in serve:
                log.info("Spawning api %s" % api_name)
                api = getattr(ApiPool, api_name)
                api.spawn_api(app, decorator=crash_handler)

        if os.path.basename(sys.argv[0]) == 'gunicorn':
            # Gunicorn takes care of spawning workers
            return

        # Debug mode is the default when not running via gunicorn
        app.debug = self.debug
        app.run(host='0.0.0.0', port=self.port)

#
# Generic code to start server, from command line or via gunicorn
#

def letsgo(name, callback=None):
    assert callback

    @click.command()
    @click.option('--port', help="Set server listening port (default: 80)", default=80)
    @click.option('--debug/--no-debug', default=True)
    def main(port, debug):
        callback(port, debug)

    if name == "__main__":
        main()

    if os.path.basename(sys.argv[0]) == 'gunicorn':
        callback()
