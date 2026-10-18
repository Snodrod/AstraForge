from .nodes import *
from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

# AstraForge addition: the all-in-one "views" node (see astra_views.py)
from .astra_views import NODE_CLASS_MAPPINGS as _astra_nodes, NODE_DISPLAY_NAME_MAPPINGS as _astra_names

NODE_CLASS_MAPPINGS = {**NODE_CLASS_MAPPINGS, **_astra_nodes}
NODE_DISPLAY_NAME_MAPPINGS = {**NODE_DISPLAY_NAME_MAPPINGS, **_astra_names}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
