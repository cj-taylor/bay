import os
import datetime
import json
import tempfile
import tarfile
import logging

import attr
from docker.utils import exclude_paths

from ..cli.colors import CYAN, remove_ansi
from ..cli.tasks import Task

from ..exceptions import BuildFailureError, FailedCommandException


class TaskExtraInfoHandler(logging.Handler):
    """
    Custom log handler that emits to a task's extra info.
    """

    def __init__(self, task):
        super(TaskExtraInfoHandler, self).__init__()
        self.task = task

    def emit(self, record):
        text = self.format(record)
        # Sanitise the text
        text = remove_ansi(text).replace("\n", "").replace("\r", "").strip()
        self.task.set_extra_info(
            self.task.extra_info[-3:] + [text]
        )


@attr.s
class Builder:
    """
    Build an image from a single container.
    """
    host = attr.ib()
    container = attr.ib()
    app = attr.ib()
    logfile_name = attr.ib()
    parent_task = attr.ib()
    # Set docker_cache to False to force docker to rebuild every layer.
    docker_cache = attr.ib(default=True)
    verbose = attr.ib(default=False)
    logger = attr.ib(init=False)

    def __attrs_post_init__(self):
        self.logger = logging.getLogger('build_logger')
        self.logger.setLevel(logging.INFO)

        # Close all old logging handlers
        if self.logger.handlers:
            [handler.close() for handler in self.logger.handlers]
            self.logger.handlers = []

        # Add build log file handler
        file_handler = logging.FileHandler(self.logfile_name)
        self.logger.addHandler(file_handler)

        # Optionally add task (console) log handler
        self.task = Task("Building {}".format(CYAN(self.container.name)), parent=self.parent_task)
        if self.verbose:
            self.logger.addHandler(TaskExtraInfoHandler(self.task))

    def build(self):
        """
        Runs the build process and raises BuildFailureError if it fails.
        """
        self.logger.info("Building image {}".format(self.container.name))

        build_successful = True
        progress = 0
        start_time = datetime.datetime.now().replace(microsecond=0)

        self.app.run_hooks('pre-build', host=self.host, container=self.container, task=self.task)

        try:
            # Prep normalised context
            build_context = self.make_build_context()
            # Run build
            result = self.host.client.build(
                self.container.path,
                dockerfile=self.container.dockerfile_name,
                tag=self.container.image_name,
                nocache=not self.docker_cache,
                rm=True,
                stream=True,
                custom_context=True,
                encoding="gzip",
                fileobj=build_context,
                buildargs=self.container.buildargs,
                pull=not self.container.build_parent_in_prefix,  # If the parent image is not in prefix, pull it during build
            )
            for data in result:
                # Make sure data is a string
                if isinstance(data, bytes):
                    data = data.decode("utf8")
                data = json.loads(data)
                if 'stream' in data:
                    # docker data stream has extra newlines in it, so we will
                    # strip them before logging.
                    self.logger.info(data['stream'].rstrip())
                    if data['stream'].startswith('Step '):
                        progress += 1
                        self.task.update(status="." * progress)
                if 'error' in data:
                    self.logger.info(data['error'].rstrip())
                    build_successful = False

            if not build_successful:
                raise FailedCommandException

        except FailedCommandException:
            message = "Build FAILED for image {}!".format(self.container.name)
            self.logger.info(message)
            self.task.finish(status="FAILED", status_flavor=Task.FLAVOR_BAD)
            raise BuildFailureError(message)

        else:
            # Run post-build hooks
            self.app.run_hooks('post-build', host=self.host, container=self.container, task=self.task)

            # Print out end-of-build message
            end_time = datetime.datetime.now().replace(microsecond=0)
            time_delta_str = str(end_time - start_time)
            if time_delta_str.startswith('0:'):
                # no point in showing hours, unless it runs for more than one hour
                time_delta_str = time_delta_str[2:]

            build_completion_message = "Build time for {image_name} image: {build_time}".format(
                image_name=self.container.name,
                build_time=time_delta_str
            )
            self.logger.info(build_completion_message)

            # Clear any verbose log peeking and close out the task
            self.task.set_extra_info([])
            self.task.finish(status='Done [{}]'.format(time_delta_str), status_flavor=Task.FLAVOR_GOOD)

    def make_build_context(self):
        """
        Makes a Docker build context from a local directory.
        Normalises all file ownership and times so that the docker hashes align
        better.
        """
        # Start temporary tar file
        fileobj = tempfile.NamedTemporaryFile()
        tfile = tarfile.open(mode='w:gz', fileobj=fileobj)
        # Get list of files/dirs to add to the tar
        paths = exclude_paths(self.container.path, [])
        # For each file, add it to the tar with normalisation
        for path in paths:
            disk_location = os.path.join(self.container.path, path)
            # TODO: Rewrite docker FROM lines with a : in them and raise a warning
            # Directory addition
            if os.path.isdir(disk_location):
                info = tarfile.TarInfo(name=path)
                info.mtime = 0
                info.mode = 0o775
                info.type = tarfile.DIRTYPE
                info.uid = 0
                info.gid = 0
                info.uname = "root"
                info.gname = "root"
                tfile.addfile(info)
            # Normal file addition
            elif os.path.isfile(disk_location):
                stat = os.stat(disk_location)
                info = tarfile.TarInfo(name=path)
                info.mtime = 0
                info.size = stat.st_size
                info.mode = 0o755
                info.type = tarfile.REGTYPE
                info.uid = 0
                info.gid = 0
                info.uname = "root"
                info.gname = "root"
                with open(disk_location, "rb") as fh:
                    tfile.addfile(info, fh)
            # Error for anything else
            else:
                raise ValueError(
                    "Cannot add non-file/dir %s to docker build context" % path
                )
        # Return that tarfile
        tfile.close()
        fileobj.seek(0)
        return fileobj