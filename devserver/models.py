from django.core import exceptions
from django.utils import six

from devserver.logger import GenericLogger


MODULES = []


def load_modules():
    global MODULES

    MODULES = []

    from devserver import settings

    for path in settings.DEVSERVER_MODULES:
        try:
            name, class_name = path.rsplit('.', 1)
        except ValueError:
            six.reraise(exceptions.ImproperlyConfigured, '%s isn\'t a devserver module' % path)

        try:
            module = __import__(name, {}, {}, [''])
        except ImportError as exceptions:
            six.reraise(ImproperlyConfigured, 'Error importing devserver module %s: "%s"' % (name, e))

        try:
            cls = getattr(module, class_name)
        except AttributeError:
            six.reraise(exceptions.ImproperlyConfigured, 'Error importing devserver module "%s" does not define a "%s" class' % (name, class_name))

        try:
            instance = cls(GenericLogger(cls))
        except:
            raise  # Bubble up problem loading panel

        MODULES.append(instance)

if not MODULES:
    load_modules()
