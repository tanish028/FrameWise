"""Exception hierarchy. UI code shows FrameWiseError messages to the user as-is."""


class FrameWiseError(Exception):
    """Base class for errors with a user-readable message."""


class ToolError(FrameWiseError):
    """An external tool (ffmpeg, an AI engine) is missing or failed."""


class ProjectError(FrameWiseError):
    """A project file could not be read or written."""


class Cancelled(Exception):
    """The user cancelled a running operation. Not an error."""
