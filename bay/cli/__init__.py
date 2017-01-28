import click
import pkg_resources
import sys
import os
import traceback
import attr

from .alias_group import SpellcheckableAliasableGroup
from .colors import PURPLE
from .tasks import RootTask
from ..config import Config
from ..docker.hosts import HostManager
from ..containers.graph import ContainerGraph
from ..containers.profile import NullProfile, Profile
from ..utils.sorting import dependency_sort


@attr.s
class App(object):
    """
    Main app object that's passed around.

    Contains a "hooks" system, which allows plugins to register hooks (callables
    that take keyword arguments and return nothing) and other code to call them.
    """
    cli = attr.ib()
    plugins = attr.ib(default=attr.Factory(dict), init=False)

    VALID_HOOKS = ["pre-start", "pre-build", "post-build"]

    def load_config(self, config_paths):
        self.config = Config(config_paths)
        self.hosts = HostManager.from_config(self.config)
        self.containers = ContainerGraph(self.config["bay"]["home"])
        self.root_task = RootTask()

    def load_plugins(self):
        """
        Loads all plugins defined in config
        """
        self.hooks = {}
        self.waits = {}
        # Load plugin classes based on entrypoints
        plugins = []
        for entrypoint in pkg_resources.iter_entry_points("bay.plugins"):
            try:
                plugin = entrypoint.load()
                plugins.append(plugin)
            except ImportError:
                click.echo(PURPLE("Failed to import plugin: {name}".format(name=entrypoint.name)), err=True)
                click.echo(PURPLE(traceback.format_exc()), err=True)
                sys.exit(1)
        # Build plugin provides
        provided = {}
        for plugin in plugins:
            for p in plugin.provides:
                # Make sure another plugin does not provide this
                if p in provided:
                    click.echo(PURPLE("Multiple plugins provide {}, please unload one.".format(p)))
                    sys.exit(1)
                provided[p] = plugin
        # Check plugin requires
        for plugin in plugins:
            for r in plugin.requires:
                if r not in provided:
                    click.echo(PURPLE("Plugin {} requires {}, but nothing provides it.".format(plugin, r)))
                    sys.exit(1)
        # Sort plugins by dependency order
        plugins = dependency_sort(plugins, lambda x: [provided[r] for r in x.requires])
        # Load plugins
        for plugin in plugins:
            # We store plugins so you can look their instances up by class
            self.plugins[plugin] = instance = plugin(self)
            instance.load()

    def load_profiles(self):
        """
        Loads the current profile stack
        """
        user_profile_path = os.path.join(
            self.config["bay"]["user_profile_home"],
            self.containers.prefix,
            "user_profile.yaml"
        )

        if os.path.exists(user_profile_path):
            user_profile = Profile(user_profile_path)
        else:
            user_profile = NullProfile()

        if user_profile.parent_profile:
            parent_profile = Profile(os.path.join(
                self.config["bay"]["home"],
                "profiles",
                "{}.yaml".format(user_profile.parent_profile)
            ))
            parent_profile.apply(self.containers)

        user_profile.apply(self.containers)

    def add_hook(self, hook_type, receiver):
        """
        Adds a plugin hook to be run later.
        """
        if hook_type not in self.VALID_HOOKS:
            raise ValueError("Invalid hook type {}".format(hook_type))
        self.hooks.setdefault(hook_type, []).append(receiver)

    def run_hooks(self, hook_type, **kwargs):
        """
        Runs all hooks of the given type with the given keyword arguments
        """
        for hook in self.hooks.get(hook_type, []):
            hook(**kwargs)

    def add_wait(self, name, check):
        """
        Adds a wait by name
        """
        if name in self.waits:
            raise ValueError("Wait '{}' already registered".format(name))
        self.waits[name] = check

    def get_plugin(self, klass):
        """
        Given a plugin's class, returns the instance of it we have loaded.
        """
        return self.plugins[klass]


class AppGroup(SpellcheckableAliasableGroup):
    """
    Group subclass that instantiates an App instance when called, loads
    plugins, and passes the app as the context obj.
    """

    def __init__(self, app_class, **kwargs):
        super(AppGroup, self).__init__(**kwargs)
        self.app = app_class(self)
        self.app.load_plugins()

    def invoke(self, ctx):
        ctx.obj = self.app
        return super(AppGroup, self).invoke(ctx)


@click.command(cls=AppGroup, app_class=App)
@click.option('-c', '--config', multiple=True)
@click.version_option()
@click.pass_obj
def cli(app, config):
    """
    Bay, the Docker-based development environment management tool.
    """
    # Load config based on CLI parameters
    app.load_config(config)
    app.load_profiles()


# Run CLI if called directly
if __name__ == '__main__':
    cli()