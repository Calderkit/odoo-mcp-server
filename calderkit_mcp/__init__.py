from . import controllers
from . import models


def post_init_hook(env):
    env['calderkit.mcp.model']._mcp_add_defaults(only_new=True)
