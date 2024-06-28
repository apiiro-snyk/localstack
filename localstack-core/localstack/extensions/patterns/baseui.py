import logging
import typing as t

from rolo.gateway import HandlerChain
from rolo.router import RuleAdapter, WithHost
from werkzeug.routing import Submount

from localstack import config
from localstack.aws.api import RequestContext
from localstack.extensions.api import Extension, http

LOG = logging.getLogger(__name__)

_default = object()


class WebAppBaseExtension(Extension):
    """
    EXPERIMENTAL! This class is experimental and the API may change without notice.

    A webapp extension serves routes via a submount and a subdomain through localstack.


    Given this layout, you can define your extensions in ``my_extension.extension`` like this. Routes defined in the
    extension itself are automatically registered::

        class MyExtension(WebAppExtension):
            name = "my-extension"

            @route("/")
            def index(request: Request) -> Response:
                # reference `static/style.css` to serve the static file from your package
                return self.render_template_response("index.html")

            @route("/hello")
            def hello(request: Request):
                return {"message": "Hello World!"}

    This will create an extension that localstack serves via:

    * Submount: https://localhost.localstack.cloud:4566/_extension/my-extension
    * Subdomain: https://my-extension.localhost.localstack.cloud:4566/

    Both are created for full flexibility:

    * Subdomains: create a domain namespace that can be helpful for some extensions, especially when
      running on the local machine
    * Submounts: for some environments, like in ephemeral instances where subdomains are harder to control,
      submounts are more convenient

    Any routes added by the extension will be served relative to these URLs.
    """

    def __init__(
            self,
            mount: str = None,
            submount: str | None = _default,
            subdomain: str | None = _default,
    ):
        """
        Overwrite to customize your extension. For example, you can disable certain behavior by calling
        ``super( ).__init__(subdomain=None)``, which will disable serving through a subdomain.

        :param mount: the "mount point" which will be used as default value for the submount and
            subdirectory, i.e., ``<mount>.localhost.localstack.cloud`` and
            ``localhost.localstack.cloud/_extension/<mount>``. Defaults to the extension name.  Note that,
            in case the mount name clashes with another extension, extensions may overwrite each other's
            routes.
        :param submount: the submount path, needs to start with a trailing slash (default
            ``/_extension/<mount>``)
        :param subdomain: the subdomain (defaults to the value of ``mount``)
        """
        mount = mount or self.name

        self.submount = f"/_extension/{mount}" if submount is _default else submount
        self.subdomain = mount if subdomain is _default else subdomain

    def collect_routes(self, routes: list[t.Any]):
        """
        This method can be overwritten to add more routes to the controller. Everything in ``routes`` will
        be added to a ``RuleAdapter`` and subsequently mounted into the gateway router.

        Here are some examples::

            class MyRoutes:
                @route("/hello")
                def hello(request):
                    return "Hello World!"

            class MyExtension(WebAppExtension):
                name = "my-extension"

                def collect_routes(self, routes: list[t.Any]):

                    # scans all routes of MyRoutes
                    routes.append(MyRoutes())
                    # use rule adapters to add routes without decorators
                    routes.append(RuleAdapter("/say-hello", self.say_hello))

                    # no idea why you would want to do this, but you can :-)
                    @route("/empty-dict")
                    def _inline_handler(request: Request) -> Response:
                        return Response.for_json({})
                    routes.append(_inline_handler)

                def say_hello(request: Request):
                    return {"message": "Hello World!"}

        This creates the following routes available through both subdomain and submount.

        With subdomain:

        * ``my-extension.localhost.localstack.cloud:4566/hello``
        * ``my-extension.localhost.localstack.cloud:4566/say-hello``
        * ``my-extension.localhost.localstack.cloud:4566/empty-dict``
\
        With submount:

        * ``localhost.localstack.cloud:4566/_extension/my-extension/hello``
        * ``localhost.localstack.cloud:4566/_extension/my-extension/say-hello``
        * ``localhost.localstack.cloud:4566/_extension/my-extension/empty-dict``
\
        :param routes: the routes being collected
        """
        pass

    def _preprocess_request(
            self, chain: HandlerChain, context: RequestContext, _response: http.Response
    ):
        """
        Default pre-processor, which implements a default behavior to add a trailing slash to the path if the
        submount is used directly. For instance ``/_extension/my-extension``, then it forwards to
        ``/_extension/my-extension/``. This is so you can reference relative paths like ``<link
        href="static/style.css">`` in your HTML safely, and it will work with both subdomain and submount.
        """
        path = context.request.path

        if path == self.submount.rstrip("/"):
            chain.respond(301, headers={"Location": context.request.url + "/"})

    def _add_superclass_routes(self, routes: list[t.Any]):
        """
        Superclasses may want to add additional routes by default.
        This function is called inside default update_gateway_routes
        """
        pass

    def update_gateway_routes(self, router: http.Router[http.RouteHandler]):
        from localstack.aws.handlers import preprocess_request

        if self.submount:
            preprocess_request.append(self._preprocess_request)

        # adding self here makes sure that any ``@route`` decorators to the extension are mapped automatically
        routes = [self]

        self._add_superclass_routes(routes)
        self.collect_routes(routes)

        app = RuleAdapter(routes)

        if self.submount:
            router.add(Submount(self.submount, [app]))
            LOG.info(
                "%s extension available at %s%s",
                self.name,
                config.external_service_url(),
                self.submount,
            )

        if self.subdomain:
            router.add(WithHost(f"{self.subdomain}.<__host__>", [app]))
            self._configure_cors_for_subdomain()
            LOG.info(
                "%s extension available at %s",
                self.name,
                config.external_service_url(subdomains=self.subdomain),
            )

    def _configure_cors_for_subdomain(self):
        """
        Automatically configures CORS for the subdomain, for both HTTP and HTTPS.
        """
        from localstack.aws.handlers.cors import ALLOWED_CORS_ORIGINS

        for protocol in ("http", "https"):
            url = self.get_subdomain_url(protocol)
            LOG.debug("adding %s to ALLOWED_CORS_ORIGINS", url)
            ALLOWED_CORS_ORIGINS.append(url)

    def get_subdomain_url(self, protocol: str = "https") -> str:
        """
        Returns the URL that serves the extension under its subdomain
        ``https://my-extension.localhost.localstack.cloud:4566/``.

        :return: a URL this extension is served at
        """
        if not self.subdomain:
            raise ValueError(f"Subdomain for extension {self.name} is not set")
        return config.external_service_url(subdomains=self.subdomain, protocol=protocol)

    def get_submount_url(self, protocol: str = "https") -> str:
        """
        Returns the URL that serves the extension under its submount
        ``https://localhost.localstack.cloud:4566/_extension/my-extension``.

        :return: a URL this extension is served at
        """

        if not self.submount:
            raise ValueError(f"Submount for extension {self.name} is not set")

        return f"{config.external_service_url(protocol=protocol)}{self.submount}"

    @classmethod
    def get_extension_module_root(cls) -> str:
        """
        Returns the root of the extension module. For instance, if the extension lives in
        ``my_extension/plugins/extension.py``, then this will return ``my_extension``. Used to set up the
        logger as well as the template environment and the static file module.

        :return: the root module the extension lives in
        """
        return cls.__module__.split(".")[0]
