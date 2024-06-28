import importlib
import logging
import mimetypes
import typing as t
from functools import cached_property

from rolo.router import RuleAdapter

from localstack import config
from localstack.extensions.api import http
from localstack.extensions.patterns.baseui import WebAppBaseExtension

if t.TYPE_CHECKING:
    # although jinja2 is included transitively via moto, let's make sure jinja2 stays optional
    import jinja2

LOG = logging.getLogger(__name__)

_default = object()


class JinjaExtension(WebAppBaseExtension):
    """
    EXPERIMENTAL! This class is experimental and the API may change without notice.

    A webapp extension serves routes, templates, and static files via a submount and a subdomain through
    localstack.

    It assumes you have the following directory layout::

        my_extension
        ├── extension.py
        ├── __init__.py
        ├── static              <-- make sure static resources get packaged!
        │   ├── __init__.py
        │   ├── favicon.ico
        │   └── style.css
        └── templates            <-- jinja2 templates
            └── index.html

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
            template_package_path: str | None = _default,
            static_package_path: str | None = _default,
            static_url_path: str = None,
    ):
        """
        Overwrite to customize your extension. For example, you can disable certain behavior by calling
        ``super( ).__init__(subdomain=None, static_package_path=None)``, which will disable serving through
        a subdomain, and disable static file serving.

        :param mount: the "mount point" which will be used as default value for the submount and
            subdirectory, i.e., ``<mount>.localhost.localstack.cloud`` and
            ``localhost.localstack.cloud/_extension/<mount>``. Defaults to the extension name.  Note that,
            in case the mount name clashes with another extension, extensions may overwrite each other's
            routes.
        :param submount: the submount path, needs to start with a trailing slash (default
            ``/_extension/<mount>``)
        :param subdomain: the subdomain (defaults to the value of ``mount``)
        :param template_package_path: the path to the templates within the module. defaults to
            ``templates`` which expands to ``<extension-module>.templates``)
        :param static_package_path: the package serving static files. defaults to ``static``, which expands to
            ``<extension-module>.static``.
        :param static_url_path: the URL path to serve static files from (defaults to `/static`)
        """
        super().__init__(mount=mount, submount=submount, subdomain=subdomain)

        self.template_package_path = (
            "templates" if template_package_path is _default else template_package_path
        )
        self.static_package_path = (
            "static" if static_package_path is _default else static_package_path
        )
        self.static_url_path = static_url_path or "/static"

        self.static_resource_module = None

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
        * ``my-extension.localhost.localstack.cloud:4566/static``  <- automatically added static file endpoint

        With submount:

        * ``localhost.localstack.cloud:4566/_extension/my-extension/hello``
        * ``localhost.localstack.cloud:4566/_extension/my-extension/say-hello``
        * ``localhost.localstack.cloud:4566/_extension/my-extension/empty-dict``
        * ``localhost.localstack.cloud:4566/_extension/my-extension/static`` <- auto-added static file serving

        :param routes: the routes being collected
        """
        pass

    @cached_property
    def template_env(self) -> t.Optional["jinja2.Environment"]:
        """
        Returns the singleton jinja2 template environment. By default, the environment uses a
        ``PackageLoader`` that loads from ``my_extension.templates`` (where ``my_extension`` is the root
        module of the extension, and ``templates`` refers to ``self.template_package_path``,
        which is ``templates`` by default).

        :return: a template environment
        """
        if self.template_package_path:
            return self._create_template_env()
        return None

    def _create_template_env(self) -> "jinja2.Environment":
        """
        Factory method to create the jinja2 template environment.
        :return: a new jinja2 environment
        """
        import jinja2

        return jinja2.Environment(
            loader=jinja2.PackageLoader(
                self.get_extension_module_root(), self.template_package_path
            ),
            autoescape=jinja2.select_autoescape(),
        )

    def render_template(self, template_name, **context) -> str:
        """
        Uses the ``template_env`` to render a template and return the string value.

        :param template_name: the template name
        :param context: template context
        :return: the rendered result
        """
        template = self.template_env.get_template(template_name)
        return template.render(**context)

    def render_template_response(self, template_name, **context) -> http.Response:
        """
        Uses the ``template_env`` to render a template into an HTTP response. It guesses the mimetype from the
        template's file name.

        :param template_name: the template name
        :param context: template context
        :return: the rendered result as response
        """
        template = self.template_env.get_template(template_name)

        mimetype = mimetypes.guess_type(template.filename)
        mimetype = mimetype[0] if mimetype and mimetype[0] else "text/plain"

        return http.Response(response=template.render(**context), mimetype=mimetype)

    def on_extension_load(self):
        logging.getLogger(self.get_extension_module_root()).setLevel(
            logging.DEBUG if config.DEBUG else logging.INFO
        )

        if self.static_package_path and not self.static_resource_module:
            try:
                self.static_resource_module = importlib.import_module(
                    self.get_extension_module_root() + "." + self.static_package_path
                )
            except ModuleNotFoundError:
                LOG.warning("disabling static resources for extension %s", self.name)

    def _add_superclass_routes(self, routes: list[t.Any]):
        if self.static_resource_module:
            routes.append(
                RuleAdapter(f"{self.static_url_path}/<path:path>", self._serve_static_file)
            )

    def _serve_static_file(self, _request: http.Request, path: str):
        """Route for serving static files, for ``/_extension/my-extension/static/<path:path>``."""
        return http.Response.for_resource(self.static_resource_module, path)
