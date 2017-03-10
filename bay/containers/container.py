import re
import os
import yaml

import attr

from ..exceptions import BadConfigError


@attr.s
class Container:
    """
    Represents a single container type that's available to run (not a running container -
    we call those Instances).

    All containers are backed by a local disk directory containing their information,
    even if the actual running server is remote.
    """
    parent_pattern = re.compile(r'^FROM\s+([\S/]+)', re.IGNORECASE | re.MULTILINE)
    git_volume_pattern = re.compile(r'^\{git@github.com:eventbrite/([\w\s\-]+).git\}(.*)$')

    graph = attr.ib(repr=False, hash=False, cmp=False)
    path = attr.ib(repr=False, hash=True, cmp=True)
    suffix = attr.ib(repr=False, hash=False, cmp=False)
    dockerfile_name = attr.ib(repr=False, hash=False, cmp=False)
    name = attr.ib(init=False, repr=True, hash=True, cmp=True)

    def __attrs_post_init__(self):
        self.load()

    @classmethod
    def from_directory(cls, graph, path):
        """
        Creates a set of one or more Container objects from a source directory.
        The "versions" key in the bay.yaml file can mean there are multiple variants
        of the container, and we treat each separately (though they share a build directory and bay.yaml settings)
        """
        versions = {None: "Dockerfile"}
        # Read config and get out any non-default versions
        config_path = os.path.join(path, "bay.yaml")
        if not os.path.isfile(config_path):
            config_path = os.path.join(path, "tug.yaml")
        if os.path.isfile(config_path):
            # Read out config, making sure a empty file (None) appears as empty dict
            with open(config_path, "r") as fh:
                config_data = yaml.safe_load(fh.read()) or {}
                # Merges extra versions in config file into versions dict
                versions.update({
                    str(suffix): dockerfile_name
                    for suffix, dockerfile_name in config_data.get("versions", {}).items()
                })
        # For each version, make a Container class for it, and return the list of them
        return [
            cls(graph, path, suffix, dockerfile_name)
            for suffix, dockerfile_name in versions.items()
        ]

    def load(self):
        """
        Loads information from the container's files.
        """
        # Work out paths to key files, make sure they exist
        self.dockerfile_path = os.path.join(self.path, self.dockerfile_name)
        self.config_path = os.path.join(self.path, "bay.yaml")
        if not os.path.isfile(self.config_path):
            self.config_path = os.path.join(self.path, "tug.yaml")
        if not os.path.isfile(self.dockerfile_path):
            raise BadConfigError("Cannot find Dockerfile for container %s" % self.path)
        # Calculate name from path component
        if self.suffix is None:
            self.name = os.path.basename(self.path)
        else:
            self.name = os.path.basename(self.path) + "-" + self.suffix
        self.image_name = '{prefix}/{name}'.format(
            prefix=self.graph.prefix,
            name=self.name,
        )
        # Load parent image from Dockerfile
        with open(self.dockerfile_path, "r") as fh:
            self.build_parent = self.parent_pattern.search(fh.read()).group(1)
            # Make sure any ":" in the dockerfile is changed to a "-"
            # TODO: Add warning here once we've converted enough of the dockerfiles
            self.build_parent = self.build_parent.replace(":", "-")
        self.build_parent_in_prefix = self.build_parent.startswith(self.graph.prefix + '/')
        # Ensure it does not have an old-style multi version inheritance
        if self.build_parent_in_prefix and ":" in self.build_parent:
            raise BadConfigError(
                "Container {} has versioned build parent - it should be converted to just a name.".format(self.path),
            )
        # Load information from bay.yaml file
        if os.path.isfile(self.config_path):
            with open(self.config_path, "r") as fh:
                config_data = yaml.safe_load(fh.read()) or {}
        else:
            config_data = {}
        # Set our attributes with empty defaults
        self.default_links = set(config_data.get("links", []))
        self.all_links = set(config_data.get("extra_links", [])).union(self.default_links)
        # Parse waits from the config format
        self.waits = []
        for wait_dict in config_data.get("waits", []):
            for wait_type, params in wait_dict.items():
                if not isinstance(params, dict):
                    # TODO: Deprecate non-dictionary params
                    if wait_type == "time":
                        params = {"seconds": params}
                    else:
                        params = {"port": params}
                self.waits.append({"type": wait_type, "params": params})
        # Volumes is a dict of {container mountpoint: volume name/host path}
        self._bound_volumes = {}
        self._named_volumes = {}
        for mount_point, source in config_data.get("volumes", {}).items():
            # Old-style git link
            # TODO: Add warning here once we've converted enough of the dockerfiles
            git_match = self.git_volume_pattern.match(source)
            if git_match:
                source = "../{}/{}".format(git_match.group(1), git_match.group(2).lstrip("/"))
            # Split named volumes and directory mounts up
            if "/" in source:
                self._bound_volumes[mount_point] = os.path.abspath(os.path.join(self.graph.path, source))
            else:
                self._named_volumes[mount_point] = source
        # Volumes_mount is a deprecated key from the old buildable volumes system.
        # They turn into named volumes.
        # TODO: Deprecate volumes_mount
        for mount_point, source in config_data.get("volumes_mount", {}).items():
            self._named_volumes[mount_point] = source
        # Devmodes might also have git URLs
        self._devmodes = {}
        for name, mounts in config_data.get("devmodes", {}).items():
            # Allow for empty devmodes
            if not mounts:
                continue
            # Add each mount individually
            self._devmodes[name] = {}
            for mount_point, source in mounts.items():
                git_match = self.git_volume_pattern.match(source)
                if git_match:
                    source = "../{}/{}".format(git_match.group(1), git_match.group(2).lstrip("/"))
                self._devmodes[name][mount_point] = os.path.abspath(os.path.join(self.graph.path, source))
        # Ports is a dict of {port on container: host exposed port}
        self.ports = config_data.get("ports", {})
        self.build_checks = config_data.get("build_checks", [])
        self.foreground = config_data.get("foreground", False)
        self.image_tag = config_data.get("image_tag", "local")
        self.environment = config_data.get("environment", {})
        self.fast_kill = config_data.get("fast_kill", False)
        self.buildargs = {}
        # Store all extra data so plugins can get to it
        self.extra_data = {
            key: value
            for key, value in config_data.items()
            if key not in ["ports", "build_checks", "devmodes", "foreground", "links", "waits", "volumes", "image_tag"]
        }

    def get_parent_value(self, name, default):
        """
        Shortcut for getting inherited values from parents with a fallback.
        """
        if self.build_parent_in_prefix:
            return getattr(self.graph.build_parent(self), name)
        else:
            return default

    def get_ancestral_extra_data(self, key):
        """
        Returns a list of all extra data values with "key" from this container
        up through all build parents.
        """
        if self.build_parent_in_prefix:
            result = self.graph.build_parent(self).get_ancestral_extra_data(key)
            if key in self.extra_data:
                result.append(self.extra_data[key])
            return result
        elif key in self.extra_data:
            return [self.extra_data[key]]
        else:
            return []

    @property
    def bound_volumes(self):
        value = self.get_parent_value("bound_volumes", {})
        value.update(self._bound_volumes)
        return value

    @property
    def named_volumes(self):
        value = self.get_parent_value("named_volumes", {})
        value.update(self._named_volumes)
        return value

    @property
    def devmodes(self):
        value = self.get_parent_value("devmodes", {})
        value.update(self._devmodes)
        return value
