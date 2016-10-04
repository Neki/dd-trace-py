

# stdlib
import logging

# 3p
import requests
import wrapt

# project
import ddtrace
from ddtrace.compat import urlparse
from ddtrace.ext import http


log = logging.getLogger(__name__)


def patch():
    """ Monkeypatch the requests library to trace http calls. """
    wrapt.wrap_function_wrapper('requests', 'Session.request', _traced_request_func)


def _traced_request_func(func, instance, args, kwargs):
    """ traced_request is a tracing wrapper for requests' Session.request
        instance method.
    """

    # perhaps a global tracer isn't what we want, so permit individual requests
    # sessions to have their own (with the standard global fallback)
    tracer = getattr(instance, 'datadog_tracer', ddtrace.tracer)

    # bail on the tracing if not enabled.
    if not tracer.enabled:
        return func(*args, **kwargs)

    # FIXME[matt] be a bit less brittle here.
    method = kwargs.get('method') or args[0]
    url = kwargs.get('url') or args[1]

    with tracer.trace("requests.request") as span:
        resp = None
        try:
            resp = func(*args, **kwargs)
            return resp
        finally:

            try:
                _apply_tags(span, method, url, resp)
            except Exception:
                log.warn("error patching tags", exc_info=True)


def _apply_tags(span, method, url, response):
    """ apply_tags will patch the given span with tags about the given request. """
    try:
        parsed = urlparse.urlparse(url)
        span.service = parsed.netloc
        # FIXME[matt] how do we decide how do we normalize arbitrary urls???
        span.resource = "%s %s" % (method.upper(), parsed.path)
    except Exception:
        pass

    span.set_tag(http.METHOD, method)
    span.set_tag(http.URL, url)
    if response is not None:
        span.set_tag(http.STATUS_CODE, response.status_code)
        span.error = 500 <= response.status_code


class TracedSession(requests.Session):
    """ TracedSession is a requests' Session that is already patched.
    """
    pass

# Always patch our traced session with the traced method (cheesy way of sharing
# code)
wrapt.wrap_function_wrapper(TracedSession, 'request', _traced_request_func)
