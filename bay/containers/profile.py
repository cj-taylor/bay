import os
import yaml

import attr

from ..exceptions import BadConfigError


@attr.s
class Profile:
    """
    Represents a profile - a way of running containers based on a base graph.

    A profile comes from a single config file, and then applies changes from that
    to a ContainerGraph. Multiple profiles might be used one after the other.
    """
    file_path = attr.ib(default=None)
    parent_profile = attr.ib(default=None, init=False)
    description = attr.ib(default=None, init=False)
    version = attr.ib(default=None, init=False)

    def __attrs_post_init__(self):
        self.load()

    def load(self):
        """
        Loads the profile data from a YAML file
        """
        self.containers = {}
        # Read in file
        with open(self.file_path, "r") as fh:
            data = yaml.safe_load(fh.read())
        # Parse container details
        try:
            self.parent_profile = data.get("name")
        except AttributeError:
            self.parent_profile = None  # The parent profile is a null.

        self.description = data.get("description")
        self.version = data.get("min-version")

        for name, details in data.get('containers', {}).items():
            if details is None:
                details = {}
            self.containers[name] = {
                "extra_links": set(details.get("extra_links") or []),
                "ignore_links": set(details.get("ignore_links") or []),
                "devmodes": set(details.get("devmodes") or []),
                "ports": details.get("ports") or {},
                "environment": details.get("environment") or {},
                "ephemeral": details.get("ephemeral") or False,
            }

    def dump(self):
        data = {
            "name": self.parent_profile,
        }
        containers = {}

        for container_name, container_data in self.containers.items():
            if container_data and not container_data.get('ephemeral'):
                container_details_to_write = {}
                for k, v in container_data.items():
                    if v:
                        if isinstance(v, set):
                            container_details_to_write[k] = sorted(v)
                        else:
                            container_details_to_write[k] = v

                if container_details_to_write:
                    containers[container_name] = container_details_to_write

        if containers:
            data["containers"] = containers

        if self.version:
            data['min-version'] = self.version

        return data

    def apply(self, graph):
        """
        Applies the profile to the given graph
        """
        self.graph = graph
        for name, details in self.containers.items():
            container = self.graph[name]
            # Apply container links
            if details.get('ignore_links') or details.get('extra_links'):
                self.graph.set_dependencies(
                    container,
                    [self.graph[link]
                    for link in self.calculate_links(container)],
                )
            # Set default boot mode
            self.graph.set_option(container, "default_boot", True)
            # Set devmodes
            self.graph.set_option(container, "devmodes", details["devmodes"])
            # Set ports to apply
            if "ports" in details:
                container.ports = {
                    int(a): int(b)
                    for a, b in details["ports"].items()
                }

    def calculate_links(self, container):
        """
        Works out what links the container should have
        """
        ignore_links = self.containers[container.name].get('ignore_links') or set()
        extra_links = self.containers[container.name].get('extra_links') or set()
        # Are any ignored links not valid links?
        if ignore_links - container.all_links:
            raise BadConfigError(
                "Profile contains invalid ignore_links for {}: {}".format(
                    container.name,
                    ignore_links - container.all_links,
                )
            )
        # Are any extra links not valid links?
        if extra_links - container.all_links:
            raise BadConfigError(
                "Profile contains invalid extra_links for {}: {}".format(
                    container.name,
                    extra_links - container.all_links,
                )
            )
        # Work out desired final set of links
        return (container.default_links - ignore_links) | extra_links

    def save(self):
        """
        Saves the user profile things to disc after loading.

        Persists the profile to disk as YAML
        """
        # Set user profile to ~/.bay/eventbrite/user_profile.yaml
        try:
            os.makedirs(os.path.dirname(self.file_path))
        except OSError:
            pass
        with open(self.file_path, "w") as fh:
            yaml.safe_dump(self.dump(), fh, default_flow_style=False, indent=4)


@attr.s
class NullProfile(Profile):
    file_path = attr.ib(default=None, init=False)

    def load(self):
        """
        Loads the empty profile.
        """
        self.containers = {}

    def save(self):
        """
        Raises an error, you can't save a NullProfile.
        """
        raise BadConfigError("You can't save a NullProfile, please load a profile using `bay profile <profile_name>`")

    def calculate_links(self, container):
        """
        Returns None, a NullProfile has no links.
        """

    def apply(self, graph):
        """
        Returns None, you can't apply a NullProfile to a container-graph.
        """