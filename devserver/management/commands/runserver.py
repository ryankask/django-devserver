from django.conf import settings
from django.core.management.commands.runserver import Command as BaseCommand
from django.core.management.base import CommandError, handle_default_options
from django.core.servers.basehttp import WSGIServerException, WSGIServer
from django.core.handlers.wsgi import WSGIHandler
from django.utils import six

import os
import sys
import imp
from six.moves import socketserver
from optparse import make_option

from devserver.handlers import DevServerHandler
from devserver.utils.http import SlimWSGIRequestHandler


STATICFILES_APPS = ('django.contrib.staticfiles', 'staticfiles')


def null_technical_500_response(request, exc_type, exc_value, tb):
    six.reraise(exc_type, exc_value, tb)


def run(addr, port, wsgi_handler, mixin=None, ipv6=False):
    if mixin:
        class new(mixin, WSGIServer):
            def __init__(self, *args, **kwargs):
                WSGIServer.__init__(self, *args, **kwargs)
    else:
        new = WSGIServer
    server_address = (addr, port)
    new.request_queue_size = 10
    httpd = new(server_address, SlimWSGIRequestHandler, ipv6=ipv6)
    httpd.set_app(wsgi_handler)
    httpd.serve_forever()


class Command(BaseCommand):
    option_list = BaseCommand.option_list + (
        make_option(
            '--werkzeug', action='store_true', dest='use_werkzeug', default=False,
            help='Tells Django to use the Werkzeug interactive debugger.'),
        make_option(
            '--forked', action='store_true', dest='use_forked', default=False,
            help='Use forking instead of threading for multiple web requests.'),
        make_option(
            '--dozer', action='store_true', dest='use_dozer', default=False,
            help='Enable the Dozer memory debugging middleware.'),
        make_option(
            '--wsgi-app', dest='wsgi_app', default=None,
            help='Load the specified WSGI app as the server endpoint.'),
    )
    if any(map(lambda app: app in settings.INSTALLED_APPS, STATICFILES_APPS)):
        option_list += make_option(
            '--nostatic', dest='use_static_files', action='store_false', default=True,
            help='Tells Django to NOT automatically serve static files at STATIC_URL.'),

    help = "Starts a lightweight Web server for development which outputs additional debug information."
    args = '[optional port number, or ipaddr:port]'

    # Validation is called explicitly each time the server is reloaded.
    requires_model_validation = False

    def run_from_argv(self, argv):
        parser = self.create_parser(argv[0], argv[1])
        default_args = getattr(settings, 'DEVSERVER_ARGS', None)
        if default_args:
            options, args = parser.parse_args(default_args)
        else:
            options = None

        options, args = parser.parse_args(argv[2:], options)

        handle_default_options(options)
        self.execute(*args, **options.__dict__)

    def handle(self, addrport='', *args, **options):
        if args:
            raise CommandError('Usage is runserver %s' % self.args)

        if not addrport:
            addr = getattr(settings, 'DEVSERVER_DEFAULT_ADDR', '127.0.0.1')
            port = getattr(settings, 'DEVSERVER_DEFAULT_PORT', '8000')
            addrport = '%s:%s' % (addr, port)

        return super(Command, self).handle(addrport=addrport, *args, **options)

    def get_handler(self, *args, **options):
        if int(options['verbosity']) < 1:
            handler = WSGIHandler()
        else:
            handler = DevServerHandler()

        # AdminMediaHandler is removed in Django 1.5
        # Add it only when it avialable.
        try:
            from django.core.servers.basehttp import AdminMediaHandler
        except ImportError:
            pass
        else:
            handler = AdminMediaHandler(
                handler, options['admin_media_path'])

        if 'django.contrib.staticfiles' in settings.INSTALLED_APPS and options['use_static_files']:
            from django.contrib.staticfiles.handlers import StaticFilesHandler
            handler = StaticFilesHandler(handler)

        return handler

    def inner_run(self, *args, **options):
        # Flag the server as active
        from devserver import settings
        import devserver
        settings.DEVSERVER_ACTIVE = True
        settings.DEBUG = True

        from django.conf import settings
        from django.utils import translation

        shutdown_message = options.get('shutdown_message', '')
        use_werkzeug = options.get('use_werkzeug', False)
        quit_command = (sys.platform == 'win32') and 'CTRL-BREAK' or 'CONTROL-C'
        wsgi_app = options.get('wsgi_app', None)

        if use_werkzeug:
            try:
                from werkzeug import run_simple, DebuggedApplication
            except ImportError as e:
                self.stderr.write("WARNING: Unable to initialize werkzeug: %s\n" % e)
                use_werkzeug = False
            else:
                from django.views import debug
                debug.technical_500_response = null_technical_500_response

        self.stdout.write("Validating models...\n\n")
        self.validate(display_num_errors=True)
        self.stdout.write((
            "Django version %(version)s, using settings %(settings)r\n"
            "Running django-devserver %(devserver_version)s\n"
            "%(server_model)s %(server_type)s server is running at http://%(addr)s:%(port)s/\n"
            "Quit the server with %(quit_command)s.\n"
        ) % {
            "server_type": use_werkzeug and 'werkzeug' or 'Django',
            "server_model": options['use_forked'] and 'Forked' or 'Threaded',
            "version": self.get_version(),
            "devserver_version": devserver.get_version(),
            "settings": settings.SETTINGS_MODULE,
            "addr": self._raw_ipv6 and '[%s]' % self.addr or self.addr,
            "port": self.port,
            "quit_command": quit_command,
        })

        # django.core.management.base forces the locale to en-us. We should
        # set it up correctly for the first request (particularly important
        # in the "--noreload" case).
        translation.activate(settings.LANGUAGE_CODE)

        app = self.get_handler(*args, **options)
        if wsgi_app:
            self.stdout.write("Using WSGI application %r\n" % wsgi_app)
            if os.path.exists(os.path.abspath(wsgi_app)):
                # load from file
                app = imp.load_source('wsgi_app', os.path.abspath(wsgi_app)).application
            else:
                try:
                    app = __import__(wsgi_app, {}, {}, ['application']).application
                except (ImportError, AttributeError):
                    raise

        if options['use_forked']:
            mixin = socketserver.ForkingMixIn
        else:
            mixin = socketserver.ThreadingMixIn

        middleware = getattr(settings, 'DEVSERVER_WSGI_MIDDLEWARE', [])
        for middleware in middleware:
            module, class_name = middleware.rsplit('.', 1)
            app = getattr(__import__(module, {}, {}, [class_name]), class_name)(app)

        if options['use_dozer']:
            from dozer import Dozer
            app = Dozer(app)

        try:
            if use_werkzeug:
                run_simple(
                    self.addr, int(self.port), DebuggedApplication(app, True),
                    use_reloader=False, use_debugger=True)
            else:
                run(self.addr, int(self.port), app, mixin, ipv6=options['use_ipv6'])

        except WSGIServerException as e:
            # Use helpful error messages instead of ugly tracebacks.
            ERRORS = {
                13: "You don't have permission to access that port.",
                98: "That port is already in use.",
                99: "That IP address can't be assigned-to.",
            }
            try:
                error_text = ERRORS[e.args[0].args[0]]
            except (AttributeError, KeyError):
                error_text = str(e)
            sys.stderr.write(self.style.ERROR("Error: %s" % error_text) + '\n')
            # Need to use an OS exit because sys.exit doesn't work in a thread
            os._exit(1)

        except KeyboardInterrupt:
            if shutdown_message:
                self.stdout.write("%s\n" % shutdown_message)
            sys.exit(0)
